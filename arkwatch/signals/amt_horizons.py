"""amt_horizons.py — Horizon-aware Auction Market Theory profiler for arbitrary time windows."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .amt import ASSET_TICK_SIZES, compute_value_area


def compute_horizon_amt(
    conn: sqlite3.Connection,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    num_bins: int = 40,
) -> dict[str, Any] | None:
    """Compute AMT Volume Profile, Value Area (70%), and VWAP over an exact time slice."""
    sym = symbol.strip().upper()
    start_str = start_utc.isoformat(timespec="seconds")
    end_str = end_utc.isoformat(timespec="seconds")

    rows = conn.execute(
        """
        SELECT bar_ts_utc, open, high, low, close, COALESCE(volume, 0.0)
        FROM intraday_bars
        WHERE symbol = ?
          AND bar_ts_utc >= ?
          AND bar_ts_utc < ?
        ORDER BY bar_ts_utc ASC
        """,
        (sym, start_str, end_str),
    ).fetchall()

    if not rows:
        return None

    # Calculate basic price metrics
    highs = [float(r[2]) for r in rows if r[2] is not None]
    lows = [float(r[3]) for r in rows if r[3] is not None]
    if not highs or not lows:
        return None

    h_max = max(highs)
    l_min = min(lows)
    total_range = h_max - l_min

    # Calculate VWAP
    cum_pv = sum(
        ((float(r[2]) + float(r[3]) + float(r[4])) / 3.0) * max(1.0, float(r[5])) for r in rows
    )
    cum_vol = sum(max(1.0, float(r[5])) for r in rows)
    vwap = round(cum_pv / cum_vol, 4) if cum_vol > 0 else float(rows[-1][4])

    # Convert rows to tuple format for compute_value_area: (ts, open, high, low, close, volume)
    bar_tuples = [
        (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows
    ]
    tick_sz = ASSET_TICK_SIZES.get(sym)
    va = compute_value_area(bar_tuples, num_bins=num_bins, va_volume_ratio=0.70, tick_size=tick_sz)

    first_bar = rows[0]
    last_bar = rows[-1]

    return {
        "symbol": sym,
        "start_utc": start_str,
        "end_utc": end_str,
        "bars_count": len(rows),
        "open": float(first_bar[1]),
        "high": h_max,
        "low": l_min,
        "close": float(last_bar[4]),
        "range": round(total_range, 4),
        "total_volume": round(cum_vol, 2),
        "vwap": vwap,
        "vah": va["vah"],
        "val": va["val"],
        "poc": va["poc"],
    }
