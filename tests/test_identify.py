import pytest

import soopts.analyzers.identify as identify_module
from soopts.analyzers.identify import (
    ARTIST_STRONG,
    MATCH_THRESHOLD,
    CatalogEntry,
    LyricsGuess,
    ScoredEntry,
    _build_disambiguation_shortlist,
    disambiguate_with_llm,
    guess_from_lyrics,
    identify_song,
    looks_meaningless,
    match_catalog,
    parse_song_guess_json,
    resolve_song_match,
)


def test_looks_meaningless_short_lyrics():
    assert looks_meaningless("don't worry")  # <30자


def test_looks_meaningless_hallucination_repeat():
    # 길이는 30자 이상이지만 단어가 2종류뿐인 STT 환각 패턴
    assert looks_meaningless("hello world hello world hello world hello")


def test_looks_meaningless_real_lyrics_passes():
    assert not looks_meaningless("don't worry about your size, chubby girls chubby girls")


def test_parse_song_guess_json_not_song():
    assert parse_song_guess_json('{"is_song": false, "title": null, "artist": null}') == (
        LyricsGuess(is_song=False)
    )


def test_parse_song_guess_json_song_with_title_and_artist():
    text = '{"is_song": true, "title": "All About That Bass", "artist": "Meghan Trainor"}'
    assert parse_song_guess_json(text) == LyricsGuess(
        is_song=True, title="All About That Bass", artist="Meghan Trainor"
    )


def test_parse_song_guess_json_song_true_but_title_unknown():
    text = '{"is_song": true, "title": null, "artist": null}'
    assert parse_song_guess_json(text) == LyricsGuess(is_song=True, title=None, artist=None)


def test_parse_song_guess_json_missing_is_song_key_returns_none():
    assert parse_song_guess_json('{"title": "x", "artist": "y"}') is None


def test_parse_song_guess_json_strips_code_fence():
    text = '```json\n{"is_song": true, "title": "매직 카펫 라이드", "artist": "쿨"}\n```'
    assert parse_song_guess_json(text) == LyricsGuess(
        is_song=True, title="매직 카펫 라이드", artist="쿨"
    )


def test_parse_song_guess_json_invalid_returns_none():
    assert parse_song_guess_json("모르겠습니다") is None


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


def test_match_catalog_artist_alone_cannot_win():
    """실제 프로덕션 버그 재현: 아티스트만 강하게 일치해도 제목이 완전히 다른 곡에
    매칭돼서는 안 된다(과거엔 "애타는 마음"/"이름에게"가 둘 다 아이유의 "unlucky"로
    100% 확신 매칭되는 사고가 있었다)."""
    catalog = [CatalogEntry(song_id="unlucky-song", title="unlucky", artist="아이유")]
    for guessed_title in ("애타는 마음", "이름에게"):
        entry, score = match_catalog(guessed_title, "아이유", catalog)
        assert score < MATCH_THRESHOLD


def test_match_catalog_uses_original_title():
    catalog = [
        CatalogEntry(song_id="1", title="Unrelated Display Name", original_title="기다리다"),
    ]
    entry, score = match_catalog("기다리다", "", catalog)
    assert entry.song_id == "1"
    assert score > MATCH_THRESHOLD


def test_match_catalog_artist_only_tiebreaks_between_equal_titles():
    catalog = [
        CatalogEntry(song_id="a", title="눈사람", artist="정승환"),
        CatalogEntry(song_id="b", title="눈사람", artist="벤"),
    ]
    entry, score = match_catalog("눈사람", "정승환", catalog)
    assert entry.song_id == "a"
    # 제목이 아예 다르면 아티스트가 완벽히 일치해도 이기지 못한다.
    entry, score = match_catalog("완전히 다른 제목", "정승환", catalog)
    assert score < MATCH_THRESHOLD


def test_build_disambiguation_shortlist_excludes_zero_title_score():
    # 실제 버그 패턴: artist는 완벽히 일치하지만 title은 0점 — 절대 후보에 들면 안 된다.
    bug_pattern = ScoredEntry(CatalogEntry(song_id="x", title="unlucky"), 0.0, 100.0)
    weak_but_plausible = ScoredEntry(CatalogEntry(song_id="y", title="기다리다"), 40.0, ARTIST_STRONG)
    shortlist = _build_disambiguation_shortlist([bug_pattern, weak_but_plausible])
    song_ids = [s.entry.song_id for s in shortlist]
    assert "x" not in song_ids
    assert "y" in song_ids


def test_build_disambiguation_shortlist_empty_when_no_scores():
    assert _build_disambiguation_shortlist([]) == []


class _FakeGroqResponse:
    def __init__(self, content: str):
        self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": content})()})]


class _FakeGroqClient:
    def __init__(self, content: str, *, api_key=None):
        self._content = content

    @property
    def chat(self):
        client = self

        class _Completions:
            def create(self, **kwargs):
                return _FakeGroqResponse(client._content)

        return type("Chat", (), {"completions": _Completions()})()


def _fake_groq_factory(content: str):
    def _make(api_key=None):
        return _FakeGroqClient(content, api_key=api_key)

    return _make


def test_disambiguate_with_llm_picks_index(monkeypatch):
    pytest.importorskip("groq")
    monkeypatch.setattr("groq.Groq", _fake_groq_factory('{"index": 1}'))
    shortlist = [
        ScoredEntry(CatalogEntry(song_id="a", title="A"), 40.0, 0.0),
        ScoredEntry(CatalogEntry(song_id="b", title="B"), 35.0, 0.0),
    ]
    choice = disambiguate_with_llm("가사...", shortlist)
    assert choice.entry.song_id == "b"


def test_disambiguate_with_llm_null_index_returns_none(monkeypatch):
    pytest.importorskip("groq")
    monkeypatch.setattr("groq.Groq", _fake_groq_factory('{"index": null}'))
    shortlist = [ScoredEntry(CatalogEntry(song_id="a", title="A"), 40.0, 0.0)]
    assert disambiguate_with_llm("가사...", shortlist) is None


def test_disambiguate_with_llm_empty_shortlist_returns_none_without_calling_groq():
    assert disambiguate_with_llm("가사...", []) is None


def test_resolve_song_match_uses_llm_pick_for_ambiguous_case(monkeypatch):
    catalog = [CatalogEntry(song_id="picked", title="기다리다", artist="윤하")]
    picked = ScoredEntry(catalog[0], 40.0, 100.0)
    monkeypatch.setattr(identify_module, "disambiguate_with_llm", lambda *a, **k: picked)
    result = resolve_song_match("기다렷다", "윤하", "가사...", True, catalog)
    assert result.song_id == "picked"
    assert result.identify_status == "auto_matched"


def test_resolve_song_match_falls_back_to_needs_review_when_llm_unsure(monkeypatch):
    catalog = [CatalogEntry(song_id="unlucky-song", title="unlucky", artist="아이유")]
    monkeypatch.setattr(identify_module, "disambiguate_with_llm", lambda *a, **k: None)
    result = resolve_song_match("애타는 마음", "아이유", "가사...", True, catalog)
    assert result.song_id is None
    assert result.identify_status == "needs_review"


def test_guess_from_lyrics_short_circuits_on_meaningless_without_calling_groq():
    # looks_meaningless가 True면 `from groq import Groq` 자체를 실행하지 않고 곧바로
    # None을 반환해야 한다 — groq 미설치 환경에서도 이 테스트는 통과해야 한다.
    assert guess_from_lyrics("hi") is None


def test_guess_from_lyrics_parses_is_song_false(monkeypatch):
    pytest.importorskip("groq")
    monkeypatch.setattr("groq.Groq", _fake_groq_factory('{"is_song": false}'))
    guess = guess_from_lyrics("가사 왜 표시해놓은거 어디갔지? 어디갔냐? 저장을 안였나?")
    assert guess == LyricsGuess(is_song=False)


def test_guess_from_lyrics_parses_song_with_title(monkeypatch):
    pytest.importorskip("groq")
    monkeypatch.setattr(
        "groq.Groq", _fake_groq_factory('{"is_song": true, "title": "기다리다", "artist": "윤하"}')
    )
    guess = guess_from_lyrics("이 정도면 30자는 충분히 넘긴 실제 가사 텍스트입니다 그렇죠")
    assert guess == LyricsGuess(is_song=True, title="기다리다", artist="윤하")


def test_identify_song_returns_none_when_confidently_not_a_song(monkeypatch):
    monkeypatch.setattr(
        identify_module, "guess_from_lyrics", lambda *a, **k: LyricsGuess(is_song=False)
    )
    assert identify_song("가사...", True, []) is None


def test_identify_song_needs_review_when_song_but_title_unknown(monkeypatch):
    monkeypatch.setattr(
        identify_module, "guess_from_lyrics",
        lambda *a, **k: LyricsGuess(is_song=True, title=None),
    )
    result = identify_song("가사...", True, [])
    assert result.song_id is None
    assert result.identify_status == "needs_review"


def test_identify_song_needs_review_when_guess_fails(monkeypatch):
    monkeypatch.setattr(identify_module, "guess_from_lyrics", lambda *a, **k: None)
    result = identify_song("가사...", True, [])
    assert result.song_id is None
    assert result.identify_status == "needs_review"


def test_identify_song_resolves_via_catalog_when_song_with_title(monkeypatch):
    catalog = [CatalogEntry(song_id="picked", title="기다리다", artist="윤하")]
    monkeypatch.setattr(
        identify_module, "guess_from_lyrics",
        lambda *a, **k: LyricsGuess(is_song=True, title="기다리다", artist="윤하"),
    )
    result = identify_song("가사...", True, catalog)
    assert result.song_id == "picked"
    assert result.identify_status == "auto_matched"
