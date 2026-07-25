"""chapi.sooplive.co.kr GET에 대한 공용 재시도 헬퍼.

SOOP의 비공식 API는 일시적으로 502/503/504를 뱉는다(서버측 blip). 재시도 없이
`raise_for_status()`만 하면 그 한 번에 호출부 전체가 죽는다 — 특히 `iter_vod_pages`는
선택 단계라 daily 런 전체를 무너뜨린다. 여기서 짧게 재시도해 blip을 넘긴다.

정책:
  - 5xx 응답·연결/타임아웃 오류만 재시도한다(일시 장애로 간주).
  - 4xx는 재시도하지 않고 응답을 그대로 반환한다 — 404를 빈 값으로 볼지,
    raise_for_status로 올릴지는 호출부마다 다르므로 상태 처리를 호출부에 맡긴다.
  - 재시도를 모두 소진하면 마지막 예외를 그대로 전파한다. daily는 6시간마다 돌므로
    지속 장애면 다음 런이 자연 재시도한다.
"""

from __future__ import annotations

import time

import requests

from soopts.config import Config
from soopts.log import get_logger

log = get_logger("collector.http")

# 리스트/댓글 같은 API 단위 호출용 backoff. 청크 단위(chat.py)의 request_delay_s보다
# 길게 둬 수초짜리 502 blip을 실제로 넘긴다(2s, 4s → 3회 합계 ~6s).
_BACKOFF_BASE_S = 2.0


def request_with_retry(
    cfg: Config,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
) -> requests.Response:
    """method(GET/POST)를 재시도와 함께 수행하고 응답을 반환한다.

    5xx·연결/타임아웃은 재시도하고, 소진하면 예외를 전파한다. 4xx는 재시도 없이
    응답을 그대로 돌려주므로 호출부가 상태를 직접 처리한다(raise_for_status/404 분기).
    """
    last_exc: requests.RequestException | None = None
    for attempt in range(1, cfg.collector.max_retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                data=data,
                headers={"User-Agent": cfg.collector.user_agent},
                timeout=cfg.collector.timeout_s,
            )
            # 5xx만 재시도 대상 — raise_for_status로 예외를 유도해 아래 except로 넘긴다.
            # 4xx는 호출부가 처리하도록 그대로 반환한다.
            if resp.status_code >= 500:
                resp.raise_for_status()
            return resp
        except requests.RequestException as e:  # noqa: PERF203
            last_exc = e
            if attempt >= cfg.collector.max_retries:
                break
            log.warning(
                "%s 재시도(%d/%d) %s: %s", method, attempt, cfg.collector.max_retries, url, e
            )
            time.sleep(_BACKOFF_BASE_S * attempt)
    assert last_exc is not None  # 루프는 return 또는 last_exc 설정 후에만 빠져나온다.
    raise last_exc


def get_with_retry(cfg: Config, url: str, *, params: dict | None = None) -> requests.Response:
    """GET 편의 래퍼. 자세한 정책은 request_with_retry 참고."""
    return request_with_retry(cfg, "GET", url, params=params)


def post_with_retry(cfg: Config, url: str, *, data: dict | None = None) -> requests.Response:
    """POST 편의 래퍼. 자세한 정책은 request_with_retry 참고."""
    return request_with_retry(cfg, "POST", url, data=data)
