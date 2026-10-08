from datetime import UTC, datetime, timedelta, timezone

from arkwatch.signals.asof import parse_as_of


def test_naive_is_utc_not_host_local():
    assert parse_as_of("2026-10-08T10:00:00") == datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def test_z_suffix_and_offset_normalised():
    assert parse_as_of("2026-10-08T10:00:00Z").utcoffset() == timedelta(0)
    wib = datetime(2026, 10, 8, 17, 0, tzinfo=timezone(timedelta(hours=7)))
    assert parse_as_of(wib) == datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def test_none_is_aware_now():
    assert parse_as_of().tzinfo is not None
