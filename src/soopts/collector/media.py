"""미디어 확보 — yt-dlp 전체 오디오 또는 HLS 세그먼트 슬라이스.

주의: 다운로드는 사용자 판단·로컬 수행 원칙(약관/저작권).

효율: 노래 감지에 전체(2시간, ~936MB)를 받을 필요 없이, 스티커로 찾은 후보 구간만
세그먼트 슬라이스로 받을 수 있다(구간당 수 MB). SOOP은 오디오 전용 포맷이 없어(전부 A+V 합쳐진
HLS) 전체 오디오도 결국 풀 다운로드이므로, 슬라이스가 유일한 절약 수단이다.
"""

from __future__ import annotations

import itertools
import subprocess
import time
import urllib.request
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from soopts.log import get_logger

if TYPE_CHECKING:
    from soopts.models import MetaPart

log = get_logger("collector.media")


def download_audio_full(url_or_id: str, out_path: Path, quality: str = "hls-hd") -> Path:
    """전체 오디오를 mp3로 받는다(전 구간 노래 감지용)."""
    url = _norm_url(url_or_id)
    out_path = Path(out_path)
    tmpl = str(out_path.with_suffix(".%(ext)s"))
    r = subprocess.run(
        ["yt-dlp", "-f", quality, "-x", "--audio-format", "mp3", "-o", tmpl, url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"오디오 다운로드 실패: {r.stderr.strip()[:200]}")
    log.info("오디오 저장: %s", out_path)
    return out_path


def resolve_m3u8_list(url_or_id: str, quality: str = "hls-hd") -> list[str]:
    """yt-dlp -g로 파트별 m3u8 URL 목록을 얻는다(멀티파트 VOD는 파트마다 1개)."""
    out = subprocess.run(
        ["yt-dlp", "-f", quality, "-g", _norm_url(url_or_id)],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"m3u8 조회 실패: {out.stderr.strip()[:200]}")
    return out.stdout.strip().splitlines()


def download_slice(
    m3u8_url: str, start_s: float, end_s: float, out_path: Path, workers: int = 4
) -> Path:
    """[start_s, end_s] 구간을 덮는 세그먼트만 받아 concat한 fMP4를 저장한다.

    yt-dlp --download-sections는 이 VOD의 HLS에서 빈 출력 버그가 있어, m3u8을 직접 파싱해
    해당 seg-*.m4s + init.m4s를 받아 붙인다(실측 검증됨).
    """
    base, init_uri, starts, seg_uris = _parse_playlist(m3u8_url)
    idxs = [i for i, s in enumerate(starts) if s + 6.0 >= start_s and s <= end_s]
    if not idxs:
        raise RuntimeError(f"구간 세그먼트 없음: {start_s}-{end_s}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        if init_uri:
            fh.write(_fetch(f"{base}/{init_uri}"))
        _write_segments(fh, [f"{base}/{seg_uris[i]}" for i in idxs], workers)
    log.info("슬라이스 저장(seg %d~%d, %ds): %s", idxs[0], idxs[-1], int(end_s - start_s), out_path)
    return out_path


def map_to_part(
    s: float, e: float, parts: list[MetaPart], m3u8s: list[str]
) -> tuple[str | None, float, float]:
    """전역 시각 구간 (s,e)를 해당 파트의 m3u8 + 파트-로컬 시각으로 매핑.

    단일 파트(메타 없음)면 그대로 첫 m3u8 사용. 파트 경계를 넘으면 시작 파트 안으로 클램프.
    """
    if not parts:
        return m3u8s[0], s, e
    for p in parts:
        if p.offset_s <= s < p.offset_s + p.duration:
            if p.idx >= len(m3u8s):
                return None, 0, 0
            ls = s - p.offset_s
            le = min(e - p.offset_s, float(p.duration))  # 파트 끝으로 클램프
            return m3u8s[p.idx], ls, le
    return None, 0, 0


# --------------------------------------------------------------------------- #
def _norm_url(url_or_id: str) -> str:
    return (
        url_or_id if url_or_id.startswith("http")
        else f"https://vod.sooplive.com/player/{url_or_id}"
    )


_FETCH_RETRIES = 3
_MIN_SEGMENT_BYTES = 512  # 이보다 작으면 명백히 잘린/빈 응답 — 재시도 대상
_RETRY_BACKOFF_S = 0.5  # 재시도 사이 지수 백오프의 기준값(0.5→1.0→…) — 순간 장애에 여유를 준다


def _read_url(url: str, *, timeout: float, min_bytes: int) -> bytes:
    """URL 하나를 재시도+지수 백오프로 받는다. 세그먼트/플레이리스트 공용 관문.

    실측 사례: 서버가 Content-Length 없이 응답하면 연결이 일찍 끊겨도 urllib이 예외 없이
    짧은 데이터를 그대로 반환해, 오디오가 티 안 나게 깨진 채로 넘어간 적이 있다(같은 VOD의
    다른 구간에서는 반대로 IncompleteRead가 터졌다 — 서버 응답 형태가 요청마다 다를 수
    있다는 뜻). 그래서 예외뿐 아니라 비정상적으로 작은 응답(min_bytes 미만)도 재시도한다.

    플레이리스트(m3u8) 읽기도 이 함수를 거친다 — 긴 멀티파트 VOD의 큰 플레이리스트가
    IncompleteRead로 잘려 재시도 없이 그대로 예외를 뱉던 게 '다운로드 단계 실패'의 원인이었다.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read()
            if len(data) < min_bytes:
                raise RuntimeError(f"응답이 비정상적으로 작음({len(data)}B): {url}")
            return data
        except Exception as ex:  # noqa: BLE001
            last_exc = ex
            log.warning("요청 실패(%d/%d) — 재시도: %s (%s)", attempt, _FETCH_RETRIES, url, ex)
            if attempt < _FETCH_RETRIES:
                time.sleep(_RETRY_BACKOFF_S * (2 ** (attempt - 1)))
    raise RuntimeError(f"요청 반복 실패: {url}") from last_exc


def _write_segments(fh, urls: list[str], workers: int) -> None:
    """세그먼트를 동시에 받되 파일에는 **원래 순서대로** 쓴다.

    구간 하나가 50여 개 세그먼트인데 순차로 받으면 요청당 왕복 지연이 그대로 누적된다.
    실측상 화질을 8배 낮춰도 다운로드는 2배밖에 안 줄었는데, 남은 시간이 대역폭이 아니라
    이 지연이었다.

    한 번에 최대 workers개만 요청 중인 상태를 유지한다(슬라이딩 윈도). 전부 모아두고
    쓰면 구간 하나가 수백 MB까지 갈 수 있어 러너 메모리를 위협한다. 세그먼트 순서가
    어긋나면 fMP4가 깨지므로 완료 순서가 아니라 제출 순서대로 꺼내 쓴다.
    """
    if workers <= 1:
        for url in urls:
            fh.write(_fetch(url))
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rest = iter(urls)
        pending: deque[Future[bytes]] = deque(
            ex.submit(_fetch, u) for u in itertools.islice(rest, workers)
        )
        for url in rest:
            fh.write(pending.popleft().result())
            pending.append(ex.submit(_fetch, url))
        while pending:
            fh.write(pending.popleft().result())


def _fetch(url: str) -> bytes:
    """세그먼트 하나를 받는다(재시도+백오프는 _read_url이 담당)."""
    return _read_url(url, timeout=30, min_bytes=_MIN_SEGMENT_BYTES)


def _parse_playlist(m3u8_url: str) -> tuple[str, str, list[float], list[str]]:
    """m3u8 → (base_url, init_uri, 세그먼트별 누적시작초, 세그먼트 URI)."""
    text = _read_url(m3u8_url, timeout=15, min_bytes=1).decode("utf-8", "replace")
    base = m3u8_url.rsplit("/", 1)[0]
    init_uri = ""
    starts: list[float] = []
    seg_uris: list[str] = []
    t, dur = 0.0, 6.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            init_uri = line.split('URI="', 1)[1].split('"', 1)[0]
        elif line.startswith("#EXTINF:"):
            dur = float(line[len("#EXTINF:"):].split(",")[0])
        elif line and not line.startswith("#"):
            seg_uris.append(line)
            starts.append(t)
            t += dur
    return base, init_uri, starts, seg_uris
