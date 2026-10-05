"""levels.py — Auction Market Theory (AMT) and session liquidity reference levels.

Extracts high-precision institutional reference levels and Value Area profiling
from 5-minute intraday bars (intraday_bars) with 100% auditable provenance:
  - Prior Day Reference: PDH (High), PDL (Low), PDC (Close)
  - Overnight Reference: ONH (High), ONL (Low)
  - Opening Range (OR): OR15 (15m range), OR30 (30m range)
  - Market Profile: POC (Point of Control), VAH (Value Area High), VAL (Value Area Low)
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, time
from typing import Any

US_CASH_OPEN_UTC_SUMMER = time(13, 30)  # 09:30 ET during EDT
US_CASH_OPEN_UTC_WINTER = time(14, 30)  # 09:30 ET during EST


def _get_cash_open_time(dt: datetime) -> time:
    """Determine US cash open in UTC based on DST (EDT vs EST)."""
    # EDT runs roughly second Sunday of March to first Sunday of November
    # A standard approximate check: March 8 to November 1 is EDT (UTC 13:30)
    month = dt.month
    if 4 <= month <= 10:
        return US_CASH_OPEN_UTC_SUMMER
    if month == 3 and dt.day >= 8:
        return US_CASH_OPEN_UTC_SUMMER
    if month == 11 and dt.day <= 7 and dt.weekday() != 6:
        return US_CASH_OPEN_UTC_SUMMER
    return US_CASH_OPEN_UTC_WINTER


def compute_value_area(
    bars: list[tuple[str, float, float, float, float, float]],
    *,
    num_bins: int = 50,
    va_volume_ratio: float = 0.70,
) -> dict[str, Any]:
    """Compute Point of Control (POC), VAH, and VAL using discrete volume profile binning.

    Args:
        bars: list of (bar_ts_utc, open, high, low, close, volume)
        num_bins: number of discrete price levels
        va_volume_ratio: cumulative volume fraction for value area (default 0.70 = 70%)

    Returns:
        dict with poc, vah, val, total_volume, and bin distribution.
    """
    if not bars:
        return {
            "poc": None,
            "vah": None,
            "val": None,
            "total_volume": 0.0,
            "bars_count": 0,
        }

    highs = [b[2] for b in bars if b[2] is not None]
    lows = [b[3] for b in bars if b[3] is not None]
    if not highs or not lows:
        return {
            "poc": None,
            "vah": None,
            "val": None,
            "total_volume": 0.0,
            "bars_count": 0,
        }

    min_p = min(lows)
    max_p = max(highs)
    if max_p <= min_p:
        return {
            "poc": round(min_p, 4),
            "vah": round(min_p, 4),
            "val": round(min_p, 4),
            "total_volume": sum(b[5] for b in bars),
            "bars_count": len(bars),
        }

    bin_size = (max_p - min_p) / float(num_bins)
    volume_by_bin = [0.0] * num_bins
    total_vol = 0.0

    for _ts, _o, h, low_val, _c, v in bars:
        if v is None or v <= 0.0:
            v = 1.0  # Equal volume proxy if volume is missing/zero
        total_vol += v

        # Allocate volume across bins covered by [low, high]
        start_idx = max(0, min(num_bins - 1, int(math.floor((low_val - min_p) / bin_size))))
        end_idx = max(0, min(num_bins - 1, int(math.floor((h - min_p) / bin_size))))
        covered_bins = end_idx - start_idx + 1
        vol_per_bin = v / float(covered_bins)
        for idx in range(start_idx, end_idx + 1):
            volume_by_bin[idx] += vol_per_bin

    # Find POC (bin with maximum volume)
    max_vol = -1.0
    poc_idx = 0
    for i, vol in enumerate(volume_by_bin):
        if vol > max_vol:
            max_vol = vol
            poc_idx = i

    poc_price = min_p + (poc_idx + 0.5) * bin_size

    # Expand outward from POC to capture 70% of total volume (Auction Market Theory)
    target_vol = total_vol * va_volume_ratio
    current_vol = volume_by_bin[poc_idx]
    upper_idx = poc_idx
    lower_idx = poc_idx

    while current_vol < target_vol and (upper_idx < num_bins - 1 or lower_idx > 0):
        next_upper_vol = volume_by_bin[upper_idx + 1] if upper_idx + 1 < num_bins else -1.0
        next_lower_vol = volume_by_bin[lower_idx - 1] if lower_idx - 1 >= 0 else -1.0

        if next_upper_vol >= next_lower_vol and next_upper_vol >= 0.0:
            upper_idx += 1
            current_vol += next_upper_vol
        elif next_lower_vol > 0.0:
            lower_idx -= 1
            current_vol += next_lower_vol
        else:
            break

    vah_price = min_p + (upper_idx + 1.0) * bin_size
    val_price = min_p + lower_idx * bin_size

    return {
        "poc": round(poc_price, 4),
        "vah": round(vah_price, 4),
        "val": round(val_price, 4),
        "total_volume": round(total_vol, 2),
        "bars_count": len(bars),
        "bin_size": round(bin_size, 4),
    }


def compute_session_reference_levels(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Compute Prior Day Levels, Overnight Levels, Opening Range, and Value Area for a symbol."""
    sym = symbol.strip().upper()

    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of).astimezone(UTC)
    else:
        target_dt = as_of.astimezone(UTC)

    # Query latest available intraday bars for this symbol up to target_dt
    rows = conn.execute(
        """
        SELECT bar_ts_utc, open, high, low, close, COALESCE(volume, 0.0), source
        FROM intraday_bars
        WHERE symbol = ?
          AND bar_ts_utc <= ?
        ORDER BY bar_ts_utc ASC
        """,
        (sym, target_dt.isoformat(timespec="seconds")),
    ).fetchall()

    if not rows:
        return None

    # Group bars by calendar date (YYYY-MM-DD)
    bars_by_date: dict[str, list[tuple[str, float, float, float, float, float]]] = defaultdict(list)
    sources = set()

    for r in rows:
        ts_str, o, h, low_val, c, v, src = r
        d_str = ts_str[:10]
        bars_by_date[d_str].append((ts_str, float(o), float(h), float(low_val), float(c), float(v)))
        sources.add(src)

    all_dates = sorted(bars_by_date.keys())
    if not all_dates:
        return None

    # Determine prior day and current day
    curr_date = all_dates[-1]
    prior_date = all_dates[-2] if len(all_dates) >= 2 else curr_date

    prior_bars = bars_by_date[prior_date]
    curr_bars = bars_by_date[curr_date]

    # 1. Prior Day Levels (PDH, PDL, PDC)
    pdh = max(b[2] for b in prior_bars)
    pdl = min(b[3] for b in prior_bars)
    pdc = prior_bars[-1][4]  # close of last bar

    # 2. Market Profile on Prior Day (VAH, VAL, POC)
    va_profile = compute_value_area(prior_bars)

    # 3. Overnight Range (ONH, ONL) for current session
    # Defined as bars from 00:00 UTC up to US cash open (13:30 / 14:30 UTC)
    cash_open_time = _get_cash_open_time(target_dt)
    cash_open_cutoff = f"{curr_date}T{cash_open_time.isoformat()}"

    overnight_bars = [b for b in curr_bars if b[0] < cash_open_cutoff]
    if overnight_bars:
        onh = max(b[2] for b in overnight_bars)
        onl = min(b[3] for b in overnight_bars)
        on_bars_count = len(overnight_bars)
    else:
        onh = None
        onl = None
        on_bars_count = 0

    # 4. Opening Range (OR15 and OR30)
    # Defined as first 15m and 30m after cash open
    rth_bars = [b for b in curr_bars if b[0] >= cash_open_cutoff]
    or15_bars = rth_bars[:3]  # 3 x 5m = 15m
    or30_bars = rth_bars[:6]  # 6 x 5m = 30m

    or15_high = max((b[2] for b in or15_bars), default=None)
    or15_low = min((b[3] for b in or15_bars), default=None)
    or30_high = max((b[2] for b in or30_bars), default=None)
    or30_low = min((b[3] for b in or30_bars), default=None)

    # Latest live price
    latest_bar = curr_bars[-1]
    last_price = latest_bar[4]
    last_bar_ts = latest_bar[0]

    return {
        "symbol": sym,
        "as_of": target_dt.isoformat(timespec="seconds"),
        "reference_date_prior": prior_date,
        "active_date_current": curr_date,
        "last_price": round(last_price, 4),
        "last_bar_utc": last_bar_ts,
        "levels": {
            "PDH": round(pdh, 4),
            "PDL": round(pdl, 4),
            "PDC": round(pdc, 4),
            "VAH": va_profile["vah"],
            "POC": va_profile["poc"],
            "VAL": va_profile["val"],
            "ONH": round(onh, 4) if onh is not None else None,
            "ONL": round(onl, 4) if onl is not None else None,
            "OR15_HIGH": round(or15_high, 4) if or15_high is not None else None,
            "OR15_LOW": round(or15_low, 4) if or15_low is not None else None,
            "OR30_HIGH": round(or30_high, 4) if or30_high is not None else None,
            "OR30_LOW": round(or30_low, 4) if or30_low is not None else None,
        },
        "auction_context": {
            "price_vs_prior_value": (
                "ABOVE_VAH"
                if va_profile["vah"] and last_price > va_profile["vah"]
                else (
                    "BELOW_VAL"
                    if va_profile["val"] and last_price < va_profile["val"]
                    else "INSIDE_VALUE"
                )
            ),
            "price_vs_prior_range": (
                "ABOVE_PDH"
                if last_price > pdh
                else ("BELOW_PDL" if last_price < pdl else "INSIDE_DAY")
            ),
        },
        "provenance": {
            "source": ",".join(sorted(sources)),
            "prior_day_bars_evaluated": len(prior_bars),
            "current_day_bars_evaluated": len(curr_bars),
            "overnight_bars_evaluated": on_bars_count,
            "opening_range_bars_evaluated": len(or15_bars),
            "prior_day_total_volume": va_profile["total_volume"],
            "method": "AMT_discrete_volume_bins_70pct",
            "calculated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }
