"""가사 → 곡 식별. Claude로 (title, artist) 추측 후 노래책 카탈로그에 rapidfuzz 매칭.

2단계: 가사가 너무 짧거나 STT 환각(같은 구절 반복)이면 곧바로 스킵한다. 그렇지 않으면
Claude API로 곡을 추측하고, 추측 결과를 카탈로그(title/original_title/artist/alias)에
token_set_ratio로 매칭해 최고점을 낸다.

신곡을 songs 테이블에 자동 생성하지 않는다 — 매칭 실패는 항상 needs_review로 사람에게 넘긴다.
무거운 의존성(anthropic, rapidfuzz)은 함수 내부에서만 import.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from soopts.log import get_logger

log = get_logger("analyzers.identify")

MATCH_THRESHOLD = 85
MIN_LYRICS_CHARS = 30
CLAUDE_MODEL = "claude-sonnet-4-6"


@dataclass
class CatalogEntry:
    song_id: str
    title: str
    original_title: str | None = None
    artist: str | None = None
    aliases: list[str] | None = None


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


def parse_guess_json(text: str) -> tuple[str, str] | None:
    """Claude 응답에서 {"title":..,"artist":..} JSON을 뽑는다(코드펜스 제거 포함)."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
    artist = (data.get("artist") or "").strip() if isinstance(data, dict) else ""
    if not title:
        return None
    return title, artist


def guess_from_lyrics(lyrics: str, *, api_key: str | None = None) -> tuple[str, str] | None:
    """가사로 (title, artist) 추측. 무의미하거나 API가 모르겠다고 하면 None."""
    if looks_meaningless(lyrics):
        return None
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                "다음은 노래 가사 일부다(자동 전사라 오류가 있을 수 있음). 이 곡의 제목과 "
                '아티스트를 JSON으로만 답하라: {"title": "...", "artist": "..."}. '
                '모르겠으면 {"title": null, "artist": null}로 답하라.\n\n가사:\n' + lyrics
            ),
        }],
    )
    return parse_guess_json(resp.content[0].text)


def match_catalog(
    title: str, artist: str, catalog: list[CatalogEntry]
) -> tuple[CatalogEntry | None, float]:
    """카탈로그에서 title/original_title/artist/alias 중 최고점 매칭 항목을 찾는다."""
    from rapidfuzz import fuzz

    query = f"{title} {artist}".strip()
    best_entry: CatalogEntry | None = None
    best_score = 0.0
    for entry in catalog:
        candidates = [entry.title, entry.original_title, entry.artist, *(entry.aliases or [])]
        score = max((fuzz.token_set_ratio(query, c) for c in candidates if c), default=0.0)
        if score > best_score:
            best_entry, best_score = entry, score
    return best_entry, best_score


def identify_song(
    lyrics: str,
    song_likely: bool,
    catalog: list[CatalogEntry],
    *,
    api_key: str | None = None,
) -> IdentifyResult:
    """가사 → (song_id, title_guess, confidence, identify_status).

    - score >= 85 and song_likely      → auto_matched (song_id 연결)
    - score >= 85 and not song_likely  → needs_review  (song_id 미리 채워 검수 가속)
    - 그 외                             → needs_review  (song_id NULL)
    - songs 행은 여기서 생성하지 않는다 — 신곡 등록은 검수 페이지의 사람 몫.
    """
    guess = guess_from_lyrics(lyrics, api_key=api_key)
    if guess is None:
        return IdentifyResult(None, None, 0.0, "needs_review")
    title, artist = guess
    entry, score = match_catalog(title, artist, catalog)

    if entry is not None and score >= MATCH_THRESHOLD:
        status = "auto_matched" if song_likely else "needs_review"
        return IdentifyResult(entry.song_id, title, score, status)
    return IdentifyResult(None, title, score, "needs_review")
