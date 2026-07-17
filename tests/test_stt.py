"""Groq Whisper API 기반 전사 선택 로직 테스트 — 실제 네트워크 호출 없이 fake client로."""

from soopts.analyzers.stt import _transcribe_best
from soopts.config import Config


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
