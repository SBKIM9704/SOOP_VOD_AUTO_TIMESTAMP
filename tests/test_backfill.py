import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "backfill_existing_clips.py"
_spec = importlib.util.spec_from_file_location("backfill_existing_clips", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def test_backfill_title_no_format():
    assert backfill.backfill_title_no("dQw4w9WgXcQ") == "backfill:dQw4w9WgXcQ"


def test_backfill_title_no_distinct_per_video():
    assert backfill.backfill_title_no("a") != backfill.backfill_title_no("b")


def test_iso8601_duration_full():
    assert backfill.iso8601_duration_to_s("PT1H2M3S") == 3723


def test_iso8601_duration_minutes_only():
    assert backfill.iso8601_duration_to_s("PT4M30S") == 270


def test_iso8601_duration_seconds_only():
    assert backfill.iso8601_duration_to_s("PT45S") == 45


def test_iso8601_duration_malformed_returns_zero():
    assert backfill.iso8601_duration_to_s("garbage") == 0
