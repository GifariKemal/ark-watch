from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from arkwatch.timezones import (
    DEFAULT_DISPLAY_LABEL,
    format_session_id,
    format_ts_display,
    resolve_timezone,
    to_display_time,
)


def test_resolve_timezone_defaults_to_new_york():
    tz, lbl = resolve_timezone()
    assert tz == ZoneInfo("America/New_York")
    assert lbl == DEFAULT_DISPLAY_LABEL

    tz, lbl = resolve_timezone("default")
    assert tz == ZoneInfo("America/New_York")
    assert lbl == "ET"


def test_resolve_timezone_handles_wib_and_utc():
    tz, lbl = resolve_timezone("WIB")
    assert tz == ZoneInfo("Asia/Jakarta")
    assert lbl == "WIB"

    tz, lbl = resolve_timezone("UTC")
    assert tz == UTC
    assert lbl == "UTC"


def test_resolve_timezone_handles_numeric_offsets():
    tz, lbl = resolve_timezone("UTC-4")
    assert tz == timezone(timedelta(hours=-4))
    assert lbl == "UTC-04"

    tz, lbl = resolve_timezone("+7")
    assert tz == timezone(timedelta(hours=7))
    assert lbl == "UTC+07"


def test_format_ts_display_default_and_dynamic():
    dt_utc = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)

    # Default to ET (UTC-4 in October EDT)
    default_fmt = format_ts_display(dt_utc)
    assert "2026-10-08 21:30 ET" in default_fmt
    assert "UTC-04:00" in default_fmt

    # Dynamic conversion to WIB (UTC+7)
    wib_fmt = format_ts_display(dt_utc, "WIB")
    assert "2026-10-09 08:30 WIB" in wib_fmt
    assert "UTC+07:00" in wib_fmt

    # Dynamic conversion to UTC


def test_to_display_time():
    dt_utc = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)
    ny_dt = to_display_time(dt_utc)
    assert ny_dt.hour == 21
    assert ny_dt.day == 8
    utc_fmt = format_ts_display(dt_utc, "UTC")
    assert "2026-10-09 01:30 UTC" in utc_fmt


def test_format_session_id():
    dt_utc = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)
    s_id = format_session_id(dt_utc)
    assert s_id == "2026-10-08 21:30 ET"
