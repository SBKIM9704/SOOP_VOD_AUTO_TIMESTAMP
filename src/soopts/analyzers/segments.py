"""전사 세그먼트 파일 — 곡 구간 가사를 모델 출력에 싣지 않고 도구끼리 넘기는 통로.

`soopts transcribe --segments --save <파일>`이 세그먼트(`[{start,end,text}]`, VOD 절대초)를
파일로 남기면, `set-perf`/`add-song`/`match-song`의 `--lyrics-from <파일>`이 곡 구간에 해당하는
텍스트만 잘라 가사로 쓴다.

**왜 이런 우회로가 있는가 — 가사를 Claude가 직접 쓰면 응답이 출력 필터에 막힌다.** vod-review의
perf 단계는 원래 `--lyrics "<정확한 가사>"`처럼 Claude가 가사를 인자로 써넣게 했다. Claude는
Whisper 오인식을 고치느라 공개된 가사를 기억에서 되살려 적었고, 곡마다 수십 단어씩 저작권
가사를 원문 그대로 출력하게 됐다. 이것이 API의 출력 콘텐츠 필터에 걸려 작업이 반복적으로
중단됐다(2026-09 백필 중 2회 — 둘 다 가사가 든 `set-perf`를 쓰기 직전). 가사가 파일→코드→DB로만
흐르면 모델 출력에는 perf id와 초 같은 숫자만 남는다.

저장되는 건 공개 가사가 아니라 **실제로 들린 전사문**이다(오인식 포함). `performances.lyrics_snippet`은
이 레포에서 `has_lyrics` 판정 외에 읽는 곳이 없고, 로컬 ingest 경로도 원래 전사문을 저장하므로
두 경로가 오히려 같은 성격의 값을 갖게 된다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_segments(path: Path) -> list[dict[str, Any]]:
    """`transcribe --segments --save`가 쓴 파일을 읽는다. 형식이 틀리면 ValueError.

    다른 JSON(예: ingest의 `{"songs": [...]}`)을 잘못 넘기면 조용히 빈 가사가 되는 대신
    여기서 멈춘다.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(
        isinstance(s, dict) and {"start", "end", "text"} <= s.keys() for s in data
    ):
        raise ValueError(f"세그먼트 파일 형식이 아닙니다(`[{{start,end,text}}]` 필요): {path}")
    return data


def lyrics_in_span(segments: list[dict[str, Any]], start_s: float, end_s: float) -> str:
    """[start_s, end_s]와 **겹치는** 세그먼트의 텍스트를 시각순으로 이어 붙인다(순수 함수).

    완전 포함이 아니라 겹침으로 고르는 이유: 곡의 첫·끝 소절은 세그먼트가 구간 경계를 걸치는 일이
    흔해, 완전 포함만 받으면 첫 줄과 마지막 줄이 빠진다. 대신 경계에 걸친 잡담 한 줄이 섞일 수
    있지만, 이 값은 곡 판정이나 유튜브 게이트에 쓰이지 않는 참고값이라 소절을 잃는 쪽보다 낫다.
    경계에 딱 닿기만 한 세그먼트(끝 == start)는 겹침으로 치지 않는다.

    구간이 비었으면(`end_s <= start_s`, daily가 남긴 센티넬) 빈 문자열 — 호출부가 거절한다.
    """
    if end_s <= start_s:
        return ""
    picked = sorted(
        (s for s in segments if s["end"] > start_s and s["start"] < end_s),
        key=lambda s: s["start"],
    )
    return " ".join(t for s in picked if (t := str(s.get("text") or "").strip()))
