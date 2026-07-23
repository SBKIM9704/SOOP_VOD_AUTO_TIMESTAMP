"""제목·설명·챕터 계산 — 네트워크/DB/ffmpeg 없이 도는 순수 함수만."""

from soopts.config import Config
from soopts.export.video import ClipPlacement
from soopts.youtube_pipeline import (
    DESCRIPTION_MAX,
    chapters_valid,
    fmt_chapter_time,
    format_youtube_description,
    format_youtube_title,
)


def _place(offset, dur, title="곡", artist="가수", perf_id=1, src=1000.0):
    return ClipPlacement(
        perf_id=perf_id, title=title, artist=artist, source_start_s=src,
        offset_s=offset, duration_s=dur,
    )


def _vod(**over):
    return {
        "soop_title_no": "201933359",
        "title": "[하데스] 노래방송",
        "broadcast_date": "2026-07-19",
        **over,
    }


# --------------------------------------------------------------------------- #
# 타임스탬프 포맷 / 챕터 요건
# --------------------------------------------------------------------------- #
def test_fmt_chapter_time_drops_hour_when_zero():
    assert fmt_chapter_time(0) == "00:00"
    assert fmt_chapter_time(252) == "04:12"
    assert fmt_chapter_time(3723) == "01:02:03"
    assert fmt_chapter_time(38423) == "10:40:23"


def test_chapters_valid_requires_first_at_zero():
    """유튜브는 첫 타임스탬프가 0:00이 아니면 챕터를 만들지 않는다."""
    places = [_place(5, 100), _place(108, 100), _place(211, 100)]
    assert chapters_valid(places, 314) is False


def test_chapters_valid_requires_three():
    places = [_place(0, 100), _place(103, 100)]
    assert chapters_valid(places, 206) is False


def test_chapters_valid_requires_ten_seconds_each():
    places = [_place(0, 100), _place(103, 3), _place(109, 100)]
    assert chapters_valid(places, 212) is False


def test_chapters_valid_happy_path():
    places = [_place(0, 100), _place(103, 100), _place(206, 100)]
    assert chapters_valid(places, 309) is True


def test_chapters_valid_checks_last_chapter_against_total():
    """마지막 챕터도 10초를 넘어야 한다 — 총 길이로만 알 수 있다."""
    places = [_place(0, 100), _place(103, 100), _place(206, 100)]
    assert chapters_valid(places, 209) is False


# --------------------------------------------------------------------------- #
# 제목
# --------------------------------------------------------------------------- #
def test_title_uses_song_name_when_single():
    """1곡짜리를 '노래 모음 (1곡)'이라 부르지 않는다."""
    cfg = Config()
    title = format_youtube_title(cfg, "2026-07-19", [_place(0, 200, "벽지무늬", "아이유")])
    assert title == "2026-07-19 벽지무늬 - 아이유"


def test_title_uses_collection_template_when_many():
    cfg = Config()
    title = format_youtube_title(cfg, "2026-07-19", [_place(0, 200), _place(203, 200)])
    assert title == "2026-07-19 노래 모음 (2곡)"


def test_title_truncated_to_youtube_limit():
    cfg = Config()
    long_place = _place(0, 200, "가" * 200, "나" * 200)
    assert len(format_youtube_title(cfg, "2026-07-19", [long_place])) <= 100


# --------------------------------------------------------------------------- #
# 설명
# --------------------------------------------------------------------------- #
def test_description_chapter_block_starts_at_zero_and_has_bare_lines():
    """챕터 줄에 링크가 섞이면 그게 통째로 챕터 이름이 된다 — 제목만 있어야 한다."""
    cfg = Config()
    places = [_place(0, 200, "벽지무늬", "아이유"), _place(203, 200, "묘해, 너와", "어쿠스틱콜라보")]
    desc = format_youtube_description(cfg, _vod(), places)
    lines = desc.splitlines()
    first = next(line for line in lines if line.startswith("00:00"))
    assert first == "00:00 벽지무늬 - 아이유"
    assert lines[lines.index(first) + 1] == "03:23 묘해, 너와 - 어쿠스틱콜라보"


def test_description_has_single_source_link_in_header():
    """출처는 헤더의 다시보기 링크 하나뿐 — 곡마다 URL을 나열하지 않는다(링크 도배)."""
    cfg = Config()
    desc = format_youtube_description(cfg, _vod(), [_place(0, 200), _place(203, 200)])
    assert desc.count("vod.sooplive") == 1
    assert "change_second" not in desc
    assert desc.splitlines()[1].startswith("2026-07-19 SOOP 방송 다시보기 ▸")


def test_description_stays_within_youtube_limit():
    cfg = Config()
    places = [_place(i * 210, 200, f"아주아주 긴 곡 제목 {i}" * 4, f"아티스트{i}" * 4, perf_id=i)
              for i in range(60)]
    desc = format_youtube_description(cfg, _vod(), places)
    assert len(desc) <= DESCRIPTION_MAX
    assert "00:00 " in desc
