"""합본 빌드의 순수 함수 — ffmpeg·네트워크 없이 도는 것만."""

from pathlib import Path

import pytest

import soopts.collector.media as media
from soopts.config import Config
from soopts.export.video import (
    assert_parts_aligned,
    build_concat_list,
    padded_bounds,
    plan_songs,
)
from soopts.models import MetaPart


class _Meta:
    """parts만 있는 최소 meta 스텁 — assert_parts_aligned 테스트용."""

    def __init__(self, *durations: int):
        off, self.parts = 0, []
        for i, d in enumerate(durations):
            self.parts.append(MetaPart(idx=i, file_info_key=f"k{i}", duration=d,
                                       offset_s=off, file_order=i + 1))
            off += d
        self.total_duration = off   # 누락 없음(contiguous order) — omitted_parts_gap None


def _cfg(**over) -> Config:
    cfg = Config()
    for k, v in over.items():
        setattr(cfg.video, k, v)
    return cfg


def _perf(pid, start, end):
    return {"id": pid, "start_s": start, "end_s": end}


def test_plan_songs_sorts_by_start_and_keeps_all_under_caps():
    perfs = [_perf(2, 300, 480), _perf(1, 10, 190)]
    kept, dropped = plan_songs(_cfg(), perfs)
    assert [p["id"] for p in kept] == [1, 2]
    assert dropped == []


def test_plan_songs_drops_spans_without_usable_end():
    """끝을 모르는 구간은 -t를 정할 수 없어 아예 뺀다."""
    kept, dropped = plan_songs(_cfg(), [_perf(1, 10, None), _perf(2, 20, 20), _perf(3, 30, 90)])
    assert [p["id"] for p in kept] == [3]
    assert [p["id"] for p in dropped] == [1, 2]


def test_plan_songs_respects_max_songs():
    perfs = [_perf(i, i * 300, i * 300 + 120) for i in range(1, 6)]
    kept, dropped = plan_songs(_cfg(max_songs=3), perfs)
    assert len(kept) == 3
    assert len(dropped) == 2


def test_plan_songs_respects_total_minutes():
    """총 길이 상한은 러너 시간·디스크 안전판이다 — 넘는 곡부터 잘라낸다."""
    perfs = [_perf(1, 0, 600), _perf(2, 1000, 1600), _perf(3, 2000, 2600)]   # 10분 × 3
    kept, dropped = plan_songs(_cfg(max_total_minutes=25.0), perfs)
    assert [p["id"] for p in kept] == [1, 2]
    assert [p["id"] for p in dropped] == [3]


def test_padded_bounds_adds_lead_and_tail():
    """전주 여백만큼 앞으로, 여운만큼 뒤로 — DB 값은 안 건드리고 빌드 클립만 늘린다."""
    s, e = padded_bounds(_cfg(intro_lead_s=10.0, outro_tail_s=4.0), _perf(1, 100, 250))
    assert (s, e) == (90.0, 254.0)


def test_padded_bounds_clamps_lead_at_zero():
    """VOD 시작 근처 곡은 여백이 0 밑으로 내려가지 않는다."""
    s, e = padded_bounds(_cfg(intro_lead_s=10.0, outro_tail_s=4.0), _perf(1, 5, 200))
    assert s == 0.0
    assert e == 204.0


def test_padded_bounds_zero_padding_is_identity():
    """여백을 0으로 두면 원래 경계 그대로 — 패딩 기능을 끌 수 있다."""
    s, e = padded_bounds(_cfg(intro_lead_s=0.0, outro_tail_s=0.0), _perf(1, 100, 250))
    assert (s, e) == (100.0, 250.0)


def test_padded_bounds_uses_start_s_with_tail():
    """클립 시작은 start_s(팬 댓글값) 그대로, 끝에만 outro_tail을 붙인다."""
    s, e = padded_bounds(_cfg(intro_lead_s=0.0, outro_tail_s=7.0), _perf(1, 200, 400))
    assert (s, e) == (200.0, 407.0)


def test_build_concat_list_format():
    body = build_concat_list([Path("/tmp/a.mp4"), Path("/tmp/b.mp4")])
    assert body == "file '/tmp/a.mp4'\nfile '/tmp/b.mp4'\n"


def test_build_concat_list_escapes_single_quote():
    """작은따옴표가 든 경로가 concat 목록을 깨면 안 된다."""
    body = build_concat_list([Path("/tmp/it's.mp4")])
    assert body == "file '/tmp/it'\\''s.mp4'\n"


def test_build_concat_list_makes_paths_absolute():
    """상대경로는 목록 파일 위치 기준으로 해석돼 접두사가 두 번 붙는다 — 절대경로로 적는다."""
    body = build_concat_list([Path("ytbuild/clip.mp4")])
    assert body.startswith("file '/")
    assert body.rstrip().endswith("ytbuild/clip.mp4'")


# --------------------------------------------------------------------------- #
# assert_parts_aligned — 파트 offset 오염 게이트 (playlist_total_s만 monkeypatch)
# --------------------------------------------------------------------------- #
def test_assert_parts_aligned_passes_when_durations_match(monkeypatch):
    monkeypatch.setattr(media, "playlist_total_s", lambda u: {"u0": 8068.0, "u1": 11918.0}[u])
    assert_parts_aligned(_Meta(8068, 11918), ["u0", "u1"])   # raise 없음


def test_assert_parts_aligned_single_part_skips_network(monkeypatch):
    called = []
    monkeypatch.setattr(media, "playlist_total_s", lambda u: called.append(u) or 0.0)
    assert_parts_aligned(_Meta(18000), ["u0"])
    assert called == []   # 단일 파트면 m3u8도 안 읽는다


def test_assert_parts_aligned_raises_on_offset_shift(monkeypatch):
    # part0 보고 8068인데 실제 8180 → 뒤 파트 offset 밀림 → 전체 중단
    monkeypatch.setattr(media, "playlist_total_s", lambda u: {"u0": 8180.0, "u1": 11918.0}[u])
    with pytest.raises(RuntimeError, match="offset"):
        assert_parts_aligned(_Meta(8068, 11918), ["u0", "u1"])


def test_assert_parts_aligned_last_part_mismatch_passes(monkeypatch):
    # 마지막 파트만 틀리면 어떤 곡의 offset도 안 미므로 통과
    monkeypatch.setattr(media, "playlist_total_s", lambda u: {"u0": 8068.0, "u1": 12500.0}[u])
    assert_parts_aligned(_Meta(8068, 11918), ["u0", "u1"])   # raise 없음
