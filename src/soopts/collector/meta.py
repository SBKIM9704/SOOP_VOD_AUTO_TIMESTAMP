"""VOD 메타데이터 조회.

POST station/video/a/view (nTitleNo, nApiLevel). 멀티파트 duration을 누적해
전역 타임라인 offset을 계산한다. 응답 스키마가 비공식이라 필드 추출은 관대하게 한다.
"""

from __future__ import annotations

import requests

from soopts.config import Config
from soopts.log import get_logger
from soopts.models import MetaPart, MetaResult, read_meta, write_meta
from soopts.paths import WorkPaths

log = get_logger("collector.meta")


def _dig(d: dict, *keys):
    """중첩 dict에서 여러 후보 키 중 처음 존재하는 값을 반환."""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _norm_duration(raw) -> int:
    """duration을 초 단위 int로 정규화. 값이 매우 크면 ms로 간주해 환산한다."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0
    # 단일 파트가 100000초(27시간)를 넘을 리 없으므로 ms로 판단
    if v > 100000:
        v = v / 1000.0
    return int(round(v))


def parse_meta_response(vod_id: str, payload: dict) -> MetaResult:
    """view API JSON 응답을 MetaResult로 변환한다(순수 함수, 테스트 대상)."""
    data = payload.get("data", payload) if isinstance(payload, dict) else {}

    title = _dig(data, "title", "full_title", "clip_title") or f"VOD {vod_id}"
    bj_id = _dig(data, "bj_id", "user_id", "userId", "writer_id") or ""
    bj_nick = _dig(data, "bj_nick", "user_nick", "userNick", "writer_nick") or ""

    files = (
        _dig(data, "files", "file_list", "fileList", "part_list", "vod_list")
        or []
    )
    if isinstance(files, dict):
        files = [files]

    parts: list[MetaPart] = []
    offset = 0
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            continue
        key = _dig(f, "file_info_key", "fileInfoKey", "key", "file_key") or ""
        dur = _norm_duration(_dig(f, "duration", "file_duration", "total_time", "playtime"))
        parts.append(MetaPart(idx=i, file_info_key=str(key), duration=dur, offset_s=offset))
        offset += dur

    return MetaResult(
        vod_id=str(vod_id),
        title=str(title),
        bj_id=str(bj_id),
        bj_nick=str(bj_nick),
        total_duration=offset,
        parts=parts,
    )


def fetch_meta(cfg: Config, vod_id: str, work: WorkPaths, *, force: bool = False) -> MetaResult:
    """meta.json 캐시를 우선 사용하고, 없거나 force면 view API를 호출한다."""
    if work.meta.exists() and not force:
        log.info("meta.json 캐시 사용: %s", work.meta)
        return read_meta(work.meta)

    work.ensure()
    log.info("VOD 메타 조회: %s", vod_id)
    resp = requests.post(
        cfg.endpoints.meta_url,
        data={"nTitleNo": vod_id, "nApiLevel": cfg.endpoints.api_level, "nPlaylistIdx": 0},
        headers={"User-Agent": cfg.collector.user_agent},
        timeout=cfg.collector.timeout_s,
    )
    resp.raise_for_status()
    meta = parse_meta_response(vod_id, resp.json())
    if not meta.parts:
        log.warning("파트 정보를 찾지 못했습니다. 응답 스키마 변경 가능성. raw 응답을 확인하세요.")
    write_meta(work.meta, meta)
    log.info("파트 %d개, 총 %d초", len(meta.parts), meta.total_duration)
    return meta
