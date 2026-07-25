import pytest
import requests

import soopts.collector.http as http_module
from soopts.collector.http import get_with_retry
from soopts.config import Config


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def _no_sleep(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda _s: None)


def test_retries_5xx_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    responses = [_FakeResponse(502), _FakeResponse(503), _FakeResponse(200)]
    calls = []

    def fake_request(method, url, params=None, data=None, headers=None, timeout=None):
        calls.append(url)
        return responses[len(calls) - 1]

    monkeypatch.setattr(http_module.requests, "request", fake_request)
    resp = get_with_retry(Config(), "http://x")
    assert resp.status_code == 200
    assert len(calls) == 3


def test_raises_after_exhausting_retries(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def fake_request(method, url, params=None, data=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(502)

    monkeypatch.setattr(http_module.requests, "request", fake_request)
    with pytest.raises(requests.HTTPError):
        get_with_retry(Config(), "http://x")
    # max_retries(기본 3)만큼만 시도하고 포기한다.
    assert len(calls) == Config().collector.max_retries


def test_does_not_retry_4xx(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def fake_request(method, url, params=None, data=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(404)

    monkeypatch.setattr(http_module.requests, "request", fake_request)
    resp = get_with_retry(Config(), "http://x")
    # 4xx는 재시도 없이 그대로 반환 — 상태 처리는 호출부 몫.
    assert resp.status_code == 404
    assert len(calls) == 1


def test_retries_connection_error(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def fake_request(method, url, params=None, data=None, headers=None, timeout=None):
        calls.append(url)
        if len(calls) < 2:
            raise requests.ConnectionError("boom")
        return _FakeResponse(200)

    monkeypatch.setattr(http_module.requests, "request", fake_request)
    resp = get_with_retry(Config(), "http://x")
    assert resp.status_code == 200
    assert len(calls) == 2
