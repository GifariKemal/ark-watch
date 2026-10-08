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
    get_month_week_anchor_ny,
    get_monthly_quarter,
    get_quarterly_session_bounds,
    get_session_window,
    get_weekly_quarter,
    get_yearly_cycle,
    subdivide_micro_22m,
    subdivide_quarter_90m,
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

    # Pilar 5: Fractal Hierarchical Naked POCs (Micro to Yearly)
    # Tier 1: Intraday 90m Sub-Quarter Naked POCs (Lookback last 3 days)
    sq_bars = defaultdict(list)
    cutoff_90m = (target_dt - timedelta(days=3)).isoformat(timespec="seconds")
    for r in rows:
        if r[0] >= cutoff_90m:
            b_epoch = int(datetime.fromisoformat(r[0]).astimezone(UTC).timestamp())
            b_id = datetime.fromtimestamp((b_epoch // 5400) * 5400, tz=UTC).strftime(
                "%Y-%m-%d %H:%M"
            )
            sq_bars[b_id].append(
                (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            )
    intraday_90m_npocs = find_naked_pocs(sq_bars, last_price, lookback_sessions=32)

    # Tier 2: Session Naked POCs (25 sessions lookback)
    session_naked_pocs = find_naked_pocs(session_bars, last_price, lookback_sessions=25)

    # Tier 3: Weekly Virgin POCs (8 weeks lookback)
    weekly_naked_pocs = find_naked_pocs(week_bars, last_price, lookback_sessions=8)

    # Tier 4 & 5: Monthly and Yearly Virgin POCs (from instrument_prices)
    month_bars_dict = defaultdict(list)
    year_bars_dict = defaultdict(list)
    rows_hist = conn.execute(
        "SELECT strftime('%Y-%m', ts), ts, open, high, low, close, COALESCE(volume, 0.0) FROM instrument_prices WHERE symbol=? AND source IN ('YAHOO', 'EODHD') ORDER BY ts ASC",
        (sym,),
    ).fetchall()
    for r in rows_hist:
        if r[3] is None or r[4] is None or r[5] is None:
            continue
        b_hist = (
            r[1],
            float(r[2] if r[2] is not None else r[5]),
            float(r[3]),
            float(r[4]),
            float(r[5]),
            float(r[6] if r[6] is not None else 0.0),
        )
        month_bars_dict[r[0]].append(b_hist)
        year_bars_dict[r[0][:4]].append(b_hist)

    monthly_naked_pocs = find_naked_pocs(month_bars_dict, last_price, lookback_sessions=12)
    yearly_naked_pocs = find_naked_pocs(year_bars_dict, last_price, lookback_sessions=10)
    naked_pocs = session_naked_pocs
    # Multi-Horizon Session Profiles (Asia, London, Overlap)
    target_d = target_dt.date()
    as_s, as_e = get_session_window("ASIA", target_d)
    asia_prof = compute_horizon_amt(conn, sym, as_s, as_e)

    ld_s, ld_e = get_session_window("LONDON", target_d)
    london_prof = compute_horizon_amt(conn, sym, ld_s, ld_e)

    ov_s, ov_e = get_session_window("NY_LONDON_OVERLAP", target_d)
    overlap_prof = compute_horizon_amt(conn, sym, ov_s, ov_e)

    # Minor Sessions & Quarterly Theory Slices
    fk_s, fk_e = get_session_window("FRANKFURT", target_d)
    frankfurt_prof = compute_horizon_amt(conn, sym, fk_s, min(fk_e, target_dt))

    sg_s, sg_e = get_session_window("SINGAPORE", target_d)
    singapore_prof = compute_horizon_amt(conn, sym, sg_s, min(sg_e, target_dt))

    pl_s, pl_e = get_session_window("PRE_LONDON", target_d)
    pre_london_prof = compute_horizon_amt(conn, sym, pl_s, min(pl_e, target_dt))

    ny_s, ny_e = get_session_window("NY_REGULAR", target_d)
    ny_regular_prof = compute_horizon_amt(conn, sym, ny_s, min(ny_e, target_dt))

    # Multi-Desk Initial Balance (Asia, Frankfurt, London, and US Cash Open)
    asia_ib = analyze_initial_balance(curr_bars, time(0, 0))
    frankfurt_ib = analyze_initial_balance(curr_bars, time(6, 0))
    london_ib = analyze_initial_balance(curr_bars, time(7, 0))
    us_rth_ib = analyze_initial_balance(curr_bars, ib_open_time)

    # Overnight CVA (Low-Horizon CVA: Asia + London Merged)
    on_cva_bars = [
        b for b in curr_bars if datetime.fromisoformat(b[0]).astimezone(UTC).time() < ib_open_time
    ]
    if on_cva_bars:
        on_cva = compute_value_area(on_cva_bars, tick_size=ASSET_TICK_SIZES.get(sym))
        on_range = (
            (on_cva["vah"] - on_cva["val"]) if on_cva.get("vah") and on_cva.get("val") else 0.0
        )
        overnight_cva = {
            "status": "COMPLETED",
            "c_poc": on_cva.get("poc"),
            "c_vah": on_cva.get("vah"),
            "c_val": on_cva.get("val"),
            "c_range": round(on_range, 4),
            "dalton_measured_move": {
                "upside_breakout_target": (
                    round(on_cva["vah"] + on_range, 4)
                    if on_cva.get("vah")
                    else "AWAITING_EXPANSION"
                ),
                "downside_breakout_target": (
                    round(on_cva["val"] - on_range, 4)
                    if on_cva.get("val")
                    else "AWAITING_EXPANSION"
                ),
            },
            "total_volume": on_cva.get("total_volume", 0.0),
        }
    else:
        overnight_cva = {
            "status": "AWAITING_BARS",
            "c_poc": "AWAITING_BARS",
            "c_vah": "AWAITING_BARS",
            "c_val": "AWAITING_BARS",
            "c_range": 0.0,
            "dalton_measured_move": {
                "upside_breakout_target": "AWAITING_BARS",
                "downside_breakout_target": "AWAITING_BARS",
            },
            "total_volume": 0.0,
        }
    q_bounds = get_quarterly_session_bounds(target_d)
    q2_s, q2_e = q_bounds["Q2_LONDON"]
    q2_london_prof = compute_horizon_amt(conn, sym, q2_s, min(q2_e, target_dt))
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
        s_rows = [
            r
            for r in daily_rows[:days]
            if r[2] is not None and r[3] is not None and r[4] is not None
        ]
        if s_rows:
            h = max(float(r[2]) for r in s_rows)
            l_val = min(float(r[3]) for r in s_rows)
            bars = [
                (
                    r[0],
                    float(r[1] if r[1] is not None else r[4]),
                    float(r[2]),
                    float(r[3]),
                    float(r[4]),
                    float(r[5] if r[5] is not None else 0.0),
                )
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

    # IPDA Intraday Lookbacks (4H, 8H, 12H)
    for h_str, hrs in [("4H", 4), ("8H", 8), ("12H", 12)]:
        s_dt = target_dt - timedelta(hours=hrs)
        h_amt = compute_horizon_amt(conn, sym, s_dt, target_dt)
        if h_amt:
            ipda_ranges[h_str] = {
                "high": round(h_amt["high"], 4),
                "low": round(h_amt["low"], 4),
                "range": round(h_amt["range"], 4),
                "midpoint": round((h_amt["high"] + h_amt["low"]) / 2.0, 4),
                "composite_poc": h_amt.get("poc"),
                "composite_vah": h_amt.get("vah"),
                "composite_val": h_amt.get("val"),
                "total_volume": h_amt.get("total_volume"),
            }
        else:
            ipda_ranges[h_str] = {
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
    active_micro_amt = compute_horizon_amt(conn, sym, mic_s, min(mic_e, target_dt))

    # All 4 Daily Quarters in Quarterly Theory
    all_daily_quarters = {}
    for q_name, (qs_b, qe_b) in q_bounds.items():
        if qs_b < target_dt:
            q_p = compute_horizon_amt(conn, sym, qs_b, min(qe_b, target_dt))
            if q_p:
                all_daily_quarters[q_name] = {
                    "vah": q_p.get("vah"),
                    "val": q_p.get("val"),
                    "poc": q_p.get("poc"),
                    "total_volume": q_p.get("total_volume"),
                    "vwap": q_p.get("vwap"),
                }
            else:
                all_daily_quarters[q_name] = "Awaiting"
        else:
            all_daily_quarters[q_name] = "Upcoming"

    # All 4 Sub-Quarters 90m for the Active Quarter
    active_quarter_sub_quarters = []
    for idx, (ss, se) in enumerate(subdivide_quarter_90m(qs, qe)):
        s_amt = compute_horizon_amt(conn, sym, ss, min(se, target_dt)) if ss < target_dt else None
        if s_amt:
            active_quarter_sub_quarters.append(
                {
                    "sub_quarter": f"Sub-{idx + 1}",
                    "status": "COMPLETED" if target_dt >= se else "ACTIVE",
                    "vah": s_amt["vah"],
                    "val": s_amt["val"],
                    "poc": s_amt["poc"],
                    "total_volume": s_amt["total_volume"],
                }
            )
        else:
            lbl = "UPCOMING" if ss >= target_dt else "AWAITING_BARS"
            active_quarter_sub_quarters.append(
                {
                    "sub_quarter": f"Sub-{idx + 1}",
                    "status": lbl,
                    "vah": lbl,
                    "val": lbl,
                    "poc": lbl,
                    "total_volume": 0.0,
                }
            )

    # All 4 Micro-Cycles 22.5m for the Active Sub-Quarter
    active_sub_quarter_micros = []
    for idx, (ms, me) in enumerate(subdivide_micro_22m(sub_s, sub_e)):
        m_amt = compute_horizon_amt(conn, sym, ms, min(me, target_dt)) if ms < target_dt else None
        if m_amt:
            active_sub_quarter_micros.append(
                {
                    "micro_cycle": f"Micro-{idx + 1}",
                    "status": "COMPLETED" if target_dt >= me else "ACTIVE",
                    "vah": m_amt["vah"],
                    "val": m_amt["val"],
                    "poc": m_amt["poc"],
                    "total_volume": m_amt["total_volume"],
                }
            )
        else:
            lbl = "UPCOMING" if ms >= target_dt else "AWAITING_BARS"
            active_sub_quarter_micros.append(
                {
                    "micro_cycle": f"Micro-{idx + 1}",
                    "status": lbl,
                    "vah": lbl,
                    "val": lbl,
                    "poc": lbl,
                    "total_volume": 0.0,
                }
            )
    monday = target_d - timedelta(days=target_d.weekday())
    days_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
    weekly_days_profile = {}
    bars_72h = []
    for day_name, d_offset in days_map.items():
        curr_day = monday + timedelta(days=d_offset)
        day_str = curr_day.isoformat()
        if curr_day < target_d:
            row = conn.execute(
                "SELECT ts, open, high, low, close, COALESCE(volume, 0.0) FROM instrument_prices WHERE symbol=? AND ts=? AND source IN ('YAHOO', 'EODHD')",
                (sym, day_str),
            ).fetchone()
            if row and row[2] is not None and row[3] is not None:
                b_tup = (
                    row[0],
                    float(row[1] or row[4]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                )
                if d_offset <= 2:
                    bars_72h.append(b_tup)
                weekly_days_profile[day_name] = {
                    "date": day_str,
                    "status": "COMPLETED",
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            else:
                weekly_days_profile[day_name] = {
                    "date": day_str,
                    "status": "AWAITING_DATA",
                    "high": "AWAITING_DATA",
                    "low": "AWAITING_DATA",
                    "close": "AWAITING_DATA",
                    "volume": 0.0,
                }
        elif curr_day == target_d:
            weekly_days_profile[day_name] = {
                "date": day_str,
                "status": "ACTIVE_TODAY",
                "high": pdh,
                "low": pdl,
                "close": last_price,
                "volume": va_profile.get("total_volume", 0.0),
            }
        else:
            weekly_days_profile[day_name] = {
                "date": day_str,
                "status": "UPCOMING",
                "high": "UPCOMING",
                "low": "UPCOMING",
                "close": "UPCOMING",
                "volume": 0.0,
            }

    # Midweek 72H Composite (Senin - Rabu)
    if bars_72h:
        cva_72h = compute_value_area(bars_72h, tick_size=ASSET_TICK_SIZES.get(sym))
        midweek_72h_composite = {
            "status": "COMPLETED",
            "composite_poc": cva_72h.get("poc"),
            "composite_vah": cva_72h.get("vah"),
            "composite_val": cva_72h.get("val"),
            "total_volume": cva_72h.get("total_volume"),
        }
    else:
        midweek_72h_composite = {
            "status": "FORMING_IN_WEEK",
            "composite_poc": "FORMING_IN_WEEK",
            "composite_vah": "FORMING_IN_WEEK",
            "composite_val": "FORMING_IN_WEEK",
            "total_volume": 0.0,
        }

    # 1. Yearly Cycle & Prior Yearly Quarters Lookback
    yearly_cycle = get_yearly_cycle(target_d)
    y_curr = target_d.year
    prior_yearly_quarters_amt = {}
    curr_q_num = ((target_d.month - 1) // 3) + 1
    for q_idx, (q_lbl, q_m_start, q_m_end) in enumerate(
        [("Q1", 1, 3), ("Q2", 4, 6), ("Q3", 7, 9), ("Q4", 10, 12)]
    ):
        if q_idx + 1 < curr_q_num:
            import calendar as _cal

            q_start_str = f"{y_curr}-{q_m_start:02d}-01"
            _, last_q_day = _cal.monthrange(y_curr, q_m_end)
            q_end_str = f"{y_curr}-{q_m_end:02d}-{last_q_day:02d}"
            rows_yq = conn.execute(
                "SELECT ts, open, high, low, close, COALESCE(volume, 0.0) FROM instrument_prices WHERE symbol=? AND ts >= ? AND ts <= ? AND source IN ('YAHOO', 'EODHD') ORDER BY ts ASC",
                (sym, q_start_str, q_end_str),
            ).fetchall()
            b_yq = [
                (r[0], float(r[1] or r[4]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                for r in rows_yq
                if r[2] is not None and r[3] is not None and r[4] is not None
            ]
            if b_yq:
                va_yq = compute_value_area(b_yq, tick_size=ASSET_TICK_SIZES.get(sym))
                prior_yearly_quarters_amt[f"{y_curr}_{q_lbl}"] = {
                    "period": f"{q_start_str} -> {q_end_str}",
                    "high": max(b[2] for b in b_yq),
                    "low": min(b[3] for b in b_yq),
                    "composite_poc": va_yq.get("poc"),
                    "composite_vah": va_yq.get("vah"),
                    "composite_val": va_yq.get("val"),
                    "total_volume": va_yq.get("total_volume"),
                }

    # 2. Prior Months Lookback (M-1 and M-2) using PineScript Anchors
    prior_months_amt = {}
    curr_y = target_d.year
    curr_m = target_d.month
    m1_y = curr_y if curr_m > 1 else curr_y - 1
    m1_m = curr_m - 1 if curr_m > 1 else 12
    m2_y = m1_y if m1_m > 1 else m1_y - 1
    m2_m = m1_m - 1 if m1_m > 1 else 12

    m1_anc = get_month_week_anchor_ny(m1_y, m1_m)
    curr_anc = get_month_week_anchor_ny(curr_y, curr_m)
    m2_anc = get_month_week_anchor_ny(m2_y, m2_m)

    for m_lbl, (anc_s, anc_e) in [
        (f"M-1_{m1_y}_{m1_m:02d}", (m1_anc, curr_anc)),
        (f"M-2_{m2_y}_{m2_m:02d}", (m2_anc, m1_anc)),
    ]:
        rows_pm = conn.execute(
            "SELECT ts, open, high, low, close, COALESCE(volume, 0.0) FROM instrument_prices WHERE symbol=? AND ts >= ? AND ts <= ? AND source IN ('YAHOO', 'EODHD') ORDER BY ts ASC",
            (sym, anc_s.strftime("%Y-%m-%d"), anc_e.strftime("%Y-%m-%d")),
        ).fetchall()
        b_pm = [
            (r[0], float(r[1] or r[4]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in rows_pm
            if r[2] is not None and r[3] is not None and r[4] is not None
        ]
        if b_pm:
            va_pm = compute_value_area(b_pm, tick_size=ASSET_TICK_SIZES.get(sym))
            prior_months_amt[m_lbl] = {
                "anchor_start": anc_s.strftime("%Y-%m-%d %H:%M ET"),
                "anchor_end": anc_e.strftime("%Y-%m-%d %H:%M ET"),
                "high": max(b[2] for b in b_pm),
                "low": min(b[3] for b in b_pm),
                "composite_poc": va_pm.get("poc"),
                "composite_vah": va_pm.get("vah"),
                "composite_val": va_pm.get("val"),
                "total_volume": va_pm.get("total_volume"),
            }

    # 3. Monthly Weeks Schedule Composite Blocks (PineScript Weeks 1-4 / Joker)
    monthly_quarter_blocks = {}
    for w_info in m_quarter.get("weeks_schedule", []):
        b_name = f"WEEK_{w_info['week_index']}_{w_info['quarter']}"
        b_start = w_info["start_et"][:10]
        b_end = w_info["end_et"][:10]
        rows = conn.execute(
            "SELECT ts, open, high, low, close, COALESCE(volume, 0.0) FROM instrument_prices WHERE symbol=? AND ts >= ? AND ts <= ? AND source IN ('YAHOO', 'EODHD') ORDER BY ts ASC",
            (sym, b_start, b_end),
        ).fetchall()
        b_bars = [
            (r[0], float(r[1] or r[4]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in rows
            if r[2] is not None and r[3] is not None and r[4] is not None
        ]
        if b_bars:
            va_mb = compute_value_area(b_bars, tick_size=ASSET_TICK_SIZES.get(sym))
            monthly_quarter_blocks[b_name] = {
                "start_date": b_start,
                "end_date": b_end,
                "status": "COMPLETED" if target_d.isoformat() >= b_end else "ACTIVE",
                "composite_poc": va_mb.get("poc"),
                "composite_vah": va_mb.get("vah"),
                "composite_val": va_mb.get("val"),
                "total_volume": va_mb.get("total_volume"),
            }
        else:
            lbl = "UPCOMING" if target_d.isoformat() < b_start else "AWAITING_BARS"
            monthly_quarter_blocks[b_name] = {
                "start_date": b_start,
                "end_date": b_end,
                "status": lbl,
                "composite_poc": lbl,
                "composite_vah": lbl,
                "composite_val": lbl,
                "total_volume": 0.0,
            }
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
            "hierarchical_naked_pocs": {
                "intraday_90m_naked_pocs": {
                    "total_naked_pocs": intraday_90m_npocs.get("total_naked_pocs", 0),
                    "nearest_naked_poc_above": (
                        intraday_90m_npocs["nearest_naked_poc_above"]
                        if intraday_90m_npocs.get("nearest_naked_poc_above")
                        else "NONE_IN_LOOKBACK (All-Time High / Blue Sky)"
                    ),
                    "nearest_naked_poc_below": (
                        intraday_90m_npocs["nearest_naked_poc_below"]
                        if intraday_90m_npocs.get("nearest_naked_poc_below")
                        else "NONE_IN_LOOKBACK (All-Time Low)"
                    ),
                    "all_naked_pocs": intraday_90m_npocs.get("all_naked_pocs", []),
                },
                "session_naked_pocs": {
                    "total_naked_pocs": session_naked_pocs.get("total_naked_pocs", 0),
                    "nearest_naked_poc_above": (
                        session_naked_pocs["nearest_naked_poc_above"]
                        if session_naked_pocs.get("nearest_naked_poc_above")
                        else "NONE_IN_LOOKBACK (All-Time High / Blue Sky)"
                    ),
                    "nearest_naked_poc_below": (
                        session_naked_pocs["nearest_naked_poc_below"]
                        if session_naked_pocs.get("nearest_naked_poc_below")
                        else "NONE_IN_LOOKBACK (All-Time Low)"
                    ),
                    "all_naked_pocs": session_naked_pocs.get("all_naked_pocs", []),
                },
                "weekly_virgin_pocs": {
                    "total_naked_pocs": weekly_naked_pocs.get("total_naked_pocs", 0),
                    "nearest_naked_poc_above": (
                        weekly_naked_pocs["nearest_naked_poc_above"]
                        if weekly_naked_pocs.get("nearest_naked_poc_above")
                        else "NONE_IN_LOOKBACK (All-Time High / Blue Sky)"
                    ),
                    "nearest_naked_poc_below": (
                        weekly_naked_pocs["nearest_naked_poc_below"]
                        if weekly_naked_pocs.get("nearest_naked_poc_below")
                        else "NONE_IN_LOOKBACK (All-Time Low)"
                    ),
                    "all_naked_pocs": weekly_naked_pocs.get("all_naked_pocs", []),
                },
                "monthly_virgin_pocs": {
                    "total_naked_pocs": monthly_naked_pocs.get("total_naked_pocs", 0),
                    "nearest_naked_poc_above": (
                        monthly_naked_pocs["nearest_naked_poc_above"]
                        if monthly_naked_pocs.get("nearest_naked_poc_above")
                        else "NONE_IN_LOOKBACK (All-Time High / Blue Sky)"
                    ),
                    "nearest_naked_poc_below": (
                        monthly_naked_pocs["nearest_naked_poc_below"]
                        if monthly_naked_pocs.get("nearest_naked_poc_below")
                        else "NONE_IN_LOOKBACK (All-Time Low)"
                    ),
                    "all_naked_pocs": monthly_naked_pocs.get("all_naked_pocs", []),
                },
                "yearly_virgin_pocs": {
                    "total_naked_pocs": yearly_naked_pocs.get("total_naked_pocs", 0),
                    "nearest_naked_poc_above": (
                        yearly_naked_pocs["nearest_naked_poc_above"]
                        if yearly_naked_pocs.get("nearest_naked_poc_above")
                        else "NONE_IN_LOOKBACK (All-Time High / Blue Sky)"
                    ),
                    "nearest_naked_poc_below": (
                        yearly_naked_pocs["nearest_naked_poc_below"]
                        if yearly_naked_pocs.get("nearest_naked_poc_below")
                        else "NONE_IN_LOOKBACK (All-Time Low)"
                    ),
                    "all_naked_pocs": yearly_naked_pocs.get("all_naked_pocs", []),
                },
            },
            "multi_desk_initial_balance": {
                "asia_open_ib": asia_ib,
                "frankfurt_open_ib": frankfurt_ib,
                "london_open_ib": london_ib,
                "us_cash_open_ib": us_rth_ib,
                "weekly_initial_balance_monday": {
                    "ib_day": "Monday",
                    "ib_high": weekly_days_profile.get("Monday", {}).get("high", "FORMING"),
                    "ib_low": weekly_days_profile.get("Monday", {}).get("low", "FORMING"),
                    "status": weekly_days_profile.get("Monday", {}).get("status", "FORMING"),
                },
                "monthly_initial_balance_week1": {
                    "ib_period": "Week_1_Q1",
                    "ib_poc": monthly_quarter_blocks.get("WEEK_1_Q1", {}).get(
                        "composite_poc", "AWAITING"
                    ),
                    "status": monthly_quarter_blocks.get("WEEK_1_Q1", {}).get("status", "ACTIVE"),
                },
            },
            "multi_horizon_open_types": {
                "us_cash_open_type": open_type_info["open_type"],
                "us_cash_conviction": open_type_info["conviction"],
                "london_open_type": (
                    classify_open_type(
                        [
                            b
                            for b in curr_bars
                            if datetime.fromisoformat(b[0]).astimezone(UTC).time() >= time(7, 0)
                        ],
                        pdh,
                        pdl,
                        va_profile["vah"],
                        va_profile["val"],
                        atr_proxy,
                    )["open_type"]
                    if len(
                        [
                            b
                            for b in curr_bars
                            if datetime.fromisoformat(b[0]).astimezone(UTC).time() >= time(7, 0)
                        ]
                    )
                    >= 3
                    else "AWAITING_SESSION"
                ),
                "weekly_open_type": (
                    "OPEN_OUTSIDE_WEEKLY_VALUE"
                    if last_price > (weekly_va.get("vah") or last_price)
                    or last_price < (weekly_va.get("val") or last_price)
                    else "OPEN_INSIDE_WEEKLY_VALUE"
                ),
            },
            "multi_horizon_cva_map": {
                "low_horizon_cvas": {"overnight_cva": overnight_cva},
                "mid_horizon_cvas": {
                    "midweek_72h_composite": midweek_72h_composite,
                    "dynamic_n_day_cva": dynamic_cva,
                },
                "high_horizon_cvas": {
                    "ipda_multi_day_cvas": ipda_ranges,
                    "monthly_quarter_blocks": monthly_quarter_blocks,
                    "prior_months_cva": prior_months_amt,
                    "prior_yearly_quarters_cva": prior_yearly_quarters_amt,
                },
            },
            "overnight_cva": overnight_cva,
            "multi_horizon_time_acceptance": {
                "prior_day_value_acceptance": time_acc["status"],
                "london_desk_value_acceptance": (
                    evaluate_time_acceptance(
                        [b[4] for b in curr_bars[-12:]],
                        london_prof["vah"],
                        london_prof["val"],
                    )["status"]
                    if london_prof and london_prof.get("vah") and london_prof.get("val")
                    else "AWAITING_BARS"
                ),
                "midweek_72h_value_acceptance": (
                    evaluate_time_acceptance(
                        [b[4] for b in curr_bars[-12:]],
                        midweek_72h_composite["composite_vah"],
                        midweek_72h_composite["composite_val"],
                    )["status"]
                    if midweek_72h_composite
                    and isinstance(midweek_72h_composite.get("composite_vah"), (int, float))
                    else "AWAITING_BARS"
                ),
                "weekly_value_acceptance": (
                    evaluate_time_acceptance(
                        [b[4] for b in curr_bars[-12:]],
                        weekly_va["vah"],
                        weekly_va["val"],
                    )["status"]
                    if weekly_va and weekly_va.get("vah") and weekly_va.get("val")
                    else "AWAITING_BARS"
                ),
            },
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
                "frankfurt": frankfurt_prof if frankfurt_prof else "N/A (Awaiting Session Bars)",
                "singapore": singapore_prof if singapore_prof else "N/A (Awaiting Session Bars)",
                "pre_london": pre_london_prof if pre_london_prof else "N/A (Awaiting Session Bars)",
                "ny_regular": ny_regular_prof if ny_regular_prof else "N/A (Awaiting Session Bars)",
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
                "all_daily_quarters": all_daily_quarters,
                "active_quarter_all_sub_quarters_90m": active_quarter_sub_quarters,
                "active_sub_quarter_all_micros_22m": active_sub_quarter_micros,
                "weekly_days_profile": weekly_days_profile,
                "midweek_72h_composite": midweek_72h_composite,
                "monthly_quarter_blocks": monthly_quarter_blocks,
                "prior_months_amt": prior_months_amt,
                "prior_yearly_quarters_amt": prior_yearly_quarters_amt,
                "yearly_cycle": yearly_cycle,
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
