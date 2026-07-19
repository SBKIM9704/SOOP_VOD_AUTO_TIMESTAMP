import pytest

import soopts.analyzers.identify as identify_module
from soopts.analyzers.identify import (
    ARTIST_STRONG,
    CATALOG_PROMPT_MAX,
    MATCH_THRESHOLD,
    CatalogEntry,
    LyricsGuess,
    ScoredEntry,
    _build_disambiguation_shortlist,
    _catalog_prompt_block,
    _reasoning_kwargs,
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


def test_reasoning_kwargs_gpt_oss_gets_low():
    assert _reasoning_kwargs("openai/gpt-oss-120b") == {"reasoning_effort": "low"}


def test_reasoning_kwargs_qwen_gets_none_not_low():
    # qwen은 "low"를 400으로 거절한다(`must be one of `none` or `default``). 이 분기가
    # 없으면 SOOPTS_IDENTIFY_MODEL을 바꾼 순간 전 구간이 조용히 실패한다.
    assert _reasoning_kwargs("qwen/qwen3.6-27b") == {"reasoning_effort": "none"}


def test_reasoning_kwargs_unknown_model_omits_param():
    assert _reasoning_kwargs("meta-llama/llama-4") == {}


def test_catalog_prompt_block_lists_titles_and_artists():
    block = _catalog_prompt_block([
        CatalogEntry(song_id="a", title="밤편지", artist="아이유"),
        CatalogEntry(song_id="b", title="아티스트없는곡"),
    ])
    assert "- 밤편지 / 아이유" in block
    assert "- 아티스트없는곡" in block


def test_catalog_prompt_block_includes_original_title():
    # _score_catalog는 original_title로도 채점한다 — 프롬프트에 title만 넣으면 원제가
    # 영문인 곡에서 LLM이 알아볼 단서를 못 받는다.
    block = _catalog_prompt_block([
        CatalogEntry(song_id="a", title="보헤미안 랩소디",
                     original_title="Bohemian Rhapsody", artist="Queen"),
    ])
    assert "- 보헤미안 랩소디 (Bohemian Rhapsody) / Queen" in block


def test_catalog_prompt_block_skips_redundant_original_title():
    block = _catalog_prompt_block([
        CatalogEntry(song_id="a", title="밤편지", original_title="밤편지"),
    ])
    assert block.count("밤편지") == 1


class _ModelGoneClient:
    """첫 모델엔 404를 던지고 두 번째 모델에만 응답하는 fake — 폴백 경로 검증용."""

    def __init__(self, dead_model: str, content: str):
        self.dead_model = dead_model
        self.content = content
        self.models_tried: list[str] = []

    @property
    def chat(self):
        client = self

        class _Completions:
            def create(self, **kwargs):
                model = kwargs["model"]
                client.models_tried.append(model)
                if model == client.dead_model:
                    raise RuntimeError("404 model_not_found: decommissioned")
                return _FakeGroqResponse(client.content)

        return type("Chat", (), {"completions": _Completions()})()


def test_create_completion_falls_back_when_model_gone(monkeypatch):
    # preview 모델이 내려가도 파이프라인은 계속 돌아야 한다 — 폴백이 없으면 모든 VOD가
    # failed로 MAX_RETRIES를 태우고 큐가 밀린다.
    monkeypatch.setattr(identify_module, "GROQ_MODEL", "qwen/qwen3.6-27b")
    client = _ModelGoneClient("qwen/qwen3.6-27b", '{"is_song": false}')
    identify_module._create_completion(client, max_tokens=10, messages=[])
    assert client.models_tried == ["qwen/qwen3.6-27b", identify_module.FALLBACK_MODEL]


def test_create_completion_uses_fallback_reasoning_effort(monkeypatch):
    # 폴백 모델은 reasoning_effort 요구값이 다르다 — 원래 모델 값을 그대로 물려주면
    # 폴백까지 400으로 죽어 그물이 찢어진다.
    monkeypatch.setattr(identify_module, "GROQ_MODEL", "qwen/qwen3.6-27b")
    seen: list[dict] = []

    class _Client:
        @property
        def chat(self):
            class _Completions:
                def create(self, **kwargs):
                    seen.append(kwargs)
                    if kwargs["model"] == "qwen/qwen3.6-27b":
                        raise RuntimeError("404 model_not_found")
                    return _FakeGroqResponse('{"is_song": false}')

            return type("Chat", (), {"completions": _Completions()})()

    identify_module._create_completion(_Client(), max_tokens=10, messages=[])
    assert seen[0]["reasoning_effort"] == "none"
    assert seen[1]["reasoning_effort"] == "low"


def test_create_completion_does_not_fall_back_on_transient_error(monkeypatch):
    # 아무 에러에나 폴백하면 429일 때 매 호출이 두 번 나가고 진짜 장애가 숨는다.
    monkeypatch.setattr(identify_module, "GROQ_MODEL", "qwen/qwen3.6-27b")

    class _RateLimited:
        @property
        def chat(self):
            class _Completions:
                def create(self, **kwargs):
                    raise RuntimeError("429 rate_limit_exceeded")

            return type("Chat", (), {"completions": _Completions()})()

    with pytest.raises(RuntimeError, match="429"):
        identify_module._create_completion(_RateLimited(), max_tokens=10, messages=[])


def test_default_model_is_not_gpt_oss():
    # A/B에서 gpt-oss는 카탈로그를 줘도 못 찾았다. 기본값이 되돌아가면 카탈로그 주입
    # 자체가 무의미해지므로 고정한다.
    assert not identify_module.GROQ_MODEL.startswith("openai/gpt-oss")
    # 기본 모델과 _reasoning_kwargs가 어긋나면 전 구간이 400으로 조용히 죽는다.
    assert _reasoning_kwargs(identify_module.GROQ_MODEL) != {"reasoning_effort": "low"}


def test_catalog_prompt_block_empty_without_catalog():
    assert _catalog_prompt_block(None) == ""
    assert _catalog_prompt_block([]) == ""


def test_catalog_prompt_block_omitted_entirely_when_over_cap():
    # 앞에서 잘라내면 하필 정답이 잘려나가도 조용히 오답이 되므로 통째로 생략한다.
    big = [CatalogEntry(song_id=str(i), title=f"곡{i}") for i in range(CATALOG_PROMPT_MAX + 1)]
    assert _catalog_prompt_block(big) == ""


class _CapturingGroqClient(_FakeGroqClient):
    """create()에 넘어간 kwargs를 보관해 프롬프트 내용을 검증할 수 있게 한다."""

    def __init__(self, content: str, *, api_key=None):
        super().__init__(content, api_key=api_key)
        self.calls: list[dict] = []

    @property
    def chat(self):
        client = self

        class _Completions:
            def create(self, **kwargs):
                client.calls.append(kwargs)
                return _FakeGroqResponse(client._content)

        return type("Chat", (), {"completions": _Completions()})()


def test_guess_from_lyrics_puts_catalog_into_prompt(monkeypatch):
    pytest.importorskip("groq")
    holder: dict[str, _CapturingGroqClient] = {}

    def _make(api_key=None):
        holder["client"] = _CapturingGroqClient(
            '{"is_song": true, "title": "밤편지", "artist": "아이유"}'
        )
        return holder["client"]

    monkeypatch.setattr("groq.Groq", _make)
    guess_from_lyrics(
        "이 정도면 30자는 충분히 넘긴 실제 가사 텍스트입니다 그렇죠",
        catalog=[CatalogEntry(song_id="a", title="밤편지", artist="아이유")],
    )
    prompt = holder["client"].calls[0]["messages"][0]["content"]
    assert "밤편지" in prompt


def test_guess_from_lyrics_without_catalog_has_no_catalog_block(monkeypatch):
    pytest.importorskip("groq")
    holder: dict[str, _CapturingGroqClient] = {}

    def _make(api_key=None):
        holder["client"] = _CapturingGroqClient('{"is_song": false}')
        return holder["client"]

    monkeypatch.setattr("groq.Groq", _make)
    guess_from_lyrics("이 정도면 30자는 충분히 넘긴 실제 가사 텍스트입니다 그렇죠")
    prompt = holder["client"].calls[0]["messages"][0]["content"]
    assert "부른 적 있는 곡 목록" not in prompt


def test_identify_song_threads_catalog_into_guess(monkeypatch):
    # 카탈로그가 rapidfuzz에만 가고 LLM에는 안 가던 것이 원래 문제였다 — 회귀 방지.
    seen: dict[str, object] = {}

    def _fake_guess(lyrics, *, catalog=None, api_key=None):
        seen["catalog"] = catalog
        return LyricsGuess(is_song=True, title="밤편지", artist="아이유")

    monkeypatch.setattr(identify_module, "guess_from_lyrics", _fake_guess)
    catalog = [CatalogEntry(song_id="a", title="밤편지", artist="아이유")]
    identify_song("가사 " * 20, True, catalog)
    assert seen["catalog"] is catalog
