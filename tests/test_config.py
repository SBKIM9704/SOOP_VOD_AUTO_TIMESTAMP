from pathlib import Path

from soopts.config import load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.audio.min_music_s == 30.0
    assert cfg.audio.sticker_rate_strong == 2.5
    assert cfg.stt.model == "small"
    assert cfg.endpoints.api_level == 10


def test_partial_override_only_named_keys(tmp_path):
    p = tmp_path / "soopts.toml"
    p.write_text(
        '[audio]\nmin_sticker_rate = 3.0\n[stt]\nlanguage = "ko"\n', encoding="utf-8"
    )
    cfg = load_config(p)
    assert cfg.audio.min_sticker_rate == 3.0     # 오버라이드됨
    assert cfg.stt.language == "ko"
    assert cfg.audio.min_music_s == 30.0         # 나머지는 기본값


def test_unknown_key_ignored(tmp_path):
    p = tmp_path / "soopts.toml"
    p.write_text("[audio]\nbogus = 1\nmin_music_s = 60\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.audio.min_music_s == 60


def test_work_root_override():
    cfg = load_config(None, work_root=Path("/tmp/xyz"))
    assert cfg.work_root == Path("/tmp/xyz")


def test_broadcast_date_from_file_info_key():
    from soopts.models import MetaPart, MetaResult, broadcast_date
    m = MetaResult("v", "t", "bj", "nick", 100,
                   parts=[MetaPart(0, "20260604_CE830094_294553483_1", 100, 0)])
    assert broadcast_date(m) == "2026-06-04"
    # 형식 다르면 빈 문자열(안전)
    m2 = MetaResult("v", "t", "bj", "nick", 100, parts=[MetaPart(0, "weirdkey", 100, 0)])
    assert broadcast_date(m2) == ""
