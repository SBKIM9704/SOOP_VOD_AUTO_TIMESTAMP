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

추측할 때 노래책 카탈로그를 프롬프트에 함께 넣는다. BJ의 레퍼토리는 유한한 목록인데
예전에는 그걸 rapidfuzz에게만 주고 LLM은 맨몸으로 곡을 회상하게 했다 — 오픈북 시험을
클로즈북으로 보게 한 셈이다. STT는 반주에 섞인 가사를 자주 뭉개므로("그녀를 만나는 곳
100m 전" → "그녀를 만나는 곳입니다 전") 자유 회상은 실패하지만, 목록 대조는 성공한다.
반환 계약(title/artist 문자열)은 그대로여서 판정은 여전히 rapidfuzz가 한다 — LLM에게
song_id를 직접 고르게 하면 위의 "아티스트만 맞는데 확신 100%" 사고가 재발한다.

기본 모델은 qwen3.6-27b다. 실측(정답을 아는 전사 1건): gpt-oss-120b는 카탈로그를 줘도
못 찾았고(None), qwen3.6-27b는 카탈로그 없이 부분 일치, 카탈로그를 주면 정확히 맞혔다.
한국어 비중이 높은 작업이라 모델 체급보다 언어 커버리지가 갈랐다. 다만 표본이 1건이니
확정된 우열이 아니라 되돌릴 수 있게 해둔 선택으로 읽어라 —
SOOPTS_IDENTIFY_MODEL=openai/gpt-oss-120b 하나로 원복된다. 넓은 재검증은 needs_review에
사람이 정답을 붙여둔 건들로 하는 게 가장 정확하다.

주의: qwen3.6-27b는 Groq에서 **preview 등급**이고, Groq 문서는 preview 모델을 프로덕션에
쓰지 말라고 명시한다 — 예고 없이 내려갈 수 있다. 그래도 기본값으로 둔 건 정확도 차이가
컸기 때문이고, 대신 _create_completion이 "모델 없음" 에러일 때만 FALLBACK_MODEL(정식
등급)로 내려가 파이프라인이 멈추지 않게 한다. 폴백 없이 두면 모델이 내려간 순간 모든
VOD가 failed로 MAX_RETRIES를 태우고 큐가 밀린다.
같은 Groq를 쓰는 comment_timeline.py와 batch.py의 narrate_with_llm은 각자 모델을 고정해
둔다 — 저쪽은 한국어 곡 회상이 아니라 구조 추출/문장 다듬기라 이 A/B의 결론이 적용되지
않고, 여기 실험이 알림 문구까지 흔들면 안 된다. reasoning_effort는 모델마다 받는 값이 달라(gpt-oss는 "low",
qwen은 "none"/"default"만, 그 외는 미지원) _reasoning_kwargs가 모델에 맞춰 붙인다 —
gpt-oss 계열은 추론 토큰을 답변 전에 소비하므로 max_tokens에 여유가 필요하다
(실제로 겪음: json_validate_failed, failed_generation='').
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from soopts.log import get_logger

log = get_logger("analyzers.identify")

MATCH_THRESHOLD = 85
MIN_LYRICS_CHARS = 30
# 기본값이 gpt-oss가 아닌 이유는 모듈 docstring의 A/B 참고. 되돌리려면 환경변수만 주면
# 된다: SOOPTS_IDENTIFY_MODEL=openai/gpt-oss-120b
GROQ_MODEL = os.environ.get("SOOPTS_IDENTIFY_MODEL", "qwen/qwen3.6-27b")

# GROQ_MODEL이 사라졌을 때만 쓰는 그물. 정식(non-preview) 등급이라 갑자기 없어지지 않는다.
FALLBACK_MODEL = "openai/gpt-oss-120b"

# 카탈로그를 프롬프트에 넣을 상한. 넘으면 블록을 통째로 생략한다 — 앞에서 잘라내면
# 하필 정답이 잘려나가도 조용히 오답이 되므로, 차라리 맨몸 추측으로 떨어뜨린다.
CATALOG_PROMPT_MAX = 1200

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


def _reasoning_kwargs(model: str) -> dict[str, str]:
    """모델이 받는 reasoning_effort 값만 골라 넘긴다 — 안 받는 값이면 400이 난다.

    gpt-oss는 "low"를 받지만 qwen은 `must be one of \\`none\\` or \\`default\\``로 거절한다.
    SOOPTS_IDENTIFY_MODEL로 모델을 갈아끼우며 A/B할 수 있어야 하므로 호출부가 아니라
    여기서 모델에 맞춰 분기한다.
    """
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low"}
    if model.startswith("qwen/"):
        return {"reasoning_effort": "none"}
    return {}


def _looks_like_model_unavailable(ex: Exception) -> bool:
    """모델이 사라져서 난 에러인가? (rate limit·서버오류 등 일시적 실패와 구분)

    좁게 본다 — 아무 에러에나 폴백하면 429가 났을 때 매 호출이 두 번씩 나가고, 진짜
    장애가 폴백 뒤에 숨는다. 여기서 안 걸린 예외는 예전처럼 그대로 올라가 region 실패 →
    재시도 경로를 탄다.
    """
    msg = str(ex).lower()
    return any(k in msg for k in ("model_not_found", "decommissioned", "does not exist", "404"))


def _create_completion(client, **kwargs):
    """GROQ_MODEL로 호출하되, 그 모델이 사라졌으면 FALLBACK_MODEL로 한 번 더 시도한다.

    기본 모델(qwen)이 Groq의 preview 등급이라 예고 없이 내려갈 수 있는데, 그때 identify가
    예외를 던지면 VOD가 failed로 떨어져 MAX_RETRIES를 태우고 큐까지 밀린다. 모델이 없어진
    것뿐인데 파이프라인이 멈출 이유는 없다 — 정식 모델로 내려가 계속 돌리고, 정확도 저하는
    경고 로그로 남긴다(이 저장소가 여러 번 확인한 "실패는 조용히 잃지 말고 드러내라" 방식).
    reasoning_effort는 모델마다 받는 값이 달라 폴백 시 다시 계산해야 한다.
    """
    kwargs.pop("model", None)
    for key in ("reasoning_effort",):
        kwargs.pop(key, None)
    try:
        return client.chat.completions.create(
            model=GROQ_MODEL, **_reasoning_kwargs(GROQ_MODEL), **kwargs
        )
    except Exception as ex:  # noqa: BLE001
        if GROQ_MODEL == FALLBACK_MODEL or not _looks_like_model_unavailable(ex):
            raise
        log.warning(
            "identify 모델 %s 사용 불가(%s) — %s로 폴백한다. 한국어 곡 식별 정확도가 "
            "떨어지므로 SOOPTS_IDENTIFY_MODEL을 살아있는 모델로 지정하라.",
            GROQ_MODEL,
            ex,
            FALLBACK_MODEL,
        )
        return client.chat.completions.create(
            model=FALLBACK_MODEL, **_reasoning_kwargs(FALLBACK_MODEL), **kwargs
        )


def _catalog_line(entry: CatalogEntry) -> str:
    """카탈로그 한 줄. original_title까지 보여준다 — _score_catalog가 채점하는 것과 맞춘다.

    채점은 title/original_title/alias 전부를 후보로 보는데(_score_catalog 참고) 프롬프트에
    title만 넣으면, 원제가 영문인 곡에서 LLM이 알아볼 단서를 못 받는다. alias는 넣지 않는다 —
    alias는 LLM이 뱉은 *출력*을 카탈로그에 붙이기 위한 것이지 인식 단서가 아니라서,
    블록만 몇 배로 부풀리고 CATALOG_PROMPT_MAX 상한을 앞당길 뿐이다.
    """
    line = f"- {entry.title}"
    if entry.original_title and entry.original_title != entry.title:
        line += f" ({entry.original_title})"
    if entry.artist:
        line += f" / {entry.artist}"
    return line


def _catalog_prompt_block(catalog: list[CatalogEntry] | None) -> str:
    """노래책을 프롬프트에 넣을 블록으로. 비었거나 너무 크면 빈 문자열(=맨몸 추측)."""
    if not catalog:
        return ""
    if len(catalog) > CATALOG_PROMPT_MAX:
        log.info(
            "카탈로그 %d곡 — 상한 %d 초과라 프롬프트에 넣지 않음(맨몸 추측으로 진행)",
            len(catalog),
            CATALOG_PROMPT_MAX,
        )
        return ""
    lines = "\n".join(_catalog_line(e) for e in catalog)
    return (
        "\n\n아래는 이 BJ가 부른 적 있는 곡 목록이다. 가사가 이 중 하나로 보이면 "
        "목록에 적힌 제목을 그대로 옮겨 답하라(전사 오류로 가사가 뭉개졌어도 음이 "
        "비슷한 항목을 찾아라). 목록에 없는 곡일 수도 있으니 억지로 끼워맞추지는 "
        "말고, 그럴 땐 아는 대로 추측하거나 title을 null로 두라.\n" + lines
    )


def guess_from_lyrics(
    lyrics: str,
    *,
    catalog: list[CatalogEntry] | None = None,
    api_key: str | None = None,
) -> LyricsGuess | None:
    """STT 전사 텍스트 → 노래 여부 판정 + (노래라면) (title, artist) 추측, 한 번의 Groq 호출로.

    STT는 노래가 아닌 방송 잡담/설명도 그대로 옮기므로(예: "가사 왜 표시해놓은거 어디갔지?
    저장을 안 했나" — 30자 이상, 단어도 다양해 looks_meaningless를 통과하지만 명백히 가사가
    아님. 실제 프로덕션에서 이런 잡담이 노래 후보로 잘못 흘러간 사례가 있었다) 길이/반복
    휴리스틱만으로는 "말은 되지만 노래는 아닌" 케이스를 못 거른다. 이 판정을 title/artist
    추측과 한 호출에 합쳐서 API 콜을 늘리지 않는다.

    catalog를 주면 후보 목록을 프롬프트에 함께 넣어 자유 회상을 목록 대조로 바꾼다
    (모듈 docstring 참고). 반환 계약은 catalog 유무와 무관하게 동일하다.
    """
    if looks_meaningless(lyrics):
        return None
    from groq import Groq

    client = Groq(api_key=api_key)
    resp = _create_completion(
        client,
        max_tokens=500,
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
                "노래가 아니면 is_song은 false, title/artist는 null로 답하라."
                + _catalog_prompt_block(catalog)
                + "\n\n텍스트:\n"
                + lyrics
            ),
        }],
    )
    return parse_song_guess_json(resp.choices[0].message.content)


def _score_catalog(title: str, artist: str, catalog: list[CatalogEntry]) -> list[ScoredEntry]:
    """title/artist를 분리 채점한다 — artist는 절대 title 없이 단독으로 점수를 만들지 못한다.

    title_score: 추측 title을 카탈로그 항목의 title/original_title/alias에만 비교.
    artist_score: 추측 artist를 카탈로그 항목의 artist에만 비교.
    """
    from rapidfuzz import fuzz

    title, artist = (title or "").strip(), (artist or "").strip()
    scored: list[ScoredEntry] = []
    for entry in catalog:
        title_candidates = [entry.title, entry.original_title, *(entry.aliases or [])]
        title_score = (
            max((fuzz.token_set_ratio(title, c) for c in title_candidates if c), default=0.0)
            if title
            else 0.0
        )
        artist_score = (
            fuzz.token_set_ratio(artist, entry.artist) if artist and entry.artist else 0.0
        )
        scored.append(ScoredEntry(entry, title_score, artist_score))
    return scored


def match_catalog(
    title: str, artist: str, catalog: list[CatalogEntry]
) -> tuple[CatalogEntry | None, float]:
    """카탈로그에서 title 유사도가 최고인 항목을 찾는다. artist는 title_score가 근소하게
    동률인 후보들 사이의 tiebreak로만 쓰인다 — 단독으로 점수를 좌우하지 못한다."""
    scored = _score_catalog(title, artist, catalog)
    if not scored:
        return None, 0.0
    top = max(s.title_score for s in scored)
    tied = [s for s in scored if top - s.title_score <= TIEBREAK_MARGIN]
    tied.sort(key=lambda s: s.artist_score, reverse=True)
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
        resp = _create_completion(
            client,
            max_tokens=200,
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
    guess = guess_from_lyrics(lyrics, catalog=catalog, api_key=api_key)
    if guess is None:
        return IdentifyResult(None, None, 0.0, "needs_review")
    if not guess.is_song:
        return None
    if not guess.title:
        return IdentifyResult(None, None, 0.0, "needs_review")
    return resolve_song_match(
        guess.title, guess.artist or "", lyrics, song_likely, catalog, api_key=api_key
    )
