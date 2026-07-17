"""댓글에서 노래 타임라인 추출 — 팬이 자원해서 남기는 비공식 타임라인 댓글은 이모지·형식이
사람마다 달라 정규식으로 안정적으로 파싱할 수 없다. LLM(Groq)에게 댓글 전체를 주고
"노래를 불렀다"고 볼 수 있는 항목만 골라 JSON으로 뽑아낸다.

이 타임라인이 발견되면(스티커 반응 기반 추측 없이) 시각·아티스트·제목이 이미 확정돼
있으므로, 노래 감지 자체를 이걸로 대체할 수 있다 — 다만 사람이 실수/누락했을 수 있어
`batch.py`는 댓글에 타임라인이 없을 때만 기존 스티커 감지로 폴백한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from soopts.log import get_logger

log = get_logger("analyzers.comment_timeline")

GROQ_MODEL = "openai/gpt-oss-120b"

_PROMPT = (
    "다음은 SOOP 방송 다시보기 VOD에 시청자들이 남긴 댓글들이다(각 댓글은 '---'로 "
    "구분돼 있다). 이 중 방송 타임라인을 정리한 댓글이 있을 수 있다(이모지·시각·주제를 "
    "나열하는 형식이며 작성자마다 컨벤션이 다르다). 그 안에서 'BJ가 노래를 불렀다'고 "
    "볼 수 있는 항목만 골라 JSON으로 답하라 — 잡담/토픽/게임 등 노래가 아닌 항목은 "
    "절대 포함하지 마라.\n"
    '반드시 이 형식의 JSON 객체 하나로만 답하라(다른 텍스트 금지): '
    '{"songs": [{"time": "HH:MM:SS", "artist": "..." 또는 null, "title": "..."}]}\n'
    '노래 타임라인이 전혀 없으면 {"songs": []}로 답하라.\n\n'
)


@dataclass
class TimelineSong:
    time_s: int
    title: str
    artist: str | None = None


def hms_to_s(hms: str) -> int:
    """"HH:MM:SS"/"MM:SS" → 초. 순수 함수."""
    parts = [int(p) for p in hms.strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse_timeline_json(text: str) -> list[TimelineSong]:
    """LLM 응답(코드펜스 있을 수 있음) → TimelineSong 리스트. 순수 함수.

    `{"songs": [...]}` 객체(Groq의 json_object 모드는 최상위 배열을 허용하지 않아
    이 형태로 요청함)와 최상위 배열 `[...]` 둘 다 받아들인다.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("songs")
    if not isinstance(data, list):
        return []

    out: list[TimelineSong] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        time_raw, title = item.get("time"), item.get("title")
        if not time_raw or not title:
            continue
        try:
            time_s = hms_to_s(str(time_raw))
        except ValueError:
            continue
        out.append(TimelineSong(time_s=time_s, title=str(title), artist=item.get("artist") or None))
    return out


def extract_song_timeline(comments: list[str], *, api_key: str | None = None) -> list[TimelineSong]:
    """댓글 목록 중 노래 타임라인이 있으면 곡만 뽑아 반환. 없으면 빈 리스트."""
    if not comments:
        return []
    from groq import Groq

    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=1000,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": _PROMPT + "\n\n---\n\n".join(comments)}],
    )
    songs = parse_timeline_json(resp.choices[0].message.content)
    if songs:
        log.info("댓글 타임라인에서 노래 %d곡 발견", len(songs))
    return songs
