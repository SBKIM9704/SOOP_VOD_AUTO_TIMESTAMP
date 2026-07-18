from soopts import db
from soopts.db import select_targets
from soopts.models import Song


# --------------------------------------------------------------------------- #
# select_targets — 우선순위 재시도 > 신규 > 백필
# --------------------------------------------------------------------------- #
def test_select_targets_picks_new_candidates_newest_first():
    candidates = [
        {"title_no": "2", "title": "B", "broadcast_date": "2026-07-16", "duration_s": 200},
        {"title_no": "1", "title": "A", "broadcast_date": "2026-07-15", "duration_s": 100},
    ]
    picked = select_targets([], candidates, existing_by_no={}, n=2)
    assert [p["soop_title_no"] for p in picked] == ["2", "1"]


def test_select_targets_skips_completed_vods():
    candidates = [{"title_no": "1", "title": "A"}, {"title_no": "2", "title": "B"}]
    existing = {
        "1": {"status": "done", "retry_count": 0},
        "2": {"status": "analyzed", "retry_count": 0},
    }
    assert select_targets([], candidates, existing, n=2) == []


def test_select_targets_backfills_past_when_newest_all_done():
    """신규(2)가 완료됐으면 과거(1)로 내려가 백필한다 — 최신순 순회가 신규>백필을 만든다."""
    candidates = [{"title_no": "2", "title": "신규"}, {"title_no": "1", "title": "과거"}]
    existing = {"2": {"status": "done", "retry_count": 0}}
    picked = select_targets([], candidates, existing, n=1)
    assert [p["soop_title_no"] for p in picked] == ["1"]


def test_select_targets_retry_takes_priority_over_new():
    """재시도 > 신규: 슬롯이 하나면 재시도가 먼저 차지한다."""
    retryable = [{"soop_title_no": "9", "status": "failed", "retry_count": 0, "title": "재시도"}]
    candidates = [{"title_no": "10", "title": "신규"}]
    picked = select_targets(retryable, candidates, existing_by_no={}, n=1)
    assert [p["soop_title_no"] for p in picked] == ["9"]


def test_select_targets_retry_then_fills_with_new():
    retryable = [{"soop_title_no": "9", "status": "failed", "retry_count": 1, "title": "재시도"}]
    candidates = [{"title_no": "10", "title": "신규"}]
    picked = select_targets(retryable, candidates, existing_by_no={}, n=2)
    assert [p["soop_title_no"] for p in picked] == ["9", "10"]


# --------------------------------------------------------------------------- #
# 재시도 — retry_count 처리
# --------------------------------------------------------------------------- #
def test_select_targets_bumps_retry_count_for_stale_pending():
    """pending은 mark_vod 경로를 못 거쳤으므로 여기서 올리지 않으면 상한에 영영 안 닿는다."""
    retryable = [{"soop_title_no": "1", "status": "pending", "retry_count": 1}]
    picked = select_targets(retryable, [], existing_by_no={}, n=1)
    assert picked[0]["retry_count"] == 2


def test_select_targets_does_not_bump_retry_count_for_failed():
    """failed는 mark_vod가 이미 올렸으므로 여기서 또 올리면 이중 계산된다."""
    retryable = [{"soop_title_no": "1", "status": "failed", "retry_count": 1}]
    picked = select_targets(retryable, [], existing_by_no={}, n=1)
    assert picked[0]["retry_count"] == 1


def test_select_targets_treats_missing_retry_count_as_zero():
    retryable = [{"soop_title_no": "1", "status": "pending"}]
    picked = select_targets(retryable, [], existing_by_no={}, n=1)
    assert picked[0]["retry_count"] == 1


def test_select_targets_retry_excludes_identity_id_column():
    # DB 행은 SELECT *라 id(GENERATED ALWAYS)를 포함한다. upsert 페이로드에 그대로 넣으면
    # PostgREST가 거부하므로 재시도 페이로드에서 id는 반드시 빠져야 한다.
    retryable = [{"id": 42, "soop_title_no": "1", "status": "failed",
                  "retry_count": 0, "title": "옛 제목"}]
    picked = select_targets(retryable, [], existing_by_no={}, n=1)
    assert "id" not in picked[0]
    assert picked[0]["title"] == "옛 제목"   # 목록에 없으면 DB 메타데이터 유지


def test_select_targets_retry_refreshes_metadata_when_in_candidates():
    """재시도 VOD가 목록에도 있으면 방금 받아온 제목으로 갱신한다."""
    retryable = [{"soop_title_no": "1", "status": "failed", "retry_count": 0, "title": "옛 제목"}]
    candidates = [{"title_no": "1", "title": "새 제목"}]
    picked = select_targets(retryable, candidates, existing_by_no={"1": retryable[0]}, n=1)
    assert len(picked) == 1   # 재시도로 한 번만
    assert picked[0]["title"] == "새 제목"


def test_select_targets_respects_n_cap():
    candidates = [{"title_no": str(i)} for i in range(5)]
    assert len(select_targets([], candidates, existing_by_no={}, n=2)) == 2


# --------------------------------------------------------------------------- #
# insert_performances — 전송 컬럼 (스키마 정리와 맞물림)
# --------------------------------------------------------------------------- #
class _FakeTable:
    def __init__(self, sink):
        self.sink = sink

    def upsert(self, rows, on_conflict=None, ignore_duplicates=None):
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
def test_select_targets_rows_share_identical_key_set():
    """PostgREST는 키 합집합으로 컬럼을 만들고 빠진 값을 NULL로 채운다 — 키가 다르면
    한쪽에만 있는 컬럼이 다른 행에서 NULL이 되어 NOT NULL 제약을 깬다.
    실제로 재시도 행과 신규 행이 섞이며 retry_count NULL로 배치 전체가 거부됐다(23502)."""
    retryable = [{"id": 7, "soop_title_no": "1", "status": "pending", "retry_count": 0,
                  "created_at": "2026-07-16T00:00:00Z", "error": None,
                  "processed_at": None, "title": "재시도"}]
    candidates = [{"title_no": "2", "title": "신규", "broadcast_date": "2026-07-17", "duration_s": 200}]
    picked = select_targets(retryable, candidates, existing_by_no={}, n=2)
    assert len(picked) == 2
    assert {frozenset(r) for r in picked} == {frozenset(picked[0])}, "행마다 키 집합이 다름"


def test_select_targets_never_sends_db_managed_columns():
    """created_at/processed_at/error는 DB와 mark_vod가 관리한다 — 되쓰면 NULL로 덮인다."""
    retryable = [{"id": 7, "soop_title_no": "1", "status": "failed", "retry_count": 1,
                  "created_at": "2026-07-16T00:00:00Z", "error": "옛 오류",
                  "processed_at": "2026-07-16T01:00:00Z", "title": "A"}]
    row = select_targets(retryable, [], existing_by_no={}, n=1)[0]
    for col in ("id", "created_at", "processed_at", "error", "status"):
        assert col not in row, f"{col}이 페이로드에 포함됨"


def test_select_targets_new_row_always_has_retry_count():
    """신규 행도 retry_count를 가져야 재시도 행과 배치가 균일해진다."""
    picked = select_targets([], [{"title_no": "9", "title": "신규"}], existing_by_no={}, n=1)
    assert picked[0]["retry_count"] == 0


# --------------------------------------------------------------------------- #
# 재처리 중복 방지
# --------------------------------------------------------------------------- #
def test_insert_performances_upserts_on_vod_and_start(monkeypatch):
    """재처리가 같은 구간을 다시 감지해도 새 행이 쌓이면 안 된다 —
    실제로 201142227에 같은 start_s/end_s가 두 벌씩 생겼다."""
    seen: dict = {}

    class _T:
        def upsert(self, rows, on_conflict=None, ignore_duplicates=None):
            seen.update(rows=rows, on_conflict=on_conflict, ignore=ignore_duplicates)
            return self

        def insert(self, rows):
            raise AssertionError("insert를 쓰면 재처리마다 중복이 쌓인다")

        def execute(self):
            return type("R", (), {"data": seen["rows"]})()

    monkeypatch.setattr(db, "_client", lambda: type("C", (), {"table": lambda s, n: _T()})())
    db.insert_performances("v1", [_song()], [None])
    assert seen["on_conflict"] == "vod_id,start_s"
    assert seen["ignore"] is True   # confirmed 행을 덮어쓰지 않는다


def test_clear_machine_performances_spares_human_confirmations(monkeypatch):
    """사람이 확정한 건 기계가 다시 만들 수 없다 — 재처리해도 남겨야 한다."""
    calls: dict = {}

    class _T:
        def delete(self):
            calls["delete"] = True
            return self

        def eq(self, col, val):
            calls.setdefault("eq", []).append((col, val))
            return self

        def neq(self, col, val):
            calls["neq"] = (col, val)
            return self

        def execute(self):
            return type("R", (), {"data": [{"id": 1}, {"id": 2}]})()

    monkeypatch.setattr(db, "_client", lambda: type("C", (), {"table": lambda s, n: _T()})())
    assert db.clear_machine_performances(7) == 2
    assert calls["eq"] == [("vod_id", 7)]
    assert calls["neq"] == ("identify_status", "confirmed")
