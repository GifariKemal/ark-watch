"""timezones.py — Centralized timezone resolution, conversion, and formatting.

Standardizes display timestamps across ark-watch with a default of New York Time
(ET / UTC-4 during EDT, UTC-5 during EST), while maintaining strict UTC (+0)
storage in SQLite. Supports dynamic conversion to any requested timezone or offset.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

# Canonical Timezone Definitions & Aliases
NY_TZ = ZoneInfo("America/New_York")
ET = NY_TZ
NEW_YORK = NY_TZ

WIB_TZ = ZoneInfo("Asia/Jakarta")
WIB = WIB_TZ

LONDON_TZ = ZoneInfo("Europe/London")
LONDON = LONDON_TZ

CHICAGO_TZ = ZoneInfo("America/Chicago")
CHICAGO = CHICAGO_TZ

UTC_TZ = UTC

# Project Default Display Timezone (ET / UTC-4)
DEFAULT_DISPLAY_TZ = "America/New_York"
DEFAULT_DISPLAY_LABEL = "ET"


def now_utc() -> datetime:
    """Return current aware UTC datetime."""
    return datetime.now(UTC_TZ)


def now_et() -> datetime:
    """Return current aware New York (ET) datetime."""
    return datetime.now(NY_TZ)


def now_wib() -> datetime:
    """Return current aware Jakarta (WIB) datetime."""
    return datetime.now(WIB_TZ)


def now_display(tz_target: str | int | float | tzinfo | None = None) -> str:
    """Return current time formatted for display in default or requested timezone."""
    return format_ts_display(now_utc(), tz_target)


def resolve_timezone(
    tz_input: str | int | float | tzinfo | None = None,
) -> tuple[tzinfo, str]:
    """Resolve any timezone input into a valid (tzinfo, display_label) tuple.

    Supports:
      - None / "" / "default" / "ET" / "NY" -> (ZoneInfo("America/New_York"), "ET")
      - "WIB" / "Jakarta" -> (ZoneInfo("Asia/Jakarta"), "WIB")
      - "UTC" / "Z" / "GMT" -> (timezone.utc, "UTC")
      - Offset strings: "UTC-4", "UTC-04", "-04:00", "-4", "+7", "UTC+7" -> timezone offset
      - Standard IANA names: "Europe/London", "Asia/Tokyo"
    """
    if (
        tz_input is None
        or tz_input == ""
        or str(tz_input).strip().upper()
        in (
            "DEFAULT",
            "ET",
            "NY",
            "NEW_YORK",
            "AMERICA/NEW_YORK",
        )
    ):
        return NY_TZ, DEFAULT_DISPLAY_LABEL

    if isinstance(tz_input, tzinfo):
        return tz_input, str(tz_input)

    raw = str(tz_input).strip()
    s = raw.upper()

    if s in ("WIB", "JAKARTA", "ASIA/JAKARTA"):
        return WIB_TZ, "WIB"
    if s in ("UTC", "Z", "GMT"):
        return UTC_TZ, "UTC"
    if s in ("LONDON", "EUROPE/LONDON"):
        return ZoneInfo("Europe/London"), "LON"
    if s in ("TOKYO", "ASIA/TOKYO"):
        return ZoneInfo("Asia/Tokyo"), "JST"

    # Match numeric offsets: UTC-4, UTC+7, -04:00, +07:00, -4, 7
    m = re.match(r"^(?:UTC)?([+-]?\d{1,2})(?::?(\d{2}))?$", s)
    if m:
        hrs = int(m.group(1))
        mins = int(m.group(2) or 0)
        total_mins = (hrs * 60) + (mins if hrs >= 0 else -mins)
        offset = timezone(timedelta(minutes=total_mins))
        lbl = f"UTC{hrs:+03d}" if mins == 0 else f"UTC{hrs:+03d}:{abs(mins):02d}"
        return offset, lbl

    try:
        zi = ZoneInfo(raw)
        return zi, raw.split("/")[-1]
    except Exception:
        return NY_TZ, DEFAULT_DISPLAY_LABEL


def to_display_time(
    dt_val: datetime | str,
    tz_target: str | int | float | tzinfo | None = None,
) -> datetime:
    """Convert an aware or naive UTC datetime / ISO string to the target display timezone."""
    if isinstance(dt_val, str):
        cleaned = dt_val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    else:
        dt = dt_val

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)

    target_tz, _ = resolve_timezone(tz_target)
    return dt.astimezone(target_tz)


def format_ts_display(
    dt_val: datetime | str,
    tz_target: str | int | float | tzinfo | None = None,
    *,
    include_offset: bool = True,
) -> str:
    """Format timestamp into a clean, human-readable standardized string.

    Example default output:
      '2026-10-08 21:30 ET (UTC-04:00)'
    """
    target_tz, lbl = resolve_timezone(tz_target)
    converted = to_display_time(dt_val, target_tz)

    base_str = converted.strftime(f"%Y-%m-%d %H:%M {lbl}")
    if include_offset:
        off = converted.strftime("%z")
        off_str = f"UTC{off[:3]}:{off[3:]}" if off else ""
        return f"{base_str} ({off_str})"
    return base_str


def format_session_id(
    dt_val: datetime | str,
    tz_target: str | int | float | tzinfo | None = None,
) -> str:
    """Standardize session ID format to the default display timezone (ET / UTC-4).

    Example default output:
      '2026-10-08 21:30 ET'
    """
    target_tz, lbl = resolve_timezone(tz_target)
    converted = to_display_time(dt_val, target_tz)
    return converted.strftime(f"%Y-%m-%d %H:%M {lbl}")
