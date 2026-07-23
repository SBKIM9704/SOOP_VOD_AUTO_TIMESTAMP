"""노래 구간 합본 영상 빌드 — 유튜브 업로드용 mp4 하나를 만든다.

곡마다 [제목 카드 → 노래 클립]을 붙이고 전부 이어 붙인다. 모든 조각을 **같은**
해상도/fps/SAR/픽셀포맷/48kHz 스테레오로 재인코딩하는 게 핵심이다 — 그래야 마지막 concat을
`-c copy`로 끝낼 수 있다(파라미터가 하나라도 다르면 디먹서가 붙기를 거부하거나 재생이 깨진다).
어차피 곡 제목 오버레이 때문에 인코딩은 피할 수 없으므로 손해가 아니다.

**멀티파트 주의: `download_span`을 쓰면 안 된다.** 그쪽 병합은 오디오만 ADTS로 이어 붙여
영상을 버린다(전사 파이프라인 전용). 여기서는 `split_by_part`로 나온 스팬을 각각 받아 각각
재인코딩하고 최종 concat에 순서대로 넣는다 — 재인코딩이 타임스탬프를 0부터 다시 쓰므로
fMP4의 baseMediaDecodeTime 문제도 자연히 사라진다.

**텍스트는 drawtext의 `textfile=`로 넣는다.** 곡 제목에 `묘해, 너와`처럼 쉼표가 들어가면
인라인 `text=`는 필터그래프가 거기서 잘려 통째로 깨진다(콜론·작은따옴표·`%`·역슬래시도 같은 문제).
파일로 넘기면 이스케이프가 아예 필요 없다. `%{...}` 치환만 `expansion=none`으로 막는다.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from soopts.config import Config
from soopts.log import get_logger
from soopts.output import fmt_hms

log = get_logger("export.video")


@dataclass
class ClipPlacement:
    """합본 안에서 곡 하나가 놓인 자리 — 챕터·링크·설명이 전부 여기서 계산된다."""

    perf_id: int
    title: str
    artist: str
    source_start_s: float     # 원본 VOD 절대초(설명의 원본 딥링크용)
    # 곡이 곧바로 시작하므로 챕터 시작과 노래 시작이 같다. 둘을 나눠 두었던 건 곡 앞에 제목
    # 카드를 3초 붙이던 시절의 흔적인데, 카드를 없애면서 구분할 이유가 사라졌다.
    offset_s: float           # 합본 내 시작초 = 챕터 시작 = performances.youtube_url의 ?t=
    duration_s: float

    @property
    def label(self) -> str:
        return f"{self.title} - {self.artist}"


# --------------------------------------------------------------------------- #
# 순수 함수
# --------------------------------------------------------------------------- #
def plan_songs(cfg: Config, perfs: list[dict]) -> tuple[list[dict], list[dict]]:
    """빌드할 곡과 상한에 걸려 잘린 곡을 나눠 돌려준다(순수 함수).

    상한(`max_songs`/`max_total_minutes`)은 러너 시간·디스크의 안전판이다. 끝을 알 수 없는
    (end_s가 없거나 시작보다 이른) 구간은 길이를 정할 수 없어 아예 뺀다 — 검증(verified)을
    통과한 곡에는 없어야 정상이지만, 여기서 걸러야 ffmpeg가 이상한 `-t`로 도는 일이 없다.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    total = 0.0
    for p in sorted(perfs, key=lambda p: p["start_s"]):
        dur = float(p.get("end_s") or 0) - float(p["start_s"])
        if dur <= 0:
            dropped.append(p)
            continue
        if len(kept) >= cfg.video.max_songs or (total + dur) > cfg.video.max_total_minutes * 60:
            dropped.append(p)
            continue
        kept.append(p)
        total += dur
    return kept, dropped


def build_concat_list(paths: list[Path]) -> str:
    """concat 디먹서 입력 파일의 본문(순수 함수).

    **절대경로로 적는다.** concat 디먹서는 상대경로를 목록 파일이 있는 디렉터리 기준으로
    해석하는데, 목록 파일도 조각들과 같은 폴더에 두다 보니 `work/x/ytbuild/work/x/ytbuild/…`
    처럼 접두사가 두 번 붙어 전부 "No such file"이 됐다(실제로 겪음).

    경로는 작은따옴표로 감싸고 그 안의 작은따옴표만 `'\\''`로 끊어준다 — concat 디먹서의
    유일한 이스케이프 규칙이다.
    """
    quote, esc = chr(39), chr(39) + chr(92) + chr(39) + chr(39)
    return "".join(f"file '{str(Path(p).absolute()).replace(quote, esc)}'\n" for p in paths)


# --------------------------------------------------------------------------- #
# ffmpeg 래퍼
# --------------------------------------------------------------------------- #
def _run(args: list[str], err_prefix: str) -> None:
    proc = subprocess.run(["ffmpeg", "-nostdin", "-y", *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{err_prefix}: {proc.stderr.decode('utf8', 'replace').strip()[-400:]}")


def probe_duration_s(path: Path) -> float:
    """ffprobe로 실제 길이를 잰다(실패 시 0.0).

    요청한 `-t` 값을 그대로 믿으면 안 된다 — 원본이 먼저 끝나면 클립이 짧게 나오고, 그 오차가
    누적되면 뒤쪽 챕터가 통째로 밀린다. 챕터·링크 오프셋은 항상 실측값으로 쌓는다.
    """
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        log.warning("길이 측정 실패(ffprobe): %s", path.name)
        return 0.0


# 설정된 폰트가 없을 때 찾아볼 후보. 러너는 apt fonts-nanum, 개발 환경(WSL)은 윈도우 맑은고딕.
_FONT_FALLBACKS = (
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/mnt/c/Windows/Fonts/malgun.ttf",
)
_FONT_FALLBACKS_BOLD = (
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/mnt/c/Windows/Fonts/malgunbd.ttf",
    *_FONT_FALLBACKS,   # 굵은 게 없으면 보통 굵기라도 — 위계는 크기가 이미 만들어준다
)


def assert_drawtext_available() -> None:
    """drawtext 필터가 실제로 있는지 확인한다(빌드 시작 전 1회).

    johnvansickle static 빌드처럼 `configuration:`에 `--enable-libfreetype`이 찍혀 있는데도
    drawtext가 빠진 경우가 있다(freetype만으로는 부족하고 harfbuzz까지 필요). 이걸 미리 안
    잡으면 곡마다 인코딩이 실패하고, 곡 단위 예외 처리가 그걸 전부 "이 곡만 건너뜀"으로
    삼켜서 결국 **아무 이유 없이 0곡**이 되는 것처럼 보인다(실제로 겪음).
    """
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    if "drawtext" not in proc.stdout:
        raise RuntimeError(
            "ffmpeg에 drawtext 필터가 없습니다(곡 제목 오버레이 불가). "
            "`apt-get install -y ffmpeg`로 배포판 빌드를 쓰세요 — "
            "일부 static 빌드는 --enable-libfreetype 표기와 달리 drawtext가 빠져 있습니다."
        )


def resolve_font(cfg: Config, *, bold: bool = False) -> str:
    """실제로 존재하는 한글 폰트 경로를 고른다. 없으면 **빌드 시작 전에** 실패한다.

    없는 폰트로 그냥 진행하면 제목이 전부 두부(□□□)로 박힌 영상이 만들어지는데, 그걸
    수 GB 다운로드와 한 시간 인코딩이 끝난 뒤에 알게 된다. 게다가 삭제 API를 쓰지 않기로 해서
    잘못 올린 영상은 되돌리기도 어렵다 — 그래서 조용한 폴백 대신 조기 실패를 택했다.
    """
    want = cfg.video.font_file_bold if bold else cfg.video.font_file
    for cand in (want, *(_FONT_FALLBACKS_BOLD if bold else _FONT_FALLBACKS)):
        if cand and Path(cand).expanduser().exists():
            if cand != want:
                log.warning("설정된 폰트(%s)가 없어 %s 사용", want, cand)
            return str(Path(cand).expanduser())
    raise RuntimeError(
        f"한글 폰트를 찾을 수 없습니다: {want}. "
        "러너는 `apt-get install -y fonts-nanum`, 로컬은 [video] font_file을 지정하세요."
    )


def _write_label(out_dir: Path, name: str, text: str) -> Path:
    p = out_dir / f"{name}.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _ribbon(cfg: Config, font: str, textfile: Path, *, size: int, y: int,
            alpha: float, pad_y: int) -> str:
    """글자 폭에 딱 맞는 반투명 리본 한 줄(drawtext 자체 box).

    폭을 계산하지 않고 drawtext의 box에 맡기는 게 요점이다 — 곡 제목 길이는 천차만별이라
    고정 폭 패널을 쓰면 긴 제목이 그대로 삐져나온다. 리본이 줄마다 따로 생겨 계단처럼 보이는
    건 의도된 모양이다(방송 자막에서 흔한 스택 리본).
    """
    v = cfg.video
    return (
        f"drawtext=fontfile={font}:textfile={textfile}:expansion=none"
        f":fontsize={size}:fontcolor=white@{alpha}:x={v.overlay_x + 22}:y={y}"
        f":box=1:boxcolor=black@{v.title_overlay_opacity}:boxborderw={pad_y}|22|{pad_y}|22"
    )


def overlay_filters(cfg: Config, title_file: Path, artist_file: Path) -> str:
    """좌상단 곡 정보 오버레이 — 제목/아티스트 리본 2줄 + 왼쪽 액센트 바.

    제목은 굵게 크게, 아티스트는 작고 옅게 해서 위계를 준다(둘이 같은 크기면 어느 쪽이
    곡명인지 한눈에 안 들어온다). 액센트 바는 **리본을 그린 뒤에** 얹는다 — 먼저 그리면
    리본 박스가 그 위를 덮어 보이지 않는다.
    """
    v = cfg.video
    font_bold, font_regular = resolve_font(cfg, bold=True), resolve_font(cfg)
    y_title = v.overlay_y + 12
    y_artist = y_title + int(v.title_overlay_font_size * 1.45)
    bar_h = y_artist + int(v.artist_overlay_font_size * 1.55) - v.overlay_y
    return ",".join([
        _ribbon(cfg, font_bold, title_file,
                size=v.title_overlay_font_size, y=y_title, alpha=1.0, pad_y=12),
        _ribbon(cfg, font_regular, artist_file,
                size=v.artist_overlay_font_size, y=y_artist, alpha=0.8, pad_y=10),
        f"drawbox=x={v.overlay_x}:y={v.overlay_y}:w={v.accent_bar_w}:h={bar_h}"
        f":color={v.accent_color}:t=fill",
    ])


def _encode_args(cfg: Config) -> list[str]:
    """모든 조각이 공유해야 하는 코덱 파라미터 — 이게 어긋나면 concat -c copy가 깨진다."""
    v = cfg.video
    return [
        "-c:v", "libx264", "-crf", str(v.crf), "-preset", v.preset, "-pix_fmt", "yuv420p",
        "-r", str(v.fps), "-c:a", "aac", "-b:a", v.audio_bitrate,
        "-ar", str(v.audio_rate), "-ac", "2",
    ]


def cut_song_clip(
    cfg: Config, src: Path, ss: float, dur: float,
    title_file: Path, artist_file: Path, out: Path,
) -> Path:
    """src의 [ss, ss+dur]를 잘라 정규화 + 좌상단 곡 정보 오버레이로 재인코딩한다.

    `-ss`를 **입력이 아니라 출력 쪽**(-i 뒤)에 둔다. 입력 -ss는 키프레임으로 되감기는데,
    슬라이스 파일의 첫 키프레임은 사실상 t=0이라 스킵이 통째로 무시된다 — 세그먼트 경계 때문에
    앞에 붙은 lead가 그대로 남아 곡이 몇 초 일찍 시작한다.
    """
    v = cfg.video
    vf = (
        f"scale={v.width}:{v.height}:force_original_aspect_ratio=decrease,"
        f"pad={v.width}:{v.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={v.fps},"
        + overlay_filters(cfg, title_file, artist_file)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-i", str(src), "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-vf", vf,
          *_encode_args(cfg), "-movflags", "+faststart", str(out)],
         f"클립 인코딩 실패({out.name})")
    return out


def concat_clips(cfg: Config, paths: list[Path], out: Path) -> Path:
    """조각들을 이어 붙인다. `-c copy` 우선, 실패하면 재인코딩으로 폴백한다."""
    list_file = out.parent / "concat.txt"
    list_file.write_text(build_concat_list(paths), encoding="utf-8")
    base = ["-f", "concat", "-safe", "0", "-i", str(list_file)]
    try:
        _run([*base, "-c", "copy", "-movflags", "+faststart", str(out)], "concat(copy) 실패")
        log.info("concat: -c copy 경로로 %d조각 병합", len(paths))
    except RuntimeError as e:
        log.warning("concat -c copy 실패 — 재인코딩으로 폴백: %s", e)
        _run([*base, *_encode_args(cfg), "-movflags", "+faststart", str(out)],
             "concat(재인코딩) 실패")
        log.info("concat: 재인코딩 경로로 %d조각 병합", len(paths))
    return out


# --------------------------------------------------------------------------- #
# 오케스트레이션
# --------------------------------------------------------------------------- #
def resolve_playlists(cfg: Config, title_no: str) -> list[str]:
    """파트별 m3u8. 기본 화질이 없는 VOD면 폴백 화질로 한 번만 재시도한다."""
    from soopts.collector.media import resolve_m3u8_list

    try:
        return resolve_m3u8_list(title_no, cfg.video.quality)
    except RuntimeError as e:
        log.warning("화질 %s 조회 실패 — %s로 폴백: %s", cfg.video.quality,
                    cfg.video.fallback_quality, e)
        return resolve_m3u8_list(title_no, cfg.video.fallback_quality)


def _download_song_pieces(
    cfg: Config, perf: dict, m3u8s: list[str], parts, out_dir: Path, idx: int
) -> list[tuple[Path, float, float]]:
    """곡 하나를 (슬라이스 파일, 파일 내 시작초, 길이) 목록으로 받는다. 파트에 걸치면 2개 이상.

    반환이 빈 목록이면 파트 매핑에 실패한 것 — 호출부가 그 곡만 건너뛴다.
    """
    from soopts.collector.media import download_slice, slice_lead_s, split_by_part

    s, e = float(perf["start_s"]), float(perf["end_s"])
    spans = split_by_part(s, e, parts, m3u8s)
    if not spans:
        return []
    pieces: list[tuple[Path, float, float]] = []
    for j, (m3u8, ls, le) in enumerate(spans):
        raw = out_dir / f"raw_{idx:03d}_{j}.mp4"
        if raw.exists() and raw.stat().st_size > 0:
            actual = ls - slice_lead_s(m3u8, ls, le)   # 캐시 재사용(로컬 반복 실행용)
        else:
            actual = download_slice(m3u8, ls, le, raw, workers=cfg.collector.segment_workers)
        pieces.append((raw, max(0.0, ls - actual), le - ls))
    return pieces


def build_vod_video(
    cfg: Config, title_no: str, perfs: list[dict], meta, out_dir: Path
) -> tuple[Path | None, list[ClipPlacement], list[dict]]:
    """노래 구간들을 이어붙인 합본 mp4를 만든다.

    반환 `(출력 경로, 배치 목록, 제외된 곡)`. 쓸 수 있는 곡이 하나도 없으면 경로가 None이다
    (업로드할 게 없다는 뜻 — 호출부가 'no_songs'로 종결한다).

    한 곡이 실패해도 전체를 실패시키지 않는다. 30곡짜리 VOD에서 파트 매핑 하나가 어긋났다고
    나머지 29곡을 버리는 건 손해가 크고, 어차피 설명·링크는 **실제로 들어간 곡**만 참조한다.
    """
    from soopts.batch import _resolved_title_artist

    out_dir.mkdir(parents=True, exist_ok=True)
    # 다운로드·인코딩(수 GB, 수십 분) 전에 텍스트 렌더 준비부터 확인한다 — 실패하면 여기서 끝난다.
    assert_drawtext_available()
    resolve_font(cfg)
    kept, dropped = plan_songs(cfg, perfs)
    if dropped:
        log.warning("상한/구간 문제로 제외된 곡 %d개 (max_songs=%d, max_total_minutes=%.0f)",
                    len(dropped), cfg.video.max_songs, cfg.video.max_total_minutes)
    m3u8s = resolve_playlists(cfg, title_no)

    segments: list[Path] = []       # concat에 들어갈 클립(파트에 걸친 곡은 2개 이상)
    placements: list[ClipPlacement] = []
    offset = 0.0

    for i, perf in enumerate(kept):
        title, artist = _resolved_title_artist(perf)
        # 곡 중간에 실패하면 그 곡이 남긴 조각·오프셋을 되돌린다. 파트에 걸친 곡은 조각이
        # 여러 개라 "곡당 n조각"을 가정할 수 없으므로, 시작 지점을 기억해 잘라낸다.
        mark, offset_before = len(segments), offset
        try:
            pieces = _download_song_pieces(cfg, perf, m3u8s, meta.parts, out_dir, i)
            if not pieces:
                raise RuntimeError("파트 매핑 실패(구간이 어느 파트에도 안 걸림)")
            title_file = _write_label(out_dir, f"title_{i:03d}", title)
            artist_file = _write_label(out_dir, f"artist_{i:03d}", artist)
            song_offset = offset
            for j, (raw, ss, dur) in enumerate(pieces):
                clip = cut_song_clip(cfg, raw, ss, dur, title_file, artist_file,
                                     out_dir / f"clip_{i:03d}_{j}.mp4")
                raw.unlink(missing_ok=True)   # 원본 슬라이스는 즉시 버린다(디스크 예산)
                segments.append(clip)
                offset += probe_duration_s(clip) or dur
        except Exception as ex:  # noqa: BLE001 — 곡 하나의 실패로 전체를 버리지 않는다
            log.warning("%s %s — 이 곡만 건너뜀: %s",
                        fmt_hms(int(perf["start_s"])), f"{title} - {artist}", ex)
            del segments[mark:]
            offset = offset_before
            dropped.append(perf)
            continue
        placements.append(ClipPlacement(
            perf_id=perf["id"], title=title, artist=artist,
            source_start_s=float(perf["start_s"]),
            offset_s=round(song_offset, 3),
            duration_s=round(offset - song_offset, 3),
        ))
        log.info("[%d/%d] %s — 합본 %s (%d초)", len(placements), len(kept),
                 f"{title} - {artist}", fmt_hms(int(song_offset)), int(offset - song_offset))

    if not placements:
        log.warning("합본에 넣을 수 있는 곡이 없습니다 (VOD %s)", title_no)
        return None, [], dropped

    out = out_dir / "output.mp4"
    concat_clips(cfg, segments, out)
    log.info("합본 완료: %s (%d곡, %s)", out, len(placements), fmt_hms(int(offset)))
    return out, placements, dropped
