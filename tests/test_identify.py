from soopts.analyzers.identify import (
    CatalogEntry,
    looks_meaningless,
    match_catalog,
    parse_guess_json,
)


def test_looks_meaningless_short_lyrics():
    assert looks_meaningless("don't worry")  # <30자


def test_looks_meaningless_hallucination_repeat():
    # 길이는 30자 이상이지만 단어가 2종류뿐인 STT 환각 패턴
    assert looks_meaningless("hello world hello world hello world hello")


def test_looks_meaningless_real_lyrics_passes():
    assert not looks_meaningless("don't worry about your size, chubby girls chubby girls")


def test_parse_guess_json_plain():
    assert parse_guess_json('{"title": "All About That Bass", "artist": "Meghan Trainor"}') == (
        "All About That Bass",
        "Meghan Trainor",
    )


def test_parse_guess_json_strips_code_fence():
    text = '```json\n{"title": "매직 카펫 라이드", "artist": "쿨"}\n```'
    assert parse_guess_json(text) == ("매직 카펫 라이드", "쿨")


def test_parse_guess_json_null_title_returns_none():
    assert parse_guess_json('{"title": null, "artist": null}') is None


def test_parse_guess_json_invalid_returns_none():
    assert parse_guess_json("모르겠습니다") is None


def test_match_catalog_finds_alias():
    catalog = [
        CatalogEntry(song_id="1", title="매직 카펫 라이드", artist="쿨", aliases=["Magic Carpet Ride"]),
        CatalogEntry(song_id="2", title="All About That Bass", artist="Meghan Trainor"),
    ]
    entry, score = match_catalog("Magic Carpet Ride", "Cool", catalog)
    assert entry.song_id == "1"
    assert score > 85


def test_match_catalog_no_match_low_score():
    catalog = [CatalogEntry(song_id="1", title="매직 카펫 라이드", artist="쿨")]
    entry, score = match_catalog("Some Totally Unrelated Song", "Nobody", catalog)
    assert score < 85
