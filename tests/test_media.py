import pytest

import soopts.collector.media as media_module
from soopts.collector.media import _fetch


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def test_fetch_returns_data_on_first_success(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=30):
        calls.append(url)
        return _FakeResponse(b"x" * 1000)

    monkeypatch.setattr(media_module.urllib.request, "urlopen", fake_urlopen)
    assert _fetch("http://example.com/seg.m4s") == b"x" * 1000
    assert len(calls) == 1


def test_fetch_retries_on_exception_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def fake_urlopen(url, timeout=30):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise TimeoutError("network blip")
        return _FakeResponse(b"y" * 1000)

    monkeypatch.setattr(media_module.urllib.request, "urlopen", fake_urlopen)
    assert _fetch("http://example.com/seg.m4s") == b"y" * 1000
    assert attempts["n"] == 2


def test_fetch_retries_on_short_response_without_exception(monkeypatch):
    # 실측 사례: 서버가 Content-Length 없이 응답하다 연결이 일찍 끊기면 urllib이 예외 없이
    # 짧은 데이터를 그대로 반환한다 — 오디오가 티 안 나게 깨지는 원인. 크기로도 재시도해야 한다.
    attempts = {"n": 0}

    def fake_urlopen(url, timeout=30):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return _FakeResponse(b"short")  # 조용히 끊긴 응답 흉내(예외 없음)
        return _FakeResponse(b"z" * 1000)

    monkeypatch.setattr(media_module.urllib.request, "urlopen", fake_urlopen)
    assert _fetch("http://example.com/seg.m4s") == b"z" * 1000
    assert attempts["n"] == 2


def test_fetch_raises_after_exhausting_retries(monkeypatch):
    def fake_urlopen(url, timeout=30):
        raise ConnectionResetError("dead")

    monkeypatch.setattr(media_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        _fetch("http://example.com/seg.m4s")
