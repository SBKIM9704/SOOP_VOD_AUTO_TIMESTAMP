"""설정 데이터클래스 + soopts.toml 로더 (노래 전용).

tomllib(3.11+) 우선, 3.10은 tomli 폴백. 섹션별 부분 오버라이드를 지원한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

try:  # py3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # py3.10
    import tomli as tomllib  # type: ignore


@dataclass
class Endpoints:
    meta_url: str = "https://api.m.sooplive.com/station/video/a/view"
    chat_url: str = "https://videoimg.sooplive.com/php/ChatLoadSplit.php"
    vod_web_url: str = "https://vod.sooplive.co.kr/player/{title_no}?change_second={sec}"
    api_level: int = 10


@dataclass
class CollectorConfig:
    request_delay_s: float = 0.3
    chunk_step_s: int = 300
    timeout_s: float = 15.0
    max_retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )


@dataclass
class AudioConfig:
    vad_engine: str = "smn"          # inaSpeechSegmenter: speech/music/noise
    min_music_s: float = 30.0        # 이보다 짧은 음악 구간 제거(BGM 오탐)
    merge_gap_s: float = 15.0        # 인접 음악 구간 병합 간격
    lead_offset_s: int = 0           # 노래 시작점 보정
    # 스티커(작은 이모티콘) 반응으로 노래 판별 — BJ가 노래하면 채팅에 스티커가 쏟아진다.
    skip_opening_s: float = 240.0    # 방송 초반(인사 스티커 폭증)은 노래에서 제외
    sticker_rate_strong: float = 2.5  # 분당 스티커 이 이상이면 "노래 유력"(떼창곡)
    min_sticker_rate: float = 0.0    # >0이면 이 미만 음악구간 제거(BGM 컷). 0=유지+주석만
    # 스티커 기반 노래 후보 구간(--slice) 탐지: 이동 윈도우 합계 방식
    sticker_bucket_s: int = 30
    sticker_window_buckets: int = 4   # 4버킷=2분 윈도우
    sticker_min_per_window: int = 4   # 2분 내 스티커 이 이상이면 후보(얕은 지속 반응도 포착)
    sticker_pad_before_s: float = 90.0  # 스티커는 노래보다 늦게 터지므로 앞을 넉넉히 당김
    sticker_pad_after_s: float = 45.0


@dataclass
class SttConfig:
    model: str = "small"             # faster-whisper 모델 (base/small/medium…)
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None      # None=자동. 노래는 "en"/"ko" 강제가 정확도 큼
    beam_size: int = 5
    lyric_chars: int = 300           # 출력 가사 길이 컷


@dataclass
class ClipConfig:
    quality: str = "hls-original"    # yt-dlp 포맷 (1080p). hls-hd4k=720p, hls-hd=540p
    dl_pad_before_s: float = 120.0   # 경계 탐지 여유를 위해 후보 구간을 넉넉히 받음
    dl_pad_after_s: float = 90.0
    min_song_s: float = 45.0         # 구간 내 최장 음악 블록이 이보다 짧으면 노래 아님(스킵)
    boundary_pad_s: float = 1.0      # 정밀 경계에서 앞뒤 살짝 여유
    crf: int = 20                    # 재인코딩 화질(낮을수록 고화질). 클린 컷 위해 재인코딩


@dataclass
class YouTubeConfig:
    client_secret: str = "client_secret.json"  # Google Cloud OAuth 클라이언트(사용자 준비)
    token_file: str = "~/.config/soopts/yt_token.json"  # 최초 동의 후 저장되는 토큰
    privacy: str = "unlisted"        # unlisted=링크로 시청 가능
    category_id: str = "10"          # 10 = Music
    title_template: str = "{bj} - {title} [{vod_id} {hms}]"
    made_for_kids: bool = False


@dataclass
class Config:
    endpoints: Endpoints = field(default_factory=Endpoints)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    clip: ClipConfig = field(default_factory=ClipConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    work_root: Path = Path("work")


def _build_section(cls: type, overrides: dict[str, Any]) -> Any:
    """dataclass의 알려진 필드만 골라 인스턴스를 만든다(미지 키는 무시)."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in overrides.items() if k in known})


def load_config(path: Path | None = None, work_root: Path | None = None) -> Config:
    """설정 로드. path 없으면 CWD의 soopts.toml, 둘 다 없으면 기본값."""
    data: dict[str, Any] = {}
    candidate = path if path is not None else Path("soopts.toml")
    if candidate.exists():
        with open(candidate, "rb") as fh:
            data = tomllib.load(fh)

    cfg = Config(
        endpoints=_build_section(Endpoints, data.get("endpoints", {})),
        collector=_build_section(CollectorConfig, data.get("collector", {})),
        audio=_build_section(AudioConfig, data.get("audio", {})),
        stt=_build_section(SttConfig, data.get("stt", {})),
        clip=_build_section(ClipConfig, data.get("clip", {})),
        youtube=_build_section(YouTubeConfig, data.get("youtube", {})),
    )
    if data.get("work_root") is not None:
        cfg.work_root = Path(data["work_root"])
    if work_root is not None:
        cfg.work_root = Path(work_root)
    return cfg
