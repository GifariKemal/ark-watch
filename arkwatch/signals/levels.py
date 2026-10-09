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
from bisect import bisect_right
from calendar import monthrange
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .amt import (
    ASSET_TICK_SIZES,
    CRYPTO_SYMBOLS,
    analyze_initial_balance,
    classify_open_type,
    classify_participant_activity,
    classify_profile_shape,
    classify_value_migration,
    compute_dynamic_cva,
    compute_tpo_profile,
    compute_value_area,
    detect_market_structure_pivots,
    evaluate_auction_extremes,
    evaluate_time_acceptance,
    evaluate_vpoc_tpoc_relationship,
    find_naked_pocs,
    get_asset_ib_timing,
    split_rth,
)
from .amt_horizons import compute_horizon_amt
from .asof import parse_as_of
from .horizons import (
    FRANKFURT_TZ,
    LONDON_TZ,
    cme_session_date,
    get_active_quarterly_cycles,
    get_month_week_anchor_ny,
    get_monthly_quarter,
    get_quarterly_session_bounds,
    get_session_window,
    get_weekly_quarter,
    get_yearly_cycle,
    local_open_utc,
    subdivide_micro_22m,
    subdivide_quarter_90m,
    to_ny_time,
)

BAR_INTERVAL = "5m"
BAR_SPAN = timedelta(minutes=5)
# Bounded history: 13 weeks covers the 12-week virgin-POC tier (the oldest, partial week
# falls outside it) and the 20-session naked-POC lookback
HISTORY_WINDOW = timedelta(weeks=13)


def _get_cme_session_id(dt: datetime | str) -> str:
    """Map a timestamp to its CME Trading Session Date (18:00 ET yesterday to 17:00 ET today)."""
    return cme_session_date(dt).isoformat()


def _get_cme_week_id(dt: datetime | str) -> str:
    """Map a timestamp to its CME Trading Week (Sunday 18:00 ET to Friday 17:00 ET)."""
    y, w, _ = cme_session_date(dt).isocalendar()
    return f"{y}-W{w:02d}"


def compute_session_reference_levels(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Compute Prior Session (T-1) Levels, Overnight Range, Developing Weekly Multi-Anchor, and Confluence."""
    sym = symbol.strip().upper()
    target_dt = parse_as_of(as_of)

    # 1. Load closed bars (bar open + 5m <= as_of) over a bounded window ending at the latest bar
    upper = (target_dt - BAR_SPAN).isoformat(timespec="seconds")
    latest = conn.execute(
        "SELECT MAX(bar_ts_utc) FROM intraday_bars WHERE symbol = ? AND interval = ?"
        " AND bar_ts_utc <= ?",
        (sym, BAR_INTERVAL, upper),
    ).fetchone()[0]
    if latest is None:
        return None
    lower = (parse_as_of(latest) - HISTORY_WINDOW).isoformat(timespec="seconds")
    raw = conn.execute(
        """
        SELECT bar_ts_utc, open, high, low, close, COALESCE(volume, 0.0), source
        FROM intraday_bars
        WHERE symbol = ? AND interval = ?
          AND bar_ts_utc >= ? AND bar_ts_utc <= ?
        ORDER BY bar_ts_utc ASC, source ASC
        """,
        (sym, BAR_INTERVAL, lower, upper),
    ).fetchall()
    # Several providers can store the same bar: keep one row per timestamp
    rows: dict[str, tuple] = {}
    for r in raw:
        rows.setdefault(r[0], r)
    all_bars = [
        (ts, float(o), float(h), float(lo), float(c), float(v))
        for ts, o, h, lo, c, v, _ in rows.values()
    ]

    # 2. Segment bars by CME Trading Session ID and Trading Week ID
    session_bars: dict[str, list[tuple[str, float, float, float, float, float]]] = defaultdict(list)
    week_bars: dict[str, list[tuple[str, float, float, float, float, float]]] = defaultdict(list)

    for b in all_bars:
        session_bars[_get_cme_session_id(b[0])].append(b)
        week_bars[_get_cme_week_id(b[0])].append(b)

    sorted_sessions = sorted(session_bars.keys())
    session_date = cme_session_date(target_dt)
    curr_session_id = session_date.isoformat()
    curr_week_id = _get_cme_week_id(target_dt)

    # Prior completed session. No bars yet in the current session (weekend, holiday, pre-open
    # gap): the latest session with bars is the prior one and the current session is empty.
    if curr_session_id in session_bars:
        prior_idx = sorted_sessions.index(curr_session_id) - 1
        if prior_idx < 0:
            return None  # no completed prior session: there are no T-1 reference levels
        curr_bars = session_bars[curr_session_id]
    else:
        prior_idx = len(sorted_sessions) - 1
        curr_bars = []
    prior_session_id = sorted_sessions[prior_idx]
    prior_bars = session_bars[prior_session_id]

    # 3. Prior Session (T-1) Reference Levels
    pdh = max(b[2] for b in prior_bars)
    pdl = min(b[3] for b in prior_bars)
    pdc = prior_bars[-1][4]

    # Prior Session Value Area
    va_profile = compute_value_area(prior_bars, tick_size=ASSET_TICK_SIZES.get(sym))

    # 4. Overnight Session (Asia + London: 18:00 ET to 09:30 ET)
    ib_open_time, ib_timing_label = get_asset_ib_timing(sym, session_date)
    overnight_bars, rth_bars = split_rth(curr_bars, ib_open_time)

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
    latest_bar = (curr_bars or prior_bars)[-1]
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
    prior_prior_bars = session_bars[sorted_sessions[prior_idx - 1]] if prior_idx >= 1 else None
    prior_prior_va = compute_value_area(prior_prior_bars) if prior_prior_bars else None
    value_migration = classify_value_migration(
        va_profile["vah"],
        va_profile["val"],
        va_profile["poc"],
        prior_prior_va["vah"] if prior_prior_va else None,
        prior_prior_va["val"] if prior_prior_va else None,
        prior_prior_va["poc"] if prior_prior_va else None,
    )
    # Quarterly Theory trading day: Q1 Asia opens 17:00 ET on the prior calendar day
    qt_day = (to_ny_time(target_dt) + timedelta(hours=7)).date()
    active_qt = get_active_quarterly_cycles(target_dt)
    q_bounds = get_quarterly_session_bounds(qt_day)
    tick = ASSET_TICK_SIZES.get(sym)
    # 5m bars sit on the 5m grid, so "closed by as_of" == "opened before as_of floored to 5m"
    close_cutoff = target_dt.replace(
        minute=target_dt.minute - target_dt.minute % 5, second=0, microsecond=0
    )

    def _hz(start: datetime, end: datetime) -> dict[str, Any] | None:
        """Horizon profile over [start, end) from bars closed by as_of (no look-ahead)."""
        end = min(end, close_cutoff)
        return compute_horizon_amt(conn, sym, start, end) if start < end else None

    bar_dts = [parse_as_of(b[0]) for b in all_bars]

    def _between(start: datetime, end: datetime) -> list[tuple]:
        return [b for t, b in zip(bar_dts, all_bars, strict=True) if start <= t < end]

    # Pilar 4: Dynamic N-Day CVA (Contiguous Balance Expansion & 100% Measured Move)
    dynamic_cva = compute_dynamic_cva(session_bars, min_sessions=2, max_sessions=8)

    # Completed daily bars only (no look-ahead), one per date with YAHOO preferred. A futures
    # daily bar is dated by its CME session; a crypto daily bar is a whole UTC day.
    day_cutoff = target_dt.date().isoformat() if sym in CRYPTO_SYMBOLS else curr_session_id
    daily: dict[str, tuple] = {}
    for r in conn.execute(
        "SELECT ts, open, high, low, close, volume FROM instrument_prices WHERE symbol = ?"
        " AND source IN ('YAHOO', 'EODHD') AND ts < ? ORDER BY ts DESC, source = 'YAHOO' DESC",
        (sym, day_cutoff),
    ):
        daily.setdefault(r[0][:10], r)
    daily_raw = list(daily.values())  # newest first, OHLC may be NULL
    daily_bars = [
        (ts[:10], float(o if o is not None else c), float(h), float(lo), float(c), float(v or 0.0))
        for ts, o, h, lo, c, v in reversed(daily_raw)
        if h is not None and lo is not None and c is not None
    ]  # oldest first
    by_date = {b[0]: b for b in daily_bars}

    def _days(after: date, upto: date) -> list[tuple]:
        """Completed daily bars dated in (after, upto]."""
        a, u = after.isoformat(), upto.isoformat()
        return [b for b in daily_bars if a < b[0] <= u]

    # Pilar 5: Fractal Hierarchical Naked POCs (Micro to Yearly)
    # Tier 1: QT 90m sub-quarter naked POCs over the last 3 days, keyed by sub-quarter start
    sq_starts = sorted(
        s
        for i in range(5)
        for qs_i, qe_i in get_quarterly_session_bounds(qt_day - timedelta(days=i)).values()
        for s, _ in subdivide_quarter_90m(qs_i, qe_i)
    )
    sq_bars: dict[str, list] = defaultdict(list)
    cutoff_3d = target_dt - timedelta(days=3)
    for t, b in zip(bar_dts, all_bars, strict=True):
        if t >= cutoff_3d:
            sq_bars[sq_starts[bisect_right(sq_starts, t) - 1].isoformat()].append(b)
    intraday_90m_npocs = find_naked_pocs(sq_bars, last_price, lookback_sessions=48)

    # Tier 2: Session Naked POCs (anchored to IPDA 20D lookback)
    session_naked_pocs = find_naked_pocs(session_bars, last_price, lookback_sessions=20)
    # Tier 3: Weekly Virgin POCs (anchored to IPDA 60D = ~12 weeks lookback)
    weekly_naked_pocs = find_naked_pocs(week_bars, last_price, lookback_sessions=12)
    # Tier 4 & 5: Monthly and Yearly Virgin POCs (completed daily bars)
    month_bars_dict: dict[str, list] = defaultdict(list)
    year_bars_dict: dict[str, list] = defaultdict(list)
    for b in daily_bars:
        month_bars_dict[b[0][:7]].append(b)
        year_bars_dict[b[0][:4]].append(b)
    monthly_naked_pocs = find_naked_pocs(month_bars_dict, last_price, lookback_sessions=12)
    yearly_naked_pocs = find_naked_pocs(year_bars_dict, last_price, lookback_sessions=10)
    naked_pocs = session_naked_pocs

    # Multi-Horizon Session Profiles (CME session date windows, capped at as_of)
    asia_prof = _hz(*get_session_window("ASIA", session_date))
    london_prof = _hz(*get_session_window("LONDON", session_date))
    overlap_prof = _hz(*get_session_window("NY_LONDON_OVERLAP", session_date))
    frankfurt_prof = _hz(*get_session_window("FRANKFURT", session_date))
    singapore_prof = _hz(*get_session_window("SINGAPORE", session_date))
    pre_london_prof = _hz(*get_session_window("PRE_LONDON", session_date))
    ny_regular_prof = _hz(*get_session_window("NY_REGULAR", session_date))
    q2_london_prof = _hz(*q_bounds["Q2_LONDON"])

    # Multi-Desk Initial Balance: each desk's local open under its own DST
    london_open = local_open_utc(session_date, 8, 0, LONDON_TZ)
    asia_ib = analyze_initial_balance(curr_bars, time(0, 0))  # Tokyo 09:00 JST, no DST
    frankfurt_ib = analyze_initial_balance(
        curr_bars, local_open_utc(session_date, 8, 0, FRANKFURT_TZ)
    )
    london_ib = analyze_initial_balance(curr_bars, london_open)

    # 90m Sub-Quarter Micro-IB (Micro-1: First 22.5m)
    sub_s = parse_as_of(active_qt["sub_quarter_start_utc"])
    sub_e = parse_as_of(active_qt["sub_quarter_end_utc"])
    active_sub_bars = _between(sub_s, sub_e)
    if active_sub_bars:
        m1_bars = active_sub_bars[:5]
        m1_h = max(b[2] for b in m1_bars)
        m1_l = min(b[3] for b in m1_bars)
        sub_90m_ib = {
            "ib_window": "Micro-1 (First 22.5m of Sub-Quarter)",
            "ib_high": round(m1_h, 4),
            "ib_low": round(m1_l, 4),
            "ib_range": round(m1_h - m1_l, 4),
            "status": "COMPLETED" if len(active_sub_bars) >= 5 else "FORMING",
        }
    else:
        sub_90m_ib = {
            "ib_window": "Micro-1",
            "ib_high": "AWAITING_BARS",
            "ib_low": "AWAITING_BARS",
            "ib_range": 0.0,
            "status": "AWAITING_BARS",
        }

    def _cva_block(bars: list[tuple], status: str, up: str, down: str) -> dict[str, Any]:
        """Composite VA with Dalton 100% measured moves; AWAITING_BARS when there are no bars."""
        if not bars:
            return {
                "status": "AWAITING_BARS",
                "c_poc": "AWAITING_BARS",
                "c_vah": "AWAITING_BARS",
                "c_val": "AWAITING_BARS",
                "c_range": 0.0,
                "dalton_measured_move": {up: "AWAITING_BARS", down: "AWAITING_BARS"},
                "total_volume": 0.0,
            }
        va = compute_value_area(bars, tick_size=tick)
        rng = va["vah"] - va["val"] if va["vah"] is not None and va["val"] is not None else None
        return {
            "status": status,
            "c_poc": va["poc"],
            "c_vah": va["vah"],
            "c_val": va["val"],
            "c_range": round(rng, 4) if rng is not None else None,
            "dalton_measured_move": {
                up: round(va["vah"] + rng, 4) if rng is not None else None,
                down: round(va["val"] - rng, 4) if rng is not None else None,
            },
            "total_volume": va["total_volume"],
        }

    # Overnight CVA (Low-Horizon CVA: the session's pre-open bars, Asia + London merged)
    overnight_cva = _cva_block(
        overnight_bars,
        "COMPLETED" if rth_bars else "FORMING",
        "upside_breakout_target",
        "downside_breakout_target",
    )
    # Micro-CVA 45m (Micro-1 + Micro-2 Merged)
    m45_end = sub_s + timedelta(minutes=45)
    micro_45m_cva = _cva_block(
        _between(sub_s, m45_end),
        "COMPLETED" if close_cutoff >= m45_end else "FORMING",
        "upside_target",
        "downside_target",
    )

    # Multi-Timeframe Market Structure Detector (M15, H1, H4 on closed bars; Daily)
    # ponytail: buckets are UTC-epoch aligned (H4 = 00/04/08 UTC), not session aligned
    def _resample(sec: int) -> list[tuple]:
        b_dict: dict[int, list] = defaultdict(list)
        for t, b in zip(bar_dts, all_bars, strict=True):
            b_dict[(int(t.timestamp()) // sec) * sec].append(b)
        return [
            (
                datetime.fromtimestamp(ep, tz=UTC).isoformat(),
                bl[0][1],
                max(x[2] for x in bl),
                min(x[3] for x in bl),
                bl[-1][4],
                sum(x[5] for x in bl),
            )
            for ep, bl in sorted(b_dict.items())
        ]

    daily_b = daily_bars[-65:]
    multi_tf_market_structure = {
        "m15_structure": detect_market_structure_pivots(_resample(15 * 60), lb=4, rb=4),
        "h1_structure": detect_market_structure_pivots(_resample(60 * 60), lb=4, rb=4),
        "h4_structure": detect_market_structure_pivots(_resample(4 * 60 * 60), lb=3, rb=3),
        "daily_structure": (
            detect_market_structure_pivots(daily_b, lb=2, rb=2)
            if len(daily_b) >= 6
            else {"trend": "CONSOLIDATION", "latest_point": "NONE", "recent_points": []}
        ),
    }

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

    # IPDA Data Ranges + Composite Value Area on the last N completed days
    empty_range = dict.fromkeys(
        (
            "high",
            "low",
            "range",
            "midpoint",
            "composite_poc",
            "composite_vah",
            "composite_val",
            "total_volume",
        )
    )
    ipda_ranges: dict[str, dict[str, Any]] = {}
    for days in [1, 2, 3, 5, 10, 15, 20, 40, 60]:
        window = daily_raw[:days]
        highs = [float(r[2]) for r in window if r[2] is not None]
        lows = [float(r[3]) for r in window if r[3] is not None]
        bars = [by_date[r[0][:10]] for r in reversed(window) if r[0][:10] in by_date]
        if highs and lows:
            h, l_val = max(highs), min(lows)
            cva = compute_value_area(bars, tick_size=tick) if bars else {}
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
            ipda_ranges[f"{days}D"] = dict(empty_range)

    # IPDA Intraday Lookbacks (4H, 8H, 12H)
    for h_str, hrs in [("4H", 4), ("8H", 8), ("12H", 12)]:
        h_amt = _hz(target_dt - timedelta(hours=hrs), target_dt)
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
            ipda_ranges[h_str] = dict(empty_range)

    # Quarterly Theory Context with Real AMT Volume Profiles
    w_quarter = get_weekly_quarter(session_date)
    m_quarter = get_monthly_quarter(target_dt)
    qs = parse_as_of(active_qt["quarter_start_utc"])
    qe = parse_as_of(active_qt["quarter_end_utc"])
    active_q_amt = _hz(qs, qe)
    active_sub_amt = _hz(sub_s, sub_e)
    prior_sub_amt = None
    if active_qt.get("prior_sub_quarter_start_utc"):
        prior_sub_amt = _hz(
            parse_as_of(active_qt["prior_sub_quarter_start_utc"]),
            parse_as_of(active_qt["prior_sub_quarter_end_utc"]),
        )
    active_micro_amt = _hz(
        parse_as_of(active_qt["micro_cycle_start_utc"]),
        parse_as_of(active_qt["micro_cycle_end_utc"]),
    )

    # All 4 Daily Quarters of the QT trading day
    all_daily_quarters: dict[str, Any] = {}
    for q_name, (qs_b, qe_b) in q_bounds.items():
        if qs_b >= target_dt:
            all_daily_quarters[q_name] = "Upcoming"
            continue
        q_p = _hz(qs_b, qe_b)
        all_daily_quarters[q_name] = (
            {k: q_p.get(k) for k in ("vah", "val", "poc", "total_volume", "vwap")}
            if q_p
            else "Awaiting"
        )

    def _slices(windows: list[tuple[datetime, datetime]], label: str) -> list[dict[str, Any]]:
        """AMT value area of each fractal slice: COMPLETED / ACTIVE / UPCOMING / AWAITING_BARS."""
        out = []
        for idx, (ss, se) in enumerate(windows):
            amt_s = _hz(ss, se) if ss < target_dt else None
            if amt_s:
                out.append(
                    {
                        label: f"{label.split('_')[0].title()}-{idx + 1}",
                        "status": "COMPLETED" if target_dt >= se else "ACTIVE",
                        "vah": amt_s["vah"],
                        "val": amt_s["val"],
                        "poc": amt_s["poc"],
                        "total_volume": amt_s["total_volume"],
                    }
                )
            else:
                lbl = "UPCOMING" if ss >= target_dt else "AWAITING_BARS"
                out.append(
                    {
                        label: f"{label.split('_')[0].title()}-{idx + 1}",
                        "status": lbl,
                        "vah": lbl,
                        "val": lbl,
                        "poc": lbl,
                        "total_volume": 0.0,
                    }
                )
        return out

    active_quarter_sub_quarters = _slices(subdivide_quarter_90m(qs, qe), "sub_quarter")
    active_sub_quarter_micros = _slices(subdivide_micro_22m(sub_s, sub_e), "micro_cycle")

    # Weekly days (Mon-Fri of the session's week) + Midweek 72H Composite (Mon-Wed)
    monday = session_date - timedelta(days=session_date.weekday())
    weekly_days_profile: dict[str, dict[str, Any]] = {}
    bars_72h = []
    for d_offset, day_name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")):
        curr_day = monday + timedelta(days=d_offset)
        day_str = curr_day.isoformat()
        if curr_day < session_date and day_str in by_date:
            b = by_date[day_str]
            if d_offset <= 2:
                bars_72h.append(b)
            weekly_days_profile[day_name] = {
                "date": day_str,
                "status": "COMPLETED",
                "high": b[2],
                "low": b[3],
                "close": b[4],
                "volume": b[5],
            }
        elif curr_day == session_date:
            weekly_days_profile[day_name] = {
                "date": day_str,
                "status": "ACTIVE_TODAY",
                "high": max((b[2] for b in curr_bars), default=None),
                "low": min((b[3] for b in curr_bars), default=None),
                "close": last_price,
                "volume": sum(b[5] for b in curr_bars),
            }
        else:
            lbl = "AWAITING_DATA" if curr_day < session_date else "UPCOMING"
            weekly_days_profile[day_name] = {
                "date": day_str,
                "status": lbl,
                "high": lbl,
                "low": lbl,
                "close": lbl,
                "volume": 0.0,
            }

    if bars_72h:
        cva_72h = compute_value_area(bars_72h, tick_size=tick)
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

    def _composite(bars: list[tuple], **extra: Any) -> dict[str, Any]:
        va = compute_value_area(bars, tick_size=tick)
        return {
            **extra,
            "high": max(b[2] for b in bars),
            "low": min(b[3] for b in bars),
            "composite_poc": va.get("poc"),
            "composite_vah": va.get("vah"),
            "composite_val": va.get("val"),
            "total_volume": va.get("total_volume"),
        }

    # 1. Yearly Cycle & Prior (completed) Yearly Quarters of the session's year
    yearly_cycle = get_yearly_cycle(session_date)
    y_curr = session_date.year
    curr_q_num = (session_date.month - 1) // 3 + 1
    prior_yearly_quarters_amt = {}
    for q_idx, q_lbl in enumerate(("Q1", "Q2", "Q3", "Q4")[: curr_q_num - 1]):
        q_start = date(y_curr, 3 * q_idx + 1, 1)
        q_end = date(y_curr, 3 * q_idx + 3, monthrange(y_curr, 3 * q_idx + 3)[1])
        b_yq = _days(q_start - timedelta(days=1), q_end)
        if b_yq:
            prior_yearly_quarters_amt[f"{y_curr}_{q_lbl}"] = _composite(
                b_yq, period=f"{q_start} -> {q_end}"
            )

    # 2. Prior QT Months (M-1, M-2) before the month that owns as_of (PineScript anchors)
    def _prev_month(y: int, m: int) -> tuple[int, int]:
        return (y, m - 1) if m > 1 else (y - 1, 12)

    o_y, o_m = map(int, m_quarter["owning_year_month"].split("-"))
    m1_y, m1_m = _prev_month(o_y, o_m)
    m2_y, m2_m = _prev_month(m1_y, m1_m)
    curr_anc = get_month_week_anchor_ny(o_y, o_m)
    m1_anc = get_month_week_anchor_ny(m1_y, m1_m)
    m2_anc = get_month_week_anchor_ny(m2_y, m2_m)
    prior_months_amt = {}
    for m_lbl, (anc_s, anc_e) in [
        (f"M-1_{m1_y}_{m1_m:02d}", (m1_anc, curr_anc)),
        (f"M-2_{m2_y}_{m2_m:02d}", (m2_anc, m1_anc)),
    ]:
        b_pm = _days(anc_s.date(), anc_e.date())
        if b_pm:
            prior_months_amt[m_lbl] = _composite(
                b_pm,
                anchor_start=anc_s.strftime("%Y-%m-%d %H:%M ET"),
                anchor_end=anc_e.strftime("%Y-%m-%d %H:%M ET"),
            )

    # 3. Monthly Weeks Schedule Composite Blocks (PineScript Weeks 1-4 / Joker)
    monthly_quarter_blocks = {}
    for w_info in m_quarter.get("weeks_schedule", []):
        b_name = f"WEEK_{w_info['week_index']}_{w_info['quarter']}"
        b_start, b_end = w_info["start_et"][:10], w_info["end_et"][:10]
        b_bars = _days(date.fromisoformat(b_start), date.fromisoformat(b_end))
        if b_bars:
            va_mb = compute_value_area(b_bars, tick_size=tick)
            monthly_quarter_blocks[b_name] = {
                "start_date": b_start,
                "end_date": b_end,
                "status": w_info["status"],
                "composite_poc": va_mb.get("poc"),
                "composite_vah": va_mb.get("vah"),
                "composite_val": va_mb.get("val"),
                "total_volume": va_mb.get("total_volume"),
            }
        else:
            lbl = "UPCOMING" if w_info["status"] == "UPCOMING" else "AWAITING_BARS"
            monthly_quarter_blocks[b_name] = {
                "start_date": b_start,
                "end_date": b_end,
                "status": lbl,
                "composite_poc": lbl,
                "composite_vah": lbl,
                "composite_val": lbl,
                "total_volume": 0.0,
            }

    london_rth = split_rth(curr_bars, london_open)[1]
    m1_vah = prior_months_amt.get(f"M-1_{m1_y}_{m1_m:02d}", {}).get("composite_vah")
    p90_vah = prior_sub_amt["vah"] if prior_sub_amt else None
    p90_val = prior_sub_amt["val"] if prior_sub_amt else None
    recent_closes = [b[4] for b in curr_bars[-12:]]

    def _accept(vah: Any, val: Any) -> str:
        if isinstance(vah, int | float) and isinstance(val, int | float):
            return evaluate_time_acceptance(recent_closes, vah, val)["status"]
        return "AWAITING_BARS"

    def _npoc_tier(npocs: dict[str, Any]) -> dict[str, Any]:
        return {
            "total_naked_pocs": npocs.get("total_naked_pocs", 0),
            "nearest_naked_poc_above": npocs.get("nearest_naked_poc_above")
            or "NONE_IN_LOOKBACK (All-Time High / Blue Sky)",
            "nearest_naked_poc_below": npocs.get("nearest_naked_poc_below")
            or "NONE_IN_LOOKBACK (All-Time Low)",
            "all_naked_pocs": npocs.get("all_naked_pocs", []),
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
                "intraday_90m_naked_pocs": _npoc_tier(intraday_90m_npocs),
                "session_naked_pocs": _npoc_tier(session_naked_pocs),
                "weekly_virgin_pocs": _npoc_tier(weekly_naked_pocs),
                "monthly_virgin_pocs": _npoc_tier(monthly_naked_pocs),
                "yearly_virgin_pocs": _npoc_tier(yearly_naked_pocs),
            },
            "multi_desk_initial_balance": {
                "asia_open_ib": asia_ib,
                "frankfurt_open_ib": frankfurt_ib,
                "london_open_ib": london_ib,
                "us_cash_open_ib": ib_data,
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
                "yearly_initial_balance_q1": {
                    "ib_period": "Yearly_Q1",
                    "ib_poc": prior_yearly_quarters_amt.get(f"{y_curr}_Q1", {}).get(
                        "composite_poc", "FORMING"
                    ),
                    "status": "COMPLETED" if curr_q_num > 1 else "ACTIVE",
                },
                "sub_quarter_90m_micro_ib": sub_90m_ib,
            },
            "multi_horizon_open_types": {
                "sub_quarter_90m_open_type": (
                    "OPEN_ABOVE_PRIOR_90M_VAH"
                    if p90_vah is not None and last_price > p90_vah
                    else (
                        "OPEN_BELOW_PRIOR_90M_VAL"
                        if p90_val is not None and last_price < p90_val
                        else "OPEN_IN_PRIOR_90M_VALUE"
                    )
                ),
                "daily_globex_open_type": (
                    "OPEN_OUTSIDE_PRIOR_DAY_RANGE"
                    if last_price > pdh or last_price < pdl
                    else "OPEN_INSIDE_PRIOR_DAY_RANGE"
                ),
                "us_cash_open_type": open_type_info["open_type"],
                "us_cash_conviction": open_type_info["conviction"],
                "london_open_type": (
                    classify_open_type(
                        london_rth,
                        pdh,
                        pdl,
                        va_profile["vah"] or last_price,
                        va_profile["val"] or last_price,
                        atr_proxy,
                    )["open_type"]
                    if len(london_rth) >= 3
                    else "AWAITING_SESSION"
                ),
                "weekly_open_type": (
                    "OPEN_OUTSIDE_WEEKLY_VALUE"
                    if last_price > (weekly_va.get("vah") or last_price)
                    or last_price < (weekly_va.get("val") or last_price)
                    else "OPEN_INSIDE_WEEKLY_VALUE"
                ),
                "monthly_open_type": (
                    "OPEN_ABOVE_PRIOR_MONTH_VAH"
                    if m1_vah is not None and last_price > m1_vah
                    else "OPEN_IN_PRIOR_MONTH_VALUE"
                ),
            },
            "multi_timeframe_market_structure": multi_tf_market_structure,
            "multi_horizon_cva_map": {
                "low_horizon_cvas": {
                    "overnight_cva": overnight_cva,
                    "micro_45m_cva": micro_45m_cva,
                },
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
                "london_desk_value_acceptance": _accept(
                    london_prof and london_prof["vah"], london_prof and london_prof["val"]
                ),
                "midweek_72h_value_acceptance": _accept(
                    midweek_72h_composite["composite_vah"], midweek_72h_composite["composite_val"]
                ),
                "weekly_value_acceptance": _accept(weekly_va["vah"], weekly_va["val"]),
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
