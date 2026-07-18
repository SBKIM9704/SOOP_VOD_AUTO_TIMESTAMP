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


def test_select_pending_skips_already_pending_or_done():
    candidates = [{"title_no": "1", "title": "A"}, {"title_no": "2", "title": "B"}]
    existing = {
        "1": {"status": "done", "retry_count": 0},
        "2": {"status": "pending", "retry_count": 0},
    }
    assert select_pending(candidates, existing, n=2) == []


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
    assert picked[0]["title"] == "옛 제목"


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
