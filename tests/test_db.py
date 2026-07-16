from soopts.db import MAX_RETRIES, select_pending


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


def test_select_pending_stops_retrying_at_limit():
    candidates = [{"title_no": "1", "title": "A"}]
    existing = {"1": {"status": "failed", "retry_count": MAX_RETRIES}}
    assert select_pending(candidates, existing, n=1) == []


def test_select_pending_respects_n_cap():
    candidates = [{"title_no": str(i)} for i in range(5)]
    picked = select_pending(candidates, existing_by_no={}, n=2)
    assert len(picked) == 2
