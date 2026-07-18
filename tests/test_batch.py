
import pytest

import soopts.batch as batch_module
from soopts.analyzers.comment_timeline import TimelineSong
from soopts.batch import (
    RunContext,
    TimelineEvent,
    _narration_preserves_numbers,
    comment_candidates,
    fmt_duration_s,
    format_detailed_summary,
    format_region_failure_alert,
    format_summary,
    format_vod_result,
    narrate_with_llm,
    next_vod_status,
    song_link,
    vod_link,
)
from soopts.config import Config


def test_format_summary_basic_counts():
    text = format_summary({"vods": 1, "detected": 3, "auto_matched": 1, "needs_review": 2})
    assert text == "VOD 1건 / 감지 3곡 / 자동매칭 1 / 검수대기 2"




def test_next_vod_status_no_songs_detected_is_done():
    # 감지된 노래가 없으면 검수·업로드할 게 없으니 바로 종결(done) — analyzed에 영구히 머물지 않는다.
    assert next_vod_status(0) == "done"


def test_next_vod_status_songs_detected_stays_analyzed():
    # 업로드 큐 소진을 거쳐야 하므로 아직 done이 아니다.
    assert next_vod_status(3) == "analyzed"


def test_vod_link_fills_title_no_and_strips_second_param():
    cfg = Config()
    assert vod_link(cfg, "201651295") == "https://vod.sooplive.co.kr/player/201651295"


def test_song_link_points_at_song_start_second():
    """유튜브 업로드를 대체하는 시청 경로 — 원본 VOD의 노래 시작 시각으로 바로 이동."""
    link = song_link(Config(), "201217563", 1883)
    assert link == "https://vod.sooplive.co.kr/player/201217563?change_second=1883"


def test_song_link_truncates_fractional_seconds():
    assert song_link(Config(), "201217563", 1883.97).endswith("change_second=1883")


def test_song_link_differs_from_plain_vod_link():
    cfg = Config()
    assert "change_second" not in vod_link(cfg, "1")
    assert "change_second" in song_link(cfg, "1", 10)


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


def test_comment_candidates_caps_at_next_song_start():
    # "고양이"(10811)와 "Lip"(11139) 사이 간격(328초)이 pad_after_s(300)보다 크지만,
    # 두 곡이 더 가까이 붙어있는 경우(예: 간격 200초)를 재현 — 다음 곡을 침범하면 안 된다.
    timeline = [
        TimelineSong(time_s=10811, title="고양이", artist="선우정아"),
        TimelineSong(time_s=11011, title="Lip", artist="디어클라우드"),  # 200초 뒤
    ]
    candidates = comment_candidates(timeline, pad_before_s=10.0, pad_after_s=300.0)
    ds0, de0, hint0 = candidates[0]
    assert hint0.title == "고양이"
    assert de0 == 11011  # 다음 곡 시작 시각에서 캡핑됨(10811+300=11111이 아니라)


def test_comment_candidates_uses_full_pad_when_gap_is_large():
    timeline = [
        TimelineSong(time_s=10811, title="고양이", artist="선우정아"),
        TimelineSong(time_s=99999, title="다음날 곡", artist="누군가"),
    ]
    candidates = comment_candidates(timeline, pad_before_s=10.0, pad_after_s=300.0)
    _, de0, _ = candidates[0]
    assert de0 == 10811 + 300.0  # 간격이 넉넉하면 그대로 pad_after_s


def test_comment_candidates_last_song_uses_full_pad_after():
    timeline = [TimelineSong(time_s=10811, title="고양이", artist="선우정아")]
    candidates = comment_candidates(timeline, pad_before_s=10.0, pad_after_s=300.0)
    ds0, de0, _ = candidates[0]
    assert ds0 == 10801.0
    assert de0 == 11111.0


def test_comment_candidates_caps_correctly_even_when_list_order_is_not_chronological():
    # 실제 프로덕션 버그 재현: Groq가 댓글에서 곡을 시간순이 아닌 순서로 추출하면
    # timeline[i+1] 기반 캡핑이 엉뚱한 값을 참조해 두 구간이 겹치게 된다("크레파스"
    # 7341~7557과 "이름에게" 7435~7707이 122초 겹친 사례). 리스트 순서를 일부러 뒤섞어도
    # 실제 시각 기준으로 올바르게 캡핑돼야 한다.
    timeline = [
        TimelineSong(time_s=7435, title="이름에게", artist="아이유"),  # 리스트상 먼저 나오지만
        TimelineSong(time_s=7341, title="크레파스", artist="누군가"),  # 시각은 이게 더 이름
    ]
    candidates = comment_candidates(timeline, pad_before_s=10.0, pad_after_s=300.0)
    by_title = {hint.title: (ds, de) for ds, de, hint in candidates}
    assert by_title["크레파스"][1] == 7435  # "이름에게" 시작 전까지만
    assert by_title["이름에게"][1] == 7435 + 300.0  # 그 뒤엔 곡이 없으니 pad_after_s 그대로




