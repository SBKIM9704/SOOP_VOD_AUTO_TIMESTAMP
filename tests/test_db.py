from soopts import db
from soopts.db import MAX_RETRIES, select_pending
from soopts.models import Song


def test_select_pending_picks_new_candidates():
    candidates = [
        {"title_no": "1", "title": "A", "broadcast_date": "2026-07-16", "duration_s": 100},
        {"title_no": "2", "title": "B", "broadcast_date": "2026-07-15", "duration_s": 200},
    ]
    picked = select_pending(candidates, existing_by_no={}, n=2)
    assert [p["soop_title_no"] for p in picked] == ["1", "2"]


def test_select_pending_skips_completed_vods():
    candidates = [{"title_no": "1", "title": "A"}, {"title_no": "2", "title": "B"}]
    existing = {
        "1": {"status": "done", "retry_count": 0},
        "2": {"status": "analyzed", "retry_count": 0},
    }
    assert select_pending(candidates, existing, n=2) == []


# --------------------------------------------------------------------------- #
# stale pending — 죽은 실행이 남긴 행을 다시 잡는다
# --------------------------------------------------------------------------- #
def test_select_pending_retries_stale_pending():
    """타임아웃/취소로 처리 도중 죽으면 pending으로 남는데, 예전엔 영영 안 잡혔다."""
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"status": "pending", "retry_count": 0}}
    picked = select_pending(candidates, existing, n=1)
    assert len(picked) == 1
    assert picked[0]["soop_title_no"] == "1"


def test_select_pending_bumps_retry_count_for_stale_pending():
    """mark_vod를 못 거쳤으므로 여기서 올리지 않으면 상한에 영영 안 닿는다."""
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"status": "pending", "retry_count": 1}}
    picked = select_pending(candidates, existing, n=1)
    assert picked[0]["retry_count"] == 2


def test_select_pending_stops_retrying_stale_pending_at_limit():
    """매번 러너를 죽이는 VOD가 큐를 무한히 막지 않아야 한다."""
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"status": "pending", "retry_count": MAX_RETRIES}}
    assert select_pending(candidates, existing, n=1) == []


def test_select_pending_does_not_bump_retry_count_for_failed():
    """failed는 mark_vod가 이미 올렸으므로 여기서 또 올리면 이중 계산된다."""
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"status": "failed", "retry_count": 1}}
    picked = select_pending(candidates, existing, n=1)
    assert picked[0]["retry_count"] == 1


def test_select_pending_treats_missing_retry_count_as_zero():
    candidates = [{"title_no": "1", "title": "A"}]
    picked = select_pending(candidates, {"1": {"status": "pending"}}, n=1)
    assert picked[0]["retry_count"] == 1


def test_select_pending_retries_failed_under_limit():
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"status": "failed", "retry_count": MAX_RETRIES - 1}}
    picked = select_pending(candidates, existing, n=1)
    assert len(picked) == 1
    assert picked[0]["soop_title_no"] == "1"


def test_select_pending_retry_row_excludes_identity_id_column():
    # 실제 장애 재현: existing_by_no의 row는 SELECT *라 id(GENERATED ALWAYS)를 포함한다.
    # 그대로 upsert 페이로드에 넣으면 PostgREST가 'cannot insert a non-DEFAULT value
    # into column "id"'로 거부한다 — 재시도 페이로드에서 id는 반드시 빠져야 한다.
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"id": 42, "status": "failed", "retry_count": 0, "title": "옛 제목"}}
    picked = select_pending(candidates, existing, n=1)
    assert len(picked) == 1
    assert "id" not in picked[0]
    assert picked[0]["soop_title_no"] == "1"
    # 제목은 방금 SOOP에서 받아온 후보 값을 쓴다(재시도 시 메타데이터 갱신)
    assert picked[0]["title"] == "A"


def test_select_pending_stops_retrying_at_limit():
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"status": "failed", "retry_count": MAX_RETRIES}}
    assert select_pending(candidates, existing, n=1) == []


def test_select_pending_respects_n_cap():
    candidates = [{"title_no": str(i)} for i in range(5)]
    picked = select_pending(candidates, existing_by_no={}, n=2)
    assert len(picked) == 2


# --------------------------------------------------------------------------- #
# insert_performances — 전송 컬럼 (스키마 정리와 맞물림)
# --------------------------------------------------------------------------- #
class _FakeTable:
    def __init__(self, sink):
        self.sink = sink

    def insert(self, rows):
        self.sink["rows"] = rows
        return self

    def execute(self):
        return type("Resp", (), {"data": [{"id": "p-1"}]})()


def _capture_insert(monkeypatch, song, result=None):
    sink: dict = {}
    monkeypatch.setattr(db, "_client",
                        lambda: type("C", (), {"table": lambda self, n: _FakeTable(sink)})())
    db.insert_performances("vod-1", [song], [result])
    return sink["rows"][0]


def _song():
    return Song(t=3797, end=4021, duration=224, sticker_rate=1.8,
                song_likely=True, lyrics="어제는 하늘이 슬퍼 보여서")


def test_insert_performances_does_not_send_dropped_columns(monkeypatch):
    """clip_status는 상수가 되어 쓰지 않는다 — 보내면 DROP COLUMN 후 insert가 깨진다."""
    row = _capture_insert(monkeypatch, _song())
    assert "clip_status" not in row
    assert "youtube_video_id" not in row
    assert "synced_at" not in row


def test_insert_performances_keeps_all_song_data(monkeypatch):
    """유튜브 정리로 노래 데이터가 유실되지 않아야 한다."""
    row = _capture_insert(monkeypatch, _song())
    assert row["start_s"] == 3797
    assert row["end_s"] == 4021
    assert row["lyrics_snippet"] == "어제는 하늘이 슬퍼 보여서"
    assert row["sticker_rate"] == 1.8
    assert row["song_likely"] is True


def test_insert_performances_defaults_to_needs_review_without_match(monkeypatch):
    row = _capture_insert(monkeypatch, _song(), result=None)
    assert row["identify_status"] == "needs_review"
    assert row["song_id"] is None


# --------------------------------------------------------------------------- #
# upsert 배치 — 신규·재시도 행의 키 집합이 같아야 한다
# --------------------------------------------------------------------------- #
def test_select_pending_rows_share_identical_key_set():
    """PostgREST는 키 합집합으로 컬럼을 만들고 빠진 값을 NULL로 채운다 — 키가 다르면
    한쪽에만 있는 컬럼이 다른 행에서 NULL이 되어 NOT NULL 제약을 깬다.
    실제로 재시도 행과 신규 행이 섞이며 retry_count NULL로 배치 전체가 거부됐다(23502)."""
    candidates = [
        {"title_no": "1", "title": "재시도", "broadcast_date": "2026-07-16", "duration_s": 100},
        {"title_no": "2", "title": "신규", "broadcast_date": "2026-07-17", "duration_s": 200},
    ]
    existing = {"1": {"id": 7, "status": "pending", "retry_count": 0,
                      "created_at": "2026-07-16T00:00:00Z", "error": None,
                      "processed_at": None, "title": "옛 제목"}}
    picked = select_pending(candidates, existing, n=2)
    assert len(picked) == 2
    assert {frozenset(r) for r in picked} == {frozenset(picked[0])}, "행마다 키 집합이 다름"


def test_select_pending_never_sends_db_managed_columns():
    """created_at/processed_at/error는 DB와 mark_vod가 관리한다 — 되쓰면 NULL로 덮인다."""
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"id": 7, "status": "failed", "retry_count": 1,
                      "created_at": "2026-07-16T00:00:00Z", "error": "옛 오류",
                      "processed_at": "2026-07-16T01:00:00Z"}}
    row = select_pending(candidates, existing, n=1)[0]
    for col in ("id", "created_at", "processed_at", "error", "status"):
        assert col not in row, f"{col}이 페이로드에 포함됨"


def test_select_pending_retry_row_always_has_retry_count():
    """신규 행도 retry_count를 가져야 배치가 균일해진다."""
    picked = select_pending([{"title_no": "9", "title": "신규"}], {}, n=1)
    assert picked[0]["retry_count"] == 0


def test_select_pending_retry_prefers_fresh_candidate_metadata():
    candidates = [{"title_no": "1", "title": "새 제목", "duration_s": 999}]
    existing = {"1": {"status": "pending", "retry_count": 0,
                      "title": "옛 제목", "duration_s": 100}}
    row = select_pending(candidates, existing, n=1)[0]
    assert row["title"] == "새 제목"
    assert row["duration_s"] == 999
