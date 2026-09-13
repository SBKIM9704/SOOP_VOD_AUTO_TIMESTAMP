"""전사 세그먼트 파일 → 곡 구간 가사 (순수 함수 + 파일 로더, 네트워크 없음)."""

import json

import pytest

from soopts.analyzers.segments import load_segments, lyrics_in_span

SEGS = [
    {"start": 90.0, "end": 98.0, "text": "곡 소개 멘트"},      # 구간 앞
    {"start": 98.0, "end": 104.0, "text": "첫 소절"},          # 구간 시작(100)을 걸침
    {"start": 104.0, "end": 110.0, "text": "가운데"},
    {"start": 110.0, "end": 122.0, "text": "마지막 소절"},      # 구간 끝(120)을 걸침
    {"start": 125.0, "end": 130.0, "text": "종료 멘트"},        # 구간 뒤
]


def test_picks_segments_overlapping_span():
    """경계를 걸친 첫·끝 소절까지 담고, 구간 밖 멘트는 버린다."""
    assert lyrics_in_span(SEGS, 100, 120) == "첫 소절 가운데 마지막 소절"


def test_segment_only_touching_boundary_is_excluded():
    # 90~98 세그먼트는 start=98에 닿기만 한다 — 겹침이 아니다. 104~110도 end=104에 닿기만 한다.
    assert lyrics_in_span(SEGS, 98, 104) == "첫 소절"


def test_sentinel_span_is_empty():
    """daily가 남긴 센티넬(end_s == start_s)은 구간이 아니다 — 호출부가 거절하도록 빈 값."""
    assert lyrics_in_span(SEGS, 100, 100) == ""
    assert lyrics_in_span(SEGS, 120, 100) == ""


def test_orders_by_time_and_skips_blank_text():
    segs = [
        {"start": 5, "end": 6, "text": "둘"},
        {"start": 1, "end": 2, "text": "   "},
        {"start": 0, "end": 1, "text": "하나"},
    ]
    assert lyrics_in_span(segs, 0, 10) == "하나 둘"


def test_load_segments_roundtrip(tmp_path):
    path = tmp_path / "segs.json"
    path.write_text(json.dumps(SEGS, ensure_ascii=False), encoding="utf-8")
    assert load_segments(path) == SEGS


def test_load_segments_rejects_other_json_shapes(tmp_path):
    """ingest 곡 목록 같은 다른 JSON을 넘기면 빈 가사로 흘려보내지 않고 멈춘다."""
    path = tmp_path / "spans.json"
    path.write_text('{"songs": [{"start_s": 1, "end_s": 2}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_segments(path)
    path.write_text('[{"start": 1, "end": 2}]', encoding="utf-8")   # text 없음
    with pytest.raises(ValueError):
        load_segments(path)
