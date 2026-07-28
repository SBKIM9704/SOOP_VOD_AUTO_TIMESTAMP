"""가사 → 곡 식별. Groq(무료 티어, 카드 불요)로 (title, artist) 추측 후 노래책 카탈로그에
rapidfuzz 매칭.

2단계: 가사가 너무 짧거나 STT 환각(같은 구절 반복)이면 곧바로 스킵한다. 그렇지 않으면
LLM API로 곡을 추측하고, 추측 결과를 카탈로그(title/original_title/artist/alias)에
token_set_ratio로 매칭해 최고점을 낸다. Anthropic(결제 필요)·Gemini(2026-03 정책 변경으로
결제 활성화 강제)를 거쳐 Groq로 정착 — 순수 rate limit만 있고 결제 계정 자체가 안 붙어
이런 문제가 없다. 매칭은 최종적으로 rapidfuzz가 하므로 LLM의 다국어(한국어) 처리가
다소 아쉬워도 실패 시 그냥 needs_review로 떨어질 뿐 데이터 오염은 없다.

title과 artist는 반드시 분리해서 채점한다 — 과거에 title/artist/alias를 한 쿼리로 합쳐
독립 후보로 놓고 최고점만 취했더니, 아티스트 이름만 강하게 일치해도(예: "아이유") 제목이
완전히 다른 곡("애타는 마음" vs 실제로는 "unlucky")에 100% 확신으로 매칭되는 사고가
프로덕션에서 실제로 발생했다(검수자가 나중에 발견해 수동으로 되돌림). artist는 title
없이 단독으로 매칭을 성립시킬 수 없고, title 점수가 동률에 가까운 후보들 사이의
tiebreak로만 쓰인다.

신곡을 songs 테이블에 자동 생성하지 않는다 — 매칭 실패는 항상 needs_review로 사람에게 넘긴다.
무거운 의존성(groq, rapidfuzz)은 함수 내부에서만 import.

GROQ_MODEL(gpt-oss 계열)은 추론(reasoning) 모델이라 답변 전에 내부적으로 생각하는 토큰을
먼저 소비한다 — max_tokens를 너무 작게 잡으면 추론만 하다 끝나 답변이 통째로 비어버린다
(실제로 겪음: json_validate_failed, failed_generation=''). reasoning_effort="low"로
추론량을 줄이고 max_tokens에 여유를 둬서 우회한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from soopts.log import get_logger

log = get_logger("analyzers.identify")

MATCH_THRESHOLD = 85
MIN_LYRICS_CHARS = 30
GROQ_MODEL = "openai/gpt-oss-120b"

# 애매한 구간(1차 판정 실패) 중 Groq 보완 확인을 시도할지 판단하는 기준.
# WEAK_TITLE_FLOOR: title_score가 이 값 미만이면 후보 자체에서 제외한다 — 실제 프로덕션
# 버그(아티스트만 맞고 title_score=0)가 이 게이트를 절대 통과하지 못하게 하는 안전장치.
WEAK_TITLE_FLOOR = 25.0
DISAMBIG_MARGIN = 10.0
ARTIST_STRONG = 90.0
SHORTLIST_MAX = 6
TIEBREAK_MARGIN = 1.0


@dataclass
class CatalogEntry:
    song_id: str
    title: str
    original_title: str | None = None
    artist: str | None = None
    aliases: list[str] | None = None


@dataclass
class ScoredEntry:
    entry: CatalogEntry
    title_score: float
    artist_score: float
    # token_set_ratio는 "여분 토큰"을 무시하므로 부분집합 제목이 만점을 받는다("애타는 마음"
    # 대 "마음" = 100). 판정 기준은 그대로 두되(장식이 붙은 제목을 관대하게 받아야 한다),
    # **동점자 사이의 순서**를 정할 때 전체 토큰을 보는 sort 점수로 가른다. 기본값 0.0은
    # ScoredEntry를 직접 만드는 호출부(테스트 포함) 호환용.
    title_sort: float = 0.0


@dataclass
class LyricsGuess:
    is_song: bool
    title: str | None = None
    artist: str | None = None


@dataclass
class IdentifyResult:
    song_id: str | None
    title_guess: str | None
    match_confidence: float
    identify_status: str  # "auto_matched" | "needs_review"


def looks_meaningless(lyrics: str) -> bool:
    """가사가 너무 짧거나(<30자) STT 환각(같은 단어 반복)이면 식별을 시도할 가치가 없다."""
    text = lyrics.strip()
    if len(text) < MIN_LYRICS_CHARS:
        return True
    words = text.split()
    return len(words) >= 4 and len(set(words)) <= 2


def parse_song_guess_json(text: str) -> LyricsGuess | None:
    """Groq 응답에서 {"is_song":bool,"title":..,"artist":..} JSON을 뽑는다(코드펜스 제거 포함)."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("is_song"), bool):
        return None
    if not data["is_song"]:
        return LyricsGuess(is_song=False)
    title = (data.get("title") or "").strip() or None
    artist = (data.get("artist") or "").strip() or None
    return LyricsGuess(is_song=True, title=title, artist=artist)


def guess_from_lyrics(lyrics: str, *, api_key: str | None = None) -> LyricsGuess | None:
    """STT 전사 텍스트 → 노래 여부 판정 + (노래라면) (title, artist) 추측, 한 번의 Groq 호출로.

    STT는 노래가 아닌 방송 잡담/설명도 그대로 옮기므로(예: "가사 왜 표시해놓은거 어디갔지?
    저장을 안 했나" — 30자 이상, 단어도 다양해 looks_meaningless를 통과하지만 명백히 가사가
    아님. 실제 프로덕션에서 이런 잡담이 노래 후보로 잘못 흘러간 사례가 있었다) 길이/반복
    휴리스틱만으로는 "말은 되지만 노래는 아닌" 케이스를 못 거른다. 이 판정을 title/artist
    추측과 한 호출에 합쳐서 API 콜을 늘리지 않는다.
    """
    if looks_meaningless(lyrics):
        return None
    from groq import Groq

    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=500,
        reasoning_effort="low",
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": (
                "다음은 방송 다시보기에서 자동 전사(STT)된 텍스트 일부다(오류가 있을 수 있음). "
                "이 텍스트가 실제로 노래를 부르는 가사인지, 방송 잡담/설명/멘트인지 먼저 "
                "판단하라. 노래라면 제목과 아티스트를 추측하라(제목을 모르겠으면 노래인 건 "
                '확실해도 title은 null로 답하라).\n'
                '반드시 이 JSON 형식으로만 답하라: {"is_song": true 또는 false, '
                '"title": "..." 또는 null, "artist": "..." 또는 null}\n'
                "노래가 아니면 is_song은 false, title/artist는 null로 답하라.\n\n"
                "텍스트:\n" + lyrics
            ),
        }],
    )
    return parse_song_guess_json(resp.choices[0].message.content)


def _score_catalog(title: str, artist: str, catalog: list[CatalogEntry]) -> list[ScoredEntry]:
    """title/artist를 분리 채점한다 — artist는 절대 title 없이 단독으로 점수를 만들지 못한다.

    title_score: 추측 title을 카탈로그 항목의 title/original_title/alias에만 비교.
    artist_score: 추측 artist를 카탈로그 항목의 artist에만 비교.
    title_sort:   같은 후보 문자열에 대한 token_sort_ratio — 동점 해소 전용(아래 참조).

    token_set_ratio는 교집합이 완전히 일치하면 여분 토큰을 무시하고 100을 준다. 그래서
    부분집합 제목이 정답과 똑같이 만점을 받는다(`"애타는 마음"` 대 카탈로그 `"마음"` = 100).
    임계값 판정에는 이 관대함이 필요하지만(장식·부제가 붙은 제목도 받아야 한다), 그 상태로
    동점이 되면 승자가 카탈로그 순서로 정해진다. 전체 토큰을 보는 token_sort_ratio를 같이
    들고 다니며(`"애타는 마음"` 대 `"마음"` = 50, 대 `"애타는 마음"` = 100) match_catalog가
    동점자를 가를 때 쓴다.
    """
    from rapidfuzz import fuzz

    title, artist = (title or "").strip(), (artist or "").strip()
    scored: list[ScoredEntry] = []
    for entry in catalog:
        title_candidates = [entry.title, entry.original_title, *(entry.aliases or [])]
        # (set, sort) 튜플의 max — set이 최고인 후보를 고르되, set이 같으면 sort가 높은 쪽을
        # 대표로 삼는다. 두 점수가 서로 다른 후보 문자열에서 나오면 안 되므로 함께 뽑는다.
        title_score, title_sort = (
            max(
                (
                    (float(fuzz.token_set_ratio(title, c)), float(fuzz.token_sort_ratio(title, c)))
                    for c in title_candidates
                    if c
                ),
                default=(0.0, 0.0),
            )
            if title
            else (0.0, 0.0)
        )
        artist_score = (
            fuzz.token_set_ratio(artist, entry.artist) if artist and entry.artist else 0.0
        )
        scored.append(ScoredEntry(entry, title_score, artist_score, title_sort))
    return scored


def match_catalog(
    title: str, artist: str, catalog: list[CatalogEntry]
) -> tuple[CatalogEntry | None, float]:
    """카탈로그에서 title 유사도가 최고인 항목을 찾는다. artist는 title_score가 근소하게
    동률인 후보들 사이의 tiebreak로만 쓰인다 — 단독으로 점수를 좌우하지 못한다.

    동점자는 **title_sort 우선, 그다음 artist_score**로 가른다. title_score(token_set_ratio)는
    부분집합 제목에 만점을 주므로 "애타는 마음"이 카탈로그의 "마음"과 "애타는 마음" 양쪽에
    100으로 걸린다. artist까지 동점이면(추측 `"울랄라세션, 아이유"`는 카탈로그 `"아이유"`와도
    token_set 100이다) 승자가 카탈로그 순서로 정해져, 실제로 `애타는 마음`이 `마음/아이유`에
    연결돼 유튜브 합본 오버레이에 잘못된 곡명이 박혔다. 전체 토큰을 보는 title_sort를 첫 키로
    두면 부분집합 후보가 뒤로 밀린다(50 대 100).
    """
    scored = _score_catalog(title, artist, catalog)
    if not scored:
        return None, 0.0
    top = max(s.title_score for s in scored)
    tied = [s for s in scored if top - s.title_score <= TIEBREAK_MARGIN]
    tied.sort(key=lambda s: (s.title_sort, s.artist_score), reverse=True)
    best = tied[0]
    return best.entry, best.title_score


def _build_disambiguation_shortlist(scored: list[ScoredEntry]) -> list[ScoredEntry]:
    """1차 판정(threshold 미달)이 애매한 경우에만 Groq 보완 확인 대상으로 삼을 후보를 고른다.

    WEAK_TITLE_FLOOR 미만인 항목은 절대 포함하지 않는다 — 이게 바로 "아티스트만 맞고
    제목은 완전히 무관"한 프로덕션 버그 패턴(title_score=0)을 걸러내는 안전장치다.
    """
    if not scored:
        return []
    top_title = max(s.title_score for s in scored)
    candidates = [
        s
        for s in scored
        if s.title_score >= WEAK_TITLE_FLOOR
        and (
            top_title - s.title_score <= DISAMBIG_MARGIN
            or s.artist_score >= ARTIST_STRONG
        )
    ]
    candidates.sort(key=lambda s: (s.title_score, s.artist_score), reverse=True)
    return candidates[:SHORTLIST_MAX]


def disambiguate_with_llm(
    lyrics: str, shortlist: list[ScoredEntry], *, api_key: str | None = None
) -> ScoredEntry | None:
    """가사 + 후보 목록을 Groq에 주고 정답을 고르게 한다 — 애매한 fuzzy 매칭의 보완용.

    실패/불확실 시 예외를 던지지 않고 None을 반환한다(=needs_review로 낙하) — 기존
    guess_from_lyrics와 동일한 "실패해도 데이터 오염 없음" 철학을 그대로 따른다.
    """
    if not shortlist:
        return None
    from groq import Groq

    options = "\n".join(
        f"{i}: 제목={s.entry.title!r} 아티스트={s.entry.artist!r}"
        for i, s in enumerate(shortlist)
    )
    prompt = (
        "다음은 노래 가사 일부(자동 전사라 오류가 있을 수 있음)와 후보 곡 목록이다. "
        "가사와 일치하는 곡의 번호를 골라라. 확신이 없거나 후보 중에 없으면 "
        'index를 null로 답하라.\n반드시 이 형식의 JSON으로만 답하라: {"index": 0 또는 null}\n\n'
        f"가사:\n{lyrics}\n\n후보:\n{options}"
    )
    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=200,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            resp.choices[0].message.content.strip(),
            flags=re.MULTILINE,
        )
        data = json.loads(cleaned)
        idx = data.get("index") if isinstance(data, dict) else None
        if isinstance(idx, int) and 0 <= idx < len(shortlist):
            return shortlist[idx]
        return None
    except Exception as ex:  # noqa: BLE001
        log.warning("disambiguate_with_llm 실패 — needs_review로 낙하: %s", ex)
        return None


def resolve_song_match(
    title: str,
    artist: str,
    lyrics: str,
    song_likely: bool,
    catalog: list[CatalogEntry],
    *,
    api_key: str | None = None,
) -> IdentifyResult:
    """(title, artist) 추측(가사 기반이든 댓글 힌트든) → 최종 IdentifyResult.

    - score >= 85 and song_likely      → auto_matched (song_id 연결)
    - score >= 85 and not song_likely  → needs_review  (song_id 미리 채워 검수 가속)
    - score < 85 인데 애매한 구간이면  → Groq에 한 번 더 확인(disambiguate_with_llm)
    - 그 외                             → needs_review  (song_id NULL)
    - songs 행은 여기서 생성하지 않는다 — 신곡 등록은 검수 페이지의 사람 몫.
    """
    scored = _score_catalog(title, artist, catalog)
    entry, score = match_catalog(title, artist, catalog)

    if not (entry is not None and score >= MATCH_THRESHOLD):
        shortlist = _build_disambiguation_shortlist(scored)
        if shortlist and lyrics:
            choice = disambiguate_with_llm(lyrics, shortlist, api_key=api_key)
            if choice is not None:
                entry, score = choice.entry, max(choice.title_score, MATCH_THRESHOLD)

    if entry is not None and score >= MATCH_THRESHOLD:
        status = "auto_matched" if song_likely else "needs_review"
        return IdentifyResult(entry.song_id, title, score, status)
    return IdentifyResult(None, title, score, "needs_review")


def identify_song(
    lyrics: str,
    song_likely: bool,
    catalog: list[CatalogEntry],
    *,
    api_key: str | None = None,
) -> IdentifyResult | None:
    """가사(STT 전사) → IdentifyResult, 단 Groq가 '노래 아님'으로 확정 판단하면 None.

    None은 호출부(_process_vod)에게 "이 구간은 노래가 아니므로 DB에 기록조차 하지 않고
    통째로 건너뛴다"는 신호다 — 기존 `clip is None` 스킵과 같은 선상이며, "추측 실패라
    needs_review로 기록은 남긴다"는 것과는 다른 상태이니 호출부에서 혼동하지 않는다.
    신곡을 songs 테이블에 자동 생성하지 않는다는 원칙은 그대로다 — 판정 로직은
    resolve_song_match에 위임한다(댓글 힌트 경로와 판정 기준을 공유하기 위함).
    """
    guess = guess_from_lyrics(lyrics, api_key=api_key)
    if guess is None:
        return IdentifyResult(None, None, 0.0, "needs_review")
    if not guess.is_song:
        return None
    if not guess.title:
        return IdentifyResult(None, None, 0.0, "needs_review")
    return resolve_song_match(
        guess.title, guess.artist or "", lyrics, song_likely, catalog, api_key=api_key
    )
