"""노래 클립 경계 로직 테스트 (ML 없이)."""

from soopts.export.clips import longest_music_block


def test_longest_music_block_picks_song():
    # 앞뒤 짧은 BGM(30s, 21s) + 가운데 긴 노래(196s) → 노래 경계 선택
    seg = [
        ("music", 7179, 7210), ("speech", 7210, 7293),
        ("music", 7293, 7489),  # 실제 노래(196s)
        ("speech", 7489, 7538), ("music", 7538, 7559),
    ]
    assert longest_music_block(seg) == (7293, 7489)


def test_merges_music_across_small_gap():
    # 짧은 speech(가사 사이 숨)로 끊긴 음악을 병합
    seg = [("music", 100, 130), ("speech", 130, 133), ("music", 133, 200)]
    assert longest_music_block(seg, merge_gap_s=15) == (100, 200)


def test_none_when_no_music():
    assert longest_music_block([("speech", 0, 60), ("noise", 60, 62)]) is None
