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
    "PRE_LONDON": ((2, 0), (3, 0)),  # 02:00 ET - 03:00 ET (False Auction Trap Window)
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
    active_micro_bounds = micro_cycles[0]
    for idx, (ms, me) in enumerate(micro_cycles):
        if ms <= now_utc < me:
            micro_idx = idx
            active_micro_bounds = (ms, me)
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
        "micro_cycle_start_utc": active_micro_bounds[0].isoformat(timespec="seconds"),
        "micro_cycle_end_utc": active_micro_bounds[1].isoformat(timespec="seconds"),
        "prior_sub_quarter_start_utc": (
            sub_quarters[sub_idx - 1][0].isoformat(timespec="seconds") if sub_idx > 0 else None
        ),
        "prior_sub_quarter_end_utc": (
            sub_quarters[sub_idx - 1][1].isoformat(timespec="seconds") if sub_idx > 0 else None
        ),
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


def get_month_week_anchor_ny(year: int, month: int) -> datetime:
    """Exact implementation of PineScript monthWeekAnchorNy:
    A month's Week 1 begins on Sunday 18:00 ET immediately before its first Monday.
    """
    month_start = datetime(year, month, 1, 0, 0, tzinfo=NY_TZ)
    days_to_monday = (0 - month_start.weekday() + 7) % 7
    first_monday = month_start + timedelta(days=days_to_monday)
    sunday_before = first_monday - timedelta(days=1)
    return datetime(sunday_before.year, sunday_before.month, sunday_before.day, 18, 0, tzinfo=NY_TZ)


def get_monthly_quarter(target_date: date | datetime) -> dict[str, Any]:
    """Calculate exact Quarterly Theory Monthly Week and Quarters based on PineScript formula:
    - Week 1 begins on Sunday 18:00 ET before the first Monday of the month.
    - Span to next month anchor is exactly 4 or 5 weeks.
    - Week 1 = Q1, Week 2 = Q2, Week 3 = Q3, Week 4 = Q4, Week 5 = Joker Week (Q0).
    """
    if isinstance(target_date, datetime):
        dt_ny = target_date.astimezone(NY_TZ)
    else:
        dt_ny = datetime(target_date.year, target_date.month, target_date.day, 12, 0, tzinfo=NY_TZ)

    year_ny = dt_ny.year
    month_ny = dt_ny.month

    anchor = get_month_week_anchor_ny(year_ny, month_ny)
    if dt_ny < anchor:
        year_ny = year_ny - 1 if month_ny == 1 else year_ny
        month_ny = 12 if month_ny == 1 else month_ny - 1
        anchor = get_month_week_anchor_ny(year_ny, month_ny)
    else:
        next_y = year_ny + 1 if month_ny == 12 else year_ny
        next_m = 1 if month_ny == 12 else month_ny + 1
        next_anchor = get_month_week_anchor_ny(next_y, next_m)
        if dt_ny >= next_anchor:
            year_ny = next_y
            month_ny = next_m
            anchor = next_anchor

    next_y = year_ny + 1 if month_ny == 12 else year_ny
    next_m = 1 if month_ny == 12 else month_ny + 1
    next_anchor = get_month_week_anchor_ny(next_y, next_m)

    total_days = (next_anchor - anchor).days
    total_weeks = total_days // 7

    elapsed_ms = (dt_ny - anchor).total_seconds() * 1000
    week_no = int(elapsed_ms // (7 * 24 * 3600 * 1000)) + 1
    cycle_num = week_no if 1 <= week_no <= 4 else 0

    roles = {
        1: ("Q1", "WEEK_1", "Monthly Accumulation / Initial Balance"),
        2: ("Q2", "WEEK_2", "Monthly Manipulation / Trend Inception"),
        3: ("Q3", "WEEK_3", "Monthly Distribution / Trend Peak"),
        4: ("Q4", "WEEK_4", "Monthly Reversal or Continuation / Close"),
        0: ("Q0", "JOKER_WEEK", "Monthly Joker Week / Anomaly Rebalancing"),
    }
    q_code, w_label, desc = roles.get(cycle_num, ("UNKNOWN", "UNKNOWN", "Unknown"))

    weeks_schedule = []
    for w in range(total_weeks):
        w_start = anchor + timedelta(days=w * 7)
        w_end = w_start + timedelta(days=7)
        c_num = (w + 1) if (w + 1) <= 4 else 0
        w_q, _, w_desc = roles.get(c_num, ("UNKNOWN", "UNKNOWN", "Unknown"))
        weeks_schedule.append(
            {
                "week_index": w + 1,
                "quarter": w_q,
                "start_et": w_start.strftime("%Y-%m-%d %H:%M ET"),
                "end_et": w_end.strftime("%Y-%m-%d %H:%M ET"),
                "start_utc": w_start.astimezone(UTC).isoformat(timespec="seconds"),
                "end_utc": w_end.astimezone(UTC).isoformat(timespec="seconds"),
                "is_active": (w + 1) == week_no,
                "status": (
                    "COMPLETED"
                    if dt_ny >= w_end
                    else ("ACTIVE" if dt_ny >= w_start else "UPCOMING")
                ),
                "description": w_desc,
            }
        )

    return {
        "owning_year_month": f"{year_ny}-{month_ny:02d}",
        "active_week_number": week_no,
        "quarter": q_code,
        "week_label": w_label,
        "is_joker_week": cycle_num == 0,
        "description": desc,
        "total_weeks_in_month": total_weeks,
        "has_joker_week": total_weeks == 5,
        "month_anchor_start_utc": anchor.astimezone(UTC).isoformat(timespec="seconds"),
        "month_anchor_end_utc": next_anchor.astimezone(UTC).isoformat(timespec="seconds"),
        "weeks_schedule": weeks_schedule,
    }


def get_yearly_cycle(target_date: date | datetime) -> dict[str, Any]:
    """Calculate Quarterly Theory Yearly Cycle:
    1 Year divided into 4 quarters: Q1 (Jan-Mar), Q2 (Apr-Jun), Q3 (Jul-Sep), Q4 (Oct-Dec).
    """
    y = target_date.year
    m = target_date.month
    q_num = ((m - 1) // 3) + 1
    roles = {
        1: ("Q1", "Jan - Mar", "Yearly Accumulation / True Open Range"),
        2: ("Q2", "Apr - Jun", "Yearly Manipulation / Spring-Summer Trend Inception"),
        3: ("Q3", "Jul - Sep", "Yearly Distribution / Late-Summer Peak"),
        4: (
            "Q4",
            "Oct - Dec",
            "Yearly Continuation or Reversal / Year-End Settlement",
        ),
    }
    q_code, period_str, desc = roles[q_num]
    return {
        "year": y,
        "quarter": q_code,
        "months": period_str,
        "description": desc,
        "all_quarters": [
            {
                "quarter": "Q1",
                "months": "Jan - Mar",
                "status": "COMPLETED" if q_num > 1 else ("ACTIVE" if q_num == 1 else "UPCOMING"),
            },
            {
                "quarter": "Q2",
                "months": "Apr - Jun",
                "status": "COMPLETED" if q_num > 2 else ("ACTIVE" if q_num == 2 else "UPCOMING"),
            },
            {
                "quarter": "Q3",
                "months": "Jul - Sep",
                "status": "COMPLETED" if q_num > 3 else ("ACTIVE" if q_num == 3 else "UPCOMING"),
            },
            {
                "quarter": "Q4",
                "months": "Oct - Dec",
                "status": "ACTIVE" if q_num == 4 else "UPCOMING",
            },
        ],
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
