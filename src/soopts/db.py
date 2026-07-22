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
    cutoff_date: str | None = None,
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
    - **cutoff_date**: 'YYYY-MM-DD'. broadcast_date가 이보다 나중인 후보는 아직 팬 타임라인이
      안 달렸을 공산이 커서 제외한다(쿨다운, `station.min_vod_age_days`). 날짜 문자열은
      고정 폭이라 사전식 비교가 곧 날짜 비교다. broadcast_date가 없으면 판단 근거가 없으니
      거르지 않는다 — 근거 없이 무기한 보류하느니 처리하는 편이 낫다. **재시도는 면제**다:
      이미 vods 행이 있는 = 착수한 작업이고, 여기서 막으면 큐가 영영 안 비워진다.

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
        bdate = c.get("broadcast_date")
        if cutoff_date and bdate and bdate > cutoff_date:
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


def fetch_performances(
    identify_status: str | None = None, local_review: str | None = None
) -> list[dict[str, Any]]:
    """performances 행을 필터로 조회하고 각 행에 소속 VOD의 soop_title_no를 붙여 반환한다.

    perf-review 스킬이 곡별로 구간을 다시 받아 로컬 검증할 때 쓴다 — 세그먼트 다운로드에
    title_no가 필요하므로 vods와 조인해 얹는다. 필터는 둘 다 선택(없으면 전체).
    """
    client = _client()
    q = client.table("performances").select("*")
    if identify_status:
        q = q.eq("identify_status", identify_status)
    if local_review:
        q = q.eq("local_review", local_review)
    perfs = q.order("vod_id").order("start_s").execute().data
    vid = {p["vod_id"] for p in perfs}
    if not vid:
        return []
    vods = client.table("vods").select("id,soop_title_no,title").in_("id", list(vid)).execute().data
    by_id = {v["id"]: v for v in vods}
    for p in perfs:
        v = by_id.get(p["vod_id"], {})
        p["soop_title_no"] = v.get("soop_title_no")
        p["vod_title"] = v.get("title")
    return perfs


def update_performance(perf_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    """performance 한 행을 갱신한다(perf-review 스킬의 보강/검증 적용).

    None 값 필드는 보내지 않는다 — 명시적 NULL 덮어쓰기를 막기 위함. 허용 필드만 통과시킨다.
    """
    allowed = {
        "start_s", "end_s", "title_guess", "lyrics_snippet", "song_id",
        "match_confidence", "identify_status", "local_review", "song_likely",
        "youtube_url",
    }
    payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not payload:
        return None
    rows = (
        _client().table("performances").update(payload).eq("id", perf_id).execute().data
    )
    return rows[0] if rows else None


def insert_draft_song(
    title: str, artist: str | None = None, lyrics: str | None = None, status: str = "draft"
) -> str:
    """songs에 신곡을 삽입하고 song_id(uuid)를 반환한다.

    무-카탈로그 곡을 로컬 검증(perf-review)에서 draft로 넣기 위한 유일한 song 생성 경로다.
    기존 원칙("songs는 이 repo에서 만들지 않는다")의 의도적 예외 — status='draft'로만 넣어
    검수 UI가 published로 승격하기 전까지 정식 카탈로그와 구분되게 한다.
    """
    row = {"title": title, "artist": artist, "lyrics": lyrics, "status": status}
    res = _client().table("songs").insert(row).execute().data
    return res[0]["id"]


# --------------------------------------------------------------------------- #
# 유튜브 노래모음 업로드 (soopts youtube-upload)
#
# vods.youtube_status가 큐 마커다: NULL=미업로드(대상) / uploaded=올라감(종결)
# / no_songs=빌드 결과가 0곡이라 종결. youtube_url의 NULL 여부로 고르면 "처리했지만 올릴 게
# 없었다"를 표현할 수 없어 그 VOD가 매일 다시 뽑히고 큐가 막힌다.
# (2026-07-23 이전 행에는 'uploaded_private'가 남아 있다 — private 업로드 + 사람이 공개
#  전환하던 시절의 값이다. 지금은 처음부터 unlisted로 올리므로 그 단계가 없다.)
# --------------------------------------------------------------------------- #
COMPLETE_IDENTIFY_STATUSES = frozenset({"auto_matched", "confirmed"})
COMPLETE_LOCAL_REVIEW = "verified"
UPLOADABLE_VOD_STATUSES = frozenset({"analyzed", "done"})


def youtube_block_reason(vod: dict[str, Any], perfs: list[dict[str, Any]]) -> str | None:
    """VOD가 업로드 대상이 **못 되는** 이유. None이면 대상이다(순수 함수).

    이유를 문자열로 돌려주는 건 `--title-no`로 특정 VOD를 지정했을 때 왜 걸렀는지 사람에게
    말해주기 위해서다. 자동 선택은 이유를 버리고 boolean처럼 쓴다.

    기준은 "사람 손을 거쳐 완결된 VOD만 올린다"이다 — identify(어느 카탈로그 곡인지)와
    local_review(구간·가사가 실제로 맞는지) **양쪽 축이 모든 곡에서** 끝나야 한다. 한 곡이라도
    미완이면 제목이 틀린 챕터나 노래가 아닌 구간이 그대로 유튜브에 박히고, 올린 뒤에는
    되돌릴 방법이 사실상 없다(삭제 API를 쓰지 않기로 했다).
    """
    if vod.get("status") not in UPLOADABLE_VOD_STATUSES:
        return f"vods.status={vod.get('status')} (analyzed/done 아님)"
    if vod.get("youtube_status"):
        return f"이미 처리됨(youtube_status={vod['youtube_status']})"
    if not perfs:
        return "performance 없음"
    for p in perfs:
        if p.get("identify_status") not in COMPLETE_IDENTIFY_STATUSES:
            return f"곡 식별 미완: perf #{p.get('id')} identify_status={p.get('identify_status')}"
        if p.get("local_review") != COMPLETE_LOCAL_REVIEW:
            return f"로컬 검증 미완: perf #{p.get('id')} local_review={p.get('local_review')}"
    return None


def select_youtube_target(
    vods: list[dict[str, Any]], perfs_by_vod: dict[int, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """업로드할 VOD 하나를 고른다 — 오래된 방송부터(순수 함수).

    오래된 순인 이유: 채널이 방송 순서대로 쌓이고, 백로그가 결정론적으로 소진된다. 방송일이
    없는 행은 판단 근거가 없으니 맨 뒤로 보낸다(빈 문자열로 두면 최고참인 척 큐를 새치기한다).
    """
    ok = [v for v in vods if youtube_block_reason(v, perfs_by_vod.get(v["id"], [])) is None]
    if not ok:
        return None
    return min(ok, key=lambda v: (v.get("broadcast_date") or "9999-99-99", str(v["soop_title_no"])))


def fetch_youtube_candidates() -> list[dict[str, Any]]:
    """아직 업로드하지 않은 analyzed/done VOD 전부(오래된 방송순).

    곡 완결 여부는 여기서 거르지 않는다 — performances를 봐야 알 수 있고, 그 판정은 순수
    함수(`youtube_block_reason`)로 빼서 테스트할 수 있게 뒀다.
    """
    return (
        _client()
        .table("vods")
        .select("*")
        .in_("status", sorted(UPLOADABLE_VOD_STATUSES))
        .is_("youtube_status", "null")
        .order("broadcast_date")
        .execute()
        .data
    )


def fetch_vod_by_title_no(title_no: str) -> dict[str, Any] | None:
    """soop_title_no로 vods 단건 조회(--title-no 지정 경로)."""
    rows = (
        _client().table("vods").select("*").eq("soop_title_no", str(title_no)).execute().data
    )
    return rows[0] if rows else None


def fetch_performances_for_vods(vod_row_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """여러 VOD의 performances를 vod_id로 묶어 반환한다(각 리스트는 start_s 순).

    곡 제목·아티스트는 songs 조인으로 가져온다 — 영상 오버레이·챕터 제목·설명이 전부 이 값을
    쓴다(`batch._resolved_title_artist`와 같은 폴백 규칙).
    """
    if not vod_row_ids:
        return {}
    rows = (
        _client()
        .table("performances")
        .select(
            "id,vod_id,start_s,end_s,identify_status,local_review,"
            "title_guess,youtube_url,songs(title,artist)"
        )
        .in_("vod_id", vod_row_ids)
        .order("start_s")
        .execute()
        .data
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["vod_id"], []).append(r)
    return out


def mark_youtube_uploaded(title_no: str, url: str) -> None:
    """업로드 직후 — 링크와 'uploaded' 마커를 기록한다. 큐에서는 이걸로 종결이다."""
    _client().table("vods").update(
        {"youtube_url": url, "youtube_status": "uploaded"}
    ).eq("soop_title_no", str(title_no)).execute()


def mark_youtube_no_songs(title_no: str) -> None:
    """빌드 결과가 0곡이라 올릴 게 없을 때의 **종결 마커**.

    이걸 안 찍으면 youtube_status가 NULL로 남아 같은 VOD가 매일 다시 뽑히고 큐가 데드락된다.
    """
    _client().table("vods").update({"youtube_status": "no_songs"}).eq(
        "soop_title_no", str(title_no)
    ).execute()


def set_performance_youtube_urls(pairs: list[tuple[int, str]]) -> int:
    """performance마다 '그 곡부터 재생되는' 유튜브 링크를 기록하고 건수를 반환한다.

    합본 안의 오프셋은 빌드한 러너에서만 알 수 있으므로 **업로드 직후** 기록한다. 업로드가
    곧바로 unlisted라 이 링크는 기록되는 순간부터 살아있다 — 소비 앱이 공개 상태를 따로
    걸러낼 필요가 없다(private으로 올리던 시절엔 그 게이트가 필요했다).
    """
    client = _client()
    for perf_id, url in pairs:
        client.table("performances").update({"youtube_url": url}).eq("id", perf_id).execute()
    return len(pairs)


# --------------------------------------------------------------------------- #
# songs (읽기 전용 카탈로그 — 단, draft 신곡은 insert_draft_song로 예외 삽입)
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
