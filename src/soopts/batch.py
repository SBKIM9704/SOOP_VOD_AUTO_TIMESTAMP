"""`soopts daily` / `soopts sync` 오케스트레이션 — GitHub Actions 무인 배치.

daily: 스테이션 최신 VOD 중 미처리분 → 감지(스티커 구간)+1080p 정밀 클립 컷+STT →
       Claude/rapidfuzz 곡 식별 → DB(performances) 기록 → 업로드 큐 소진.
sync : 검수 확정(confirmed)된 건의 유튜브 제목/설명을 갱신.

휘발성 러너 대응이 핵심 설계 포인트: 업로드 큐의 진실은 파일이 아니라 DB
(clip_status + start_s/end_s + vods.soop_title_no)다. 클립 파일 경로는
`clip_file_path()`로 title_no+start_s만으로 결정론적으로 재구성되므로, 이전
실행에서 파일이 사라졌어도(러너 초기화) 같은 경로가 없으면 그 구간만 재슬라이스한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from soopts.config import Config
from soopts.log import get_logger
from soopts.paths import work_paths

log = get_logger("batch")


# --------------------------------------------------------------------------- #
# 순수 함수 (테스트 대상)
# --------------------------------------------------------------------------- #
def clip_file_path(work_root: Path, title_no: str, start_s: float) -> Path:
    """title_no+start_s만으로 결정되는 클립 파일 경로. 업로드 큐 재생성의 기준점."""
    return work_paths(work_root, title_no).clips_dir / f"song_{int(start_s):06d}.mp4"


def next_vod_status(detected: int) -> str:
    """VOD 처리 직후 상태. 감지된 노래가 없으면 검수·업로드할 게 없으니 바로 종결(done),
    있으면 업로드 큐 소진까지 거쳐야 하니 analyzed로 남긴다."""
    return "done" if detected == 0 else "analyzed"


def format_summary(stats: dict[str, int]) -> str:
    """일일 배치 결과 한 줄 요약. Slack/로그 공용."""
    parts = [
        f"VOD {stats.get('vods', 0)}건",
        f"감지 {stats.get('detected', 0)}곡",
        f"자동매칭 {stats.get('auto_matched', 0)}",
        f"검수대기 {stats.get('needs_review', 0)}",
    ]
    if "uploaded" in stats:
        parts.append(f"업로드 {stats['uploaded']}건 (큐 잔여 {stats.get('queue_remaining', 0)})")
    return " / ".join(parts)


def vod_link(cfg: Config, title_no: str) -> str:
    """VOD 웹 플레이어 링크 (Slack 하이퍼링크용, 특정 시각 지정 없음)."""
    return cfg.endpoints.vod_web_url.replace("{title_no}", str(title_no)).replace(
        "?change_second={sec}", ""
    )


def format_vod_result(cfg: Config, title_no: str, stats: dict[str, Any]) -> str:
    """VOD 하나 처리 결과 — Slack에 개별 발송할 상세 메시지."""
    link = vod_link(cfg, title_no)
    if stats.get("error"):
        return f"❌ VOD {title_no} 처리 실패: {link}\n    {stats['error']}"

    lines = [f"✅ VOD {title_no} 처리 완료: {link}"]
    lines.append(
        f"    감지 {stats.get('detected', 0)}곡 "
        f"(자동매칭 {stats.get('auto_matched', 0)}, 검수대기 {stats.get('needs_review', 0)})"
    )
    region_errors = stats.get("region_errors") or []
    if region_errors:
        lines.append(f"    일부 구간 실패({len(region_errors)}건): " + " / ".join(region_errors))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# daily
# --------------------------------------------------------------------------- #
def run_daily(cfg: Config, *, bj_id: str, count: int, upload: bool) -> dict[str, Any]:
    from soopts import db
    from soopts.collector.vod_list import fetch_recent_vods

    try:
        candidates = fetch_recent_vods(cfg, bj_id, count * 3)
        picked = db.pick_unprocessed_vods(candidates, count)
    except Exception as e:  # noqa: BLE001
        _notify_slack_failure("VOD 목록 조회", e)
        raise

    stats = {"vods": 0, "detected": 0, "auto_matched": 0, "needs_review": 0}
    for vod_row in picked:
        title_no = vod_row["soop_title_no"]
        try:
            vod_stats = _process_vod(cfg, vod_row, bj_id)
            stats["detected"] += vod_stats["detected"]
            stats["auto_matched"] += vod_stats["auto_matched"]
            stats["needs_review"] += vod_stats["needs_review"]
            db.mark_vod(title_no, next_vod_status(vod_stats["detected"]))
            _notify_slack(format_vod_result(cfg, title_no, vod_stats))
        except Exception as e:  # noqa: BLE001
            log.error("VOD %s 처리 실패: %s", title_no, e)
            db.mark_vod(title_no, "failed", error=str(e)[:500])
            _notify_slack(format_vod_result(cfg, title_no, {"error": str(e)[:500]}))
        stats["vods"] += 1

    if upload:
        try:
            upload_stats = _drain_upload_queue(cfg)
            stats["uploaded"] = upload_stats["uploaded"]
            stats["queue_remaining"] = db.count_upload_queue()
        except Exception as e:  # noqa: BLE001
            _notify_slack_failure("업로드 큐 소진", e)
            raise

    text = format_summary(stats)
    log.info("daily 완료: %s", text)
    _notify_slack(text)
    return {**stats, "text": text}


def _find_candidates(cfg: Config, bj_id: str, title_no: str, stickers: list[float], meta) -> list[tuple]:
    """(ds, de, hint) 후보 목록 — 댓글 타임라인이 있으면 그걸 우선 쓰고, 없으면 스티커 감지로 폴백.

    댓글 타임라인은 팬이 자원해서 남긴 비공식 타임스탬프라 정확한 노래 길이를 모르니
    ds/de를 넉넉히 잡아 뒤에서 inaSpeechSegmenter가 정밀 경계를 찾게 한다. hint가 있으면
    이미 아티스트/제목이 확정된 것이므로 이후 identify 단계에서 Claude 가사 추측을 건너뛴다.
    """
    from soopts.analyzers.comment_timeline import extract_song_timeline
    from soopts.collector.comments import fetch_comments

    try:
        comments = fetch_comments(cfg, bj_id, title_no)
        timeline = extract_song_timeline(comments)
    except Exception as ex:  # noqa: BLE001
        log.warning("VOD %s 댓글 조회/추출 실패 — 스티커 감지로 폴백: %s", title_no, ex)
        timeline = []

    if timeline:
        log.info("VOD %s 댓글 타임라인 사용 — 노래 %d곡", title_no, len(timeline))
        c = cfg.comment
        return [
            (max(0.0, t.time_s - c.pad_before_s), t.time_s + c.pad_after_s, t) for t in timeline
        ]

    from soopts.analyzers.audio_analyzer import sticker_burst_regions

    a = cfg.audio
    regions = sticker_burst_regions(
        stickers, bucket_s=a.sticker_bucket_s, window_buckets=a.sticker_window_buckets,
        min_per_window=a.sticker_min_per_window, pad_before_s=a.sticker_pad_before_s,
        pad_after_s=a.sticker_pad_after_s, skip_opening_s=a.skip_opening_s,
        total_s=meta.total_duration,
    )
    log.info("VOD %s 노래 후보 %d구간(스티커 감지)", title_no, len(regions))
    return [
        (max(0.0, s - cfg.clip.dl_pad_before_s), e + cfg.clip.dl_pad_after_s, None)
        for s, e in regions
    ]


def _process_vod(cfg: Config, vod_row: dict[str, Any], bj_id: str) -> dict[str, Any]:
    """collect → 노래 구간 후보(댓글 타임라인 우선, 없으면 스티커 감지) → 구간별로 하나씩
    (1080p 정밀 클립+STT+식별+DB 기록).

    구간 하나의 실패는 그 구간만 건너뛰고 나머지는 계속 진행한다(이전엔 VOD 전체를
    실패 처리해 이미 성공한 구간까지 버렸다 — VOD 201651295에서 실제로 이렇게 30분
    분량 작업이 통째로 날아간 사례가 있어 구간 단위로 바꿨다). 다만 후보 구간이
    있었는데 전부 실패하면(예: 네트워크 장애) 재시도 대상이 되도록 예외를 던진다 —
    그렇지 않으면 next_vod_status가 이걸 "노래 없음"과 구분 못 하고 done으로
    잘못 종결시킨다.
    """
    from soopts import db
    from soopts.analyzers.audio_analyzer import sticker_rate
    from soopts.analyzers.identify import (
        MATCH_THRESHOLD,
        IdentifyResult,
        identify_song,
        match_catalog,
    )
    from soopts.analyzers.stt import _load_model, _transcribe_best
    from soopts.collector.chat import fetch_chat
    from soopts.collector.media import download_slice, map_to_part, resolve_m3u8_list
    from soopts.collector.meta import fetch_meta
    from soopts.export.clips import make_clip
    from soopts.models import Song, read_chat_jsonl
    from soopts.output import fmt_hms

    title_no = vod_row["soop_title_no"]
    work = work_paths(cfg.work_root, title_no).ensure()
    meta = fetch_meta(cfg, title_no, work)
    fetch_chat(cfg, title_no, meta, work)

    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    candidates = _find_candidates(cfg, bj_id, title_no, stickers, meta)
    stats: dict[str, Any] = {"detected": 0, "auto_matched": 0, "needs_review": 0, "region_errors": []}
    if not candidates:
        return stats

    m3u8s = resolve_m3u8_list(title_no, cfg.clip.quality)
    parts = meta.parts
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    catalog = db.load_song_catalog()
    stt_model = None

    for ds, de, hint in candidates:
        label = f"{fmt_hms(int(ds))}~{fmt_hms(int(de))}"
        try:
            m3u8, ls, le = map_to_part(ds, de, parts, m3u8s)
            if m3u8 is None:
                log.warning("VOD %s 구간 %s 파트 매핑 실패 — 건너뜀", title_no, label)
                continue
            raw = work.clips_dir / f"region_{int(ds)}.mp4"
            if not raw.exists():
                download_slice(m3u8, ls, le, raw)
            clip = make_clip(cfg, title_no, str(raw), ds, de, work.clips_dir, media_offset=ds)
            if clip is None:
                continue

            if stt_model is None:
                stt_model = _load_model(cfg)
            text, _lang = _transcribe_best(stt_model, clip.path, cfg)
            lyrics = text[: cfg.stt.lyric_chars]
            rate = sticker_rate((clip.t, clip.end), stickers)
            song = Song(
                t=clip.t, end=clip.end, duration=clip.duration,
                sticker_rate=round(rate, 1),
                song_likely=(hint is not None) or (rate >= cfg.audio.sticker_rate_strong),
                lyrics=lyrics,
            )

            if hint is not None:
                # 댓글에 이미 아티스트/제목이 있으니 Claude 가사 추측을 건너뛰고 바로 매칭한다.
                entry, score = match_catalog(hint.title, hint.artist or "", catalog)
                result = (
                    IdentifyResult(entry.song_id, hint.title, score, "auto_matched")
                    if entry is not None and score >= MATCH_THRESHOLD
                    else IdentifyResult(None, hint.title, score, "needs_review")
                )
            else:
                result = identify_song(song.lyrics, song.song_likely, catalog) if song.lyrics else None

            perf_rows = db.insert_performances(vod_row["id"], [song], [result])
            if perf_rows:
                db.update_performance(perf_rows[0]["id"], clip_status="clipped")

            stats["detected"] += 1
            if result and result.identify_status == "auto_matched":
                stats["auto_matched"] += 1
            else:
                stats["needs_review"] += 1
        except Exception as ex:  # noqa: BLE001
            log.warning("VOD %s 구간 %s 처리 실패: %s", title_no, label, ex)
            stats["region_errors"].append(f"{label}: {ex}")
            continue

    if stats["detected"] == 0 and stats["region_errors"]:
        raise RuntimeError(
            f"후보 {len(candidates)}구간 전부 실패 (예: {stats['region_errors'][0]})"
        )
    return stats


# --------------------------------------------------------------------------- #
# 업로드 큐 (휘발성 러너 대응)
# --------------------------------------------------------------------------- #
def _drain_upload_queue(cfg: Config) -> dict[str, int]:
    from soopts import db
    from soopts.export.youtube import upload_unlisted
    from soopts.models import broadcast_date
    from soopts.output import fmt_hms

    stats = {"uploaded": 0, "failed": 0}
    queue = db.fetch_upload_queue(cfg.youtube.daily_upload_limit)
    touched_vod_nos: set[str] = set()

    for perf in queue:
        title_no = perf["vods"]["soop_title_no"]
        touched_vod_nos.add(title_no)
        start_s, end_s = perf["start_s"], perf["end_s"]
        path = clip_file_path(cfg.work_root, title_no, start_s)
        try:
            from soopts.collector.meta import fetch_meta

            work = work_paths(cfg.work_root, title_no).ensure()
            if not path.exists():
                log.info("클립 파일 없음(러너 재시작) — %s %s초 재슬라이스", title_no, start_s)
                meta = fetch_meta(cfg, title_no, work)
                path = _reslice_clip(cfg, title_no, meta, start_s, end_s)
            else:
                meta = fetch_meta(cfg, title_no, work)

            bj = meta.bj_nick
            date = broadcast_date(meta)
            title = cfg.youtube.title_template.format(
                bj=bj, title=(perf.get("title_guess") or "곡명 미상"),
                vod_id=title_no, hms=fmt_hms(int(start_s)), date=date,
            )
            vod_url = (
                cfg.endpoints.vod_web_url.replace("{title_no}", str(title_no))
                .replace("{sec}", str(int(start_s)))
            )
            desc = f"{bj} 다시보기 노래 구간\n원본: {vod_url}"
            if perf.get("lyrics_snippet"):
                desc += f"\n\n가사(자동전사):\n{perf['lyrics_snippet']}"

            url = upload_unlisted(cfg, str(path), title, desc)
            video_id = url.rsplit("/", 1)[-1]
            db.update_performance(perf["id"], clip_status="uploaded", youtube_video_id=video_id)
            stats["uploaded"] += 1
        except Exception as e:  # noqa: BLE001
            log.error("업로드 실패 perf=%s: %s", perf.get("id"), e)
            db.update_performance(perf["id"], clip_status="failed")
            stats["failed"] += 1
        finally:
            Path(path).unlink(missing_ok=True)

    for title_no in touched_vod_nos:
        if db.count_clipped_for_vod(title_no) == 0:
            db.mark_vod(title_no, "done")
    return stats


def _reslice_clip(cfg: Config, title_no: str, meta, start_s: float, end_s: float) -> Path:
    """러너가 휘발성이라 클립 파일이 사라졌을 때, DB의 start_s/end_s만으로 재생성한다."""
    from soopts.collector.media import download_slice, map_to_part, resolve_m3u8_list
    from soopts.export.clips import cut_clip

    m3u8s = resolve_m3u8_list(title_no, cfg.clip.quality)
    m3u8, ls, le = map_to_part(start_s, end_s, meta.parts, m3u8s)
    if m3u8 is None:
        raise RuntimeError(f"재슬라이스 파트 매핑 실패: {title_no} {start_s}-{end_s}")

    work = work_paths(cfg.work_root, title_no).ensure()
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    raw = work.clips_dir / f"reslice_{int(start_s)}.mp4"
    download_slice(m3u8, ls, le, raw)
    out = clip_file_path(cfg.work_root, title_no, start_s)
    cut_clip(cfg, str(raw), 0.0, le - ls, out)
    raw.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def run_sync(cfg: Config) -> int:
    from datetime import UTC, datetime

    from soopts import db
    from soopts.export.youtube import update_video_metadata

    try:
        rows = db.fetch_confirmed_pending_sync()
    except Exception as e:  # noqa: BLE001
        _notify_slack_failure("sync 대상 조회", e)
        raise

    n = 0
    failures: list[str] = []
    for perf in rows:
        video_id = perf.get("youtube_video_id")
        if not video_id:
            continue
        song = perf.get("songs") or {}
        title = song.get("title") or perf.get("title_guess") or "곡명 미상"
        artist = song.get("artist")
        display_title = f"{title} - {artist}" if artist else title

        vod = perf.get("vods") or {}
        desc = None
        if vod.get("soop_title_no"):
            vod_url = (
                cfg.endpoints.vod_web_url.replace("{title_no}", str(vod["soop_title_no"]))
                .replace("{sec}", str(int(perf["start_s"])))
            )
            desc = f"원본: {vod_url}"
            if perf.get("lyrics_snippet"):
                desc += f"\n\n가사(자동전사):\n{perf['lyrics_snippet']}"

        try:
            update_video_metadata(cfg, video_id, display_title, desc)
            db.update_performance(perf["id"], synced_at=datetime.now(UTC).isoformat())
            n += 1
        except Exception as e:  # noqa: BLE001
            log.error("동기화 실패 perf=%s: %s", perf.get("id"), e)
            failures.append(f"{video_id}({display_title}): {e}")

    if failures:
        _notify_slack(
            f"⚠️ soopts sync {len(failures)}건 실패(성공 {n}건)\n" + "\n".join(failures[:10])
        )
    return n


# --------------------------------------------------------------------------- #
def _notify_slack(text: str) -> None:
    import os

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:
        import requests

        requests.post(webhook, json={"text": text}, timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("Slack 알림 실패: %s", e)


def _notify_slack_failure(context: str, exc: Exception) -> None:
    """개별 VOD/구간 단위가 아니라 배치 전체를 죽이는 예외(SOOP API 502, DB 연결 실패 등)가
    나면 로그뿐 아니라 Slack에도 어느 단계에서 무슨 에러였는지 남긴다 — 이런 경우는 루프
    진입 전/후라 기존 per-VOD 알림으로는 커버되지 않는다."""
    import traceback

    tb = traceback.format_exc()
    _notify_slack(f"❌ soopts 실패 — {context}\n{type(exc).__name__}: {exc}\n```\n{tb[-1500:]}\n```")
