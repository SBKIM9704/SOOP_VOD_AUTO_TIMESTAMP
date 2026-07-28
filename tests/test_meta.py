"""parse_meta_response — 누락 파트 offset 보정 로직(순수 함수)."""

from __future__ import annotations

from soopts.collector.meta import parse_meta_response


def _payload(files: list[dict], total_ms: int | None = None) -> dict:
    data: dict = {"title": "T", "bj_id": "b", "files": files}
    if total_ms is not None:
        data["total_file_duration"] = total_ms
    return {"data": data}


def _file(order: int, dur_ms: int, key: str | None = None) -> dict:
    return {"file_info_key": key or f"20260715_X_1_{order}",
            "duration": dur_ms, "file_order": order}


def test_contiguous_parts_offsets_are_cumulative():
    meta = parse_meta_response("v", _payload(
        [_file(1, 8000_000), _file(2, 12000_000)], total_ms=20000_000))
    assert [p.offset_s for p in meta.parts] == [0, 8000]
    assert [p.duration for p in meta.parts] == [8000, 12000]
    assert meta.total_duration == 20000


def test_single_internal_gap_shifts_later_offset_by_omitted():
    # file_order [1,4], 반환 합 19986, total 20088 → 누락 102는 _4 앞에.
    meta = parse_meta_response("201586597", _payload(
        [_file(1, 8068_000), _file(4, 11918_417)], total_ms=20088_417))
    assert [p.offset_s for p in meta.parts] == [0, 8170]   # 8068 + 102
    assert [p.file_order for p in meta.parts] == [1, 4]
    assert meta.total_duration == 20088


def test_order_gap_but_no_omission_keeps_offsets():
    # [1,3]이지만 total == 반환 합 → 빈 번호 파트는 길이 0, offset 정상.
    meta = parse_meta_response("v", _payload(
        [_file(1, 10000_000), _file(3, 7000_000)], total_ms=17000_000))
    assert [p.offset_s for p in meta.parts] == [0, 10000]


def test_leading_gap_shifts_all_offsets():
    # 첫 파트 order=3(앞에 _1,_2 누락), 반환 13000, total 13100 → 누락 100 앞에.
    meta = parse_meta_response("v", _payload(
        [_file(3, 13000_000)], total_ms=13100_000))
    assert meta.parts[0].offset_s == 100


def test_entries_sorted_by_file_order():
    # 응답 순서가 뒤섞여 와도 file_order로 정렬.
    meta = parse_meta_response("v", _payload(
        [_file(2, 5000_000), _file(1, 8000_000)], total_ms=13000_000))
    assert [p.file_order for p in meta.parts] == [1, 2]
    assert [p.offset_s for p in meta.parts] == [0, 8000]


def test_no_total_field_falls_back_to_cumulative():
    # total_file_duration 없으면 기존처럼 반환 합만.
    meta = parse_meta_response("v", _payload(
        [_file(1, 8000_000), _file(2, 12000_000)], total_ms=None))
    assert [p.offset_s for p in meta.parts] == [0, 8000]
    assert meta.total_duration == 20000
