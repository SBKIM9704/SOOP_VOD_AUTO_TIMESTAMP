"""VOD 댓글 조회 — 팬이 남긴 노래 타임라인 댓글을 찾기 위한 용도.

chapi.sooplive.co.kr의 비공식 댓글 API. 실제 캡처로 검증됐다(2026-07-16, VOD 201651295):
  - GET https://chapi.sooplive.co.kr/api/{bj_id}/title/{title_no}/comment?page={n}
  - 응답은 data[] 배열(댓글 최신순으로 추정) + meta{current_page,last_page,total}.
  - 댓글이 최신순이라 타임라인 댓글(오래전에 달렸을 수 있음)이 뒷페이지에 있을 수 있다 —
    첫 페이지만 보면 인기 VOD에서 놓칠 수 있어 meta.last_page까지 전부 순회한다.
    폭주 방지용으로 _MAX_PAGES를 안전판으로 둔다.
"""

from __future__ import annotations

import requests

from soopts.config import Config
from soopts.log import get_logger

log = get_logger("collector.comments")

_COMMENT_URL = "https://chapi.sooplive.co.kr/api/{bj_id}/title/{title_no}/comment"
_MAX_PAGES = 20


def extract_comments(data: dict) -> list[str]:
    """응답 JSON → 댓글 본문 목록. 순수 함수."""
    return [c["comment"] for c in data.get("data", []) if c.get("comment")]


def fetch_comments(cfg: Config, bj_id: str, title_no: str) -> list[str]:
    """VOD 댓글 본문 목록(전체 페이지, 최대 _MAX_PAGES). 실패 시 예외를 그대로 전파한다(호출부가 처리)."""
    comments: list[str] = []
    page = 1
    while True:
        resp = requests.get(
            _COMMENT_URL.format(bj_id=bj_id, title_no=title_no),
            params={"page": page},
            headers={"User-Agent": cfg.collector.user_agent},
            timeout=cfg.collector.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        comments.extend(extract_comments(data))
        last_page = data.get("meta", {}).get("last_page", 1)
        if page >= last_page or page >= _MAX_PAGES:
            break
        page += 1
    return comments
