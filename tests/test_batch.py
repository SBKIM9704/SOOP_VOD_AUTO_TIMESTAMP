
import pytest

import soopts.batch as batch_module
from soopts.batch import (
    RunContext,
    TimelineEvent,
    _narration_preserves_numbers,
    fmt_duration_s,
    format_detailed_summary,
    format_summary,
    format_vod_result,
    narrate_with_llm,
    next_vod_status,
    quality_warning,
    song_link,
    span_to_song,
    vod_link,
)
from soopts.config import Config


def test_format_summary_basic_counts():
    text = format_summary({"vods": 1, "detected": 3, "auto_matched": 1, "needs_review": 2})
    assert text == "VOD 1건 / 감지 3곡 / 자동매칭 1 / 검수대기 2"




def test_span_to_song_basic():
    s = span_to_song({"start_s": 100, "end_s": 250, "title": "곡", "lyrics": "가사"})
    assert (s.t, s.end, s.duration) == (100, 250, 150)
    assert s.title == "곡"
    assert s.lyrics == "가사"
    # 채팅 분석이 없으니 sticker_rate=0, 사람이 노래라 단언 → song_likely=True.
    assert s.sticker_rate == 0.0
    assert s.song_likely is True


def test_span_to_song_minimal_fields():
    """start_s/end_s만 있어도 되고, title/lyrics 없으면 None/빈문자열로 남는다."""
    s = span_to_song({"start_s": 0, "end_s": 60})
    assert s.title is None
    assert s.lyrics == ""


def test_span_to_song_rejects_bad_range():
    with pytest.raises(ValueError):
        span_to_song({"start_s": 200, "end_s": 100})  # end <= start


def test_span_to_song_requires_bounds():
    with pytest.raises(ValueError):
        span_to_song({"title": "곡"})  # start_s/end_s 없음

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

def test_run_context_alert_stops_at_limit_and_counts_suppressed(monkeypatch):
    sent = []
    monkeypatch.setattr(batch_module, "_notify_slack", lambda text: sent.append(text))
    ctx = RunContext(alert_limit=3)
    for i in range(5):
        ctx.alert(f"alert {i}")
    assert len(sent) == 3
    assert ctx.alert_sent == 3
    assert ctx.alert_suppressed == 2


def test_format_detailed_summary_includes_detection_and_footnotes():
    cfg = Config()
    ctx = RunContext()
    ctx.record(TimelineEvent(
        kind="detection", title_no="201651295",
        detail="댓글 타임라인 4곡", duration_s=1.2,
    ))
    ctx.alert_suppressed = 2
    stats = {"vods": 1, "detected": 4, "auto_matched": 4, "needs_review": 0,
             "manual_skipped": 2}

    text = format_detailed_summary(cfg, ctx, stats)

    assert text.startswith(format_summary(stats))
    assert "201651295" in text
    assert "댓글 타임라인 4곡" in text
    assert "추가 2건은 이 요약에만 반영" in text
    assert "'manual'로 표시한 VOD: 2건" in text


def test_format_detailed_summary_no_footnotes_when_nothing_suppressed():
    cfg = Config()
    ctx = RunContext()
    stats = {"vods": 0, "detected": 0, "auto_matched": 0, "needs_review": 0}
    text = format_detailed_summary(cfg, ctx, stats)
    assert "이 요약에만 반영" not in text
    assert "manual'로 표시한 VOD" not in text


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

def test_quality_warning_fires_when_stt_fails_wholesale():
    """실제로 있었던 장애: Groq가 413으로 전량 거절하는 동안 실행은 계속 성공으로 끝났다."""
    cfg = Config()
    w = quality_warning(cfg, {"stt_attempted": 32, "stt_ok": 0})
    assert w is not None
    assert "0%" in w and "0/32" in w


def test_quality_warning_silent_when_rate_is_healthy():
    assert quality_warning(Config(), {"stt_attempted": 10, "stt_ok": 8}) is None


def test_quality_warning_silent_when_nothing_attempted():
    """처리할 VOD가 없던 실행을 실패로 만들면 안 된다."""
    assert quality_warning(Config(), {"stt_attempted": 0, "stt_ok": 0}) is None


def test_quality_warning_respects_configured_threshold():
    cfg = Config()
    cfg.stt.min_success_rate = 0.9
    assert quality_warning(cfg, {"stt_attempted": 10, "stt_ok": 8}) is not None
    cfg.stt.min_success_rate = 0.7
    assert quality_warning(cfg, {"stt_attempted": 10, "stt_ok": 8}) is None


def test_quality_warning_boundary_is_inclusive():
    cfg = Config()
    cfg.stt.min_success_rate = 0.5
    assert quality_warning(cfg, {"stt_attempted": 10, "stt_ok": 5}) is None


def test_format_summary_shows_lyric_rate_and_match_basis():
    """자동매칭이 가사 덕분인지 타임라인 덕분인지 구분되어야 한다 —
    STT가 죽어도 타임라인 힌트만으로 매칭돼 정상처럼 보이던 게 이번 사건의 사각지대였다."""
    text = format_summary({
        "vods": 1, "detected": 11, "auto_matched": 7, "needs_review": 4,
        "stt_attempted": 11, "stt_ok": 9, "hint_available": 11, "lyrics_only": 0,
    })
    assert "가사확보 9/11(82%)" in text
    assert "근거 타임라인 11/가사 0" in text


def test_format_summary_omits_metrics_when_nothing_processed():
    text = format_summary({"vods": 0, "detected": 0, "auto_matched": 0, "needs_review": 0})
    assert "가사확보" not in text
    assert "근거" not in text


# --------------------------------------------------------------------------- #
# _select_vods — 재시도 > 신규 > 백필 오케스트레이션 (페이징 포함)
# --------------------------------------------------------------------------- #
def _fake_db(monkeypatch, existing_rows, retryable=None):
    """db의 I/O 함수만 스텁으로 바꾼다 — select_targets(순수 로직)는 실제 것을 쓴다.
    _select_vods가 `from soopts import db`로 실제 모듈을 잡으므로 그 함수들을 직접 패치한다."""
    from soopts import db

    upserted = {}
    monkeypatch.setattr(db, "fetch_retryable", lambda n: (retryable or [])[:n])
    monkeypatch.setattr(db, "fetch_existing",
                        lambda nos: {no: existing_rows[no] for no in nos if no in existing_rows})
    monkeypatch.setattr(db, "upsert_pending",
                        lambda targets: upserted.setdefault("rows", targets))
    return upserted


def _pages(*pages):
    def _iter(cfg, bj_id):
        yield from pages
    return _iter


def test_select_vods_backfills_across_pages(monkeypatch):
    """1페이지가 전부 처리 완료면 2페이지(과거)로 내려가 백필한다."""
    from soopts.collector import vod_list
    monkeypatch.setattr(vod_list, "iter_vod_pages", _pages(
        [{"title_no": "20"}, {"title_no": "19"}],   # 최신 페이지: 둘 다 완료
        [{"title_no": "10"}, {"title_no": "9"}],    # 과거 페이지: 미처리
    ))
    up = _fake_db(monkeypatch, existing_rows={
        "20": {"status": "done"}, "19": {"status": "analyzed"},
    })
    batch_module._select_vods(object(), "bj", 2)
    assert [r["soop_title_no"] for r in up["rows"]] == ["10", "9"]


def test_select_vods_stops_paging_once_filled(monkeypatch):
    """목표를 채우면 더 과거 페이지를 조회하지 않는다."""
    from soopts.collector import vod_list
    visited = []

    def _iter(cfg, bj_id):
        for pg in ([{"title_no": "20"}], [{"title_no": "10"}]):
            visited.append(pg[0]["title_no"])
            yield pg

    monkeypatch.setattr(vod_list, "iter_vod_pages", _iter)
    _fake_db(monkeypatch, existing_rows={})
    batch_module._select_vods(object(), "bj", 1)
    assert visited == ["20"]   # 1페이지에서 채웠으므로 2페이지 미방문


def test_select_vods_retry_skips_paging_when_full(monkeypatch):
    """재시도만으로 count가 차면 SOOP 목록을 아예 조회하지 않는다."""
    from soopts.collector import vod_list

    def _boom(cfg, bj_id):
        raise AssertionError("재시도로 찼는데 목록을 조회함")
        yield

    monkeypatch.setattr(vod_list, "iter_vod_pages", _boom)
    up = _fake_db(monkeypatch, existing_rows={},
                  retryable=[{"soop_title_no": "9", "status": "failed", "retry_count": 0}])
    batch_module._select_vods(object(), "bj", 1)
    assert [r["soop_title_no"] for r in up["rows"]] == ["9"]


def test_select_vods_converges_when_no_past_left(monkeypatch):
    """과거가 없으면(목록 소진) 채운 만큼만 반환하고 멈춘다."""
    from soopts.collector import vod_list
    monkeypatch.setattr(vod_list, "iter_vod_pages", _pages([{"title_no": "20"}]))
    up = _fake_db(monkeypatch, existing_rows={"20": {"status": "done"}})
    batch_module._select_vods(object(), "bj", 3)
    assert up["rows"] == []
