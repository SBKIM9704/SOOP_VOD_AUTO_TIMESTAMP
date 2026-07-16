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


# --------------------------------------------------------------------------- #
# daily
# --------------------------------------------------------------------------- #
def run_daily(cfg: Config, *, bj_id: str, count: int, upload: bool) -> dict[str, Any]:
    from soopts import db
    from soopts.collector.vod_list import fetch_recent_vods

    candidates = fetch_recent_vods(cfg, bj_id, count * 3)
    picked = db.pick_unprocessed_vods(candidates, count)

    stats = {"vods": 0, "detected": 0, "auto_matched": 0, "needs_review": 0}
    for vod_row in picked:
        title_no = vod_row["soop_title_no"]
        try:
            vod_stats = _process_vod(cfg, vod_row)
            stats["detected"] += vod_stats["detected"]
            stats["auto_matched"] += vod_stats["auto_matched"]
            stats["needs_review"] += vod_stats["needs_review"]
            db.mark_vod(title_no, next_vod_status(vod_stats["detected"]))
        except Exception as e:  # noqa: BLE001
            log.error("VOD %s 처리 실패: %s", title_no, e)
            db.mark_vod(title_no, "failed", error=str(e)[:500])
        stats["vods"] += 1

    if upload:
        upload_stats = _drain_upload_queue(cfg)
        stats["uploaded"] = upload_stats["uploaded"]
        stats["queue_remaining"] = db.count_upload_queue()

    text = format_summary(stats)
    log.info("daily 완료: %s", text)
    _notify_slack(text)
    return {**stats, "text": text}


def _process_vod(cfg: Config, vod_row: dict[str, Any]) -> dict[str, int]:
    """collect → 노래 구간 감지+1080p 정밀 클립+STT → 식별 → DB 기록.

    한 VOD 안의 개별 구간 실패는 전체 VOD 실패로 전파한다(부분 커밋하지 않음) —
    실패 시 run_daily가 mark_vod('failed')로 재시도 대상으로 남긴다.
    """
    from soopts import db
    from soopts.analyzers.audio_analyzer import sticker_burst_regions, sticker_rate
    from soopts.analyzers.identify import identify_song
    from soopts.analyzers.stt import _load_model, _transcribe_best
    from soopts.collector.chat import fetch_chat
    from soopts.collector.media import download_slice, map_to_part, resolve_m3u8_list
    from soopts.collector.meta import fetch_meta
    from soopts.models import Song, read_chat_jsonl

    title_no = vod_row["soop_title_no"]
    work = work_paths(cfg.work_root, title_no).ensure()
    meta = fetch_meta(cfg, title_no, work)
    fetch_chat(cfg, title_no, meta, work)

    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    a = cfg.audio
    regions = sticker_burst_regions(
        stickers, bucket_s=a.sticker_bucket_s, window_buckets=a.sticker_window_buckets,
        min_per_window=a.sticker_min_per_window, pad_before_s=a.sticker_pad_before_s,
        pad_after_s=a.sticker_pad_after_s, skip_opening_s=a.skip_opening_s,
        total_s=meta.total_duration,
    )
    stats = {"detected": 0, "auto_matched": 0, "needs_review": 0}
    if not regions:
        return stats

    from soopts.export.clips import make_clip

    m3u8s = resolve_m3u8_list(title_no, cfg.clip.quality)
    parts = meta.parts
    work.clips_dir.mkdir(parents=True, exist_ok=True)

    songs: list[Song] = []
    stt_model = None
    for s, e in regions:
        ds = max(0.0, s - cfg.clip.dl_pad_before_s)
        de = e + cfg.clip.dl_pad_after_s
        m3u8, ls, le = map_to_part(ds, de, parts, m3u8s)
        if m3u8 is None:
            log.warning("VOD %s 구간 %d-%d 파트 매핑 실패 — 건너뜀", title_no, int(ds), int(de))
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
        songs.append(Song(
            t=clip.t, end=clip.end, duration=clip.duration,
            sticker_rate=round(rate, 1), song_likely=rate >= a.sticker_rate_strong,
            lyrics=lyrics,
        ))

    stats["detected"] = len(songs)
    if not songs:
        return stats

    catalog = db.load_song_catalog()
    results = [
        identify_song(s.lyrics, s.song_likely, catalog) if s.lyrics else None for s in songs
    ]
    for r in results:
        if r and r.identify_status == "auto_matched":
            stats["auto_matched"] += 1
        else:
            stats["needs_review"] += 1

    perf_rows = db.insert_performances(vod_row["id"], songs, results)
    for perf in perf_rows:
        db.update_performance(perf["id"], clip_status="clipped")
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

    rows = db.fetch_confirmed_pending_sync()
    n = 0
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
