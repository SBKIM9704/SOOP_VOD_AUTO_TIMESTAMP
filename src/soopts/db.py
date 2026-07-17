"""Supabase 연동 — soopts daily/sync 배치가 vods/performances 상태를 읽고 쓴다.

스키마의 주인은 singgyul_sing_book 레포(supabase/migrations/)다. 이 모듈은 스키마를
소비만 하고 여기서 만들지 않는다. songs 테이블은 읽기 전용으로만 다룬다 — 신곡 행을
자동으로 만드는 함수는 없다(신곡 등록은 검수 UI에서 사람이 한다).

env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (service role — RLS 우회)
supabase-py 클라이언트는 첫 호출 시 지연 생성해 `import soopts`를 가볍게 유지한다.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from soopts.analyzers.identify import CatalogEntry, IdentifyResult
from soopts.log import get_logger
from soopts.models import Song

log = get_logger("db")

MAX_RETRIES = 3

_client_singleton: Any = None


def _client() -> Any:
    global _client_singleton
    if _client_singleton is None:
        from supabase import create_client

        _client_singleton = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
    return _client_singleton


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# vods
# --------------------------------------------------------------------------- #
def select_pending(
    candidates: list[dict[str, Any]],
    existing_by_no: dict[str, dict[str, Any]],
    n: int,
) -> list[dict[str, Any]]:
    """순수 함수: candidates(최신순)에서 신규 또는 재시도 가능한 failed를 최대 n개 고른다.

    이미 vods에 있고 failed가 아니거나 retry_count가 상한에 닿은 건은 건너뛴다.
    """
    picked: list[dict[str, Any]] = []
    for c in candidates:
        if len(picked) >= n:
            break
        title_no = str(c["title_no"])
        row = existing_by_no.get(title_no)
        if row is not None:
            if row.get("status") == "failed" and row.get("retry_count", 0) < MAX_RETRIES:
                # id는 GENERATED ALWAYS AS IDENTITY라 upsert 페이로드에 넣으면
                # PostgREST가 거부한다(실제로 재시도 케이스에서 발생 확인) — 제외.
                retry_row = {k: v for k, v in row.items() if k != "id"}
                picked.append({**retry_row, "soop_title_no": title_no})
            continue
        picked.append({
            "soop_title_no": title_no,
            "title": c.get("title", ""),
            "broadcast_date": c.get("broadcast_date"),
            "duration_s": c.get("duration_s"),
        })
    return picked


def pick_unprocessed_vods(candidates: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """candidates 중 미처리분 최대 n개를 pending으로 upsert하고 그 vods 행을 반환한다."""
    if not candidates or n <= 0:
        return []
    client = _client()
    title_nos = [str(c["title_no"]) for c in candidates]
    existing = client.table("vods").select("*").in_("soop_title_no", title_nos).execute().data
    existing_by_no = {row["soop_title_no"]: row for row in existing}
    picked = select_pending(candidates, existing_by_no, n)
    if not picked:
        return []
    rows = [{**row, "status": "pending"} for row in picked]
    return client.table("vods").upsert(rows, on_conflict="soop_title_no").execute().data


def upsert_backfill_vod(soop_title_no: str, title: str, duration_s: int) -> dict[str, Any] | None:
    """기존 유튜브 업로드용 더미 vods 행(scripts/backfill_existing_clips.py 전용)."""
    rows = (
        _client()
        .table("vods")
        .upsert(
            {"soop_title_no": soop_title_no, "title": title, "status": "done", "duration_s": duration_s},
            on_conflict="soop_title_no",
        )
        .execute()
        .data
    )
    return rows[0] if rows else None


def mark_vod(title_no: str, status: str, error: str | None = None) -> None:
    """vods.status 갱신. failed면 retry_count를 1 증가시킨다."""
    fields: dict[str, Any] = {"status": status, "error": error}
    if status in ("analyzed", "done"):
        fields["processed_at"] = _now_iso()
    client = _client()
    if status == "failed":
        row = (
            client.table("vods")
            .select("retry_count")
            .eq("soop_title_no", title_no)
            .single()
            .execute()
            .data
        )
        fields["retry_count"] = (row.get("retry_count") or 0) + 1
    client.table("vods").update(fields).eq("soop_title_no", title_no).execute()


# --------------------------------------------------------------------------- #
# performances
# --------------------------------------------------------------------------- #
def insert_performances(
    vod_row_id: str,
    songs: list[Song],
    identify_results: list[IdentifyResult | None] | None = None,
) -> list[dict[str, Any]]:
    """Song(+identify 결과)을 performances에 1:1 매핑해 insert한다.

    identify_results를 생략하면 song_id=NULL, identify_status='needs_review'로 남는다.
    songs 테이블에 신곡 행을 만들지 않는다 — song_id는 기존 카탈로그 매칭 결과만 연결한다.
    """
    if not songs:
        return []
    results: list[IdentifyResult | None] = identify_results or [None] * len(songs)
    rows = [
        {
            "vod_id": vod_row_id,
            "start_s": s.t,
            "end_s": s.end,
            "sticker_rate": s.sticker_rate,
            "song_likely": s.song_likely,
            "lyrics_snippet": s.lyrics,
            "title_guess": r.title_guess if r else None,
            "match_confidence": r.match_confidence if r else None,
            "song_id": r.song_id if r else None,
            "identify_status": r.identify_status if r else "needs_review",
            "clip_status": "none",
        }
        for s, r in zip(songs, results, strict=True)
    ]
    return _client().table("performances").insert(rows).execute().data


def update_performance(perf_id: str, **fields: Any) -> None:
    _client().table("performances").update(fields).eq("id", perf_id).execute()


def fetch_upload_queue(limit: int) -> list[dict[str, Any]]:
    """clip_status='clipped'이면서 노래로 확정(auto_matched/confirmed)된 건을 오래된 순으로
    최대 limit개, songs(title,artist)/vods.soop_title_no를 join해 반환.

    identify_status가 needs_review/rejected인 건(노래 아님/미확정)은 clip_status만으로
    거르면 큐에 섞여 업로드돼버린다 — 실제로 발생해 잘못 업로드된 적이 있어 명시적으로 제외한다.
    songs(title,artist)는 유튜브 제목/설명에 원곡 아티스트를 넣기 위해 join한다 — 여기서
    거른 조건(auto_matched/confirmed) 덕분에 song_id는 항상 있어 join이 비지 않는다.

    러너가 휘발성이라 로컬 clip 파일이 사라질 수 있다 — 재슬라이스에 필요한 start_s/end_s/
    soop_title_no가 반환값에 이미 있으므로 파일 없이도 재생성 가능해야 한다.
    """
    return (
        _client()
        .table("performances")
        .select("*, songs(title, artist), vods(soop_title_no)")
        .eq("clip_status", "clipped")
        .in_("identify_status", ["auto_matched", "confirmed"])
        .order("created_at")
        .limit(limit)
        .execute()
        .data
    )


def fetch_confirmed_pending_sync() -> list[dict[str, Any]]:
    """검수 확정(confirmed)됐지만 아직 유튜브에 반영 안 된(synced_at NULL) 건."""
    return (
        _client()
        .table("performances")
        .select("*, songs(title, original_title, artist), vods(soop_title_no, broadcast_date)")
        .eq("identify_status", "confirmed")
        .is_("synced_at", "null")
        .execute()
        .data
    )


def count_upload_queue() -> int:
    """업로드 대상(clip_status='clipped' + identify_status auto_matched/confirmed)으로
    아직 업로드되지 않고 남은 전체 건수(요약 출력용). fetch_upload_queue와 동일 조건이어야
    "큐 잔여"가 실제로 다음 실행에서 드레인될 건수와 일치한다."""
    resp = (
        _client()
        .table("performances")
        .select("id", count="exact")
        .eq("clip_status", "clipped")
        .in_("identify_status", ["auto_matched", "confirmed"])
        .execute()
    )
    return resp.count or 0


def count_clipped_for_vod(title_no: str) -> int:
    """해당 vod에 남은 clip_status='clipped' 건수 — 0이면 vod를 'done'으로 넘길 수 있다."""
    resp = (
        _client()
        .table("performances")
        .select("id, vods!inner(soop_title_no)", count="exact")
        .eq("clip_status", "clipped")
        .eq("vods.soop_title_no", title_no)
        .execute()
    )
    return resp.count or 0


# --------------------------------------------------------------------------- #
# songs (읽기 전용)
# --------------------------------------------------------------------------- #
def load_song_catalog() -> list[CatalogEntry]:
    client = _client()
    songs_rows = client.table("songs").select("id,title,original_title,artist").execute().data
    alias_rows = client.table("song_aliases").select("song_id,alias").execute().data
    aliases_by_song: dict[str, list[str]] = {}
    for row in alias_rows:
        aliases_by_song.setdefault(row["song_id"], []).append(row["alias"])
    return [
        CatalogEntry(
            song_id=row["id"],
            title=row["title"],
            original_title=row.get("original_title"),
            artist=row.get("artist"),
            aliases=aliases_by_song.get(row["id"], []),
        )
        for row in songs_rows
    ]
