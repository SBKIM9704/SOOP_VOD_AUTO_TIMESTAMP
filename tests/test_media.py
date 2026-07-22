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


@pytest.fixture(autouse=True)
def _clear_playlist_cache():
    # _parse_playlist는 URL 단위 lru_cache라 테스트 간에 결과가 새면 안 된다.
    media._parse_playlist.cache_clear()
    yield
    media._parse_playlist.cache_clear()


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


# --------------------------------------------------------------------------- #
# 슬라이스 시작 시각 — 파일은 요청 시각이 아니라 세그먼트 경계에서 시작한다
# --------------------------------------------------------------------------- #
_PLAYLIST_10 = "#EXTM3U\n" + '#EXT-X-MAP:URI="init.mp4"\n' + "".join(
    f"#EXTINF:6.0,\nseg-{i}.m4s\n" for i in range(10)
)   # 세그먼트 시작: 0, 6, 12, … 54


@pytest.fixture
def fake_hls(monkeypatch):
    """m3u8은 플레이리스트를, 나머지 URL은 더미 세그먼트를 돌려주는 가짜 서버."""
    fetched: list[str] = []

    def fake_urlopen(url, timeout=15):
        fetched.append(url)
        if url.endswith(".m3u8"):
            return _FakeResponse(_PLAYLIST_10.encode())
        return _FakeResponse(b"\x00" * 1024)   # _MIN_SEGMENT_BYTES 통과용

    monkeypatch.setattr(media_module.urllib.request, "urlopen", fake_urlopen)
    return fetched


def test_download_slice_returns_actual_segment_start(tmp_path, fake_hls):
    """20초를 요청해도 파일은 그 시각을 품은 세그먼트(18초)에서 시작한다 — 그 값을 돌려줘야
    호출부가 전사 타임스탬프를 맞출 수 있다."""
    actual = media.download_slice("http://x/y/i.m3u8", 20.0, 30.0, tmp_path / "s.mp4")
    assert actual == 18.0


def test_slice_lead_s_is_gap_between_request_and_segment_start(fake_hls):
    assert media.slice_lead_s("http://x/y/i.m3u8", 20.0, 30.0) == 2.0
    assert media.slice_lead_s("http://x/y/i.m3u8", 12.0, 30.0) == 0.0   # 경계에 딱 맞으면 0


def test_download_span_returns_lead_and_covered(tmp_path, fake_hls):
    spans = [("http://x/y/i.m3u8", 20.0, 30.0)]
    lead, covered = media.download_span(spans, tmp_path / "s.mp4")
    assert (lead, covered) == (2.0, 10.0)


def test_download_span_reports_same_lead_from_cache(tmp_path, fake_hls):
    """캐시 여부에 따라 타임스탬프가 달라지면 안 된다 — 다운로드를 건너뛰어도 lead는 같다."""
    out = tmp_path / "s.mp4"
    first = media.download_span([("http://x/y/i.m3u8", 20.0, 30.0)], out)
    n_before = len(fake_hls)
    second = media.download_span([("http://x/y/i.m3u8", 20.0, 30.0)], out)
    assert second == first
    assert len(fake_hls) == n_before   # 세그먼트도 m3u8도 다시 받지 않았다(플레이리스트 캐시)


def test_download_span_force_redownloads(tmp_path, fake_hls):
    out = tmp_path / "s.mp4"
    media.download_span([("http://x/y/i.m3u8", 20.0, 30.0)], out)
    n_before = len(fake_hls)
    media.download_span([("http://x/y/i.m3u8", 20.0, 30.0)], out, force=True)
    assert len(fake_hls) > n_before


def test_covering_idxs_excludes_segment_that_only_touches_start():
    # 세그먼트 [6,12)는 요청 시작 12와 겹치지 않는다 — 예전 하드코딩(6.0) 조건은 이걸 포함해
    # 슬라이스가 항상 한 세그먼트 일찍 시작했다.
    assert media._covering_idxs([0.0, 6.0, 12.0, 18.0], 12.0, 20.0) == [2, 3]


def test_covering_idxs_handles_segments_longer_than_six_seconds():
    # 길이가 6초보다 길면 예전 조건은 요청 시각을 품은 세그먼트를 빠뜨려 앞부분이 잘렸다.
    assert media._covering_idxs([0.0, 10.0, 20.0], 8.0, 12.0) == [0, 1]
