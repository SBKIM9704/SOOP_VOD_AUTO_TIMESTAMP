import pytest

import soopts.collector.media as media
import soopts.collector.media as media_module
from soopts.collector.media import _fetch, split_by_part
from soopts.models import MetaPart


def _parts(*durations: int) -> list[MetaPart]:
    parts, off = [], 0
    for i, d in enumerate(durations):
        parts.append(MetaPart(idx=i, file_info_key=f"k{i}", duration=d, offset_s=off))
        off += d
    return parts


# --------------------------------------------------------------------------- #
# split_by_part — 파트 경계를 넘는 구간
# --------------------------------------------------------------------------- #
def test_split_by_part_without_meta_uses_first_m3u8():
    assert split_by_part(10, 20, [], ["A"]) == [("A", 10, 20)]


def test_split_by_part_within_single_part():
    assert split_by_part(100, 200, _parts(18000, 300), ["A", "B"]) == [("A", 100, 200)]


def test_split_by_part_spanning_boundary_returns_both_parts():
    """경계를 넘는 구간은 잘리지 않고 양쪽 파트로 쪼개진다 — 예전엔 앞 파트로 클램프돼
    뒷부분이 조용히 사라졌다(198797609 방종곡이 3초만 받아진 사건)."""
    assert split_by_part(17950, 18100, _parts(18000, 300), ["A", "B"]) == [
        ("A", 17950, 18000), ("B", 0, 100)
    ]


def test_split_by_part_start_exactly_on_boundary_uses_next_part():
    """s == 파트 시작이면 뒤 파트다. 예전 조건(offset <= s < end)에서도 맞았지만, 앞 파트
    끝(=같은 값)에 먼저 걸려 15초짜리 꼬리만 받아지던 실제 사례가 있었다."""
    assert split_by_part(18000, 18050, _parts(18000, 300), ["A", "B"]) == [("B", 0, 50)]


def test_split_by_part_spanning_three_parts():
    spans = split_by_part(90, 260, _parts(100, 100, 100), ["A", "B", "C"])
    assert spans == [("A", 90, 100), ("B", 0, 100), ("C", 0, 60)]


def test_split_by_part_skips_parts_without_m3u8():
    # m3u8이 파트 수보다 적으면(목록 조회 불일치) 받을 수 없는 파트는 빠진다.
    assert split_by_part(17950, 18100, _parts(18000, 300), ["A"]) == [("A", 17950, 18000)]


def test_split_by_part_outside_all_parts_is_empty():
    assert split_by_part(50000, 50100, _parts(18000, 300), ["A", "B"]) == []


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
    monkeypatch.setattr(media_module.time, "sleep", lambda *_: None)

    def fake_urlopen(url, timeout=30):
        raise ConnectionResetError("dead")

    monkeypatch.setattr(media_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        _fetch("http://example.com/seg.m4s")


# --------------------------------------------------------------------------- #
# _parse_playlist — 플레이리스트 읽기도 재시도해야 한다(IncompleteRead가 재시도 없이
# 그대로 새던 게 '다운로드 단계 실패'의 원인이었다).
# --------------------------------------------------------------------------- #
_PLAYLIST = (
    "#EXTM3U\n"
    '#EXT-X-MAP:URI="init.mp4"\n'
    "#EXTINF:6.0,\nseg-0.m4s\n"
    "#EXTINF:6.0,\nseg-1.m4s\n"
)


def test_parse_playlist_retries_on_incomplete_read(monkeypatch):
    from http.client import IncompleteRead

    monkeypatch.setattr(media_module.time, "sleep", lambda *_: None)
    attempts = {"n": 0}

    def fake_urlopen(url, timeout=15):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise IncompleteRead(b"\x00" * 64525)
        return _FakeResponse(_PLAYLIST.encode())

    monkeypatch.setattr(media_module.urllib.request, "urlopen", fake_urlopen)
    base, init_uri, starts, seg_uris = media._parse_playlist("http://x/y/index.m3u8")
    assert attempts["n"] == 2
    assert base == "http://x/y"
    assert init_uri == "init.mp4"
    assert seg_uris == ["seg-0.m4s", "seg-1.m4s"]
    assert starts == [0.0, 6.0]


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
