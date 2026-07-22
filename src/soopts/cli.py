"""soopts CLI — SOOP VOD 노래 하이라이트 추출·식별.

파이프라인: collect(채팅→스티커) → songs(오디오 음악감지 + 스티커 + STT 전사).
각 단계는 work/{vod_id}/ 캐시를 쓰고 --force로 재계산한다.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from soopts.config import Config, load_config
from soopts.log import get_logger, setup_logging
from soopts.paths import WorkPaths, work_paths

log = get_logger("cli")

_VOD_ID_RE = re.compile(r"(\d{5,})")


def extract_vod_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    if s.isdigit():
        return s
    m = _VOD_ID_RE.search(s)
    if not m:
        raise ValueError(f"VOD 번호를 찾을 수 없습니다: {url_or_id!r}")
    return m.group(1)


def _ctx(args) -> tuple[Config, str, WorkPaths]:
    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    vod_id = extract_vod_id(args.vod)
    work = work_paths(cfg.work_root, vod_id).ensure()
    return cfg, vod_id, work


# --------------------------------------------------------------------------- #
def _do_collect(cfg: Config, vod_id: str, work: WorkPaths, *, force: bool, reparse: bool):
    from soopts.collector.chat import fetch_chat
    from soopts.collector.meta import fetch_meta

    meta = fetch_meta(cfg, vod_id, work, force=force)
    fetch_chat(cfg, vod_id, meta, work, force=force, reparse=reparse)
    return meta


def cmd_collect(args) -> int:
    cfg, vod_id, work = _ctx(args)
    _do_collect(cfg, vod_id, work, force=args.force, reparse=args.reparse)
    return 0


def cmd_clips(args) -> int:
    """BJ 부른 노래 1080p 클립 추출 + 가사 전사 → clips.json 저장(검수용)."""
    cfg, vod_id, work = _ctx(args)
    from soopts.models import read_meta

    meta = read_meta(work.meta) if work.meta.exists() else None
    return _produce_clips(cfg, vod_id, work, meta, args)


def _produce_clips(cfg: Config, vod_id: str, work: WorkPaths, meta, args) -> int:
    from soopts.analyzers.audio_analyzer import sticker_burst_regions
    from soopts.collector.media import download_span, resolve_m3u8_list
    from soopts.export.clips import detect_song_span, write_clips
    from soopts.models import read_chat_jsonl
    from soopts.output import fmt_hms

    if not work.chat.exists():
        raise RuntimeError("chat.jsonl 없음 — 먼저 `soopts collect` 로 채팅(스티커)을 수집하세요")
    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    a = cfg.audio
    regions = sticker_burst_regions(
        stickers, bucket_s=a.sticker_bucket_s, window_buckets=a.sticker_window_buckets,
        min_per_window=a.sticker_min_per_window, pad_before_s=a.sticker_pad_before_s,
        pad_after_s=a.sticker_pad_after_s, skip_opening_s=a.skip_opening_s,
        total_s=meta.total_duration if meta else None,
    )
    log.info("노래 후보 %d구간 → 1080p 클립 추출(%s)", len(regions), cfg.clip.quality)

    from soopts.collector.media import split_by_part

    m3u8s = resolve_m3u8_list(args.vod, cfg.clip.quality)
    parts = meta.parts if meta else []
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    sources: list[tuple[str, float, float]] = []   # 전사용 (구간파일, 로컬시작, 로컬끝)
    for s, e in regions:
        ds = max(0.0, s - cfg.clip.dl_pad_before_s)
        de = e + cfg.clip.dl_pad_after_s
        spans = split_by_part(ds, de, parts, m3u8s)
        if not spans:
            continue
        raw = work.clips_dir / f"region_{int(ds)}.mp4"
        lead, _ = download_span(spans, raw, workers=cfg.collector.segment_workers, force=args.force)
        # 파일 t=0은 요청 시각(ds)이 아니라 그 시각을 품은 세그먼트의 시작이다(lead만큼 이르다).
        span = detect_song_span(cfg, str(raw), ds, de, media_offset=ds - lead)
        if span:
            clip, local_start, local_end = span
            clips.append(clip)
            sources.append((str(raw), local_start, local_end))

    if not clips:
        print("감지된 노래 구간이 없습니다.")
        return 0

    # 노래 경계만 잘라 전사 → 가사를 채워 곡 식별에 사용
    if not args.no_stt:
        from soopts.analyzers.stt import _load_model, _transcribe_best
        model = _load_model(cfg)
        for c, (src, ls_, le_) in zip(clips, sources, strict=True):
            text, _lang = _transcribe_best(model, src, cfg, start=ls_, dur=le_ - ls_)
            c.lyrics = text[: cfg.stt.lyric_chars]

    # 검수 파일 저장 + 곡명 확인 안내
    write_clips(work.clips_dir, clips)
    print(f"\n🎵 노래 구간 {len(clips)}개 감지 — 곡명 확인 단계:")
    for c in clips:
        print(f"\n  [{fmt_hms(c.t)}~{fmt_hms(c.end)}] {c.duration}초")
        print(f"    가사: {c.lyrics[:70] or '(전사 없음)'}")
    print(f"\n다음: {work.clips_dir / 'clips.json'} 의 각 \"title\" 에 곡명을 채우세요.")
    return 0


def cmd_daily(args) -> int:
    """스테이션 최신 VOD의 댓글 타임라인(🎤)을 파싱해 곡 식별·DB 기록 — GitHub Actions 배치용."""
    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    from soopts import batch

    result = batch.run_daily(
        cfg,
        bj_id=args.bj or cfg.station.bj_id,
        count=args.count or cfg.station.daily_vod_count,
    )
    print(result["text"])
    return 0


def cmd_youtube_upload(args) -> int:
    """검증 완료 VOD 하나의 노래 구간을 합본 영상으로 만들어 유튜브에 **unlisted** 업로드.

    하루 1건만 돈다(GitHub Actions youtube.yml). 대상은 status가 analyzed/done이고 모든
    performance의 identify_status·local_review가 완료된 VOD 중 가장 오래된 방송이다.
    올린 뒤 코드가 영상을 고치거나 지우는 경로는 없다 — 손볼 일은 유튜브 스튜디오에서.
    필요 env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, (Slack) SLACK_WEBHOOK_URL.
    OAuth 토큰: [youtube] token_file (러너는 YT_TOKEN 시크릿에서 복원).
    """
    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    from soopts import youtube_pipeline

    return youtube_pipeline.main_upload(
        cfg,
        title_no=extract_vod_id(args.title_no) if args.title_no else None,
        dry_run=args.dry_run,
    )


def cmd_ingest(args) -> int:
    """로컬 분석(analyze_vod.py)으로 뽑은 곡 목록(JSON)을 DB에 기록 — 무-타임라인 VOD 로컬 처리 경로.

    daily가 'manual'로 표시한 무-타임라인 VOD를 사람이 로컬에서 analyze_vod.py 전체 전사로
    보고 곡 구간·제목을 뽑은 뒤, 그 JSON을 이 명령으로 넣는다. 감지/STT를 돌리지 않고
    식별(카탈로그 매칭)과 DB 기록만 한다. daily와 동일하게 Supabase에 남아 딥링크가 생성된다.
    필요 env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GROQ_API_KEY.

    JSON 형식: {"songs": [{"start_s": 1023, "end_s": 1245, "title": "...", "artist": "...",
    "lyrics": "..."}]} — start_s/end_s만 필수, 나머지는 선택(식별 단서).
    """
    import json

    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    from soopts import batch

    vod_id = extract_vod_id(args.vod)
    data = json.loads(Path(args.spans).read_text(encoding="utf-8"))
    songs = data.get("songs") if isinstance(data, dict) else data
    if not isinstance(songs, list):
        raise ValueError("spans JSON은 곡 목록이거나 {\"songs\": [...]} 형식이어야 합니다")
    result = batch.ingest_vod(
        cfg, title_no=vod_id, bj_id=args.bj or cfg.station.bj_id, songs=songs
    )
    print(result["text"])
    return 0


def cmd_vods(args) -> int:
    """처리된 VOD 목록 + performance 수를 출력 — vod-audit 스킬의 감사 대상 조회용.

    판정을 하지 않는 순수 조회 명령이다(Groq 없음). 어떤 VOD가 잘못 처리됐는지는 스킬
    안에서 Claude가 `soopts comments`로 원본 댓글을 읽고 정한다. 필요 env: SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY.
    """
    import json

    from soopts import db

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    rows = db.fetch_vods_by_status(statuses)
    out = [
        {
            "id": r["id"],
            "title_no": r["soop_title_no"],
            "title": r.get("title") or "",
            "status": r.get("status"),
            "duration_s": r.get("duration_s"),
            "note": r.get("error"),  # manual 사유 메모(로컬 처리 우선순위 힌트)
            "machine_perfs": db.count_machine_performances(r["id"]),
            "confirmed_perfs": db.count_confirmed_performances(r["id"]),
        }
        for r in rows
    ]
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"status={statuses} — {len(out)}건")
        for o in out:
            note = f" · {o['note']}" if o["note"] else ""
            print(
                f"  {o['title_no']} [{o['status']}] {o['title']} "
                f"— 기계 {o['machine_perfs']} / confirmed {o['confirmed_perfs']}{note}"
            )
    return 0


def cmd_comments(args) -> int:
    """VOD 원본 댓글을 출력 — vod-audit 스킬이 노래 타임라인 유무를 Claude로 판정하는 입력.

    Groq를 쓰지 않는 순수 조회다(코드가 노래/게임/티저를 구분하지 않는다 — 그건 스킬 안의
    Claude 몫). 필요 env: (없음, 공개 댓글 API).
    """
    import json

    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    from soopts.collector.comments import fetch_comments

    vod_id = extract_vod_id(args.vod)
    comments = fetch_comments(cfg, args.bj or cfg.station.bj_id, vod_id)
    if args.json:
        print(json.dumps(
            {"title_no": vod_id, "count": len(comments), "comments": comments},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(f"VOD {vod_id} — 댓글 {len(comments)}개")
        for c in comments:
            print("---")
            print(c)
    return 0


def cmd_set_manual(args) -> int:
    """VOD 하나를 'manual'로 되돌린다 — vod-audit 스킬이 Claude 판정 후 호출하는 적용 명령.

    --clear-machine면 기계 생성 performances를 먼저 지운다(confirmed는 보존). 판정은 스킬
    안의 Claude가 하고, 이 명령은 결정된 VOD에만 결정론적으로 적용한다. 필요 env:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
    """
    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    from soopts import batch

    vod_id = extract_vod_id(args.vod)
    result = batch.set_vod_manual(
        cfg, title_no=vod_id, clear_machine=args.clear_machine
    )
    print(result["text"])
    return 0


def cmd_perfs(args) -> int:
    """performance 목록을 출력 — perf-review 스킬의 로컬 검증 대상 조회용.

    필터: --identify(identify_status), --local(local_review). 각 행에 title_no·시각·추측제목·
    lyrics 유무를 얹는다. Groq 없이 순수 조회. 필요 env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
    """
    import json

    from soopts import db

    rows = db.fetch_performances(identify_status=args.identify, local_review=args.local)
    out = [
        {
            "id": p["id"],
            "title_no": p.get("soop_title_no"),
            "start_s": p.get("start_s"),
            "end_s": p.get("end_s"),
            "title_guess": p.get("title_guess"),
            "song_id": p.get("song_id"),
            "identify_status": p.get("identify_status"),
            "local_review": p.get("local_review"),
            "has_lyrics": bool((p.get("lyrics_snippet") or "").strip()),
        }
        for p in rows
    ]
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"performances {len(out)}건 (identify={args.identify}, local={args.local})")
        for o in out:
            print(f"  #{o['id']} {o['title_no']} {o['start_s']}~{o['end_s']}s "
                  f"[{o['identify_status']}/{o['local_review']}] {o['title_guess'] or '(미상)'}")
    return 0


def cmd_set_perf(args) -> int:
    """performance 한 행을 갱신 — perf-review 스킬이 로컬 검증·보강 결과를 적용.

    보내지 않은 필드는 그대로 둔다. 필요 env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
    """
    from soopts import db

    fields = {
        "start_s": args.start_s, "end_s": args.end_s, "title_guess": args.title_guess,
        "lyrics_snippet": args.lyrics, "song_id": args.song_id,
        "identify_status": args.identify_status, "local_review": args.local_review,
    }
    r = db.update_performance(args.perf_id, fields)
    if r is None:
        print(f"performance #{args.perf_id}: 갱신할 필드 없음 또는 행 없음")
        return 1
    print(f"performance #{args.perf_id} 갱신: "
          f"identify={r.get('identify_status')} local={r.get('local_review')} song_id={r.get('song_id')}")
    return 0


def cmd_add_song(args) -> int:
    """songs에 draft 신곡을 삽입하고 song_id를 출력 — 무-카탈로그 곡 등록(perf-review).

    status는 기본 'draft'(검수 UI가 published로 승격 전까지 정식 카탈로그와 구분). 필요 env:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
    """
    from soopts import db

    song_id = db.insert_draft_song(
        title=args.title, artist=args.artist, lyrics=args.lyrics, status=args.status
    )
    print(song_id)
    return 0


def cmd_match_song(args) -> int:
    """제목/가수(+가사)를 카탈로그에 매칭해 결과를 JSON으로 — perf-review가 재식별 후 확인용.

    resolve_song_match(ingest와 동일 로직)를 재사용한다. song_id가 나오면 카탈로그에 있는 것,
    null이면 신곡(draft 삽입 대상). 필요 env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GROQ_API_KEY.
    """
    import json

    from soopts import db
    from soopts.analyzers.identify import resolve_song_match

    catalog = db.load_song_catalog()
    r = resolve_song_match(args.title, args.artist or "", args.lyrics or "", True, catalog)
    print(json.dumps({
        "song_id": r.song_id, "title_guess": r.title_guess,
        "confidence": round(r.match_confidence, 1), "identify_status": r.identify_status,
    }, ensure_ascii=False))
    return 0


def cmd_transcribe(args) -> int:
    """VOD의 [start,end] 구간만 받아 Whisper로 전사·출력 — perf-review 스킬의 검증 입력.

    구간만 받으므로(멀티파트 안전) 긴 VOD도 빠르다. 캐시는 work/{id}/clips/에 남는다.
    필요 env: GROQ_API_KEY (전사).
    """
    import tempfile

    from soopts.analyzers.stt import (
        _ensure_uploadable,
        _load_model,
        _transcribe_best,
        _transcribe_langs,
        _transcribe_segments,
    )
    from soopts.collector.media import download_span, resolve_m3u8_list, split_by_part
    from soopts.collector.meta import fetch_meta

    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    vod_id = extract_vod_id(args.vod)
    work = work_paths(cfg.work_root, vod_id).ensure()
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    meta = fetch_meta(cfg, vod_id, work)
    ds = max(0.0, args.start - args.pad)
    de = args.end + args.pad
    m3u8s = resolve_m3u8_list(vod_id, cfg.clip.quality)
    spans = split_by_part(ds, de, meta.parts, m3u8s)
    if not spans:
        raise RuntimeError(f"구간 [{ds:.0f},{de:.0f}] 파트 매핑 실패")
    raw = work.clips_dir / f"seg_{int(ds)}_{int(de)}.mp4"
    # lead: 파일이 요청 시각보다 앞서 시작하는 초(세그먼트 단위로만 받을 수 있어서). 그래서
    # 파일 t=0의 절대초는 ds가 아니라 ds-lead다. 파일 안에서 lead초를 건너뛰는 대신 **환산에서
    # 빼는** 이유: ffmpeg의 입력 -ss는 키프레임으로 되감겨(세그먼트 경계라 사실상 0) 스킵이
    # 먹지 않는다 — 실측에서 창을 1초씩 옮기면 같은 가사가 1초씩 밀려 찍혔다.
    lead, dur = download_span(spans, raw, workers=cfg.collector.segment_workers, force=args.force)
    file_start = ds - lead
    dur += lead   # 앞에 lead가 붙었으므로 그만큼 더 읽어야 요청 끝까지 담긴다
    model = _load_model(cfg)
    langs = [args.lang] if args.lang else ([cfg.stt.language] if cfg.stt.language else ["en", "ko"])
    if args.segments:
        # 세그먼트별 타임스탬프 JSON — 종료 경계 판정용(가사 끝→아웃트로 잡담 시작 지점 찾기).
        # 시각은 파일 t=0의 절대초(file_start = ds - lead)를 더해 VOD 절대초로 환산한다.
        import json as _json

        with tempfile.TemporaryDirectory() as td:
            p = _ensure_uploadable(str(raw), Path(td), 0, dur)
            segs, _ = _transcribe_segments(model, p, cfg.stt, langs) if p else ([], "")
        out = [
            {"start": round(file_start + s["start"], 1),
             "end": round(file_start + s["end"], 1), "text": s["text"]}
            for s in segs
        ]
        print(_json.dumps(out, ensure_ascii=False))
        return 0
    if args.lang:
        with tempfile.TemporaryDirectory() as td:
            p = _ensure_uploadable(str(raw), Path(td), 0, dur)
            text, _ = _transcribe_langs(model, p, cfg.stt, [args.lang]) if p else ("", "")
    else:
        text, _ = _transcribe_best(model, str(raw), cfg, start=0, dur=dur)
    print(text)
    return 0


def cmd_fetch(args) -> int:
    cfg, vod_id, work = _ctx(args)
    from soopts.collector.media import download_audio_full

    download_audio_full(args.vod, work.root / "audio.mp3", args.quality)
    print(f"오디오 저장: {work.root / 'audio.mp3'}")
    return 0


def cmd_songs(args) -> int:
    """노래 감지 → STT 전사·판별 → 타임라인 출력."""
    cfg, vod_id, work = _ctx(args)
    from soopts.models import read_meta, write_songs
    from soopts.output import render_songs, write_songs_txt

    meta = read_meta(work.meta) if work.meta.exists() else None
    songs = _songs_slice(cfg, vod_id, work, meta, args) if args.slice \
        else _songs_full(cfg, vod_id, work, args)

    write_songs(work.songs_json, songs, vod_id)
    write_songs_txt(work.songs_txt, meta, songs)
    print(render_songs(meta, songs))
    print(f"저장: {work.songs_txt}")
    if any(s.lyrics and not s.title for s in songs):
        print("\n※ 가사가 있는 곡은 Claude/사람이 가사로 곡명을 채우면 됩니다.")
    return 0


def _songs_full(cfg: Config, vod_id: str, work: WorkPaths, args):
    """전체 오디오 모드: inaSpeechSegmenter 음악감지 + 스티커 + STT."""
    from soopts.analyzers.audio_analyzer import detect_songs

    songs = detect_songs(cfg, vod_id, work, audio_path=args.audio, force=args.force)
    if songs and not args.no_stt:
        if not args.audio:
            log.warning("STT 전사 생략 — --audio 로 오디오/영상 파일을 지정하세요")
        else:
            from soopts.analyzers.stt import transcribe_songs

            songs = transcribe_songs(cfg, songs, args.audio)
    return songs


def _songs_slice(cfg: Config, vod_id: str, work: WorkPaths, meta, args):
    """효율 모드: 스티커로 노래 후보 위치 → 그 구간만 슬라이스 다운로드 → STT.

    전체 영상(수백 MB)을 받지 않고 후보 구간(구간당 수 MB)만 받는다. 스티커 적은
    조용한 곡은 놓칠 수 있다(재현율↓, 다운로드↓ 트레이드오프).
    """
    from soopts.analyzers.audio_analyzer import sticker_burst_regions
    from soopts.analyzers.stt import transcribe_slices
    from soopts.collector.media import download_span, resolve_m3u8_list, split_by_part
    from soopts.models import Song, read_chat_jsonl

    if not work.chat.exists():
        raise RuntimeError("chat.jsonl 없음 — 먼저 `soopts collect` 로 채팅(스티커)을 수집하세요")
    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    total = meta.total_duration if meta else None
    a = cfg.audio
    regions = sticker_burst_regions(
        stickers,
        bucket_s=a.sticker_bucket_s,
        window_buckets=a.sticker_window_buckets,
        min_per_window=a.sticker_min_per_window,
        pad_before_s=a.sticker_pad_before_s,
        pad_after_s=a.sticker_pad_after_s,
        skip_opening_s=a.skip_opening_s,
        total_s=total,
    )
    log.info("스티커 기반 노래 후보 %d구간 (전체 다운로드 없이)", len(regions))
    if not regions:
        return []

    m3u8s = resolve_m3u8_list(args.vod)
    parts = meta.parts if meta else []
    if len(m3u8s) != len(parts) and parts:
        log.warning("m3u8 파트 수(%d) != 메타 파트 수(%d) — 매핑 부정확 가능", len(m3u8s), len(parts))
    slice_dir = work.root / "slices"
    slice_dir.mkdir(exist_ok=True)

    pairs = []
    for s, e in regions:
        spans = split_by_part(s, e, parts, m3u8s)
        if not spans:
            log.warning("구간 %d-%d 파트 매핑 실패 — 건너뜀", int(s), int(e))
            continue
        path = slice_dir / f"slice_{int(s)}_{int(e)}.mp4"
        # 이 경로는 슬라이스 전체를 통째로 전사하고 시각은 region(s,e)에서 오므로 lead는 안 쓴다.
        download_span(spans, path, workers=cfg.collector.segment_workers, force=args.force)
        rate = sum(1 for t in stickers if s <= t <= e) / max((e - s) / 60.0, 1e-6)
        song = Song(t=int(s), end=int(e), duration=int(e - s),
                    sticker_rate=round(rate, 1), song_likely=rate >= cfg.audio.sticker_rate_strong)
        pairs.append((song, str(path)))

    if args.no_stt:
        return [s for s, _ in pairs]
    return transcribe_slices(cfg, pairs)


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="soopts", description="SOOP VOD 노래 하이라이트 추출·식별")
    p.add_argument("--config", help="soopts.toml 경로 (기본: CWD의 soopts.toml)")
    p.add_argument("--work-root", help="작업 디렉터리 루트 (기본: ./work)")
    p.add_argument("--force", action="store_true", help="캐시 무시하고 재계산")
    p.add_argument("-v", "--verbose", action="store_true", help="디버그 로그")

    sub = p.add_subparsers(dest="cmd", required=True)

    def add_vod(sp):
        sp.add_argument("vod", help="VOD URL 또는 번호")

    sp = sub.add_parser("collect", help="메타 + 채팅(스티커) 수집")
    add_vod(sp)
    sp.add_argument("--reparse", action="store_true", help="raw XML에서 chat.jsonl만 재생성(네트워크 없음)")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("clips", help="BJ 부른 노래만 1080p 클립 추출 + (옵션)유튜브 업로드")
    add_vod(sp)
    sp.add_argument("--no-stt", action="store_true", help="클립 가사 전사 생략")
    sp.set_defaults(func=cmd_clips)

    sp = sub.add_parser(
        "daily", help="스테이션 최신 VOD 댓글 타임라인(🎤) 파싱·식별·DB 기록 — GitHub Actions 배치용"
    )
    sp.add_argument("--count", type=int, help="처리할 미처리 VOD 수(기본: soopts.toml [station] daily_vod_count)")
    sp.add_argument("--bj", help="대상 스테이션 bj_id(기본: soopts.toml [station] bj_id)")
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser(
        "youtube-upload",
        help="검증 완료 VOD 하나의 노래 구간을 합본 영상으로 유튜브 업로드(unlisted, 하루 1건)",
    )
    sp.add_argument("--title-no", help="특정 VOD 지정(기본: 가장 오래된 대상 자동 선택)")
    sp.add_argument("--dry-run", action="store_true",
                    help="합본 영상·제목·설명만 만들고 업로드하지 않음(산출물 보존)")
    sp.set_defaults(func=cmd_youtube_upload)

    sp = sub.add_parser(
        "ingest",
        help="로컬 분석(analyze_vod.py)으로 뽑은 곡 목록(JSON)을 DB에 기록 (무-타임라인 VOD)",
    )
    add_vod(sp)
    sp.add_argument("spans", help="곡 목록 JSON 파일 경로 ({\"songs\": [{start_s,end_s,title,...}]})")
    sp.add_argument("--bj", help="스테이션 bj_id(기본: soopts.toml [station] bj_id)")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser(
        "vods",
        help="처리된 VOD 목록 + performance 수 조회 (vod-audit 스킬용, 판정 안 함)",
    )
    sp.add_argument("--status", default="analyzed,done", help="쉼표구분 status 필터(기본: analyzed,done)")
    sp.add_argument("--json", action="store_true", help="JSON 출력")
    sp.set_defaults(func=cmd_vods)

    sp = sub.add_parser(
        "comments",
        help="VOD 원본 댓글 출력 (vod-audit 스킬이 노래 타임라인 유무를 판정하는 입력)",
    )
    add_vod(sp)
    sp.add_argument("--json", action="store_true", help="JSON 출력")
    sp.add_argument("--bj", help="스테이션 bj_id(기본: soopts.toml [station] bj_id)")
    sp.set_defaults(func=cmd_comments)

    sp = sub.add_parser(
        "set-manual",
        help="VOD 하나를 manual로 되돌림 (vod-audit 스킬이 Claude 판정 후 적용)",
    )
    add_vod(sp)
    sp.add_argument("--clear-machine", action="store_true",
                    help="기계 생성 performances를 먼저 삭제(confirmed는 보존)")
    sp.set_defaults(func=cmd_set_manual)

    sp = sub.add_parser(
        "perfs", help="performance 목록 조회 (perf-review 스킬용, 판정 안 함)")
    sp.add_argument("--identify", help="identify_status 필터(needs_review/auto_matched/confirmed 등)")
    sp.add_argument("--local", help="local_review 필터(pending/verified/needs_human)")
    sp.add_argument("--json", action="store_true", help="JSON 출력")
    sp.set_defaults(func=cmd_perfs)

    sp = sub.add_parser(
        "set-perf", help="performance 갱신 (perf-review 스킬이 로컬 검증·보강 적용)")
    sp.add_argument("perf_id", type=int, help="performance id")
    sp.add_argument("--start-s", type=int, dest="start_s")
    sp.add_argument("--end-s", type=int, dest="end_s")
    sp.add_argument("--title-guess", dest="title_guess")
    sp.add_argument("--lyrics")
    sp.add_argument("--song-id", dest="song_id")
    sp.add_argument("--identify-status", dest="identify_status",
                    help="auto_matched/needs_review/confirmed 등")
    sp.add_argument("--local-review", dest="local_review",
                    help="verified/needs_human/pending")
    sp.set_defaults(func=cmd_set_perf)

    sp = sub.add_parser(
        "add-song", help="songs에 draft 신곡 삽입 → song_id 출력 (무-카탈로그 곡 등록)")
    sp.add_argument("--title", required=True)
    sp.add_argument("--artist")
    sp.add_argument("--lyrics")
    sp.add_argument("--status", default="draft", help="기본 draft")
    sp.set_defaults(func=cmd_add_song)

    sp = sub.add_parser(
        "match-song", help="제목/가수를 카탈로그에 매칭 → song_id JSON (perf-review 재식별용)")
    sp.add_argument("--title", required=True)
    sp.add_argument("--artist")
    sp.add_argument("--lyrics")
    sp.set_defaults(func=cmd_match_song)

    sp = sub.add_parser(
        "transcribe", help="VOD의 [start,end] 구간만 받아 Whisper 전사·출력 (perf-review용)")
    add_vod(sp)
    sp.add_argument("--start", type=float, required=True, help="시작 초")
    sp.add_argument("--end", type=float, required=True, help="끝 초")
    sp.add_argument("--pad", type=float, default=15.0, help="앞뒤 여유 초(기본 15)")
    sp.add_argument("--lang", help="강제 언어(en/ko). 없으면 자동")
    sp.add_argument(
        "--segments", action="store_true",
        help="텍스트 대신 세그먼트별 [{start,end,text}] 타임스탬프 JSON 출력(종료 경계 판정용)")
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser("fetch", help="yt-dlp로 전체 오디오 다운로드")
    add_vod(sp)
    sp.add_argument("--quality", default="hls-hd", help="yt-dlp 포맷(기본 hls-hd)")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("songs", help="노래 감지 + STT 전사·판별 + 타임라인")
    add_vod(sp)
    sp.add_argument("--audio", help="[전체모드] 오디오/영상 파일 경로 (노래감지·STT용)")
    sp.add_argument("--slice", action="store_true",
                    help="[효율모드] 스티커로 찾은 노래 후보 구간만 슬라이스 다운로드해 처리(전체 DL 없음)")
    sp.add_argument("--no-stt", action="store_true", help="STT 전사 생략(감지만)")
    sp.set_defaults(func=cmd_songs)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001
        if args.verbose:
            raise
        log.error("%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
