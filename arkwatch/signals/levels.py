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
    ASSET_TICK_SIZES,
    analyze_initial_balance,
    classify_open_type,
    classify_participant_activity,
    classify_profile_shape,
    classify_value_migration,
    compute_dynamic_cva,
    compute_tpo_profile,
    compute_value_area,
    evaluate_auction_extremes,
    evaluate_time_acceptance,
    evaluate_vpoc_tpoc_relationship,
    find_naked_pocs,
    get_asset_ib_timing,
)
from .amt_horizons import compute_horizon_amt
from .horizons import (
    get_active_quarterly_cycles,
    get_monthly_quarter,
    get_quarterly_session_bounds,
    get_session_window,
    get_weekly_quarter,
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
        idx = len(sorted_sessions) - 1
        prior_session_id = sorted_sessions[-1]
        curr_session_id = prior_session_id
    prior_bars = session_bars[prior_session_id]
    curr_bars = session_bars.get(curr_session_id, [rows[-1]])

    # 3. Prior Session (T-1) Reference Levels
    pdh = max(b[2] for b in prior_bars)
    pdl = min(b[3] for b in prior_bars)
    pdc = prior_bars[-1][4]

    # Prior Session Value Area
    va_profile = compute_value_area(prior_bars, tick_size=ASSET_TICK_SIZES.get(sym))

    # 4. Overnight Session (Asia + London: 18:00 ET to 09:30 ET)
    edt_active = _is_dst_edt(target_dt)
    ib_open_time, ib_timing_label = get_asset_ib_timing(sym, is_dst=edt_active)

    overnight_bars = []
    rth_bars = []
    for b in curr_bars:
        b_dt = datetime.fromisoformat(b[0]).astimezone(UTC)
        if b_dt.time() < ib_open_time:
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

    # AMT 5 Pillars & Advanced Profiling Integration
    tpo_data = compute_tpo_profile(prior_bars, num_bins=40, rth_open_utc=ib_open_time)
    ib_data = analyze_initial_balance(curr_bars, ib_open_time)
    shape_data = classify_profile_shape(
        va_profile["poc"] or last_price,
        va_profile["vah"] or last_price,
        va_profile["val"] or last_price,
        pdh,
        pdl,
    )
    extremes_data = evaluate_auction_extremes(prior_bars, atr_proxy)
    time_acc = evaluate_time_acceptance(
        [b[4] for b in curr_bars],
        va_profile["vah"] or last_price,
        va_profile["val"] or last_price,
    )
    vpoc_tpoc_align = evaluate_vpoc_tpoc_relationship(
        va_profile["poc"] or last_price,
        tpo_data["tpo_poc"] or last_price,
        atr_proxy,
    )

    # Pilar 1: The 4 Open Types (James Dalton)
    open_type_info = classify_open_type(
        rth_bars if rth_bars else curr_bars[:6],
        pdh,
        pdl,
        va_profile["vah"] or last_price,
        va_profile["val"] or last_price,
        atr_proxy,
    )

    # Pilar 2: Participant Activity (Initiative vs Responsive)
    participant_info = classify_participant_activity(
        last_price,
        va_profile["vah"] or last_price,
        va_profile["val"] or last_price,
        latest_bar,
    )

    # Pilar 3: Value Migration Day-to-Day
    prior_prior_bars = session_bars.get(sorted_sessions[idx - 2]) if idx >= 2 else None
    prior_prior_va = compute_value_area(prior_prior_bars) if prior_prior_bars else None
    value_migration = classify_value_migration(
        va_profile["vah"],
        va_profile["val"],
        va_profile["poc"],
        prior_prior_va["vah"] if prior_prior_va else None,
        prior_prior_va["val"] if prior_prior_va else None,
        prior_prior_va["poc"] if prior_prior_va else None,
    )

    # Pilar 4: Dynamic N-Day CVA (Contiguous Balance Expansion & 100% Measured Move)
    dynamic_cva = compute_dynamic_cva(session_bars, min_sessions=2, max_sessions=8)

    # Pilar 5: Naked POCs (Virgin POC Liquidity Magnets)
    naked_pocs = find_naked_pocs(session_bars, last_price, lookback_sessions=25)

    # Multi-Horizon Session Profiles (Asia, London, Overlap)
    target_d = target_dt.date()
    as_s, as_e = get_session_window("ASIA", target_d)
    asia_prof = compute_horizon_amt(conn, sym, as_s, as_e)

    ld_s, ld_e = get_session_window("LONDON", target_d)
    london_prof = compute_horizon_amt(conn, sym, ld_s, ld_e)

    ov_s, ov_e = get_session_window("NY_LONDON_OVERLAP", target_d)
    overlap_prof = compute_horizon_amt(conn, sym, ov_s, ov_e)

    # Separate London Desk vs London Quarterly Theory Q2
    q_bounds = get_quarterly_session_bounds(target_d)
    q2_s, q2_e = q_bounds["Q2_LONDON"]
    q2_london_prof = compute_horizon_amt(conn, sym, q2_s, q2_e)

    # Session-to-Session Value Migration (London Desk vs Asia)
    if asia_prof and london_prof:
        session_migration = classify_value_migration(
            london_prof["vah"],
            london_prof["val"],
            london_prof["poc"],
            asia_prof["vah"],
            asia_prof["val"],
            asia_prof["poc"],
        )
    else:
        session_migration = {
            "relationship": "INSUFFICIENT_SESSION_DATA",
            "bias": "NEUTRAL",
            "meaning": "Awaiting session completion",
        }

    # IPDA Composite Value Area on Multi-Day Daily Lookbacks
    daily_rows = conn.execute(
        "SELECT ts, open, high, low, close, COALESCE(volume, 0.0) FROM instrument_prices WHERE symbol=? AND source IN ('YAHOO', 'EODHD') ORDER BY ts DESC LIMIT 65",
        (sym,),
    ).fetchall()
    ipda_ranges = {}
    for days in [1, 2, 3, 5, 10, 15, 20, 40, 60]:
        s_rows = daily_rows[:days]
        if s_rows:
            h = max(float(r[2]) for r in s_rows if r[2] is not None)
            l_val = min(float(r[3]) for r in s_rows if r[3] is not None)
            bars = [
                (r[0], float(r[1] or r[4]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                for r in reversed(s_rows)
            ]
            cva = compute_value_area(bars, tick_size=ASSET_TICK_SIZES.get(sym))
            ipda_ranges[f"{days}D"] = {
                "high": round(h, 4),
                "low": round(l_val, 4),
                "range": round(h - l_val, 4),
                "midpoint": round((h + l_val) / 2.0, 4),
                "composite_poc": cva.get("poc"),
                "composite_vah": cva.get("vah"),
                "composite_val": cva.get("val"),
                "total_volume": cva.get("total_volume"),
            }
        else:
            ipda_ranges[f"{days}D"] = {
                "high": None,
                "low": None,
                "range": None,
                "midpoint": None,
                "composite_poc": None,
                "composite_vah": None,
                "composite_val": None,
                "total_volume": None,
            }

    # Quarterly Theory Context with Real AMT Volume Profiles
    w_quarter = get_weekly_quarter(target_d)
    m_quarter = get_monthly_quarter(target_d)
    active_qt = get_active_quarterly_cycles(target_dt)

    # Active Quarter AMT Profile (6 Hours)
    qs = datetime.fromisoformat(active_qt["quarter_start_utc"])
    qe = datetime.fromisoformat(active_qt["quarter_end_utc"])
    active_q_amt = compute_horizon_amt(conn, sym, qs, qe)

    # Active 90m Sub-Quarter AMT Profile
    sub_s = datetime.fromisoformat(active_qt["sub_quarter_start_utc"])
    sub_e = datetime.fromisoformat(active_qt["sub_quarter_end_utc"])
    active_sub_amt = compute_horizon_amt(conn, sym, sub_s, sub_e)

    # Prior 90m Sub-Quarter AMT Profile
    prior_sub_amt = None
    if active_qt.get("prior_sub_quarter_start_utc") and active_qt.get("prior_sub_quarter_end_utc"):
        p_sub_s = datetime.fromisoformat(active_qt["prior_sub_quarter_start_utc"])
        p_sub_e = datetime.fromisoformat(active_qt["prior_sub_quarter_end_utc"])
        prior_sub_amt = compute_horizon_amt(conn, sym, p_sub_s, p_sub_e)

    # Active 22.5m Micro-Cycle AMT Profile
    mic_s = datetime.fromisoformat(active_qt["micro_cycle_start_utc"])
    mic_e = datetime.fromisoformat(active_qt["micro_cycle_end_utc"])
    active_micro_amt = compute_horizon_amt(conn, sym, mic_s, mic_e)

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
            "ONH": round(onh, 4) if onh is not None else round(pdh, 4),
            "ONL": round(onl, 4) if onl is not None else round(pdl, 4),
            "OR15_HIGH": round(or15_high, 4) if or15_high is not None else "FORMING_IN_RTH",
            "OR15_LOW": round(or15_low, 4) if or15_low is not None else "FORMING_IN_RTH",
            "OR30_HIGH": round(or30_high, 4) if or30_high is not None else "FORMING_IN_RTH",
            "OR30_LOW": round(or30_low, 4) if or30_low is not None else "FORMING_IN_RTH",
            "TPO_POC": tpo_data["tpo_poc"],
            "TPO_VAH": tpo_data["tpo_vah"],
            "TPO_VAL": tpo_data["tpo_val"],
            "TPO_SINGLE_PRINTS": tpo_data.get("single_prints", []),
            "DYNAMIC_CVA_NAME": dynamic_cva["composite_name"] if dynamic_cva else "BALANCED_RANGE",
            "DYNAMIC_CVA_POC": dynamic_cva["c_poc"] if dynamic_cva else va_profile["poc"],
            "DYNAMIC_CVA_VAH": dynamic_cva["c_vah"] if dynamic_cva else va_profile["vah"],
            "DYNAMIC_CVA_VAL": dynamic_cva["c_val"] if dynamic_cva else va_profile["val"],
            "CVA_MEASURED_MOVE_LONG": dynamic_cva["dalton_measured_move"]["upside_breakout_target"]
            if dynamic_cva
            else round(pdh + (0.5 * atr_proxy), 4),
            "CVA_MEASURED_MOVE_SHORT": dynamic_cva["dalton_measured_move"][
                "downside_breakout_target"
            ]
            if dynamic_cva
            else round(pdl - (0.5 * atr_proxy), 4),
            "NAKED_POC_ABOVE": naked_pocs["nearest_naked_poc_above"]["poc"]
            if naked_pocs.get("nearest_naked_poc_above")
            else None,
            "NAKED_POC_BELOW": naked_pocs["nearest_naked_poc_below"]["poc"]
            if naked_pocs.get("nearest_naked_poc_below")
            else None,
            "WEEKLY_VWAP": weekly_vwap,
            "WEEKLY_VAH": weekly_va["vah"],
            "WEEKLY_POC": weekly_va["poc"],
            "WEEKLY_VAL": weekly_va["val"],
            "ASIA_VAH": asia_prof["vah"] if asia_prof else None,
            "ASIA_VAL": asia_prof["val"] if asia_prof else None,
            "ASIA_POC": asia_prof["poc"] if asia_prof else None,
            "ASIA_VWAP": asia_prof["vwap"] if asia_prof else None,
            "LONDON_VAH": london_prof["vah"] if london_prof else None,
            "LONDON_VAL": london_prof["val"] if london_prof else None,
            "LONDON_POC": london_prof["poc"] if london_prof else None,
            "LONDON_VWAP": london_prof["vwap"] if london_prof else None,
            "IPDA_1D_HIGH": ipda_ranges.get("1D", {}).get("high"),
            "IPDA_1D_LOW": ipda_ranges.get("1D", {}).get("low"),
            "IPDA_2D_HIGH": ipda_ranges.get("2D", {}).get("high"),
            "IPDA_2D_LOW": ipda_ranges.get("2D", {}).get("low"),
            "IPDA_3D_HIGH": ipda_ranges.get("3D", {}).get("high"),
            "IPDA_3D_LOW": ipda_ranges.get("3D", {}).get("low"),
            "IPDA_5D_HIGH": ipda_ranges.get("5D", {}).get("high"),
            "IPDA_5D_LOW": ipda_ranges.get("5D", {}).get("low"),
            "IPDA_10D_HIGH": ipda_ranges.get("10D", {}).get("high"),
            "IPDA_10D_LOW": ipda_ranges.get("10D", {}).get("low"),
            "IPDA_15D_HIGH": ipda_ranges.get("15D", {}).get("high"),
            "IPDA_15D_LOW": ipda_ranges.get("15D", {}).get("low"),
            "IPDA_20D_HIGH": ipda_ranges.get("20D", {}).get("high"),
            "IPDA_20D_LOW": ipda_ranges.get("20D", {}).get("low"),
            "IPDA_40D_HIGH": ipda_ranges.get("40D", {}).get("high"),
            "IPDA_40D_LOW": ipda_ranges.get("40D", {}).get("low"),
            "IPDA_60D_HIGH": ipda_ranges.get("60D", {}).get("high"),
            "IPDA_60D_LOW": ipda_ranges.get("60D", {}).get("low"),
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
            "ib_timing_convention": ib_timing_label,
            "vpoc_tpoc_alignment": vpoc_tpoc_align,
            "day_type": ib_data["day_type"],
            "open_type": open_type_info["open_type"],
            "open_conviction": open_type_info["conviction"],
            "open_rationale": open_type_info["rationale"],
            "participant_activity": participant_info["activity"],
            "participant_meaning": participant_info["meaning"],
            "value_migration": value_migration["relationship"],
            "value_migration_bias": value_migration["bias"],
            "dynamic_cva_days": dynamic_cva["composite_days_count"] if dynamic_cva else 1,
            "naked_pocs_count": naked_pocs["total_naked_pocs"],
            "profile_shape": shape_data["shape"],
            "profile_meaning": shape_data["meaning"],
            "time_acceptance_status": time_acc["status"],
            "time_acceptance_level": time_acc["acceptance_level"],
            "high_auction_structure": extremes_data["high_structure"],
            "low_auction_structure": extremes_data["low_structure"],
            "session_profiles": {
                "asia": asia_prof if asia_prof else "N/A (Awaiting Session Bars)",
                "london_desk": london_prof if london_prof else "N/A (Awaiting Session Bars)",
                "overlap": overlap_prof if overlap_prof else "N/A (Awaiting Session Bars)",
                "q2_london_quarter": q2_london_prof
                if q2_london_prof
                else "N/A (Awaiting Session Bars)",
            },
            "session_value_migration": session_migration,
            "ipda_data_ranges": ipda_ranges,
            "quarterly_theory": {
                "active_quarter": active_qt["active_quarter"],
                "active_quarter_amt": active_q_amt if active_q_amt else "N/A",
                "active_90m_sub_quarter": active_qt["active_90m_sub_quarter"],
                "sub_quarter_role": active_qt["sub_quarter_role"],
                "active_90m_sub_quarter_amt": active_sub_amt if active_sub_amt else "N/A",
                "prior_90m_sub_quarter_amt": prior_sub_amt if prior_sub_amt else "N/A",
                "active_22m_micro_cycle": active_qt["active_22m_micro_cycle"],
                "micro_cycle_role": active_qt["micro_cycle_role"],
                "active_22m_micro_cycle_amt": active_micro_amt if active_micro_amt else "N/A",
                "weekly_quarter": w_quarter,
                "monthly_quarter": m_quarter,
            },
        },
        "provenance": {
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
