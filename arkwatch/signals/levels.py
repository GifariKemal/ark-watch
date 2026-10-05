"""levels.py — Auction Market Theory (AMT) and CME Globex session reference levels.

Extracts high-precision institutional reference levels and Multi-Anchor Value Area profiling
from 5-minute intraday bars (intraday_bars) with 100% auditable provenance:
  - Prior Day Reference (T-1): PDH (High), PDL (Low), PDC (Close)
  - Prior Day Value Area (T-1): VAH, VAL, POC (70% volume distribution)
  - Overnight Session (Asia/London): ONH (High), ONL (Low) from 18:00 ET to 09:30 ET
  - Opening Range (OR): OR15 (15m range), OR30 (30m range) after 09:30 ET cash open
  - Developing Weekly Multi-Anchor: Weekly VWAP, Weekly VAH, Weekly VAL, Weekly POC
  - Confluence Analysis: Detection of Weekly VWAP + Prior Day VAH/VAL compression zones
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Any

US_CASH_OPEN_UTC_SUMMER = time(13, 30)  # 09:30 ET during EDT
US_CASH_OPEN_UTC_WINTER = time(14, 30)  # 09:30 ET during EST


def _is_dst_edt(dt: datetime) -> bool:
    """Check if date falls within US Daylight Saving Time (EDT, UTC-4)."""
    m = dt.month
    if 4 <= m <= 10:
        return True
    if m == 3 and dt.day >= 8:
        return True
    return bool(m == 11 and dt.day <= 7 and dt.weekday() != 6)


def _get_cash_open_time(dt: datetime) -> time:
    """Determine US cash open in UTC based on DST (EDT vs EST)."""
    return US_CASH_OPEN_UTC_SUMMER if _is_dst_edt(dt) else US_CASH_OPEN_UTC_WINTER


def _get_cme_session_id(dt: datetime) -> str:
    """Map UTC timestamp to CME Trading Session Date (18:00 ET yesterday to 17:00 ET today)."""
    edt = _is_dst_edt(dt)
    shift_hour = 22 if edt else 23  # 18:00 ET in UTC
    if dt.hour >= shift_hour:
        # Bars starting at 18:00 ET belong to the next calendar trading day
        session_dt = dt.date() + timedelta(days=1)
        return session_dt.isoformat()
    return dt.date().isoformat()


def _get_cme_week_id(dt: datetime) -> str:
    """Map UTC timestamp to CME Trading Week (Sunday 18:00 ET to Friday 17:00 ET)."""
    edt = _is_dst_edt(dt)
    shift_hour = 22 if edt else 23
    effective_dt = dt
    if dt.weekday() == 6 and dt.hour >= shift_hour:
        effective_dt = dt + timedelta(days=1)
    y, w, _ = effective_dt.isocalendar()
    return f"{y}-W{w:02d}"


def compute_value_area(
    bars: list[tuple[str, float, float, float, float, float]],
    *,
    num_bins: int = 50,
    va_volume_ratio: float = 0.70,
) -> dict[str, Any]:
    """Compute Point of Control (POC), VAH, and VAL using discrete volume profile binning."""
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
        vol = max(1.0, float(v) if v is not None else 1.0)
        total_vol += vol

        start_idx = max(0, min(num_bins - 1, int(math.floor((low_val - min_p) / bin_size))))
        end_idx = max(0, min(num_bins - 1, int(math.floor((h - min_p) / bin_size))))
        covered_bins = end_idx - start_idx + 1
        vol_per_bin = vol / float(covered_bins)
        for idx in range(start_idx, end_idx + 1):
            volume_by_bin[idx] += vol_per_bin

    poc_idx = max(range(num_bins), key=lambda i: volume_by_bin[i])
    poc_price = min_p + (poc_idx + 0.5) * bin_size

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
    """Compute Prior Session (T-1) Levels, Overnight Range, Developing Weekly Multi-Anchor, and Confluence."""
    sym = symbol.strip().upper()

    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of).astimezone(UTC)
    else:
        target_dt = as_of.astimezone(UTC)

    # 1. Query all historical intraday bars up to target_dt
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

    # 2. Segment bars by CME Trading Session ID and Trading Week ID
    session_bars: dict[str, list[tuple[str, float, float, float, float, float]]] = defaultdict(list)
    week_bars: dict[str, list[tuple[str, float, float, float, float, float]]] = defaultdict(list)
    sources = set()

    for r in rows:
        ts_str, o, h, low_val, c, v, src = r
        bar_dt = datetime.fromisoformat(ts_str).astimezone(UTC)
        s_id = _get_cme_session_id(bar_dt)
        w_id = _get_cme_week_id(bar_dt)
        bar_tuple = (ts_str, float(o), float(h), float(low_val), float(c), float(v))
        session_bars[s_id].append(bar_tuple)
        week_bars[w_id].append(bar_tuple)
        sources.add(src)

    sorted_sessions = sorted(session_bars.keys())
    if not sorted_sessions:
        return None

    curr_session_id = _get_cme_session_id(target_dt)
    curr_week_id = _get_cme_week_id(target_dt)

    # Determine prior completed session
    if curr_session_id in sorted_sessions:
        idx = sorted_sessions.index(curr_session_id)
        prior_session_id = sorted_sessions[idx - 1] if idx > 0 else sorted_sessions[0]
    else:
        prior_session_id = sorted_sessions[-1]
        curr_session_id = prior_session_id

    prior_bars = session_bars[prior_session_id]
    curr_bars = session_bars.get(curr_session_id, [rows[-1]])

    # 3. Prior Session (T-1) Reference Levels
    pdh = max(b[2] for b in prior_bars)
    pdl = min(b[3] for b in prior_bars)
    pdc = prior_bars[-1][4]

    # Prior Session Value Area
    va_profile = compute_value_area(prior_bars)

    # 4. Overnight Session (Asia + London: 18:00 ET to 09:30 ET)
    cash_open_time = _get_cash_open_time(target_dt)
    # Bars before 09:30 ET are overnight
    overnight_bars = []
    rth_bars = []
    for b in curr_bars:
        b_dt = datetime.fromisoformat(b[0]).astimezone(UTC)
        if b_dt.time() < cash_open_time:
            overnight_bars.append(b)
        else:
            rth_bars.append(b)

    onh = max((b[2] for b in overnight_bars), default=None)
    onl = min((b[3] for b in overnight_bars), default=None)

    # 5. Opening Range (OR15 and OR30)
    or15_bars = rth_bars[:3]
    or30_bars = rth_bars[:6]
    or15_high = max((b[2] for b in or15_bars), default=None)
    or15_low = min((b[3] for b in or15_bars), default=None)
    or30_high = max((b[2] for b in or30_bars), default=None)
    or30_low = min((b[3] for b in or30_bars), default=None)

    # 6. Developing Weekly Multi-Anchor (Weekly VWAP & Weekly Value Area)
    cur_week_bars = week_bars.get(curr_week_id, curr_bars)
    cum_pv = sum(((b[2] + b[3] + b[4]) / 3.0) * max(1.0, b[5]) for b in cur_week_bars)
    cum_v = sum(max(1.0, b[5]) for b in cur_week_bars)
    weekly_vwap = round(cum_pv / cum_v, 4) if cum_v > 0 else None
    weekly_va = compute_value_area(cur_week_bars)

    # 7. Latest Price and Confluence Analysis
    latest_bar = curr_bars[-1]
    last_price = latest_bar[4]
    last_bar_ts = latest_bar[0]

    # Calculate ATR proxy to measure distance
    daily_range = pdh - pdl
    atr_proxy = max(0.001, daily_range)

    confluence_notes = []
    vah_price = va_profile["vah"]
    val_price = va_profile["val"]

    # Confluence Check: Weekly VWAP within 0.20 ATR of Prior Day VAH or VAL
    is_confluence_vah = (
        weekly_vwap is not None
        and vah_price is not None
        and abs(weekly_vwap - vah_price) <= (0.20 * atr_proxy)
    )
    is_confluence_val = (
        weekly_vwap is not None
        and val_price is not None
        and abs(weekly_vwap - val_price) <= (0.20 * atr_proxy)
    )

    if is_confluence_vah:
        confluence_notes.append("WEEKLY_VWAP_CONFLUENCE_WITH_VAH (Compression Breakout Zone)")
    if is_confluence_val:
        confluence_notes.append("WEEKLY_VWAP_CONFLUENCE_WITH_VAL (Compression Breakout Zone)")

    confluence_state = (
        "COMPRESSION_BREAKOUT_ZONE"
        if (is_confluence_vah or is_confluence_val)
        else "NORMAL_DISPERSED"
    )

    return {
        "symbol": sym,
        "as_of": target_dt.isoformat(timespec="seconds"),
        "reference_session_prior": prior_session_id,
        "active_session_current": curr_session_id,
        "active_week": curr_week_id,
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
            "WEEKLY_VWAP": weekly_vwap,
            "WEEKLY_VAH": weekly_va["vah"],
            "WEEKLY_POC": weekly_va["poc"],
            "WEEKLY_VAL": weekly_va["val"],
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
            "price_vs_weekly_vwap": (
                "ABOVE_WEEKLY_VWAP"
                if weekly_vwap and last_price >= weekly_vwap
                else "BELOW_WEEKLY_VWAP"
            ),
            "confluence_state": confluence_state,
            "confluence_details": confluence_notes,
        },
        "provenance": {
            "source": ",".join(sorted(sources)),
            "session_convention": "CME_Globex_18ET_to_17ET",
            "prior_session_bars_evaluated": len(prior_bars),
            "current_session_bars_evaluated": len(curr_bars),
            "overnight_bars_evaluated": len(overnight_bars),
            "rth_bars_evaluated": len(rth_bars),
            "weekly_bars_accumulated": len(cur_week_bars),
            "prior_session_total_volume": va_profile["total_volume"],
            "method": "AMT_discrete_volume_bins_70pct_and_cumulative_weekly_vwap",
            "calculated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }
