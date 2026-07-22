"""무-타임라인 VOD 전체 전사 (멀티파트·재개 가능).

파트별로 repo 다운로더(download_slice)로 받아(work/{vod}/clips/ 저장 → teardown 생존) 오디오를
추출하고, Whisper로 청크 단위 전체 전사한다. 노래는 전사문에 흐르는 가사로 찍혀 게임 대화/BGM과
구분되므로(세그멘터의 BGM 오탐 없음), 사람/Claude가 transcript.txt를 읽어 'BJ 혼자 부른 풀곡'만
골라 ingest 한다. 청크별로 캐싱해 재실행 시 이미 전사한 부분은 건너뛴다.

사용: python scripts/analyze_vod.py <title_no> [--chunk 540]
결과: work/{vod}/transcript.txt (전역 타임스탬프 [H:MM:SS] 붙은 전사)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from soopts.analyzers.stt import _ensure_uploadable
from soopts.collector.media import download_slice, resolve_m3u8_list
from soopts.collector.meta import fetch_meta
from soopts.config import load_config
from soopts.paths import work_paths


def hms(t: float) -> str:
    t = int(t)
    return f"{t//3600}:{t%3600//60:02d}:{t%60:02d}"


# --- Groq 키 로테이션 전사 --------------------------------------------------- #
_KEYS = [k for k in [os.environ.get("GROQ_API_KEY"), os.environ.get("GROQ_API_KEY_2"),
                     os.environ.get("GROQ_API_KEY_3")] if k]
_ki = 0
_clients: dict = {}


def _client():
    from groq import Groq
    if _ki not in _clients:
        _clients[_ki] = Groq(api_key=_KEYS[_ki])
    return _clients[_ki]


def _tx_once(client, path: str, scfg, langs) -> str:
    """한 청크 전사(에러를 삼키지 않고 전파 — 로테이션 판단용)."""
    best_text, best_score = "", -1e9
    for lang in langs:
        with open(path, "rb") as f:
            resp = client.audio.transcriptions.create(
                file=f, model=scfg.groq_model, language=lang, response_format="verbose_json",
            )
        segs = resp.segments or []
        if not segs:
            continue
        text = " ".join(s.get("text", "").strip() for s in segs).strip()
        score = sum(s.get("avg_logprob", 0.0) for s in segs) / len(segs)
        if score > best_score:
            best_text, best_score = text, score
    return best_text


def transcribe_rot(wav: str, cfg, start: float, dur: float) -> str:
    """키 로테이션 전사. 현재 키가 소진되면 다음 키로 교체 재시도. 모두 소진 시 예외."""
    global _ki
    scfg = cfg.stt
    langs = [scfg.language] if scfg.language else ["en", "ko"]
    with tempfile.TemporaryDirectory() as td:
        p = _ensure_uploadable(wav, Path(td), start, dur)
        if p is None:
            return ""
        while True:
            try:
                return _tx_once(_client(), p, scfg, langs)
            except Exception as e:  # noqa: BLE001
                if _ki < len(_KEYS) - 1:
                    print(f"  [키{_ki+1} 오류: {type(e).__name__}] → 키{_ki+2}로 교체", flush=True)
                    _ki += 1
                    continue
                raise


def main():
    tno = sys.argv[1]
    chunk = float(next((a.split("=")[1] for a in sys.argv if a.startswith("--chunk=")), 540))

    cfg = load_config(None)
    work = work_paths(cfg.work_root, tno).ensure()
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = work.root / "tx_chunks"
    chunks_dir.mkdir(exist_ok=True)
    meta = fetch_meta(cfg, tno, work)
    parts = meta.parts
    m3u8s = resolve_m3u8_list(tno, cfg.clip.quality)
    print(f"VOD {tno}: {hms(meta.total_duration)}, 파트 {len(m3u8s)}개 · Groq 키 {len(_KEYS)}개", flush=True)

    segments: list[tuple[float, str]] = []  # (global_start, text)

    for i, m3u8 in enumerate(m3u8s):
        if i < len(parts):
            offset, pdur = float(parts[i].offset_s), float(parts[i].duration)
        else:
            offset, pdur = (offset + pdur if i else 0.0), meta.total_duration
        raw = work.clips_dir / f"part_{i}.mp4"
        if not raw.exists():
            print(f"  파트 {i} 다운로드 (offset {hms(offset)}, {hms(pdur)})…", flush=True)
            download_slice(m3u8, 0.0, pdur or 1e9, raw, workers=cfg.collector.segment_workers)
        wav = work.clips_dir / f"part_{i}.wav"
        if not wav.exists():
            print(f"  파트 {i} 오디오 추출…", flush=True)
            subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-ac", "1", "-ar", "16000", str(wav)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        n = int((pdur or meta.total_duration) // chunk) + 1
        for c in range(n):
            local = c * chunk
            if local >= (pdur or meta.total_duration):
                break
            gstart = offset + local
            cache = chunks_dir / f"{int(gstart)}.txt"
            if cache.exists():
                text = cache.read_text(encoding="utf-8")
            else:
                text = transcribe_rot(str(wav), cfg, local, chunk)
                cache.write_text(text, encoding="utf-8")
                print(f"  전사 {hms(gstart)} ({len(text)}자)", flush=True)
            if text.strip():
                segments.append((gstart, text))

    out = work.root / "transcript.txt"
    with out.open("w", encoding="utf-8") as f:
        for gstart, text in sorted(segments):
            f.write(f"[{hms(gstart)}] {text}\n")
    print(f"\n전사 완료 → {out} ({len(segments)}청크)", flush=True)


if __name__ == "__main__":
    main()
