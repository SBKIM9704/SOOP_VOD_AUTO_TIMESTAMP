import json

from soopts.analyzers.comment_timeline import TimelineSong, hms_to_s, parse_timeline_json
from soopts.collector.comments import extract_comments


def test_hms_to_s_full():
    assert hms_to_s("01:03:22") == 3802


def test_hms_to_s_minutes_seconds_only():
    assert hms_to_s("3:22") == 202


def test_parse_timeline_json_plain():
    text = json.dumps([
        {"time": "01:03:22", "artist": "아이유", "title": "애타는 마음"},
        {"time": "01:48:15", "artist": "윤하", "title": "기다리다"},
    ])
    songs = parse_timeline_json(text)
    assert songs == [
        TimelineSong(time_s=3802, title="애타는 마음", artist="아이유"),
        TimelineSong(time_s=6495, title="기다리다", artist="윤하"),
    ]


def test_parse_timeline_json_strips_code_fence():
    text = '```json\n[{"time": "00:01:00", "artist": null, "title": "곡명"}]\n```'
    songs = parse_timeline_json(text)
    assert songs == [TimelineSong(time_s=60, title="곡명", artist=None)]


def test_parse_timeline_json_empty_array_when_no_songs():
    assert parse_timeline_json("[]") == []


def test_parse_timeline_json_invalid_returns_empty():
    assert parse_timeline_json("타임라인 없습니다") == []


def test_parse_timeline_json_skips_items_missing_title_or_time():
    text = json.dumps([{"time": "00:01:00"}, {"title": "곡명"}, {}])
    assert parse_timeline_json(text) == []


def test_parse_timeline_json_non_list_returns_empty():
    assert parse_timeline_json('{"time": "00:01:00", "title": "곡명"}') == []


def test_parse_timeline_json_object_wrapped_songs_key():
    # Groq의 json_object 모드는 최상위 배열을 허용하지 않아 {"songs": [...]}로 감싸 요청한다.
    text = json.dumps({"songs": [{"time": "00:01:00", "artist": None, "title": "곡명"}]})
    assert parse_timeline_json(text) == [TimelineSong(time_s=60, title="곡명", artist=None)]


def test_parse_timeline_json_songs_key_not_a_list_returns_empty():
    assert parse_timeline_json(json.dumps({"songs": "곡명"})) == []


def test_real_fan_timeline_comment_parses_expected_four_songs(fixtures_dir):
    # 실제 캡처된 팬 타임라인 댓글(VOD 201651295, 익명화된 fixture)과 동일한 형식을
    # LLM이 {"songs": [...]}로 정확히 추출했다고 가정했을 때 파싱이 올바른지 확인한다.
    data = json.loads((fixtures_dir / "vod_comments_page1.json").read_bytes())
    comments = extract_comments(data)
    assert any("🎤" in c for c in comments)

    simulated_llm_response = json.dumps({"songs": [
        {"time": "01:03:22", "artist": "아이유", "title": "애타는 마음"},
        {"time": "01:48:15", "artist": "윤하", "title": "기다리다"},
        {"time": "02:03:39", "artist": "아이유", "title": "이름에게"},
        {"time": "02:10:46", "artist": "윤하", "title": "오늘 헤어졌어요"},
    ]})
    songs = parse_timeline_json(simulated_llm_response)
    assert [s.time_s for s in songs] == [3802, 6495, 7419, 7846]
    assert [s.artist for s in songs] == ["아이유", "윤하", "아이유", "윤하"]
