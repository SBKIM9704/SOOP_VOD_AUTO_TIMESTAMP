"""미디어 확보 — yt-dlp 전체 오디오 또는 HLS 세그먼트 슬라이스.

주의: 다운로드는 사용자 판단·로컬 수행 원칙(약관/저작권).

효율: 노래 감지에 전체(2시간, ~936MB)를 받을 필요 없이, 스티커로 찾은 후보 구간만
세그먼트 슬라이스로 받을 수 있다(구간당 수 MB). SOOP은 오디오 전용 포맷이 없어(전부 A+V 합쳐진
HLS) 전체 오디오도 결국 풀 다운로드이므로, 슬라이스가 유일한 절약 수단이다.
"""

from __future__ import annotations

import itertools
import subprocess
import tempfile
import time
import urllib.request
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
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
) -> float:
    """[start_s, end_s] 구간을 덮는 세그먼트만 받아 concat한 fMP4를 저장한다.

    yt-dlp --download-sections는 이 VOD의 HLS에서 빈 출력 버그가 있어, m3u8을 직접 파싱해
    해당 seg-*.m4s + init.m4s를 받아 붙인다(실측 검증됨).

    **반환값은 저장된 파일의 t=0이 실제로 몇 초인지**(세그먼트 경계라 요청한 start_s보다
    이르다). 세그먼트를 통째로만 받을 수 있어 파일은 요청 시각이 아니라 그 시각을 포함하는
    세그먼트의 시작에서 출발한다 — 이 값을 무시하고 파일 t=0을 start_s로 취급하면 전사
    타임스탬프가 최대 세그먼트 길이(~6초)만큼 어긋난다.
    """
    base, init_uri, starts, seg_uris = _parse_playlist(m3u8_url)
    idxs = _covering_idxs(starts, start_s, end_s)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        if init_uri:
            fh.write(_fetch(f"{base}/{init_uri}"))
        _write_segments(fh, [f"{base}/{seg_uris[i]}" for i in idxs], workers)
    log.info("슬라이스 저장(seg %d~%d, %ds): %s", idxs[0], idxs[-1], int(end_s - start_s), out_path)
    return starts[idxs[0]]


def slice_lead_s(m3u8_url: str, start_s: float, end_s: float) -> float:
    """요청 시작보다 슬라이스 파일이 몇 초 앞서 시작하는지(≥0). 다운로드 없이 m3u8만 읽는다.

    캐시된 슬라이스를 재사용할 때도 이 값이 필요해서 분리했다(`_parse_playlist`는 URL 단위로
    캐시되므로 같은 실행 안에서는 추가 요청이 없다).
    """
    _, _, starts, _ = _parse_playlist(m3u8_url)
    return max(0.0, start_s - starts[_covering_idxs(starts, start_s, end_s)[0]])


def split_by_part(
    s: float, e: float, parts: list[MetaPart], m3u8s: list[str]
) -> list[tuple[str, float, float]]:
    """전역 시각 구간 (s,e)가 걸치는 **모든** 파트를 (m3u8, 파트-로컬 시작, 끝)으로 쪼갠다.

    순수 함수. 단일 파트(메타 없음)면 첫 m3u8을 그대로 쓴다. 겹치는 파트가 없거나 해당
    파트의 m3u8이 없으면 빈 목록 — 호출부가 건너뛰거나 실패로 처리한다.

    예전에는 시작 파트 하나로만 매핑하고 끝을 그 파트 안으로 클램프했는데, 파트 경계를
    넘는 구간이 **에러 없이 앞부분만** 받아져 전사가 조용히 잘렸다(실제 사례: 198797609의
    방종곡이 파트3/4 경계 36140s에 걸려 앞 3초만 받아졌고, 그 결과 가사 대신 영어 환청이
    나왔다). 경계에 정확히 시작하는 구간(s == offset_s)이 이전 파트 끝으로 잘못 붙던 것도
    같은 원인이다 — 이제 겹침 판정(`e > 파트시작 and s < 파트끝`)이라 뒤 파트로 간다.
    """
    if not parts:
        return [(m3u8s[0], s, e)] if m3u8s else []
    spans: list[tuple[str, float, float]] = []
    for p in parts:
        p_start, p_end = float(p.offset_s), float(p.offset_s + p.duration)
        if e <= p_start or s >= p_end or p.idx >= len(m3u8s):
            continue
        spans.append((m3u8s[p.idx], max(s, p_start) - p_start, min(e, p_end) - p_start))
    return spans


def download_span(
    spans: list[tuple[str, float, float]], out_path: Path,
    *, workers: int = 4, force: bool = False,
) -> tuple[float, float]:
    """`split_by_part`가 쪼갠 구간들을 out_path 하나로 만들고 `(lead_s, covered_s)`를 반환.

    - **lead_s**: 요청 시작보다 파일이 앞서 시작하는 초(세그먼트 경계 때문, ≥0). 호출부는
      파일 안에서 `lead_s`부터 읽어야 요청 구간과 정확히 맞는다.
    - **covered_s**: 요청 구간의 길이.

    파트가 하나면 그대로 슬라이스 저장. 여러 파트에 걸치면 파트별로 받아 ffmpeg concat으로
    이어 붙인다 — 파트마다 자체 init.m4s(fMP4 헤더)가 있어 한 파일에 그냥 이어 쓰면
    디코더가 두 번째 파트를 읽지 못한다.

    캐시(out_path 존재)일 때도 lead_s는 계산해서 돌려준다 — 캐시 여부에 따라 타임스탬프가
    달라지면 안 되기 때문이다. 다운로드를 건너뛰어도 m3u8은 읽지만 URL 단위로 캐시된다.
    """
    if not spans:
        raise RuntimeError("구간 파트 매핑 실패 — 받을 세그먼트가 없습니다")
    out_path = Path(out_path)
    covered = sum(le - ls for _, ls, le in spans)
    head = spans[0]
    if out_path.exists() and not force:
        return slice_lead_s(*head), covered
    if len(spans) == 1:
        m3u8, ls, le = head
        return max(0.0, ls - download_slice(m3u8, ls, le, out_path, workers=workers)), covered
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp: list[Path] = []
    lead = 0.0
    try:
        for i, (m3u8, ls, le) in enumerate(spans):
            part_path = out_path.with_name(f"{out_path.stem}.part{i}{out_path.suffix}")
            actual = download_slice(m3u8, ls, le, part_path, workers=workers)
            if i == 0:
                lead = max(0.0, ls - actual)
            tmp.append(part_path)
        _concat(tmp, out_path)
        log.info("파트 %d개에 걸친 구간 병합: %s", len(tmp), out_path)
    finally:
        for p in tmp:
            p.unlink(missing_ok=True)
    return lead, covered


# --------------------------------------------------------------------------- #
def _covering_idxs(starts: list[float], start_s: float, end_s: float) -> list[int]:
    """[start_s, end_s]와 겹치는 세그먼트 인덱스. 순수 함수.

    끝 시각은 다음 세그먼트의 시작에서 얻는다(마지막은 직전 간격으로 추정) — 예전엔 길이를
    6초로 하드코딩해 `s + 6.0 >= start_s`로 골랐는데, 두 가지가 어긋났다: 요청이 세그먼트
    경계에 정확히 맞으면 아무것도 겹치지 않는 앞 세그먼트를 통째로 더 받았고, 반대로 6초보다
    긴 세그먼트가 있으면 정작 요청 시각을 품은 세그먼트를 빠뜨려 앞부분이 잘릴 수 있었다.
    """
    if not starts:
        raise RuntimeError(f"구간 세그먼트 없음: {start_s}-{end_s}")
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    ends = [*starts[1:], starts[-1] + (gaps[-1] if gaps else 6.0)]
    idxs = [i for i, (s0, e0) in enumerate(zip(starts, ends, strict=True))
            if e0 > start_s and s0 <= end_s]
    if not idxs:
        raise RuntimeError(f"구간 세그먼트 없음: {start_s}-{end_s}")
    return idxs


def _concat(paths: list[Path], out_path: Path) -> None:
    """조각들을 **오디오만** ADTS로 뽑아 이어 붙인 뒤 mp4로 리먹스한다(재인코딩 없음).

    concat 디먹서(-c copy)로는 안 된다: 파트마다 자체 타임라인(fMP4 baseMediaDecodeTime)을
    갖고 있어 두 번째 조각이 원래 시각에 그대로 얹힌다. 실측에서 390초짜리 구간이 duration
    18308초 파일이 됐고, 앞 87초 뒤로는 거대한 공백이라 뒷부분 오디오가 사실상 사라졌다.
    ADTS는 컨테이너 타임스탬프가 없어 바이트로 이으면 그대로 연속 재생된다.

    영상을 버리는 건 손해가 아니다 — 하위 단계(WAV 추출·세그멘테이션·STT)는 전부 오디오만
    본다(`clip.quality`를 최저 화질로 두는 이유와 같다).
    """
    with tempfile.TemporaryDirectory() as td:
        aacs = []
        for i, p in enumerate(paths):
            aac = Path(td) / f"{i}.aac"
            _run_ffmpeg(["-i", str(p), "-vn", "-c:a", "copy", "-f", "adts", str(aac)],
                        f"오디오 추출 실패({p.name})")
            aacs.append(str(aac))
        _run_ffmpeg(["-i", "concat:" + "|".join(aacs), "-c", "copy", str(out_path)],
                    "파트 병합 실패")
    if not out_path.exists():
        raise RuntimeError(f"파트 병합 결과 없음: {out_path}")


def _run_ffmpeg(args: list[str], err_prefix: str) -> None:
    proc = subprocess.run(["ffmpeg", "-nostdin", "-y", *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{err_prefix}: {proc.stderr.decode('utf8', 'replace').strip()[-300:]}")


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


@lru_cache(maxsize=8)
def _parse_playlist(m3u8_url: str) -> tuple[str, str, list[float], list[str]]:
    """m3u8 → (base_url, init_uri, 세그먼트별 누적시작초, 세그먼트 URI).

    VOD의 플레이리스트는 불변이라 URL 단위로 캐시한다 — 한 실행에서 구간을 여러 개 받거나
    캐시된 슬라이스의 lead만 다시 계산할 때 큰 m3u8을 반복해서 받지 않는다. 반환 리스트는
    공유되므로 호출부는 읽기만 한다.
    """
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
