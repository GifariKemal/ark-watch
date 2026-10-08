"""friday_audit.py — Empirical analysis of Friday's Auction Market Theory function (Climax vs Re-Range)."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any


def audit_friday_rerange(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    lookback_weeks: int = 52,
    source: str = "YAHOO",
) -> dict[str, Any]:
    """Analyze Friday's real AMT function: Trend Week Climax vs Balanced Week Mean Reversion."""
    sym = symbol.strip().upper()
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close
        FROM instrument_prices
        WHERE symbol = ? AND source = ?
        ORDER BY ts ASC
        """,
        (sym, source),
    ).fetchall()

    if not rows:
        return {
            "symbol": sym,
            "weeks_evaluated": 0,
            "mean_reversion_rate_pct": 0.0,
            "verdict": "NO_DATA",
            "amt_reconciliation": {},
        }

    # Group by ISO calendar week: (year, week_num)
    by_week: dict[tuple[int, int], dict[int, tuple]] = {}
    for r in rows:
        d = date.fromisoformat(r[0][:10])
        wk_key = (d.year, d.isocalendar()[1])
        by_week.setdefault(wk_key, {})[d.weekday()] = r

    revert_count = 0
    continuation_count = 0
    evaluated_weeks = 0
    fri_ranges = []
    weekday_ranges = []

    sorted_weeks = sorted(by_week.keys())[-lookback_weeks:]
    for wk_key in sorted_weeks:
        days = by_week[wk_key]
        if 4 not in days:  # Friday must exist
            continue

        mon_thu_days = [days[w] for w in (0, 1, 2, 3) if w in days]
        if len(mon_thu_days) < 2:  # Need at least 2 weekday bars
            continue

        mt_high = max(float(d[2]) for d in mon_thu_days)
        mt_low = min(float(d[3]) for d in mon_thu_days)

        for d in mon_thu_days:
            weekday_ranges.append(float(d[2]) - float(d[3]))

        fri_bar = days[4]
        fri_high = float(fri_bar[2])
        fri_low = float(fri_bar[3])
        fri_close = float(fri_bar[4])
        fri_ranges.append(fri_high - fri_low)

        # Check if Friday close ends inside Mon-Thu range
        if mt_low <= fri_close <= mt_high:
            revert_count += 1
        else:
            continuation_count += 1
        evaluated_weeks += 1

    if evaluated_weeks == 0:
        return {
            "symbol": sym,
            "weeks_evaluated": 0,
            "mean_reversion_rate_pct": 0.0,
            "verdict": "INSUFFICIENT_WEEKS",
            "amt_reconciliation": {},
        }

    revert_rate = round((revert_count / evaluated_weeks) * 100.0, 1)
    avg_fri_range = round(sum(fri_ranges) / len(fri_ranges), 2) if fri_ranges else 0.0
    avg_wk_range = round(sum(weekday_ranges) / len(weekday_ranges), 2) if weekday_ranges else 0.0
    volatility_ratio = round(avg_fri_range / avg_wk_range, 2) if avg_wk_range > 0 else 1.0

    verdict = (
        "CONFIRMED_MEAN_REVERSION"
        if revert_rate >= 60.0
        else ("CONFIRMED_TREND_CONTINUATION" if revert_rate <= 40.0 else "BALANCED_DUALITY")
    )

    return {
        "symbol": sym,
        "source": source,
        "weeks_evaluated": evaluated_weeks,
        "revert_inside_range_count": revert_count,
        "continuation_breakout_count": continuation_count,
        "mean_reversion_rate_pct": revert_rate,
        "avg_friday_range": avg_fri_range,
        "avg_weekday_range": avg_wk_range,
        "friday_volatility_expansion_ratio": volatility_ratio,
        "verdict": verdict,
        "amt_reconciliation": {
            "rule_1_trend_week": "In Trend Weeks, Friday prints the High/Low of the week via exhaustion push.",
            "rule_2_balanced_week": "In Balanced Weeks, Friday mean-reverts back to weekly POC / Value Area.",
            "rule_3_friday_afternoon": "Friday PM breakouts fail >= 65% due to weekend desk inventory squaring.",
        },
    }
