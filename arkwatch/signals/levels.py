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

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Any

from .amt import (
    analyze_initial_balance,
    classify_profile_shape,
    compute_composite_value_area,
    compute_tpo_profile,
    compute_value_area,
    evaluate_auction_extremes,
    evaluate_time_acceptance,
)

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

    # AMT Advanced Profiling Integration
    tpo_data = compute_tpo_profile(prior_bars, num_bins=40, rth_open_utc=cash_open_time)
    ib_data = analyze_initial_balance(curr_bars, cash_open_time)
    shape_data = classify_profile_shape(
        va_profile["poc"] or last_price,
        va_profile["vah"] or last_price,
        va_profile["val"] or last_price,
        pdh,
        pdl,
    )
    extremes_data = evaluate_auction_extremes(prior_bars, atr_proxy)
    cva_2d = compute_composite_value_area(session_bars, num_sessions=2)
    time_acc = evaluate_time_acceptance(
        [b[4] for b in curr_bars],
        va_profile["vah"] or last_price,
        va_profile["val"] or last_price,
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
            "TPO_POC": tpo_data["tpo_poc"],
            "TPO_VAH": tpo_data["tpo_vah"],
            "TPO_VAL": tpo_data["tpo_val"],
            "CVA_2D_POC": cva_2d["c_poc"] if cva_2d else None,
            "CVA_2D_VAH": cva_2d["c_vah"] if cva_2d else None,
            "CVA_2D_VAL": cva_2d["c_val"] if cva_2d else None,
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
            "day_type": ib_data["day_type"],
            "profile_shape": shape_data["shape"],
            "profile_meaning": shape_data["meaning"],
            "time_acceptance_status": time_acc["status"],
            "time_acceptance_level": time_acc["acceptance_level"],
            "high_auction_structure": extremes_data["high_structure"],
            "low_auction_structure": extremes_data["low_structure"],
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
