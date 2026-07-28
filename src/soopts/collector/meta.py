"""VOD 메타데이터 조회.

POST station/video/a/view (nTitleNo, nApiLevel). 멀티파트 duration을 누적해
전역 타임라인 offset을 계산한다. 응답 스키마가 비공식이라 필드 추출은 관대하게 한다.
"""

from __future__ import annotations

from soopts.collector.http import post_with_retry
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
    """view API JSON 응답을 MetaResult로 변환한다(순수 함수, 테스트 대상).

    **누락 파트 보정 — 전역 offset은 SOOP 플레이어(=팬 타임라인) 기준이어야 한다.**
    방송이 중간에 끊기면 파트가 여러 개로 저장되는데(`file_order` 1,2,3,…), 이 API가
    **일부 파트를 응답에서 빼먹는 일이 있다**(실측: 201586597은 `file_order` [1,4]만 반환,
    중간 _2·_3 누락). 그런데 SOOP 플레이어는 그 누락 파트도 이어붙여 재생하므로, 팬이 찍은
    타임스탬프(딥링크·start_s)는 누락분까지 포함한 전역초다. 반환된 파트 duration만 누적하면
    누락분(∑)만큼 뒤 파트 offset이 **작게** 잡혀, 팬 시각을 그대로 HLS 좌표로 쓰면 그 파트의
    모든 곡이 누락분만큼 **뒤에서**(곡 중간) 잘린다(종료는 전사로 찾아 맞는데 시작만 틀림).

    누락 여부는 `file_order` 빈칸이 아니라 **`total_file_duration − ∑(반환 duration) > 0`**
    으로 판정한다(빈 번호라도 그 파트 길이가 0이면 offset은 정상 — total이 말해준다). 누락분을
    넣을 자리(리딩=첫 order>1, 또는 반환 파트 사이 order 점프)가 **하나면** 거기에 전부 더해
    정확히 보정하고, 자리가 둘 이상이면 파트별 길이를 알 수 없어 분배가 모호하므로 첫 자리에
    몰아넣고 경고한다(호출부/빌드 가드가 보류하도록).
    """
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

    # (order, key, dur) — file_order로 정렬(응답 순서를 신뢰하지 않는다)
    entries: list[tuple[int, str, int]] = []
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            continue
        key = _dig(f, "file_info_key", "fileInfoKey", "key", "file_key") or ""
        dur = _norm_duration(_dig(f, "duration", "file_duration", "total_time", "playtime"))
        raw_order = _dig(f, "file_order", "fileOrder", "order")
        order = int(raw_order) if str(raw_order).lstrip("-").isdigit() else (i + 1)
        entries.append((order, str(key), dur))
    entries.sort(key=lambda e: e[0])

    returned_sum = sum(d for _, _, d in entries)
    total_api = _norm_duration(_dig(data, "total_file_duration"))
    omitted = total_api - returned_sum if total_api else 0

    # 누락분을 더할 자리(=그 파트부터 offset을 밀어야 하는 인덱스). 리딩 + 내부 점프.
    slots: list[int] = []
    if entries and entries[0][0] > 1:
        slots.append(0)
    for i in range(1, len(entries)):
        if entries[i][0] - entries[i - 1][0] > 1:
            slots.append(i)

    add_at: int | None = None
    if omitted > 0:
        if len(slots) == 1:
            add_at = slots[0]
        elif len(slots) == 0:
            log.warning("VOD %s: 누락 %ds인데 자리를 못 찾음(후행 누락 추정) — offset 보정 생략",
                        vod_id, omitted)
        else:
            add_at = slots[0]
            log.warning("VOD %s: 누락 %ds인데 빈 자리 %d곳 — 분배 모호, 첫 자리에 몰아넣음(빌드 전 검토 요망)",
                        vod_id, omitted, len(slots))

    parts: list[MetaPart] = []
    offset = 0
    for i, (order, key, dur) in enumerate(entries):
        if i == add_at:
            offset += omitted
        parts.append(MetaPart(idx=i, file_info_key=key, duration=dur,
                              offset_s=offset, file_order=order))
        offset += dur

    return MetaResult(
        vod_id=str(vod_id),
        title=str(title),
        bj_id=str(bj_id),
        bj_nick=str(bj_nick),
        total_duration=max(offset, total_api),
        parts=parts,
    )


def fetch_meta(cfg: Config, vod_id: str, work: WorkPaths, *, force: bool = False) -> MetaResult:
    """meta.json 캐시를 우선 사용하고, 없거나 force면 view API를 호출한다."""
    if work.meta.exists() and not force:
        log.info("meta.json 캐시 사용: %s", work.meta)
        return read_meta(work.meta)

    work.ensure()
    log.info("VOD 메타 조회: %s", vod_id)
    resp = post_with_retry(
        cfg,
        cfg.endpoints.meta_url,
        data={"nTitleNo": vod_id, "nApiLevel": cfg.endpoints.api_level, "nPlaylistIdx": 0},
    )
    resp.raise_for_status()
    meta = parse_meta_response(vod_id, resp.json())
    if not meta.parts:
        log.warning("파트 정보를 찾지 못했습니다. 응답 스키마 변경 가능성. raw 응답을 확인하세요.")
    write_meta(work.meta, meta)
    log.info("파트 %d개, 총 %d초", len(meta.parts), meta.total_duration)
    return meta
