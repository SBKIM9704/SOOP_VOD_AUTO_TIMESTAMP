"""Supabase 연동 — soopts daily 배치가 vods/performances 상태를 읽고 쓴다.

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
    """순수 함수: candidates(최신순)에서 신규 또는 재시도 가능한 건을 최대 n개 고른다.

    재시도 대상은 두 가지다.

    - **failed**: 처리 중 예외가 나 `mark_vod`가 명시적으로 기록한 상태.
    - **pending**: 처리를 시작만 하고 끝내지 못한 상태. `pick_unprocessed_vods`가 처리 전에
      pending으로 올려두는데, 실행이 SIGKILL되면(타임아웃, 워크플로우 취소, 러너 리셋)
      `mark_vod`가 실행되지 못해 그대로 남는다. 예전엔 이걸 건너뛰어서 **해당 VOD가 영원히
      다시 잡히지 않았다** — 실제로 5.5시간 타임아웃으로 취소된 실행이 VOD 하나를 이 상태에
      가둔 사례가 있다.

    선택 시점에 보이는 pending은 반드시 죽은 실행이 남긴 것이다. daily 워크플로우가
    `concurrency: soopts-daily`로 동시 실행을 막으므로 "지금 다른 실행이 처리 중인 행"일 수
    없고, 같은 실행 안에서는 선택이 처리보다 먼저 한 번만 일어나기 때문이다.

    pending을 재시도할 땐 retry_count를 여기서 직접 올린다. failed는 `mark_vod`가 이미
    올려주지만 pending은 그 경로를 못 거쳤으므로, 올리지 않으면 매번 러너를 죽이는 VOD가
    상한에 영영 닿지 못해 큐를 무한히 막는다.
    """
    picked: list[dict[str, Any]] = []
    for c in candidates:
        if len(picked) >= n:
            break
        title_no = str(c["title_no"])
        row = existing_by_no.get(title_no) or {}
        status = row.get("status")
        retry_count = row.get("retry_count") or 0
        if row:
            if status not in ("failed", "pending") or retry_count >= MAX_RETRIES:
                continue
            # failed는 mark_vod가 이미 올렸다. pending은 그 경로를 못 거쳤으니 여기서 올린다.
            if status == "pending":
                retry_count += 1
        picked.append(_vod_row(title_no, c, row, retry_count))
    return picked


def _vod_row(
    title_no: str, candidate: dict[str, Any], existing: dict[str, Any], retry_count: int
) -> dict[str, Any]:
    """upsert에 넣을 vods 행. 신규·재시도가 **똑같은 키 집합**을 갖는 게 핵심이다.

    PostgREST는 배치의 키 합집합으로 컬럼 목록을 만들고 빠진 값을 NULL로 채운다. 그래서
    키가 다른 행이 한 배치에 섞이면, 어떤 행에만 있는 컬럼이 나머지 행에서 명시적 NULL이
    되어 NOT NULL 제약을 깬다 — 실제로 재시도 행과 신규 행이 함께 올라가면서
    `null value in column "retry_count" violates not-null constraint`로 배치 전체가
    거부됐다. 행마다 dict를 다르게 만들지 말 것.

    id도 넣지 않는다 — GENERATED ALWAYS AS IDENTITY라 PostgREST가 거부한다.
    created_at/processed_at/error는 DB와 mark_vod가 관리하므로 여기서 되쓰지 않는다.
    """
    return {
        "soop_title_no": title_no,
        "title": candidate.get("title") or existing.get("title") or "",
        "broadcast_date": candidate.get("broadcast_date") or existing.get("broadcast_date"),
        "duration_s": candidate.get("duration_s") or existing.get("duration_s"),
        "retry_count": retry_count,
    }


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

    clip_status는 쓰지 않는다. 업로드 큐가 사라진 뒤로 이 컬럼은 전 행이 'clipped'인
    상수가 되어 아무 정보도 담지 않는다(예전엔 'none'으로 넣고 곧바로 'clipped'로
    UPDATE 했다). 컬럼 자체는 관리자 UI가 참조할 수 있어 DB에 남아 있지만, 여기서
    쓰지 않으므로 나중에 DROP 해도 이 코드는 영향을 받지 않는다.
    상태는 identify_status가 담는다 — 검수 흐름에서 실제로 쓰이는 건 그쪽이다.
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
        }
        for s, r in zip(songs, results, strict=True)
    ]
    return _client().table("performances").insert(rows).execute().data


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
