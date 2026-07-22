"""합본 빌드의 순수 함수 — ffmpeg·네트워크 없이 도는 것만."""

from pathlib import Path

from soopts.config import Config
from soopts.export.video import build_concat_list, plan_songs


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
