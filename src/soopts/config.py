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
    # HLS 세그먼트 동시 요청 수. 구간 하나가 50여 개 세그먼트라 순차로 받으면 요청당
    # 왕복 지연이 그대로 쌓인다(실측: 화질 8배↓에도 다운로드는 2배만 줄었다).
    # 영상 플레이어의 프리페치와 비슷한 수준으로만 올린다.
    segment_workers: int = 4
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
    groq_model: str = "whisper-large-v3-turbo"  # Groq 호스팅 Whisper API(가사 전사)
    language: str | None = None      # None=자동(en/ko 둘 다 시도). 노래는 강제가 정확도 큼
    lyric_chars: int = 300           # 출력 가사 길이 컷
    # 전사 성공률이 이 값 미만이면 실행을 실패로 처리한다. 노래는 반주에 묻혀 원래 일부는
    # 빈 결과가 나오므로 100%를 기대하면 안 되지만, 대량 실패(API 한도 초과·인증 만료 등)는
    # 반드시 잡아야 한다 — 실제로 413으로 전량 실패하는 동안 몇 달간 아무도 몰랐다.
    min_success_rate: float = 0.5


@dataclass
class ClipConfig:
    # 영상을 만들지 않으므로 화질은 무의미하다 — 받은 구간은 경계 탐지(inaSpeechSegmenter)와
    # 가사 전사(Whisper)의 입력으로만 쓰이고 둘 다 오디오만 본다. 세 rendition이 같은 AAC를
    # 물고 있어(hls-hd 1000k / hls-hd4k 4000k / hls-original 8000k) 최저 화질로 받으면
    # 결과는 같고 다운로드만 8배 줄어든다.
    quality: str = "hls-hd"          # yt-dlp 포맷(540p). 오디오는 상위 rendition과 동일
    dl_pad_before_s: float = 120.0   # 경계 탐지 여유를 위해 후보 구간을 넉넉히 받음
    dl_pad_after_s: float = 90.0
    min_song_s: float = 45.0         # 구간 내 최장 음악 블록이 이보다 짧으면 노래 아님(스킵)


@dataclass
class StationConfig:
    bj_id: str = "singgyul"          # 데일리 배치 대상 스테이션
    daily_vod_count: int = 2         # 하루 처리할 미처리 VOD 수
    # 방송 후 이 일수가 지나기 전엔 서버가 손대지 않는다(0=쿨다운 없음). 팬 댓글 타임라인은
    # 방송 직후엔 없거나 쓰이는 중이라, 일찍 잡으면 되돌릴 수 없는 두 손해가 난다:
    # 타임라인 0개면 'manual'로 빠져 사람이 전체 전사로 푸는 제일 비싼 큐에 들어가고,
    # 절반만 쓰인 시점에 잡히면 그만큼만 기록하고 'analyzed'로 끝나 나머지를 조용히 잃는다.
    # 두 상태 다 자동 재처리 대상이 아니다(fetch_retryable는 failed/pending만 본다).
    # 쿨다운이 지나도 타임라인이 없으면 그때는 정상적으로 'manual' → 사람이 처리한다.
    min_vod_age_days: int = 7
    # 무-타임라인 VOD는 서버에서 처리하지 않는다 — daily가 'manual'로 표시하고, 사람이
    # 로컬에서 analyze_vod.py 전체 전사로 곡을 뽑아 `soopts ingest`로 기록한다(느리고 부정확한 서버
    # 전체 오디오 sweep을 없앴다). 로컬 full_sweep 폴백은 `soopts process`.


@dataclass
class CommentConfig:
    # 댓글 타임라인(팬이 자원해서 다는 비공식 타임스탬프)에서 노래 시각을 찾으면,
    # 정확한 길이를 모르니 앞뒤로 넉넉히 다운로드한 뒤 inaSpeechSegmenter로 정밀 경계를 찾는다.
    pad_before_s: float = 10.0
    pad_after_s: float = 300.0


@dataclass
class YouTubeConfig:
    """노래모음 업로드(soopts youtube-upload).

    `unlisted`(일부 공개)로 올린다 — 링크를 아는 사람만 볼 수 있고 검색·추천에는 뜨지 않는다.
    검수 뒤에 공개하는 2단계 게이트는 두지 않는다: performances.youtube_url이 업로드 시점에
    기록되므로 곧바로 살아있는 링크여야 소비 앱이 상태를 따로 걸러낼 필요가 없다. 완전 공개
    (public) 전환은 사람이 스튜디오에서 한다 — 코드에는 그 경로가 없다.
    """

    client_secret: str = "client_secret.json"  # Google Cloud OAuth 클라이언트(사용자 준비)
    token_file: str = "~/.config/soopts/yt_token.json"  # 최초 동의 후 저장되는 토큰
    privacy: str = "unlisted"        # 링크로만 시청 가능(검색·추천 노출 없음)
    category_id: str = "10"          # 10 = Music
    made_for_kids: bool = False
    # 쿼터: videos.insert 1600유닛/건, 일일 10000유닛. 하루 1건이면 조회/수정 여유가 충분하고,
    # 무엇보다 봇 같은 케이던스를 만들지 않는다.
    daily_upload_limit: int = 1
    # 치환자: {date}(방송일 YYYY-MM-DD) {n}(곡 수) {title} {artist}(1곡일 때만)
    title_template: str = "{date} 노래 모음 ({n}곡)"
    # 1곡짜리 VOD는 "모음"이 어색해서 곡명을 그대로 제목에 쓴다(전체의 1/4가 1곡짜리다).
    title_template_single: str = "{date} {title} - {artist}"


@dataclass
class VideoConfig:
    """노래 구간을 이어붙인 합본 영상 빌드(export/video.py).

    모든 클립을 같은 해상도/fps/SAR/샘플레이트로 재인코딩한다 — concat 디먹서가 `-c copy`로
    붙으려면 파라미터가 완전히 같아야 하고, 어차피 곡 제목 오버레이 때문에 인코딩은 피할 수 없다.
    """

    quality: str = "hls-original"    # 1920x1080 8Mbps (yt-dlp -F 실측)
    fallback_quality: str = "hls-hd4k"  # 1280x720 4Mbps — original이 없는 VOD용
    width: int = 1920
    height: int = 1080
    fps: int = 30
    crf: int = 23
    preset: str = "veryfast"         # 러너 2코어로 100분짜리를 시간 안에 끝내려면 이 이상 못 올린다
    audio_bitrate: str = "192k"
    audio_rate: int = 48000
    # 좌상단 곡 정보 오버레이: 제목/아티스트 리본 2줄 + 왼쪽 액센트 바. 리본 폭은 drawtext의
    # box에 맡겨 글자 길이에 자동으로 맞춘다(고정 폭 패널은 긴 곡 제목이 삐져나온다).
    font_file: str = "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf"
    font_file_bold: str = "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf"
    title_overlay_font_size: int = 44
    artist_overlay_font_size: int = 28   # 제목보다 작아야 어느 쪽이 곡명인지 한눈에 들어온다
    title_overlay_opacity: float = 0.6
    overlay_x: int = 40
    overlay_y: int = 40
    accent_bar_w: int = 7
    accent_color: str = "0xFFD24A@0.95"
    # 상한 — 러너 시간·디스크 예산의 안전판. 실측 최대가 30곡/109분이라 여유를 두고 잡았다.
    max_songs: int = 40
    max_total_minutes: float = 150.0


@dataclass
class Config:
    endpoints: Endpoints = field(default_factory=Endpoints)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    clip: ClipConfig = field(default_factory=ClipConfig)
    station: StationConfig = field(default_factory=StationConfig)
    comment: CommentConfig = field(default_factory=CommentConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
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
        station=_build_section(StationConfig, data.get("station", {})),
        comment=_build_section(CommentConfig, data.get("comment", {})),
        youtube=_build_section(YouTubeConfig, data.get("youtube", {})),
        video=_build_section(VideoConfig, data.get("video", {})),
    )
    if data.get("work_root") is not None:
        cfg.work_root = Path(data["work_root"])
    if work_root is not None:
        cfg.work_root = Path(work_root)
    return cfg
