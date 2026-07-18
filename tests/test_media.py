import pytest

import soopts.collector.media as media
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


# --------------------------------------------------------------------------- #
# _write_segments — 병렬로 받되 순서는 반드시 보존
# --------------------------------------------------------------------------- #
class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


def test_write_segments_preserves_order_despite_completion_order(monkeypatch):
    """세그먼트 순서가 어긋나면 fMP4가 깨진다 — 완료 순서가 아니라 제출 순서로 써야 한다."""
    import time as _t

    def fake_fetch(url):
        n = int(url.rsplit("-", 1)[1])
        _t.sleep((5 - n) * 0.02)   # 뒤 세그먼트일수록 빨리 끝나게 해 완료 순서를 뒤집는다
        return f"[{n}]".encode()

    monkeypatch.setattr(media, "_fetch", fake_fetch)
    sink = _Sink()
    media._write_segments(sink, [f"http://x/seg-{i}" for i in range(5)], workers=5)
    assert sink.data == b"[0][1][2][3][4]"


def test_write_segments_serial_path_matches_parallel(monkeypatch):
    monkeypatch.setattr(media, "_fetch", lambda u: u.rsplit("-", 1)[1].encode())
    urls = [f"http://x/seg-{i}" for i in range(6)]
    serial, parallel = _Sink(), _Sink()
    media._write_segments(serial, urls, workers=1)
    media._write_segments(parallel, urls, workers=3)
    assert serial.data == parallel.data == b"012345"


def test_write_segments_propagates_fetch_failure(monkeypatch):
    def boom(url):
        if url.endswith("-2"):
            raise RuntimeError("세그먼트 요청 반복 실패")
        return b"ok"

    monkeypatch.setattr(media, "_fetch", boom)
    with pytest.raises(RuntimeError, match="반복 실패"):
        media._write_segments(_Sink(), [f"http://x/seg-{i}" for i in range(5)], workers=2)


def test_write_segments_handles_fewer_urls_than_workers(monkeypatch):
    monkeypatch.setattr(media, "_fetch", lambda u: b"x")
    sink = _Sink()
    media._write_segments(sink, ["http://x/seg-0"], workers=8)
    assert sink.data == b"x"
