import json

import soopts.collector.comments as comments_module
from soopts.collector.comments import extract_comments, fetch_comments
from soopts.config import Config


def test_extract_comments_from_real_schema(fixtures_dir):
    data = json.loads((fixtures_dir / "vod_comments_page1.json").read_bytes())
    comments = extract_comments(data)
    assert len(comments) == 2
    assert "초록주먹뱅" in comments[0]
    assert "🎤" in comments[1]


def test_extract_comments_skips_empty():
    data = {"data": [{"comment": "hi"}, {"comment": ""}, {}]}
    assert extract_comments(data) == ["hi"]


def test_extract_comments_no_data_key():
    assert extract_comments({}) == []


class _FakeResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _page(comments: list[str], *, page: int, last_page: int) -> dict:
    return {
        "data": [{"comment": c} for c in comments],
        "meta": {"current_page": page, "last_page": last_page},
    }


def test_fetch_comments_walks_all_pages(monkeypatch):
    pages = {
        1: _page(["최신 댓글"], page=1, last_page=2),
        2: _page(["오래된 타임라인 댓글"], page=2, last_page=2),
    }
    requested_pages = []

    def fake_get(url, params, headers, timeout):
        requested_pages.append(params["page"])
        return _FakeResponse(pages[params["page"]])

    monkeypatch.setattr(comments_module.requests, "get", fake_get)
    result = fetch_comments(Config(), "bj", "12345")
    assert result == ["최신 댓글", "오래된 타임라인 댓글"]
    assert requested_pages == [1, 2]


def test_fetch_comments_stops_at_max_pages_safety_cap(monkeypatch):
    requested_pages = []

    def fake_get(url, params, headers, timeout):
        requested_pages.append(params["page"])
        # last_page가 비정상적으로 크게 와도(폭주 방지) _MAX_PAGES에서 멈춰야 한다.
        return _FakeResponse(_page([f"p{params['page']}"], page=params["page"], last_page=9999))

    monkeypatch.setattr(comments_module.requests, "get", fake_get)
    fetch_comments(Config(), "bj", "12345")
    assert len(requested_pages) == comments_module._MAX_PAGES
