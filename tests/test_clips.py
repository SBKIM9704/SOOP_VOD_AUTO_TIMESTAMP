"""노래 클립 경계 로직 테스트 (ML 없이)."""

from soopts.config import Config
from soopts.export import clips
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


# --------------------------------------------------------------------------- #
# detect_song_span — 영상을 만들지 않고 경계 시각만 돌려준다
# --------------------------------------------------------------------------- #
def test_detect_song_span_returns_global_and_local_boundaries(monkeypatch):
    """전역 시각은 DB 기록용, 로컬 시각은 같은 파일에서 그 구간만 전사하기 위해 필요하다."""
    monkeypatch.setattr(clips, "refine_boundary", lambda cfg, path, s, e: (5.0, 229.0))
    span = clips.detect_song_span(Config(), "region_3792.mp4", 3792.0, 4102.0, media_offset=3792.0)
    assert span is not None
    clip, local_start, local_end = span
    assert (clip.t, clip.end, clip.duration) == (3797, 4021, 224)
    assert (local_start, local_end) == (5.0, 229.0)


def test_detect_song_span_returns_none_when_no_music_block(monkeypatch):
    monkeypatch.setattr(clips, "refine_boundary", lambda cfg, path, s, e: None)
    assert clips.detect_song_span(Config(), "x.mp4", 0.0, 310.0) is None


def test_clip_has_no_file_path_field():
    """영상을 만들지 않으므로 Clip은 파일 경로를 갖지 않는다."""
    assert "path" not in clips.Clip.__dataclass_fields__


def test_cut_clip_is_gone():
    """재인코딩 제거 — 되살아나면 실행 시간의 76%가 돌아온다."""
    assert not hasattr(clips, "cut_clip")
