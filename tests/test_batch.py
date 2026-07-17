from pathlib import Path

import pytest

import soopts.batch as batch_module
from soopts.batch import (
    RunContext,
    TimelineEvent,
    _narration_preserves_numbers,
    clip_file_path,
    fmt_duration_s,
    format_detailed_summary,
    format_region_failure_alert,
    format_summary,
    format_upload_failure_alert,
    format_vod_result,
    narrate_with_llm,
    next_vod_status,
    vod_link,
)
from soopts.config import Config


def test_clip_file_path_deterministic_from_title_no_and_start_s():
    # 러너가 휘발성이라 파일이 사라져도, title_no+start_s만으로 같은 경로를 재구성해야 한다.
    p1 = clip_file_path(Path("work"), "12345", 90.0)
    p2 = clip_file_path(Path("work"), "12345", 90.0)
    assert p1 == p2 == Path("work/12345/clips/song_000090.mp4")


def test_clip_file_path_truncates_fractional_seconds():
    assert clip_file_path(Path("work"), "1", 90.9) == Path("work/1/clips/song_000090.mp4")


def test_format_summary_without_upload_stage():
    text = format_summary({"vods": 1, "detected": 3, "auto_matched": 1, "needs_review": 2})
    assert text == "VOD 1건 / 감지 3곡 / 자동매칭 1 / 검수대기 2"


def test_format_summary_with_upload_stage():
    text = format_summary({
        "vods": 2, "detected": 9, "auto_matched": 4, "needs_review": 5,
        "uploaded": 5, "queue_remaining": 4,
    })
    assert text == "VOD 2건 / 감지 9곡 / 자동매칭 4 / 검수대기 5 / 업로드 5건 (큐 잔여 4)"


def test_next_vod_status_no_songs_detected_is_done():
    # 감지된 노래가 없으면 검수·업로드할 게 없으니 바로 종결(done) — analyzed에 영구히 머물지 않는다.
    assert next_vod_status(0) == "done"


def test_next_vod_status_songs_detected_stays_analyzed():
    # 업로드 큐 소진을 거쳐야 하므로 아직 done이 아니다.
    assert next_vod_status(3) == "analyzed"


def test_vod_link_fills_title_no_and_strips_second_param():
    cfg = Config()
    assert vod_link(cfg, "201651295") == "https://vod.sooplive.co.kr/player/201651295"


def test_format_vod_result_success_includes_hyperlink_and_counts():
    cfg = Config()
    text = format_vod_result(cfg, "201651295", {"detected": 3, "auto_matched": 1, "needs_review": 2})
    assert "https://vod.sooplive.co.kr/player/201651295" in text
    assert "감지 3곡" in text
    assert "자동매칭 1" in text
    assert "검수대기 2" in text


def test_format_vod_result_reports_partial_region_failures():
    cfg = Config()
    text = format_vod_result(cfg, "201651295", {
        "detected": 1, "auto_matched": 0, "needs_review": 1,
        "region_errors": ["01:03:22~01:06:10: IncompleteRead(...)"],
    })
    assert "일부 구간 실패(1건)" in text
    assert "01:03:22~01:06:10" in text


def test_format_vod_result_whole_vod_failure():
    cfg = Config()
    text = format_vod_result(cfg, "201651295", {"error": "IncompleteRead(34668 bytes read, ...)"})
    assert text.startswith("❌ VOD 201651295 처리 실패")
    assert "IncompleteRead" in text


def test_fmt_duration_s_seconds_only():
    assert fmt_duration_s(5) == "5초"


def test_fmt_duration_s_minutes_and_seconds():
    assert fmt_duration_s(65) == "1분 5초"


def test_fmt_duration_s_hours_minutes_seconds():
    assert fmt_duration_s(3661) == "1시간 1분 1초"


def test_fmt_duration_s_zero_is_zero_seconds():
    assert fmt_duration_s(0) == "0초"


class IncompleteReadError(Exception):
    pass


def test_format_region_failure_alert_includes_context():
    cfg = Config()
    text = format_region_failure_alert(
        cfg, "201651295", "00:50:39~00:55:49", "다운로드", IncompleteReadError("60983 bytes read")
    )
    assert "201651295" in text
    assert "00:50:39~00:55:49" in text
    assert "다운로드" in text
    assert "60983 bytes read" in text
    assert "https://vod.sooplive.co.kr/player/201651295" in text


def test_format_upload_failure_alert_includes_context():
    cfg = Config()
    text = format_upload_failure_alert(cfg, "201651295", 42, 90.0, 300.0, RuntimeError("업로드 실패"))
    assert "201651295" in text
    assert "perf=42" in text
    assert "업로드 실패" in text


def test_run_context_alert_stops_at_limit_and_counts_suppressed(monkeypatch):
    sent = []
    monkeypatch.setattr(batch_module, "_notify_slack", lambda text: sent.append(text))
    ctx = RunContext(alert_limit=3)
    for i in range(5):
        ctx.alert(f"alert {i}")
    assert len(sent) == 3
    assert ctx.alert_sent == 3
    assert ctx.alert_suppressed == 2


def test_format_detailed_summary_includes_all_sections():
    cfg = Config()
    ctx = RunContext()
    ctx.record(TimelineEvent(
        kind="detection", title_no="201651295",
        detail="댓글 타임라인 (노래 4곡 파싱됨)", duration_s=1.2,
    ))
    ctx.record(TimelineEvent(
        kind="region", title_no="201651295", label="00:10:00~00:12:00", ok=True,
        detail="기다리다", duration_s=45.0, clip_duration_s=30.0, stt_duration_s=10.0,
    ))
    ctx.record(TimelineEvent(
        kind="region", title_no="201651295", label="00:50:39~00:55:49", ok=False,
        detail="다운로드: IncompleteReadError: 60983 bytes read", duration_s=12.0,
    ))
    ctx.record(TimelineEvent(
        kind="region", title_no="201651295", label="01:00:00~01:02:00", ok=None,
        detail="노래 아님(Groq) — 건너뜀", duration_s=8.0,
    ))
    ctx.record(TimelineEvent(kind="upload_item", title_no="201651295", ok=True,
                              detail="기다리다", duration_s=20.0))
    ctx.record(TimelineEvent(kind="upload_summary", count=1, duration_s=20.0))
    ctx.alert_suppressed = 2
    stats = {"vods": 1, "detected": 1, "auto_matched": 1, "needs_review": 0,
              "not_song_skipped": 1}

    text = format_detailed_summary(cfg, ctx, stats)

    assert text.startswith(format_summary(stats))
    assert "201651295" in text
    assert "댓글 타임라인 (노래 4곡 파싱됨)" in text
    assert "00:10:00~00:12:00" in text
    assert "성공" in text
    assert "실패" in text
    assert "건너뜀" in text
    assert "IncompleteReadError" in text
    assert "총 1건 성공" in text
    assert "추가 2건은 이 요약에만 반영" in text
    assert "'노래 아님'으로 판정해 검수 큐에 올리지 않고 건너뛴 구간: 1건" in text


def test_format_detailed_summary_no_footnotes_when_nothing_suppressed():
    cfg = Config()
    ctx = RunContext()
    stats = {"vods": 0, "detected": 0, "auto_matched": 0, "needs_review": 0}
    text = format_detailed_summary(cfg, ctx, stats)
    assert "이 요약에만 반영" not in text
    assert "건너뛴 구간" not in text


class _FakeGroqResponse:
    def __init__(self, content: str):
        self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": content})()})]


class _FakeGroqClient:
    def __init__(self, content: str, *, api_key=None):
        self._content = content

    @property
    def chat(self):
        client = self

        class _Completions:
            def create(self, **kwargs):
                return _FakeGroqResponse(client._content)

        return type("Chat", (), {"completions": _Completions()})()


def _fake_groq_factory(content: str):
    def _make(api_key=None):
        return _FakeGroqClient(content, api_key=api_key)

    return _make


def test_narrate_with_llm_returns_narration_when_numbers_preserved(monkeypatch):
    pytest.importorskip("groq")
    deterministic = "VOD 1건 / 감지 3곡 / 자동매칭 1 / 검수대기 2"
    monkeypatch.setattr(
        "groq.Groq", _fake_groq_factory("이번 배치에서는 VOD 1건을 처리해 3곡을 감지했고, "
                                        "그중 1곡은 자동 매칭, 2곡은 검수 대기 상태입니다.")
    )
    result = narrate_with_llm(deterministic)
    assert result != deterministic
    assert "3곡" in result


def test_narrate_with_llm_falls_back_when_groq_raises(monkeypatch):
    def _explode(api_key=None):
        raise RuntimeError("Groq API 다운")

    monkeypatch.setattr("groq.Groq", _explode, raising=False)
    deterministic = "VOD 1건 / 감지 3곡"
    assert narrate_with_llm(deterministic) == deterministic


def test_narrate_with_llm_falls_back_when_numbers_altered(monkeypatch):
    pytest.importorskip("groq")
    deterministic = "VOD 1건 / 감지 3곡 / 자동매칭 1 / 검수대기 2"
    # 숫자를 몰래 바꿔치기한 응답 — 신뢰할 수 없으므로 원문으로 폴백해야 한다.
    monkeypatch.setattr(
        "groq.Groq", _fake_groq_factory("이번 배치에서는 VOD 1건을 처리해 5곡을 감지했습니다.")
    )
    assert narrate_with_llm(deterministic) == deterministic


def test_narration_preserves_numbers_true_when_subset():
    original = "VOD 1건 / 감지 3곡 / 검수대기 2"
    narrated = "이번엔 VOD 1건에서 3곡을 감지했고 2곡은 검수 대기입니다."
    assert _narration_preserves_numbers(original, narrated)


def test_narration_preserves_numbers_false_when_count_changed():
    original = "VOD 1건 / 감지 22곡"
    narrated = "이번엔 VOD 1건에서 23곡을 감지했습니다."
    assert not _narration_preserves_numbers(original, narrated)


def test_narration_preserves_numbers_ignores_hms_labels():
    original = "구간 00:50:39~00:55:49 실패"
    narrated = "50분 39초부터 55분 49초 구간에서 실패했습니다."
    assert _narration_preserves_numbers(original, narrated)
