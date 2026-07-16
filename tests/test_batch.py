from pathlib import Path

from soopts.batch import clip_file_path, format_summary, next_vod_status


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
