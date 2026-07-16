"""전사 기반 노래/토크 판별 + 스티커 후보 구간 테스트 (ML 없이)."""

from soopts.analyzers.audio_analyzer import sticker_burst_regions
from soopts.analyzers.stt import looks_like_song


def test_talk_transcription_rejected():
    # 실측 BGM-토크 전사(대화체) → 노래 아님
    talk = "잠시만요 정리하느라 정리하느라 한 마리나 하는 거예요 지금 아 근데 이거 되게 묵직해 이거 되게 무거워요"
    assert looks_like_song(talk) is False


def test_song_lyrics_accepted():
    # 반복 있는 가사체 → 노래
    lyric = ("I'm all about that bass about that bass no treble "
             "I'm all about that bass about that bass no treble")
    assert looks_like_song(lyric) is True


def test_too_short_not_song():
    assert looks_like_song("음 어 아") is False


def test_sticker_burst_regions_finds_hot_zone():
    # 3600초 근방에 스티커 폭증, 나머지는 드문드문
    stickers = []
    for base in range(3600, 3750, 30):  # 5개 버킷 연속(버킷당 5개)
        stickers += [base + i for i in range(5)]
    stickers += [100, 200, 5000]  # 흩어진 노이즈
    regions = sticker_burst_regions(stickers, skip_opening_s=240)
    assert len(regions) == 1
    s, e = regions[0]
    assert s <= 3600 and e >= 3740  # 폭증 구간을 덮음(패딩 포함)


def test_sticker_regions_catches_sustained_light_flow():
    # 버킷당 2개씩 얕게 지속(단일 버킷 임계는 못 넘지만 2분 윈도우 합은 넘음) → 노래로 잡아야
    stickers = []
    for base in range(3600, 3900, 30):  # 10버킷 × 2개 = 지속
        stickers += [base + 1, base + 2]
    regions = sticker_burst_regions(stickers, window_buckets=4, min_per_window=4, skip_opening_s=240)
    assert len(regions) == 1
    s, e = regions[0]
    assert s <= 3600 <= e


def test_sticker_regions_skip_opening():
    # 오프닝(인사 스티커)만 있으면 후보 없음
    stickers = [i for i in range(0, 120)]  # 0~2분 폭증
    assert sticker_burst_regions(stickers, skip_opening_s=240) == []
