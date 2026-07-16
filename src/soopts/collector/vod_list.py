"""스테이션(bj_id)의 최신 VOD 목록 조회.

chapi.sooplive.co.kr의 비공식 목록 API. 요청 URL과 응답 스키마는 실제 캡처로
검증됐다(2026-07-16, bj_id=singgyul, `tests/fixtures/vod_list_page1.json` 참고):
  - GET https://chapi.sooplive.co.kr/api/{bj_id}/vods/review?page={n}
  - `per_page` 쿼리 파라미터는 서버가 무시하고 항상 페이지당 20개 고정.
  - 응답은 최신순(title_no 내림차순)으로 정렬돼 있다.
  - `ucc.total_file_duration`은 밀리초 단위(기존 meta API의 초 단위 total_duration과
    대조 검증: 8332900ms ≈ 8333s).
"""

from __future__ import annotations

import time

import requests

from soopts.config import Config
from soopts.log import get_logger

log = get_logger("collector.vod_list")

_LIST_URL = "https://chapi.sooplive.co.kr/api/{bj_id}/vods/review"


def to_candidate(item: dict) -> dict:
    """목록 API의 원본 item → {title_no, title, broadcast_date, duration_s}. 순수 함수."""
    ucc = item.get("ucc") or {}
    duration_ms = ucc.get("total_file_duration")
    reg_date = item.get("reg_date") or ""
    return {
        "title_no": item["title_no"],
        "title": item.get("title_name", ""),
        "broadcast_date": reg_date.split(" ", 1)[0] if reg_date else None,
        "duration_s": round(duration_ms / 1000) if duration_ms else None,
    }


def extract_page(data: dict) -> tuple[list[dict], int, int]:
    """응답 JSON → (candidates, current_page, last_page). 순수 함수."""
    candidates = [to_candidate(item) for item in data.get("data", [])]
    meta = data.get("meta") or {}
    return candidates, meta.get("current_page", 1), meta.get("last_page", 1)


def fetch_recent_vods(cfg: Config, bj_id: str, count: int) -> list[dict]:
    """최신 VOD 최대 count개를 최신순으로 반환한다. 필요하면 다음 페이지까지 조회한다."""
    out: list[dict] = []
    page = 1
    headers = {"User-Agent": cfg.collector.user_agent}
    while len(out) < count:
        resp = requests.get(
            _LIST_URL.format(bj_id=bj_id),
            params={"page": page},
            headers=headers,
            timeout=cfg.collector.timeout_s,
        )
        resp.raise_for_status()
        candidates, current_page, last_page = extract_page(resp.json())
        if not candidates:
            break
        out.extend(candidates[: count - len(out)])
        if current_page >= last_page:
            break
        page += 1
        time.sleep(cfg.collector.request_delay_s)
    return out
