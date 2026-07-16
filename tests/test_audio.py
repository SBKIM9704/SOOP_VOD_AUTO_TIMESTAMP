"""노래 감지 후처리 순수 함수 테스트 (ML 없이)."""

from soopts.analyzers.audio_analyzer import (
    intervals_to_songs,
    merge_intervals,
    music_intervals,
    sticker_rate,
)


def test_music_intervals_filters_label():
    seg = [("speech", 0, 5), ("music", 5, 30), ("noise", 30, 31), ("music", 40, 50)]
    assert music_intervals(seg) == [(5, 30), (40, 50)]


def test_merge_and_min_length():
    assert merge_intervals([(0, 30), (35, 55)], merge_gap_s=15, min_len_s=20) == [(0, 55)]


def test_short_bgm_dropped():
    assert merge_intervals([(0, 10), (100, 130)], merge_gap_s=15, min_len_s=20) == [(100, 130)]


def test_gap_too_large_not_merged():
    assert merge_intervals([(0, 30), (100, 130)], 15, 20) == [(0, 30), (100, 130)]


def test_sticker_rate():
    # 60초 구간에 스티커 6개 → 6/분
    assert sticker_rate((0.0, 60.0), [1, 2, 3, 4, 5, 6]) == 6.0
    # 구간 밖 스티커는 세지 않음
    assert sticker_rate((0.0, 60.0), [100, 200]) == 0.0


def test_intervals_to_songs_sticker_flag():
    # 60초 음악 + 스티커 6개(6/분) → 노래 유력
    songs = intervals_to_songs([(0.0, 60.0)], 0, [1, 2, 3, 4, 5, 6], strong_rate=2.5)
    assert len(songs) == 1
    s = songs[0]
    assert s.t == 0
    assert s.duration == 60
    assert s.sticker_rate == 6.0
    assert s.song_likely is True
    # 스티커 없으면 후보
    quiet = intervals_to_songs([(0.0, 60.0)], 0, [], strong_rate=2.5)
    assert quiet[0].song_likely is False
