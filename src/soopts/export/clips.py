"""노래 클립 추출 — 정밀 경계 컷 + 1080p.

흐름(구간당): 후보 구간을 넉넉히 1080p로 다운로드 → inaSpeechSegmenter로 음악 경계 정밀 탐지
→ **구간 내 가장 긴 음악 블록 = 노래**로 잡아 그 경계만 ffmpeg로 클린 컷.
실측: 매직카펫 구간에서 정답 대비 1초 오차로 경계 탐지됨.
무거운 ML은 함수 내부에서만 import.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from soopts.config import Config
from soopts.log import get_logger
from soopts.output import fmt_hms

log = get_logger("export.clips")


@dataclass
class Clip:
    t: int              # 노래 시작(전역초, 정밀)
    end: int            # 노래 끝(전역초, 정밀)
    duration: int
    path: str           # 추출된 클립 파일 경로
    title: str | None = None   # 확정 곡명(검수 단계에서 채움). None/"" = 곡명 미상
    lyrics: str = ""


def clips_json_path(clips_dir: Path) -> Path:
    return clips_dir / "clips.json"


def write_clips(clips_dir: Path, clips: list[Clip]) -> Path:
    p = clips_json_path(clips_dir)
    p.write_text(
        json.dumps([asdict(c) for c in clips], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return p


def read_clips(clips_dir: Path) -> list[Clip]:
    p = clips_json_path(clips_dir)
    if not p.exists():
        return []
    return [Clip(**d) for d in json.loads(p.read_text(encoding="utf-8"))]


def longest_music_block(
    segmentation: list[tuple[str, float, float]], merge_gap_s: float = 15.0
) -> tuple[float, float] | None:
    """세그먼테이션에서 음악 블록을 병합해 가장 긴 것을 반환. 없으면 None."""
    mus = sorted((s, e) for lab, s, e in segmentation if lab == "music")
    if not mus:
        return None
    merged: list[list[float]] = [list(mus[0])]
    for s, e in mus[1:]:
        if s - merged[-1][1] <= merge_gap_s:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    s, e = max(merged, key=lambda iv: iv[1] - iv[0])
    return (s, e)


def refine_boundary(
    cfg: Config, media_path: str, region_start: float, region_end: float
) -> tuple[float, float] | None:
    """구간 [region_start, region_end]에서 노래(최장 음악블록)의 정밀 경계를 반환한다."""
    import os

    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    from inaSpeechSegmenter import Segmenter

    seg = Segmenter(vad_engine=cfg.audio.vad_engine, detect_gender=False)
    res = [(lab, float(s), float(e)) for lab, s, e in seg(media_path, start_sec=region_start, stop_sec=region_end)]
    block = longest_music_block(res)
    if block is None or (block[1] - block[0]) < cfg.clip.min_song_s:
        return None
    return block


def cut_clip(cfg: Config, src: str, start: float, end: float, out: Path) -> Path:
    """src 영상을 [start, end]로 재인코딩 클린 컷(1080p 유지)."""
    pad = cfg.clip.boundary_pad_s
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-ss", str(max(0, start - pad)),
         "-i", src, "-t", str((end - start) + 2 * pad),
         "-c:v", "libx264", "-crf", str(cfg.clip.crf), "-preset", "medium",
         "-c:a", "aac", "-b:a", "192k", str(out)],
        capture_output=True,
    )
    return out


def make_clip(
    cfg: Config, vod_id: str, media_path: str, region_start: float, region_end: float,
    clips_dir: Path, media_offset: float = 0.0,
) -> Clip | None:
    """다운로드된 구간 영상(media_path)에서 노래를 정밀 컷해 Clip 반환.

    media_offset: media_path의 t=0 이 전역 몇 초인지(슬라이스면 region_start). 경계는 media 로컬
    기준으로 탐지하고, 전역 시각은 offset을 더해 기록한다.
    """
    local_end = region_end - media_offset
    boundary = refine_boundary(cfg, media_path, 0.0, local_end)
    if boundary is None:
        log.info("%s 구간: 노래로 볼 음악 블록 없음 — 스킵", fmt_hms(int(region_start)))
        return None
    ls, le = boundary
    g_start, g_end = int(media_offset + ls), int(media_offset + le)
    out = clips_dir / f"song_{g_start:06d}.mp4"
    cut_clip(cfg, media_path, ls, le, out)
    log.info("클립 저장 %s~%s (%d초): %s", fmt_hms(g_start), fmt_hms(g_end), g_end - g_start, out.name)
    return Clip(t=g_start, end=g_end, duration=g_end - g_start, path=str(out))
