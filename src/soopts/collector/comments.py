"""VOD 댓글 조회 — 팬이 남긴 노래 타임라인 댓글을 찾기 위한 용도.

chapi.sooplive.co.kr의 비공식 댓글 API. 실제 캡처로 검증됐다(2026-07-16, VOD 201651295):
  - GET https://chapi.sooplive.co.kr/api/{bj_id}/title/{title_no}/comment?page={n}
  - 응답은 data[] 배열(댓글 최신순으로 추정) + meta{current_page,last_page,total}.
  - 댓글 수가 대개 몇 개뿐이라(관찰된 VOD 기준 2~4개) 첫 페이지만 조회한다.
"""

from __future__ import annotations

import requests

from soopts.config import Config
from soopts.log import get_logger

log = get_logger("collector.comments")

_COMMENT_URL = "https://chapi.sooplive.co.kr/api/{bj_id}/title/{title_no}/comment"


def extract_comments(data: dict) -> list[str]:
    """응답 JSON → 댓글 본문 목록. 순수 함수."""
    return [c["comment"] for c in data.get("data", []) if c.get("comment")]


def fetch_comments(cfg: Config, bj_id: str, title_no: str) -> list[str]:
    """VOD 댓글 본문 목록(첫 페이지). 실패 시 예외를 그대로 전파한다(호출부가 처리)."""
    resp = requests.get(
        _COMMENT_URL.format(bj_id=bj_id, title_no=title_no),
        params={"page": 1},
        headers={"User-Agent": cfg.collector.user_agent},
        timeout=cfg.collector.timeout_s,
    )
    resp.raise_for_status()
    return extract_comments(resp.json())
