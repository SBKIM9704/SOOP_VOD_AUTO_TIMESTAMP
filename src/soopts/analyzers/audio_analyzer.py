"""노래 구간 감지 — 오디오 음악감지 + 스티커 반응.

inaSpeechSegmenter로 music 구간을 뽑고, 채팅 스티커(작은 이모티콘) 반응으로 실제 노래와
BGM을 구분한다(BJ가 노래하면 스티커가 쏟아진다). 무거운 ML은 메서드 내부에서만 import.
"""

from __future__ import annotations

import json

from soopts.config import Config
from soopts.log import get_logger
from soopts.models import Song, read_chat_jsonl
from soopts.paths import WorkPaths

log = get_logger("analyzers.audio")


# --------------------------------------------------------------------------- #
# 순수 후처리 (단위 테스트 대상)
# --------------------------------------------------------------------------- #
def music_intervals(segmentation: list[tuple[str, float, float]]) -> list[tuple[float, float]]:
    return [(s, e) for lab, s, e in segmentation if lab == "music"]


def merge_intervals(
    intervals: list[tuple[float, float]], merge_gap_s: float, min_len_s: float
) -> list[tuple[float, float]]:
    """간격 merge_gap_s 이내는 병합, 병합 후 min_len_s 미만은 제거."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged: list[list[float]] = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s - merged[-1][1] <= merge_gap_s:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if (e - s) >= min_len_s]


def sticker_rate(interval: tuple[float, float], sticker_times: list[float]) -> float:
    """음악 구간 동안의 분당 스티커 수. 노래(떼창)면 스티커가 쏟아진다."""
    s, e = interval
    dur_min = max((e - s) / 60.0, 1e-6)
    return sum(1 for t in sticker_times if s <= t <= e) / dur_min


def sticker_burst_regions(
    sticker_times: list[float],
    *,
    bucket_s: int = 30,
    window_buckets: int = 4,
    min_per_window: int = 4,
    merge_gap_s: float = 90.0,
    pad_before_s: float = 90.0,
    pad_after_s: float = 45.0,
    skip_opening_s: float = 240.0,
    total_s: float | None = None,
) -> list[tuple[float, float]]:
    """스티커 반응 구간을 찾아 노래 후보 (start, end)로 반환한다(채팅만으로).

    BJ가 노래하면 채팅에 스티커가 쏟아진다는 신호로, 전체 영상 다운로드 없이 후보 위치를 잡는다.
    **이동 윈도우 합계**: 최근 window_buckets(기본 4=2분) 안 스티커가 min_per_window(기본 4) 이상이면
    hot. 단발 버스트뿐 아니라 "얕게 지속되는" 반응(부른 곡의 전형)도 잡는다.
    스티커는 노래보다 늦게 터지므로 앞쪽(pad_before_s)을 넉넉히 당겨 노래 시작을 덮는다.
    """
    from collections import Counter

    if not sticker_times:
        return []
    counts = Counter(int(t // bucket_s) for t in sticker_times)
    max_b = max(counts)
    hot = [
        b for b in range(max_b + 1)
        if b * bucket_s >= skip_opening_s
        and sum(counts.get(b - i, 0) for i in range(window_buckets)) >= min_per_window
    ]
    if not hot:
        return []
    gap_buckets = max(1, int(merge_gap_s // bucket_s))
    regions: list[list[float]] = []
    for b in hot:
        s, e = b * bucket_s, (b + 1) * bucket_s
        if regions and s - regions[-1][1] <= gap_buckets * bucket_s:
            regions[-1][1] = e
        else:
            regions.append([s, e])
    out = []
    for s, e in regions:
        s2 = max(0.0, s - pad_before_s)
        e2 = e + pad_after_s
        if total_s is not None:
            e2 = min(e2, total_s)
        out.append((s2, e2))
    return out


def intervals_to_songs(
    intervals: list[tuple[float, float]],
    lead_offset_s: int,
    sticker_times: list[float],
    strong_rate: float,
) -> list[Song]:
    songs: list[Song] = []
    for s, e in intervals:
        t = max(0, int(round(s)) - lead_offset_s)
        rate = sticker_rate((s, e), sticker_times)
        songs.append(
            Song(
                t=t,
                end=int(round(e)),
                duration=int(round(e - s)),
                sticker_rate=round(rate, 1),
                song_likely=rate >= strong_rate,
            )
        )
    return songs


# --------------------------------------------------------------------------- #
def detect_songs(
    cfg: Config, vod_id: str, work: WorkPaths, *, audio_path: str | None, force: bool
) -> list[Song]:
    """음악 구간 감지 + 스티커 판별 → Song 리스트."""
    acfg = cfg.audio
    seg = _segmentation(cfg, work, audio_path, force=force)
    if seg is None:
        return []

    intervals = merge_intervals(music_intervals(seg), acfg.merge_gap_s, acfg.min_music_s)
    intervals = [(s, e) for s, e in intervals if s >= acfg.skip_opening_s]

    sticker_times = _sticker_times(work)
    if acfg.min_sticker_rate > 0 and sticker_times:
        intervals = [iv for iv in intervals if sticker_rate(iv, sticker_times) >= acfg.min_sticker_rate]

    songs = intervals_to_songs(intervals, acfg.lead_offset_s, sticker_times, acfg.sticker_rate_strong)
    n_strong = sum(1 for s in songs if s.song_likely)
    log.info("노래 구간 %d개 (스티커 반응 유력 %d개)", len(songs), n_strong)
    return songs


def _segmentation(cfg: Config, work: WorkPaths, audio_path: str | None, *, force: bool):
    """음성 세그먼테이션(값비쌈)을 캐시와 함께 얻는다. 오디오 없고 캐시도 없으면 None."""
    if work.segmentation.exists() and not force:
        log.info("audio_segmentation.json 캐시 사용")
        return [tuple(x) for x in json.loads(work.segmentation.read_text())]
    if not audio_path:
        log.warning("audio_path 미지정 & 세그먼트 캐시 없음 — 노래 감지 불가")
        return None
    seg = _segment(audio_path, cfg.audio.vad_engine)
    work.segmentation.write_text(json.dumps(seg), encoding="utf-8")
    return seg


def _sticker_times(work: WorkPaths) -> list[float]:
    if not work.chat.exists():
        return []
    return [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]


def _segment(
    audio_path: str, vad_engine: str, window_s: float = 600.0
) -> list[tuple[str, float, float]]:
    """inaSpeechSegmenter 실행. 무거운 import는 여기서만.

    2시간 전체를 한 번에 로드하면 메모리가 터지므로 window_s(기본 10분) 단위로 처리한다.
    inaSpeechSegmenter는 start_sec 지정 시에도 파일 절대시각을 반환하므로 오프셋을 더하지 않는다.
    """
    import os
    import subprocess

    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    from inaSpeechSegmenter import Segmenter

    dur_out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", audio_path],
        capture_output=True, text=True,
    ).stdout.strip()
    total = float(dur_out) if dur_out else 0.0

    seg = Segmenter(vad_engine=vad_engine, detect_gender=False)
    if total <= 0:
        return [(lab, float(s), float(e)) for lab, s, e in seg(audio_path)]
    out: list[tuple[str, float, float]] = []
    start = 0.0
    while start < total:
        stop = min(total, start + window_s)
        for lab, s, e in seg(audio_path, start_sec=start, stop_sec=stop):
            out.append((lab, float(s), float(e)))
        start = stop
    return out
