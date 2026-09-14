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


def test_short_leading_part_in_ms_is_not_read_as_seconds():
    # 179806825 실측: _1이 92초(92000ms). 값별 추정이면 92000초가 되어 _2 offset이 25시간 밀렸다.
    meta = parse_meta_response("179806825", _payload(
        [_file(1, 92_000), _file(2, 6354_167)], total_ms=6446_167))
    assert [p.duration for p in meta.parts] == [92, 6354]
    assert [p.offset_s for p in meta.parts] == [0, 92]
    assert meta.total_duration == 6446


def test_short_middle_parts_do_not_shift_later_offsets():
    # 188819223 실측: 중간 _2·_3이 4초·3초.
    meta = parse_meta_response("v", _payload(
        [_file(1, 6505_000), _file(2, 4_000), _file(3, 3_000), _file(4, 10471_000)],
        total_ms=16983_000))
    assert [p.duration for p in meta.parts] == [6505, 4, 3, 10471]
    assert [p.offset_s for p in meta.parts] == [0, 6505, 6509, 6512]


def test_short_trailing_part_keeps_total_duration():
    # 196974651 실측: 마지막 _2가 51.684초. offset엔 영향 없지만 총길이가 5만 초로 부풀었다.
    meta = parse_meta_response("v", _payload(
        [_file(1, 18000_000), _file(2, 51_684)], total_ms=18051_684))
    assert [p.duration for p in meta.parts] == [18000, 52]
    assert meta.total_duration == 18052


def test_all_small_values_fall_back_to_per_value_guess():
    # 임계를 넘는 값이 하나도 없으면(초 단위 응답으로 보이는 경우) 예전 추정 그대로.
    meta = parse_meta_response("v", _payload([_file(1, 3600), _file(2, 1800)]))
    assert [p.duration for p in meta.parts] == [3600, 1800]
    assert [p.offset_s for p in meta.parts] == [0, 3600]
