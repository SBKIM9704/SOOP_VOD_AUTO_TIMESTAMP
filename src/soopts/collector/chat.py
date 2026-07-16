"""채팅 리플레이 수집.

2계층 캐시:
  - raw/chat_p{part}_{start}.xml : 네트워크 캐시(원본 XML). 파서 버그가 나도 재수집 불필요.
  - chat.jsonl                   : 파싱·dedup 완료 캐시.

--force  : raw까지 재수집(네트워크)
--reparse: raw에서 chat.jsonl만 재생성(네트워크 없음)
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from soopts.collector.xml_parse import parse_chat_xml
from soopts.config import Config
from soopts.log import get_logger
from soopts.models import ChatMsg, MetaResult, write_chat_jsonl
from soopts.paths import WorkPaths, raw_chat_path

log = get_logger("collector.chat")


def _fetch_raw(cfg: Config, row_key: str, start_time: int) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(1, cfg.collector.max_retries + 1):
        try:
            resp = requests.get(
                cfg.endpoints.chat_url,
                params={"rowKey": row_key, "startTime": start_time},
                headers={"User-Agent": cfg.collector.user_agent},
                timeout=cfg.collector.timeout_s,
            )
            # 404 = 그 구간에 채팅 없음(파트 끝/공백). 치명적 아님 → 빈 값으로 넘어간다.
            if resp.status_code == 404:
                return b""
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:  # noqa: PERF203
            last_exc = e
            log.warning("요청 실패(%d/%d) start=%d: %s", attempt, cfg.collector.max_retries, start_time, e)
            time.sleep(cfg.collector.request_delay_s * attempt)
    # 재시도 모두 실패해도 그 청크만 빈 값으로 건너뛰고 수집을 계속한다.
    log.warning("청크 수집 실패 start=%d — 건너뜀: %s", start_time, last_exc)
    return b""


def _iter_raw_files(
    cfg: Config,
    meta: MetaResult,
    work: WorkPaths,
    *,
    reparse: bool,
    force: bool,
):
    """(part, part_offset, xml_bytes) 를 순회한다. reparse면 디스크만, 아니면 필요 시 네트워크."""
    for part in meta.parts:
        row_key = f"{part.file_info_key}_c"
        # duration을 모르면(0) 안전하게 넉넉히 순회하다가 빈 응답에서 멈춘다.
        duration = part.duration if part.duration > 0 else 24 * 3600
        empty_streak = 0
        for start in range(0, duration, cfg.collector.chunk_step_s):
            raw_path = raw_chat_path(work.raw_dir, part.idx, start)
            if raw_path.exists() and not force:
                xml_bytes = raw_path.read_bytes()
            elif reparse:
                # reparse 모드인데 raw가 없으면 그 청크는 건너뛴다(네트워크 금지).
                continue
            else:
                xml_bytes = _fetch_raw(cfg, row_key, start)
                raw_path.write_bytes(xml_bytes)
                time.sleep(cfg.collector.request_delay_s)

            yield part.idx, part.offset_s, xml_bytes

            # duration 미상일 때: 연속으로 빈 청크가 나오면 파트 끝으로 간주
            if part.duration <= 0:
                if not xml_bytes or xml_bytes.strip() in (b"", b"<record/>", b"<record></record>"):
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                else:
                    empty_streak = 0


def fetch_chat(
    cfg: Config,
    vod_id: str,
    meta: MetaResult,
    work: WorkPaths,
    *,
    force: bool = False,
    reparse: bool = False,
) -> Path:
    """채팅을 수집·파싱·dedup 후 chat.jsonl에 저장하고 경로를 반환한다."""
    if work.chat.exists() and not force and not reparse:
        log.info("chat.jsonl 캐시 사용: %s", work.chat)
        return work.chat

    work.ensure()
    seen: set[str] = set()
    msgs: list[ChatMsg] = []
    for part_idx, offset, xml_bytes in _iter_raw_files(
        cfg, meta, work, reparse=reparse, force=force
    ):
        for m in parse_chat_xml(xml_bytes, part_idx, offset):
            if m.key in seen:
                continue
            seen.add(m.key)
            msgs.append(m)

    msgs.sort(key=lambda m: m.t)
    write_chat_jsonl(work.chat, msgs)
    n_chat = sum(1 for m in msgs if m.kind == "chat")
    n_ogq = sum(1 for m in msgs if m.kind == "ogq")
    log.info("채팅 %d개(chat=%d, ogq=%d) 저장: %s", len(msgs), n_chat, n_ogq, work.chat)
    return work.chat
