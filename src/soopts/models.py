"""핵심 데이터 구조 (노래 전용).

- ChatMsg   : chat.jsonl 한 줄 (스티커 반응 계산에 필요)
- MetaResult: VOD 메타 (제목/BJ/파트 duration)
- Song      : 감지·식별된 노래 구간
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


# --------------------------------------------------------------------------- #
# VOD 메타
# --------------------------------------------------------------------------- #
@dataclass
class MetaPart:
    idx: int
    file_info_key: str
    duration: int      # 파트 길이(초)
    offset_s: int      # 전역 타임라인 시작 오프셋(초)


@dataclass
class MetaResult:
    vod_id: str
    title: str
    bj_id: str
    bj_nick: str
    total_duration: int
    parts: list[MetaPart] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> MetaResult:
        d = dict(d)
        d["parts"] = [MetaPart(**p) for p in d.get("parts", [])]
        return MetaResult(**d)


def write_meta(path: Path, meta: MetaResult) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta.to_dict(), fh, ensure_ascii=False, indent=2)


def read_meta(path: Path) -> MetaResult:
    with open(path, encoding="utf-8") as fh:
        return MetaResult.from_dict(json.load(fh))


# --------------------------------------------------------------------------- #
# ChatMsg
# --------------------------------------------------------------------------- #
@dataclass
class ChatMsg:
    key: str          # dedup 키 (part|t_local|nick|msg 해시)
    part: int
    t: int            # 전역 타임라인 초
    t_local: int      # 파트 내 초 (API 원본)
    kind: str         # "chat" | "ogq"(스티커)
    nick: str
    user_id: str
    msg: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(line: str) -> ChatMsg:
        return ChatMsg(**json.loads(line))


def make_chat_key(part: int, t_local: int, nick: str, msg: str) -> str:
    raw = f"{part}|{t_local}|{nick}|{msg}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def write_chat_jsonl(path: Path, msgs: list[ChatMsg]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for m in msgs:
            fh.write(m.to_json() + "\n")


def read_chat_jsonl(path: Path) -> list[ChatMsg]:
    out: list[ChatMsg] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(ChatMsg.from_json(line))
    return out


# --------------------------------------------------------------------------- #
# Song
# --------------------------------------------------------------------------- #
@dataclass
class Song:
    t: int                       # 시작 시각(전역초)
    end: int                     # 끝 시각(전역초)
    duration: int
    sticker_rate: float          # 분당 스티커 수 (노래 반응 세기)
    song_likely: bool            # 스티커 반응 강함 = 떼창곡 유력
    lyrics: str = ""             # STT 전사 가사 (곡 식별 단서)
    title: str | None = None     # 식별된 곡명 (LLM/수동)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Song:
        return Song(**d)


def write_songs(path: Path, songs: list[Song], vod_id: str) -> None:
    payload = {
        "schema": SCHEMA_VERSION,
        "vod_id": vod_id,
        "songs": [s.to_dict() for s in songs],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def read_songs(path: Path) -> list[Song]:
    with open(path, encoding="utf-8") as fh:
        return [Song.from_dict(s) for s in json.load(fh).get("songs", [])]
