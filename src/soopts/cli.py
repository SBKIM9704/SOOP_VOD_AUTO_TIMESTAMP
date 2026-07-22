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
    from soopts.collector.media import download_slice, resolve_m3u8_list
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

    from soopts.collector.media import map_to_part

    m3u8s = resolve_m3u8_list(args.vod, cfg.clip.quality)
    parts = meta.parts if meta else []
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    sources: list[tuple[str, float, float]] = []   # 전사용 (구간파일, 로컬시작, 로컬끝)
    for s, e in regions:
        ds = max(0.0, s - cfg.clip.dl_pad_before_s)
        de = e + cfg.clip.dl_pad_after_s
        m3u8, ls, le = map_to_part(ds, de, parts, m3u8s)
        if m3u8 is None:
            continue
        raw = work.clips_dir / f"region_{int(ds)}.mp4"
        if not raw.exists() or args.force:
            download_slice(m3u8, ls, le, raw, workers=cfg.collector.segment_workers)
        span = detect_song_span(cfg, str(raw), ds, de, media_offset=ds)
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
    from soopts.collector.media import download_slice, map_to_part, resolve_m3u8_list
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
        m3u8, ls, le = map_to_part(s, e, parts, m3u8s)
        if m3u8 is None:
            log.warning("구간 %d-%d 파트 매핑 실패 — 건너뜀", int(s), int(e))
            continue
        path = slice_dir / f"slice_{int(s)}_{int(e)}.mp4"
        if not path.exists() or args.force:
            download_slice(m3u8, ls, le, path, workers=cfg.collector.segment_workers)
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
