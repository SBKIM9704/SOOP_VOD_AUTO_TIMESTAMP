import json

from soopts.collector.vod_list import extract_page, to_candidate


def test_to_candidate_maps_real_item(fixtures_dir):
    data = json.loads((fixtures_dir / "vod_list_page1.json").read_text(encoding="utf-8"))
    item = data["data"][0]
    c = to_candidate(item)
    assert c["title_no"] == 201651295
    assert c["title"] == "[하데스] 초록주먹뱅 (짧뱅이라는 뜻)"
    assert c["broadcast_date"] == "2026-07-16"
    # total_file_duration은 밀리초(8332900) → 초 단위로 환산, 실제 meta API(8333s)와 대조 검증됨
    assert c["duration_s"] == 8333


def test_extract_page_real_fixture(fixtures_dir):
    data = json.loads((fixtures_dir / "vod_list_page1.json").read_text(encoding="utf-8"))
    candidates, current_page, last_page = extract_page(data)
    assert len(candidates) == 2
    assert current_page == 1
    assert last_page == 32
    assert [c["title_no"] for c in candidates] == [201651295, 201586597]


def test_extract_page_empty_data():
    candidates, current_page, last_page = extract_page({"data": [], "meta": {}})
    assert candidates == []
    assert current_page == 1
    assert last_page == 1
