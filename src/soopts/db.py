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
def select_targets(
    retryable: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    existing_by_no: dict[str, dict[str, Any]],
    n: int,
) -> list[dict[str, Any]]:
    """순수 함수: 우선순위 **재시도 > 신규 > 백필** 로 최대 n개를 고른다.

    - **retryable**: 재시도 대상 DB 행(failed/pending, retry_count < 상한). 호출부가 SOOP
      목록과 무관하게 DB에서 직접 뽑아 넘긴다 — pending이 최신 목록 창 밖으로 밀려나도
      재시도되도록. failed는 처리 중 예외로 `mark_vod`가 기록한 상태, pending은 처리를
      시작만 하고 SIGKILL(타임아웃·취소·러너 리셋)로 끝내지 못한 상태다.
    - **candidates**: SOOP 목록 최신순. 신규(창 상단)와 백필(그보다 과거)이 한 리스트에
      섞여 있으며, 최신순 순회가 '신규 > 백필' 우선순위를 자동으로 만든다. 처리 완료분
      (existing_by_no에 있고 재시도 대상이 아닌 것)은 건너뛰므로, 신규가 없으면 자연히
      과거로 내려가며 백필한다.

    선택 시점에 보이는 pending은 반드시 죽은 실행이 남긴 것이다 — `concurrency: soopts-daily`
    가 동시 실행을 막고, 한 실행 안에서 선택은 처리보다 먼저 한 번만 일어난다.

    pending 재시도 시 retry_count를 여기서 올린다. failed는 `mark_vod`가 이미 올려주지만
    pending은 그 경로를 못 거쳤으므로, 올리지 않으면 매번 러너를 죽이는 VOD가 상한에 영영
    닿지 못해 큐를 무한히 막는다.
    """
    cand_by_no = {str(c["title_no"]): c for c in candidates}
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. 재시도 — SOOP 창과 무관하게 DB에서 온다. 목록에 아직 있으면 메타데이터를 갱신한다.
    for row in retryable:
        if len(picked) >= n:
            break
        title_no = str(row["soop_title_no"])
        retry_count = row.get("retry_count") or 0
        if row.get("status") == "pending":
            retry_count += 1
        picked.append(_vod_row(title_no, cand_by_no.get(title_no, {}), row, retry_count))
        seen.add(title_no)

    # 2+3. 신규·백필 — 최신순 순회. DB에 이미 있는 건(완료분·위에서 처리한 재시도)은 건너뛴다.
    for c in candidates:
        if len(picked) >= n:
            break
        title_no = str(c["title_no"])
        if title_no in seen or title_no in existing_by_no:
            continue
        picked.append(_vod_row(title_no, c, {}, 0))
        seen.add(title_no)
    return picked


def fetch_retryable(n: int) -> list[dict[str, Any]]:
    """재시도 가능한 vods 행(failed/pending, retry_count < 상한)을 최신 방송순 최대 n개."""
    if n <= 0:
        return []
    return (
        _client()
        .table("vods")
        .select("*")
        .in_("status", ["failed", "pending"])
        .lt("retry_count", MAX_RETRIES)
        .order("broadcast_date", desc=True)
        .limit(n)
        .execute()
        .data
    )


def fetch_existing(title_nos: list[str]) -> dict[str, dict[str, Any]]:
    """주어진 title_no들의 기존 vods 행을 soop_title_no로 키잉해 반환."""
    if not title_nos:
        return {}
    rows = _client().table("vods").select("*").in_("soop_title_no", title_nos).execute().data
    return {row["soop_title_no"]: row for row in rows}


def upsert_pending(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """선정된 대상 행을 status='pending'으로 upsert하고 그 행을 반환한다."""
    if not targets:
        return []
    rows = [{**row, "status": "pending"} for row in targets]
    return _client().table("vods").upsert(rows, on_conflict="soop_title_no").execute().data


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


def fetch_vods_by_status(statuses: list[str]) -> list[dict[str, Any]]:
    """주어진 status의 vods 행을 최신 방송순으로 — vod-audit 스킬의 감사 대상 목록용.

    `soopts vods --status analyzed,done`가 이걸 그대로 노출해, 스킬 안에서 Claude가 어떤
    VOD를 검증할지 고른다(판정은 코드가 아니라 스킬 안의 Claude가 원본 댓글로 한다)."""
    if not statuses:
        return []
    return (
        _client()
        .table("vods")
        .select("*")
        .in_("status", statuses)
        .order("broadcast_date", desc=True)
        .execute()
        .data
    )


def count_machine_performances(vod_row_id: int) -> int:
    """VOD의 기계 생성 performances 수(confirmed 제외) — recheck dry-run 리포트용."""
    rows = (
        _client()
        .table("performances")
        .select("id")
        .eq("vod_id", vod_row_id)
        .neq("identify_status", "confirmed")
        .execute()
        .data
    )
    return len(rows or [])


def count_confirmed_performances(vod_row_id: int) -> int:
    """VOD의 사람 확정(confirmed) performances 수 — recheck에서 보존됨을 확인용."""
    rows = (
        _client()
        .table("performances")
        .select("id")
        .eq("vod_id", vod_row_id)
        .eq("identify_status", "confirmed")
        .execute()
        .data
    )
    return len(rows or [])


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
def clear_machine_performances(vod_row_id: int) -> int:
    """VOD의 기계 생성 performances를 지우고 지운 건수를 반환한다.

    재처리는 이제 일상이다 — 중단된 실행이 남긴 pending VOD를 다음 실행이 다시 잡는다.
    그런데 insert만 하면 같은 구간이 매번 새 행으로 쌓인다. 실제로 201142227에서
    같은 start_s/end_s가 두 벌씩 생겼다(타임아웃 실행 4건 + 재처리 4건).

    identify_status='confirmed'는 남긴다 — 사람이 검수 UI에서 확정한 결과이고, 기계가
    다시 만들어낼 수 없는 유일한 정보다. 나머지(needs_review/auto_matched/rejected)는
    이번 실행이 다시 만들어낸다.
    """
    rows = (
        _client()
        .table("performances")
        .delete()
        .eq("vod_id", vod_row_id)
        .neq("identify_status", "confirmed")
        .execute()
        .data
    )
    return len(rows or [])


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
    # (vod_id, start_s) 유니크 위반은 무시한다 — confirmed로 남겨둔 행과 같은 구간을
    # 다시 감지한 경우다. 사람이 확정한 쪽을 덮어쓰지 않는다.
    return (
        _client()
        .table("performances")
        .upsert(rows, on_conflict="vod_id,start_s", ignore_duplicates=True)
        .execute()
        .data
    )


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
