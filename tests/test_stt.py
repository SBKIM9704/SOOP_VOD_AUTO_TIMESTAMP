"""Groq Whisper API 기반 전사 선택 로직 테스트 — 실제 네트워크 호출 없이 fake client로."""

import pytest

from soopts.analyzers import stt
from soopts.analyzers.stt import _transcribe_best
from soopts.config import Config
from soopts.models import Song


class _FakeTranscriptions:
    def __init__(self, by_lang):
        self.by_lang = by_lang  # lang -> list[dict](세그먼트) 또는 [] 또는 Exception 인스턴스
        self.calls = []

    def create(self, *, file, model, language, response_format):
        self.calls.append(language)
        result = self.by_lang.get(language, [])
        if isinstance(result, Exception):
            raise result
        return type("Resp", (), {"segments": result})()


class _FakeClient:
    def __init__(self, by_lang):
        self.audio = type("Audio", (), {"transcriptions": _FakeTranscriptions(by_lang)})()


def _segs(text: str, avg_logprob: float):
    return [{"text": text, "avg_logprob": avg_logprob}]


def test_transcribe_best_picks_higher_scoring_language(tmp_path):
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    client = _FakeClient({
        "en": _segs("hello world", -0.9),
        "ko": _segs("안녕하세요 노래", -0.1),
    })
    text, lang = _transcribe_best(client, str(wav), Config())
    assert lang == "ko"
    assert text == "안녕하세요 노래"


def test_transcribe_best_skips_language_with_no_segments(tmp_path):
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    client = _FakeClient({"en": [], "ko": _segs("가사 있음", -0.3)})
    text, lang = _transcribe_best(client, str(wav), Config())
    assert lang == "ko"
    assert text == "가사 있음"


def test_transcribe_best_respects_forced_language(tmp_path):
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    client = _FakeClient({"en": _segs("hello", -0.2)})
    cfg = Config()
    cfg.stt.language = "en"
    text, lang = _transcribe_best(client, str(wav), cfg)
    assert lang == "en"
    assert text == "hello"
    assert client.audio.transcriptions.calls == ["en"]


def test_transcribe_best_returns_empty_when_all_languages_empty(tmp_path):
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    client = _FakeClient({"en": [], "ko": []})
    text, lang = _transcribe_best(client, str(wav), Config())
    assert text == ""
    assert lang == ""


def test_transcribe_best_skips_language_that_raises(tmp_path):
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    client = _FakeClient({"en": RuntimeError("rate limited"), "ko": _segs("가사 있음", -0.3)})
    text, lang = _transcribe_best(client, str(wav), Config())
    assert lang == "ko"
    assert text == "가사 있음"
    assert client.audio.transcriptions.calls == ["en", "ko"]


def test_transcribe_best_returns_empty_when_all_languages_raise(tmp_path):
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    client = _FakeClient({"en": RuntimeError("timeout"), "ko": RuntimeError("timeout")})
    text, lang = _transcribe_best(client, str(wav), Config())
    assert text == ""
    assert lang == ""


# --------------------------------------------------------------------------- #
# _extract_wav — ffmpeg 실행 없이 반환값/정리 동작만 검증
# --------------------------------------------------------------------------- #
def _song() -> Song:
    return Song(t=600, end=780, duration=180, sticker_rate=1.0, song_likely=True)


class _FakeProc:
    def __init__(self, returncode, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def test_extract_wav_returns_false_when_ffmpeg_fails(tmp_path, monkeypatch):
    out = tmp_path / "seg.wav"
    monkeypatch.setattr(stt.subprocess, "run", lambda *a, **k: _FakeProc(1, b"boom"))
    assert stt._extract_wav("in.mp4", 0.0, 10.0, out) is False


def test_extract_wav_returns_false_when_output_missing(tmp_path, monkeypatch):
    """ffmpeg가 0을 반환해도 산출물이 없으면 실패로 본다."""
    out = tmp_path / "seg.wav"
    monkeypatch.setattr(stt.subprocess, "run", lambda *a, **k: _FakeProc(0))
    assert stt._extract_wav("in.mp4", 0.0, 10.0, out) is False


def test_extract_wav_removes_stale_output_before_running(tmp_path, monkeypatch):
    """직전 곡의 WAV가 남아 다음 곡 가사로 잘못 붙는 것을 막는다."""
    out = tmp_path / "seg.wav"
    out.write_bytes(b"previous-song")
    monkeypatch.setattr(stt.subprocess, "run", lambda *a, **k: _FakeProc(1, b"boom"))
    assert stt._extract_wav("in.mp4", 0.0, 10.0, out) is False
    assert not out.exists()


def test_extract_wav_returns_true_on_success(tmp_path, monkeypatch):
    out = tmp_path / "seg.wav"

    def _run(*a, **k):
        out.write_bytes(b"wav-data")
        return _FakeProc(0)

    monkeypatch.setattr(stt.subprocess, "run", _run)
    assert stt._extract_wav("in.mp4", 0.0, 10.0, out) is True


def test_transcribe_best_extracts_audio_from_video_clip(tmp_path, monkeypatch):
    """1080p mp4를 그대로 올리면 Groq가 413으로 거절한다 — 추출된 WAV가 가야 한다."""
    mp4 = tmp_path / "song_001883.mp4"
    mp4.write_bytes(b"x" * (200 * 1024 * 1024))  # 실제 클립 크기대(수백 MB)
    sent: list[str] = []

    def _fake_extract(src, start, dur, out):
        sent.append(src)
        out.write_bytes(b"wav-data")
        return True

    monkeypatch.setattr(stt, "_extract_wav", _fake_extract)
    client = _FakeClient({"en": _segs("lyrics", -0.2), "ko": []})
    monkeypatch.setattr(stt, "_transcribe_langs",
                        lambda c, path, scfg, langs: (sent.append(path), ("가사", "ko"))[1])
    text, lang = _transcribe_best(client, str(mp4), Config())
    assert sent[0] == str(mp4)          # 추출 입력은 mp4
    assert sent[1].endswith("stt.wav")  # Groq로 가는 건 WAV
    assert text == "가사"


def test_transcribe_best_passes_through_small_wav(tmp_path, monkeypatch):
    """transcribe_songs는 이미 구간 WAV를 넘기므로 재변환하지 않는다."""
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    monkeypatch.setattr(stt, "_extract_wav",
                        lambda *a, **k: pytest.fail("한도 안쪽 WAV는 재변환하면 안 된다"))
    client = _FakeClient({"en": _segs("hello", -0.5), "ko": []})
    text, _ = _transcribe_best(client, str(wav), Config())
    assert text == "hello"


def test_transcribe_best_returns_empty_when_extraction_fails(tmp_path, monkeypatch):
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"broken")
    monkeypatch.setattr(stt, "_extract_wav", lambda *a, **k: False)
    monkeypatch.setattr(stt, "_transcribe_langs",
                        lambda *a, **k: pytest.fail("추출 실패 시 업로드하면 안 된다"))
    assert _transcribe_best(_FakeClient({}), str(mp4), Config()) == ("", "")


def _tsegs(*items):
    """(text, start, end) 튜플들을 verbose_json 세그먼트(dict)로. avg_logprob는 고정."""
    return [{"text": t, "start": s, "end": e, "avg_logprob": -0.2} for (t, s, e) in items]


def test_transcribe_segments_returns_slim_timestamps(tmp_path):
    """세그먼트별 {start,end,text}만 남기고 float은 소수1자리로 반올림한다."""
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    client = _FakeClient({"ko": _tsegs(("가사 한 줄", 0.0, 3.44), ("다음 줄", 3.44, 7.06))})
    segs, lang = stt._transcribe_segments(client, str(wav), Config().stt, ["ko"])
    assert lang == "ko"
    assert segs == [
        {"start": 0.0, "end": 3.4, "text": "가사 한 줄"},
        {"start": 3.4, "end": 7.1, "text": "다음 줄"},
    ]


def test_transcribe_segments_picks_best_scoring_language(tmp_path):
    """언어별 평균 logprob이 높은 쪽의 세그먼트를 고른다(텍스트 선택과 동일 기준)."""
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"fake-audio")
    en = [{"text": "la la", "start": 0.0, "end": 2.0, "avg_logprob": -0.9}]
    ko = [{"text": "노래 가사", "start": 0.0, "end": 2.0, "avg_logprob": -0.1}]
    segs, lang = stt._transcribe_segments(_FakeClient({"en": en, "ko": ko}), str(wav),
                                          Config().stt, ["en", "ko"])
    assert lang == "ko"
    assert [s["text"] for s in segs] == ["노래 가사"]
