"""미디어 확보 — yt-dlp 전체 오디오 또는 HLS 세그먼트 슬라이스.

주의: 다운로드는 사용자 판단·로컬 수행 원칙(약관/저작권).

효율: 노래 감지에 전체(2시간, ~936MB)를 받을 필요 없이, 스티커로 찾은 후보 구간만
세그먼트 슬라이스로 받을 수 있다(구간당 수 MB). SOOP은 오디오 전용 포맷이 없어(전부 A+V 합쳐진
HLS) 전체 오디오도 결국 풀 다운로드이므로, 슬라이스가 유일한 절약 수단이다.
"""

from __future__ import annotations

import subprocess
import urllib.request
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


def resolve_m3u8(url_or_id: str, quality: str = "hls-hd") -> str:
    """첫 파트 m3u8 (단일 파트용)."""
    return resolve_m3u8_list(url_or_id, quality)[0]


def download_slice(m3u8_url: str, start_s: float, end_s: float, out_path: Path) -> Path:
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
        for i in idxs:
            fh.write(_fetch(f"{base}/{seg_uris[i]}"))
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


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def _parse_playlist(m3u8_url: str) -> tuple[str, str, list[float], list[str]]:
    """m3u8 → (base_url, init_uri, 세그먼트별 누적시작초, 세그먼트 URI)."""
    with urllib.request.urlopen(m3u8_url, timeout=15) as r:
        text = r.read().decode("utf-8", "replace")
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
