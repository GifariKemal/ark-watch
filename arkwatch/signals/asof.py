"""asof.py - single place to resolve an `as_of` instant (tz-aware UTC)."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_as_of(value: str | datetime | None = None) -> datetime:
    """None -> now(UTC). Naive input is interpreted as UTC, never host-local time."""
    if value is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
