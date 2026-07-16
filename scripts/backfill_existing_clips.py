#!/usr/bin/env python3
"""SBKIM9704 채널의 기존 수동 업로드 클립을 performances로 소급 등록 (1회성 수동 실행).

channels.list(mine=true) → uploads 재생목록 → playlistItems.list 전체 조회 →
영상 제목을 노래책 카탈로그에 매칭(song_id 추정, 전부 needs_review로 insert — 사람 검수 필요).

실행: python scripts/backfill_existing_clips.py
필요 env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
필요 파일: soopts.toml의 [youtube] client_secret/token_file (기존 업로드에 쓴 OAuth 그대로 재사용)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from soopts.config import load_config  # noqa: E402
from soopts.log import get_logger, setup_logging  # noqa: E402

log = get_logger("backfill")

_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def backfill_title_no(video_id: str) -> str:
    """기존 유튜브 영상을 가리키는 더미 vods.soop_title_no 키. 순수 함수."""
    return f"backfill:{video_id}"


def iso8601_duration_to_s(duration: str) -> int:
    """유튜브 contentDetails.duration(PT#H#M#S) → 초. 순수 함수."""
    m = _DURATION_RE.match(duration)
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def fetch_uploaded_videos(service) -> list[dict]:
    """channels.list(mine=true) → uploads 재생목록 → 전체 영상(id/title/duration_s)."""
    ch = service.channels().list(part="contentDetails", mine=True).execute()
    uploads_id = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids: list[str] = []
    page_token = None
    while True:
        resp = (
            service.playlistItems()
            .list(part="contentDetails", playlistId=uploads_id, maxResults=50, pageToken=page_token)
            .execute()
        )
        video_ids.extend(item["contentDetails"]["videoId"] for item in resp["items"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    videos: list[dict] = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        resp = service.videos().list(part="snippet,contentDetails", id=",".join(chunk)).execute()
        for item in resp["items"]:
            videos.append({
                "video_id": item["id"],
                "title": item["snippet"]["title"],
                "duration_s": iso8601_duration_to_s(item["contentDetails"]["duration"]),
            })
    return videos


def main() -> int:
    setup_logging()
    cfg = load_config()

    from soopts import db
    from soopts.analyzers.identify import MATCH_THRESHOLD, IdentifyResult, match_catalog
    from soopts.export.youtube import _get_service
    from soopts.models import Song

    service = _get_service(cfg)
    videos = fetch_uploaded_videos(service)
    log.info("기존 업로드 %d개 발견", len(videos))

    catalog = db.load_song_catalog()
    inserted = 0
    for v in videos:
        title_no = backfill_title_no(v["video_id"])
        vod_row = db.upsert_backfill_vod(title_no, v["title"], v["duration_s"])
        if not vod_row:
            log.warning("vods upsert 실패, 건너뜀: %s", title_no)
            continue

        entry, score = match_catalog(v["title"], "", catalog)
        song_id = entry.song_id if entry and score >= MATCH_THRESHOLD else None
        result = IdentifyResult(
            song_id=song_id, title_guess=v["title"], match_confidence=score,
            identify_status="needs_review",
        )
        song = Song(
            t=0, end=v["duration_s"], duration=v["duration_s"],
            sticker_rate=0.0, song_likely=False, lyrics="", title=v["title"],
        )
        perf_rows = db.insert_performances(vod_row["id"], [song], [result])
        if perf_rows:
            db.update_performance(
                perf_rows[0]["id"], clip_status="uploaded", youtube_video_id=v["video_id"]
            )
        inserted += 1

    log.info("백필 완료: %d건", inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
