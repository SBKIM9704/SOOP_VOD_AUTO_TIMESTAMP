import json

from soopts.collector.comments import extract_comments


def test_extract_comments_from_real_schema(fixtures_dir):
    data = json.loads((fixtures_dir / "vod_comments_page1.json").read_bytes())
    comments = extract_comments(data)
    assert len(comments) == 2
    assert "초록주먹뱅" in comments[0]
    assert "🎤" in comments[1]


def test_extract_comments_skips_empty():
    data = {"data": [{"comment": "hi"}, {"comment": ""}, {}]}
    assert extract_comments(data) == ["hi"]


def test_extract_comments_no_data_key():
    assert extract_comments({}) == []
