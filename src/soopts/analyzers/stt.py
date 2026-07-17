"""노래 구간 전사 + 노래/토크 판별 — Groq Whisper API(호스팅, GROQ_API_KEY 필요).

- 언어 자동: 후보 언어(기본 en/ko)로 각각 전사해 평균 logprob이 높은 쪽을 채택
  (노래는 반주와 섞여 자동 언어감지가 자주 틀리므로).
- 노래/토크 필터: 전사가 대화체(잠시만요/근데/이거…)면 BGM 깔린 토크로 보고 걸러낸다.
- 예전엔 로컬 faster-whisper를 썼으나, 노래 오디오에 대해 압축률/logprob 임계값을 못
  넘겨 temperature 0.0~1.0 재시도 루프를 반복하는 바람에 GH Actions CPU 러너에서 곡 하나에
  수 분씩 걸려 배치 전체가 5시간을 넘긴 사례가 있었다. Groq 호스팅 모델은 재시도 없이
  한 번에 결과를 반환해 이 지연이 없다.
무거운 import는 함수 내부에서만.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from soopts.config import Config
from soopts.log import get_logger
from soopts.models import Song

log = get_logger("analyzers.stt")

# 한국어 대화체 마커(노래엔 잘 안 나옴) — 있으면 토크(BGM)로 판단
_TALK_MARKERS = [
    "잠시만", "근데", "그니까", "그러니까", "왜냐", "어차피", "약간", "이거", "그거",
    "저거", "뭐야", "뭐지", "뭐임", "니까", "거든", "거예요", "거에요", "습니다",
    "봐봐", "이렇게", "저렇게", "아니야", "맞아요", "네네", "자 ", "어유", "에헷",
]


def looks_like_song(text: str) -> bool:
    """전사 텍스트가 노래 가사처럼 보이면 True, 대화체(BGM 토크)면 False."""
    words = text.split()
    if len(words) < 6:
        return False  # 너무 짧으면 판단 불가 → 노래 아님 처리
    talk_hits = sum(1 for w in words if any(mk in w for mk in _TALK_MARKERS))
    talk_ratio = talk_hits / len(words)
    # 물음표(대화) 밀도
    q_ratio = text.count("?") / max(len(words), 1)
    # 반복성(노래는 후렴 반복 → 고유단어 비율 낮음)
    rep = 1 - len(set(words)) / len(words)
    # 대화 마커·물음표 많으면 토크, 반복 높으면 노래 가점
    return (talk_ratio < 0.12 and q_ratio < 0.1) or rep >= 0.45


def _extract_wav(audio_path: str, start: float, dur: float, out: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-ss", str(start), "-t", str(dur),
         "-i", audio_path, "-ac", "1", "-ar", "16000", str(out)],
        capture_output=True,
    )


def _transcribe_best(client, path: str, cfg: Config) -> tuple[str, str]:
    """Groq Whisper API로 후보 언어별 전사해 평균 logprob 높은 결과를 (가사, 언어)로 반환."""
    scfg = cfg.stt
    langs = [scfg.language] if scfg.language else ["en", "ko"]
    best_text, best_lang, best_score = "", "", -1e9
    for lang in langs:
        try:
            with open(path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    file=f, model=scfg.groq_model, language=lang, response_format="verbose_json",
                )
        except Exception as e:  # noqa: BLE001
            # 언어 하나가 API 오류(레이트리밋/타임아웃 등)로 실패해도 나머지 후보 언어는
            # 계속 시도한다 — 여기서 전파하면 호출부의 곡 하나가 아니라 전사 루프 전체가
            # 죽어 이미 끝난 다른 곡의 결과까지 날아간다.
            log.warning("Groq 전사 실패(lang=%s): %s", lang, e)
            continue
        segs = resp.segments or []
        if not segs:
            continue
        text = " ".join(s.get("text", "").strip() for s in segs).strip()
        score = sum(s.get("avg_logprob", 0.0) for s in segs) / len(segs)
        if score > best_score:
            best_text, best_lang, best_score = text, lang, score
    return best_text, best_lang


def _load_model(cfg: Config):
    from groq import Groq

    return Groq()


def _finalize(song: Song, text: str, lang: str, cfg: Config) -> bool:
    song.lyrics = re.sub(r"\s+", " ", text)[: cfg.stt.lyric_chars].strip()
    is_song = looks_like_song(song.lyrics)
    log.info("%s [%s] %s → %s", song.t, lang, "노래" if is_song else "토크",
             (song.lyrics[:45] or "(빈 결과)"))
    return is_song


def transcribe_songs(
    cfg: Config, songs: list[Song], audio_path: str, *,
    max_seconds: float = 180.0, drop_talk: bool = True,
) -> list[Song]:
    """전체 오디오에서 각 구간을 잘라 전사·판별. (full-audio 모드)"""
    if not songs:
        return songs
    model = _load_model(cfg)
    kept: list[Song] = []
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "seg.wav"
        for s in songs:
            _extract_wav(audio_path, s.t, min(max_seconds, s.duration), wav)
            text, lang = _transcribe_best(model, str(wav), cfg)
            if _finalize(s, text, lang, cfg) or not drop_talk:
                kept.append(s)
    log.info("전사 후 노래 %d곡 (토크/BGM %d개 제외)", len(kept), len(songs) - len(kept))
    return kept


def transcribe_slices(
    cfg: Config, pairs: list[tuple[Song, str]], *, drop_talk: bool = True
) -> list[Song]:
    """구간별 슬라이스 파일을 직접 전사·판별. (slice 모드 — 전체 다운로드 없이)"""
    if not pairs:
        return []
    model = _load_model(cfg)
    kept: list[Song] = []
    for song, path in pairs:
        text, lang = _transcribe_best(model, path, cfg)  # Groq API가 mp4 파일을 직접 받음
        if _finalize(song, text, lang, cfg) or not drop_talk:
            kept.append(song)
    log.info("전사 후 노래 %d곡 (토크/BGM %d개 제외)", len(kept), len(pairs) - len(kept))
    return kept
