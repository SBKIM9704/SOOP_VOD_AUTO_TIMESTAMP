"""클립금지 레지스트리 — 파싱과 게이트 판정(순수 함수만, 파일/DB 없음)."""

from __future__ import annotations

import pytest

from soopts.clip_ban import ClipBans, clip_ban_reason, load_clip_bans, parse_clip_bans


def _vod(title_no: str = "205491275") -> dict:
    return {"id": 1, "soop_title_no": title_no, "status": "analyzed", "youtube_status": None}


def _perf(perf_id: int) -> dict:
    return {"id": perf_id, "identify_status": "auto_matched", "local_review": "verified"}


# --------------------------------------------------------------------------- #
# parse_clip_bans
# --------------------------------------------------------------------------- #
def test_parse_reads_both_scopes():
    bans = parse_clip_bans(
        {
            "vod": [{"title_no": "205491275", "reason": "방송 전체 금지"}],
            "song": [{"perf_id": 2293, "title_no": "205967387", "reason": "곡 금지"}],
        }
    )
    assert bans.vods == {"205491275": "방송 전체 금지"}
    assert bans.songs == {2293: "곡 금지"}


def test_parse_normalizes_numeric_title_no():
    """toml에 숫자로 적어도 DB의 soop_title_no(text)와 비교되도록 문자열로 맞춘다."""
    bans = parse_clip_bans({"vod": [{"title_no": 205491275, "reason": "x"}]})
    assert bans.vods == {"205491275": "x"}


def test_parse_rejects_entry_without_reason():
    """사유 없는 금지는 나중에 풀 수 있는지 판단할 근거가 없다 — 조용히 넘기지 않는다."""
    with pytest.raises(ValueError):
        parse_clip_bans({"vod": [{"title_no": "205491275"}]})
    with pytest.raises(ValueError):
        parse_clip_bans({"song": [{"perf_id": 2293, "reason": "  "}]})


def test_parse_rejects_song_without_perf_id():
    with pytest.raises(ValueError):
        parse_clip_bans({"song": [{"title_no": "205967387", "reason": "곡 금지"}]})


def test_empty_registry_is_falsy():
    assert not parse_clip_bans({})
    assert not ClipBans()


# --------------------------------------------------------------------------- #
# clip_ban_reason
# --------------------------------------------------------------------------- #
def test_no_bans_means_no_reason():
    assert clip_ban_reason(None, _vod(), [_perf(2293)]) is None
    assert clip_ban_reason(ClipBans(), _vod(), [_perf(2293)]) is None


def test_vod_scope_ban_blocks_regardless_of_perfs():
    bans = ClipBans(vods={"205491275": "BJ가 전체 금지"})
    reason = clip_ban_reason(bans, _vod("205491275"), [])
    assert reason is not None and "BJ가 전체 금지" in reason


def test_song_scope_ban_blocks_the_whole_vod():
    """합본은 한 영상이라 한 곡만 빼고 올릴 수 없다 — 곡 하나 금지면 VOD가 통째로 빠진다."""
    bans = ClipBans(songs={2293: "팬 [클립금지] 태그"})
    reason = clip_ban_reason(bans, _vod("205967387"), [_perf(2292), _perf(2293)])
    assert reason is not None and "#2293" in reason


def test_unrelated_vod_and_perf_pass():
    bans = ClipBans(vods={"205491275": "x"}, songs={2293: "y"})
    assert clip_ban_reason(bans, _vod("204781959"), [_perf(1992)]) is None


# --------------------------------------------------------------------------- #
# load_clip_bans — 파일이 없어도 동작해야 한다(금지는 예외 장부지 필수 설정이 아니다)
# --------------------------------------------------------------------------- #
def test_load_missing_file_returns_empty(tmp_path):
    assert not load_clip_bans(tmp_path / "없는파일.toml")


def test_load_reads_file(tmp_path):
    path = tmp_path / "clip_bans.toml"
    path.write_text(
        '[[vod]]\ntitle_no = "205491275"\nreason = "전체 금지"\n', encoding="utf-8"
    )
    assert load_clip_bans(path).vods == {"205491275": "전체 금지"}


def test_repo_registry_parses():
    """레포에 커밋된 장부가 항상 읽히는지 — 오타 한 줄이 금지 누락이 되면 안 된다."""
    from pathlib import Path

    registry = Path(__file__).resolve().parents[1] / "clip_bans.toml"
    if not registry.exists():  # 장부를 비워 둘 수도 있다
        pytest.skip("clip_bans.toml 없음")
    bans = load_clip_bans(registry)
    assert all(v for v in bans.vods.values())
    assert all(v for v in bans.songs.values())
