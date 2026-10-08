"""horizons.py — Time partitioning engine: Sessions, Quarterly Theory & IPDA data ranges."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

SESSION_HOURS_ET: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    # format: ((start_hour, start_min), (end_hour, end_min)) in New York Time (ET)
    "ASIA": ((20, 0), (5, 0)),  # 20:00 ET (D-1) - 05:00 ET
    "LONDON": ((3, 0), (12, 0)),  # 03:00 ET - 12:00 ET
    "NY_REGULAR": ((8, 0), (17, 0)),  # 08:00 ET - 17:00 ET
    "NY_AM": ((8, 0), (12, 0)),  # 08:00 ET - 12:00 ET
    "NY_PM": ((12, 0), (17, 0)),  # 12:00 ET - 17:00 ET
    "NY_LONDON_OVERLAP": ((8, 0), (12, 0)),  # 08:00 ET - 12:00 ET
    "FRANKFURT": ((2, 0), (11, 0)),  # 02:00 ET - 11:00 ET (opens 1h before London)
    "SINGAPORE": ((21, 0), (4, 0)),  # 21:00 ET (D-1) - 04:00 ET
}

QUARTERLY_HOURS_ET: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "Q1_ASIA": ((17, 0), (0, 0)),  # 17:00 ET (D-1) - 00:00 ET (7 hours)
    "Q2_LONDON": ((0, 0), (6, 0)),  # 00:00 ET - 06:00 ET (6 hours)
    "Q3_NY_AM": ((6, 0), (12, 0)),  # 06:00 ET - 12:00 ET (6 hours)
    "Q4_NY_PM": ((12, 0), (17, 0)),  # 12:00 ET - 17:00 ET (5 hours)
}


def is_dst_edt(dt: datetime) -> bool:
    """Check if datetime falls within US Daylight Saving Time (EDT, UTC-4)."""
    aware = dt.astimezone(NY_TZ)
    return bool(aware.dst())


def to_ny_time(dt_utc: datetime) -> datetime:
    """Convert UTC datetime to New York local datetime."""
    return dt_utc.astimezone(NY_TZ)


def get_session_window(session_name: str, target_date: date) -> tuple[datetime, datetime]:
    """Calculate exact UTC start and end bounds for a named trading session on a target date."""
    key = session_name.strip().upper()
    if key not in SESSION_HOURS_ET:
        raise ValueError(
            f"Unknown session '{session_name}', must be one of {list(SESSION_HOURS_ET.keys())}"
        )

    (sh, sm), (eh, em) = SESSION_HOURS_ET[key]

    if sh > eh or key in ("ASIA", "SINGAPORE"):
        # Sessions starting in the evening of previous calendar day
        start_ny = datetime(
            target_date.year, target_date.month, target_date.day, sh, sm, tzinfo=NY_TZ
        ) - timedelta(days=1)
        end_ny = datetime(
            target_date.year, target_date.month, target_date.day, eh, em, tzinfo=NY_TZ
        )
    else:
        start_ny = datetime(
            target_date.year, target_date.month, target_date.day, sh, sm, tzinfo=NY_TZ
        )
        end_ny = datetime(
            target_date.year, target_date.month, target_date.day, eh, em, tzinfo=NY_TZ
        )

    return start_ny.astimezone(UTC), end_ny.astimezone(UTC)


def get_quarterly_session_bounds(
    target_date: date,
) -> dict[str, tuple[datetime, datetime]]:
    """Return UTC start and end bounds for the 4 daily quarters in Quarterly Theory."""
    out = {}
    for q_name, ((sh, sm), (eh, em)) in QUARTERLY_HOURS_ET.items():
        if sh > eh or (sh == 17 and eh == 0):
            s_ny = datetime(
                target_date.year, target_date.month, target_date.day, sh, sm, tzinfo=NY_TZ
            ) - timedelta(days=1)
            e_ny = datetime(
                target_date.year, target_date.month, target_date.day, eh, em, tzinfo=NY_TZ
            )
        else:
            s_ny = datetime(
                target_date.year, target_date.month, target_date.day, sh, sm, tzinfo=NY_TZ
            )
            e_ny = datetime(
                target_date.year, target_date.month, target_date.day, eh, em, tzinfo=NY_TZ
            )
        out[q_name] = (s_ny.astimezone(UTC), e_ny.astimezone(UTC))
    return out


def subdivide_quarter_90m(
    start_utc: datetime, end_utc: datetime
) -> list[tuple[datetime, datetime]]:
    """Fractal division: subdivide a quarter into 4 cycles of 90 minutes each."""
    duration_total = (end_utc - start_utc).total_seconds()
    sub_dur = duration_total / 4.0
    return [
        (
            start_utc + timedelta(seconds=i * sub_dur),
            start_utc + timedelta(seconds=(i + 1) * sub_dur),
        )
        for i in range(4)
    ]


def subdivide_micro_22m(start_utc: datetime, end_utc: datetime) -> list[tuple[datetime, datetime]]:
    """Micro-fractal division: subdivide a 90-minute sub-quarter into 4 micro-cycles of 22.5 minutes."""
    duration_total = (end_utc - start_utc).total_seconds()
    micro_dur = duration_total / 4.0
    return [
        (
            start_utc + timedelta(seconds=i * micro_dur),
            start_utc + timedelta(seconds=(i + 1) * micro_dur),
        )
        for i in range(4)
    ]


def get_active_quarterly_cycles(now_utc: datetime) -> dict[str, Any]:
    """Identify currently active 6h quarter, 90m sub-quarter, and 22.5m micro-cycle."""
    target_d = now_utc.date()
    q_bounds = get_quarterly_session_bounds(target_d)

    # Check yesterday's bounds too in case Q1 Asia started yesterday
    prev_d = target_d - timedelta(days=1)
    prev_bounds = get_quarterly_session_bounds(prev_d)
    all_bounds = {**prev_bounds, **q_bounds}

    active_q = "Q1_ASIA"
    active_q_bounds = None
    for q_name, (qs, qe) in all_bounds.items():
        if qs <= now_utc < qe:
            active_q = q_name
            active_q_bounds = (qs, qe)
            break

    if active_q_bounds is None:
        qs, qe = q_bounds["Q3_NY_AM"]
        active_q = "Q3_NY_AM"
        active_q_bounds = (qs, qe)
    else:
        qs, qe = active_q_bounds

    sub_quarters = subdivide_quarter_90m(qs, qe)
    sub_idx = 0
    active_sub_bounds = sub_quarters[0]
    for idx, (ss, se) in enumerate(sub_quarters):
        if ss <= now_utc < se:
            sub_idx = idx
            active_sub_bounds = (ss, se)
            break

    sub_roles = [
        "ACCUMULATION_INITIAL_RANGE",
        "MANIPULATION_LIQUIDITY_PROBE",
        "DISTRIBUTION_EXPANSION_DRIVE",
        "CLOSING_RANGE_TRANSITION",
    ]

    micro_cycles = subdivide_micro_22m(active_sub_bounds[0], active_sub_bounds[1])
    micro_idx = 0
    for idx, (ms, me) in enumerate(micro_cycles):
        if ms <= now_utc < me:
            micro_idx = idx
            break

    micro_roles = [
        "MICRO_OPEN_DISCOVERY",
        "MICRO_JUDAH_PIVOT",
        "MICRO_CONTINUATION_RUN",
        "MICRO_SETTLEMENT_RETEST",
    ]

    return {
        "active_quarter": active_q,
        "quarter_start_utc": qs.isoformat(timespec="seconds"),
        "quarter_end_utc": qe.isoformat(timespec="seconds"),
        "active_90m_sub_quarter": f"Sub-{sub_idx + 1}",
        "sub_quarter_role": sub_roles[sub_idx],
        "sub_quarter_start_utc": active_sub_bounds[0].isoformat(timespec="seconds"),
        "sub_quarter_end_utc": active_sub_bounds[1].isoformat(timespec="seconds"),
        "active_22m_micro_cycle": f"Micro-{micro_idx + 1}",
        "micro_cycle_role": micro_roles[micro_idx],
    }


def get_weekly_quarter(target_date: date) -> dict[str, Any]:
    """Map day of week to Quarterly Theory Weekly Profile."""
    weekday = target_date.weekday()
    mapping = {
        0: ("Q1", "ACCUMULATION"),
        1: ("Q2", "MANIPULATION_JUDAH"),
        2: ("Q3", "DISTRIBUTION"),
        3: ("Q4", "CONTINUATION_REVERSAL"),
        4: ("FRIDAY_SPECIAL", "MEAN_REVERSION_RANGE_RETURN"),
        5: ("WEEKEND", "CLOSED"),
        6: ("WEEKEND", "GLOBEX_OPEN"),
    }
    q, theory_role = mapping.get(weekday, ("UNKNOWN", "UNKNOWN"))
    return {
        "date": target_date.isoformat(),
        "weekday": target_date.strftime("%A"),
        "quarter": q,
        "theory_role": theory_role,
        "is_friday": weekday == 4,
    }


def get_monthly_quarter(target_date: date) -> dict[str, Any]:
    """Map calendar day to Monthly Quarters (Q1: 04-11, Q2: 11-18, Q3: 18-25, Q4: 25-01, Joker: 01-08)."""
    d = target_date.day
    if 4 <= d <= 11:
        return {
            "quarter": "Q1",
            "is_joker_week": False,
            "description": "Monthly Accumulation / Initial Balance",
        }
    if 11 < d <= 18:
        return {
            "quarter": "Q2",
            "is_joker_week": False,
            "description": "Monthly Manipulation / Trend Inception",
        }
    if 18 < d <= 25:
        return {
            "quarter": "Q3",
            "is_joker_week": False,
            "description": "Monthly Distribution / Trend Peak",
        }
    if d > 25:
        return {
            "quarter": "Q4",
            "is_joker_week": False,
            "description": "Monthly Profit Taking / Range Return",
        }
    return {
        "quarter": "JOKER_WEEK",
        "is_joker_week": True,
        "description": "Transition / Expansion Anomaly",
    }


def get_ipda_ranges(target_dt: datetime) -> dict[str, datetime]:
    """Generate Interbank Price Delivery Algorithm (IPDA) lookback anchor timestamps."""
    return {
        "60D": target_dt - timedelta(days=60),
        "40D": target_dt - timedelta(days=40),
        "20D": target_dt - timedelta(days=20),
        "15D": target_dt - timedelta(days=15),
        "10D": target_dt - timedelta(days=10),
        "5D": target_dt - timedelta(days=5),
        "3D": target_dt - timedelta(days=3),
        "2D": target_dt - timedelta(days=2),
        "1D": target_dt - timedelta(days=1),
        "12H": target_dt - timedelta(hours=12),
        "8H": target_dt - timedelta(hours=8),
        "4H": target_dt - timedelta(hours=4),
    }
