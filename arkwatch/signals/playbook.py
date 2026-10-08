"""playbook.py — Actionable Trading Playbook & Scenario Outlook Engine.

Synthesizes Auction Market Theory reference levels, intraday price action (VWAP & ATR),
fast news catalyst stances, and macro regime score into actionable if-then trading
scenarios with mathematically grounded target profits and invalidation levels.

Grounds all baseline breakout and trap probabilities on empirical historical studies
(docs/analysis/) with exact sample counts (N >= 500) and confidence intervals.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from ..transforms import xccy
from . import cot_signals, etf_flows, fiscal, options, vixterm
from .crypto import liquidation_summary
from .dealers import dealers_snapshot
from .expectations import inflation_risk_premium
from .futures_flow import futures_flow_matrix
from .intraday import session_intraday_intelligence
from .levels import compute_session_reference_levels
from .news import news_velocity
from .pillars import compute_dollar_smile, compute_pillars, compute_quadrant, compute_regime_score
from .playbook_tracker import (
    evaluate_active_playbooks,
    get_playbook_performance_metrics,
    record_playbook_scenarios,
)
from .recession import recession_snapshot
from .sentiment import compute_asset_sentiment_radar, compute_intraday_catalyst_radar

OPTIONS_PRODUCT_MAP: dict[str, str] = {
    "NQ1": "NQ",
    "ES1": "ES",
    "YM1": "YM",
    "GC1": "OG",
    "SI1": "SO",
    "PL1": "PO",
    "HG1": "HXE",
    "CL1": "LO",
    "BTCUSD": "BTC",
}
COT_CONTRACT_MAP: dict[str, str] = {
    "NQ1": "209742",
    "ES1": "13874A",
    "YM1": "124603",
    "GC1": "088691",
    "SI1": "084691",
    "CL1": "067651",
    "BTCUSD": "133741",
    "EURUSD": "099741",
    "GBPUSD": "096742",
    "USDJPY": "097741",
}
FUTURES_PRODUCT_MAP: dict[str, str] = {
    "NQ1": "NQ",
    "ES1": "ES",
    "YM1": "YM",
    "GC1": "GC",
    "SI1": "SI",
    "HG1": "HG",
    "CL1": "CL",
    "BTCUSD": "BTC",
}

# Empirical historical parameters from docs/analysis/weekly-scenarios/daily-breakout-report.md
# and docs/analysis/weekly-context/sections/07-conditional-probability.md
EMPIRICAL_BREAKOUT_STATS: dict[str, dict[str, Any]] = {
    "NQ1": {
        "source_doc": "docs/analysis/weekly-scenarios/daily-breakout-report.md",
        "sample_weeks_high": 658,
        "sample_weeks_low": 521,
        "high_break_rate_pct": 79.85,
        "high_false_close_pct": 44.22,
        "high_false_close_ci95": (40.47, 48.04),
        "high_continuation_median_atr": 0.3151,
        "low_break_rate_pct": 63.23,
        "low_false_close_pct": 52.98,
        "low_false_close_ci95": (48.68, 57.22),
        "low_continuation_median_atr": 0.2125,
    },
    "ES1": {
        "source_doc": "docs/analysis/weekly-scenarios/daily-breakout-report.md",
        "sample_weeks_high": 1130,
        "sample_weeks_low": 990,
        "high_break_rate_pct": 77.61,
        "high_false_close_pct": 46.55,
        "high_false_close_ci95": (43.66, 49.46),
        "high_continuation_median_atr": 0.2774,
        "low_break_rate_pct": 67.99,
        "low_false_close_pct": 53.03,
        "low_false_close_ci95": (49.92, 56.12),
        "low_continuation_median_atr": 0.2246,
    },
    "YM1": {
        "source_doc": "docs/analysis/weekly-scenarios/daily-breakout-report.md",
        "sample_weeks_high": 691,
        "sample_weeks_low": 600,
        "high_break_rate_pct": 78.34,
        "high_false_close_pct": 49.78,
        "high_false_close_ci95": (46.07, 53.50),
        "high_continuation_median_atr": 0.2372,
        "low_break_rate_pct": 68.03,
        "low_false_close_pct": 49.33,
        "low_false_close_ci95": (45.35, 53.33),
        "low_continuation_median_atr": 0.2454,
    },
    "GC1": {
        "source_doc": "docs/analysis/weekly-scenarios/daily-breakout-report.md",
        "sample_weeks_high": 1708,
        "sample_weeks_low": 1621,
        "high_break_rate_pct": 70.90,
        "high_false_close_pct": 33.55,
        "high_false_close_ci95": (31.35, 35.82),
        "high_continuation_median_atr": 0.2690,
        "low_break_rate_pct": 67.29,
        "low_false_close_pct": 27.70,
        "low_false_close_ci95": (25.58, 29.93),
        "low_continuation_median_atr": 0.2173,
    },
    "BTCUSD": {
        "source_doc": "docs/analysis/weekly-scenarios/daily-breakout-report.md",
        "sample_weeks_high": 627,
        "sample_weeks_low": 545,
        "high_break_rate_pct": 74.38,
        "high_false_close_pct": 49.12,
        "high_false_close_ci95": (45.23, 53.03),
        "high_continuation_median_atr": 0.4220,
        "low_break_rate_pct": 64.65,
        "low_false_close_pct": 54.31,
        "low_false_close_ci95": (50.11, 58.45),
        "low_continuation_median_atr": 0.2366,
    },
    "EURUSD": {
        "source_doc": "docs/analysis/weekly-scenarios/daily-breakout-report.md",
        "sample_weeks_high": 893,
        "sample_weeks_low": 861,
        "high_break_rate_pct": 70.37,
        "high_false_close_pct": 43.67,
        "high_false_close_ci95": (40.45, 46.95),
        "high_continuation_median_atr": 0.2056,
        "low_break_rate_pct": 67.85,
        "low_false_close_pct": 44.72,
        "low_false_close_ci95": (41.43, 48.05),
        "low_continuation_median_atr": 0.2276,
    },
    "GBPUSD": {
        "source_doc": "docs/analysis/weekly-scenarios/daily-breakout-report.md",
        "sample_weeks_high": 925,
        "sample_weeks_low": 853,
        "high_break_rate_pct": 72.89,
        "high_false_close_pct": 54.49,
        "high_false_close_ci95": (51.27, 57.67),
        "high_continuation_median_atr": 0.2152,
        "low_break_rate_pct": 67.22,
        "low_false_close_pct": 56.74,
        "low_false_close_ci95": (53.39, 60.03),
        "low_continuation_median_atr": 0.2284,
    },
}


def generate_trading_playbook(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    as_of: datetime | str | None = None,
    cfd_basis_offset: float = 0.0,
) -> dict[str, Any] | None:
    """Generate an actionable probabilistic trading playbook with target profits and invalidation levels."""
    sym = symbol.strip().upper()

    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of).astimezone(UTC)
    else:
        target_dt = as_of.astimezone(UTC)
    # 1. Fetch Session Reference Levels (Auction Market Theory)
    ref = compute_session_reference_levels(conn, sym, as_of=as_of)
    if not ref:
        return None

    levels = ref["levels"]
    last_price = ref["last_price"]
    pdh = levels["PDH"]
    pdl = levels["PDL"]
    pdc = levels.get("PDC", last_price)
    vah = levels["VAH"]
    val = levels["VAL"]
    poc = levels["POC"]
    cva_measured_long = levels.get("CVA_MEASURED_MOVE_LONG")
    cva_measured_short = levels.get("CVA_MEASURED_MOVE_SHORT")
    cva_name = levels.get("DYNAMIC_CVA_NAME")
    naked_poc_above = levels.get("NAKED_POC_ABOVE")
    naked_poc_below = levels.get("NAKED_POC_BELOW")
    single_prints = levels.get("TPO_SINGLE_PRINTS", [])
    tpo_poc = levels.get("TPO_POC")
    tpo_vah = levels.get("TPO_VAH")
    tpo_val = levels.get("TPO_VAL")

    ctx = ref["auction_context"]
    open_type = ctx.get("open_type", "OPEN_IN_VALUE")
    open_conviction = ctx.get("open_conviction", "MODERATE_CONVICTION")
    participant_activity = ctx.get("participant_activity", "ROTATIONAL_AUCTION")
    value_migration = ctx.get("value_migration", "INSIDE_VALUE")
    vpoc_tpoc_align = ctx.get("vpoc_tpoc_alignment", {})
    qt_ctx = ctx.get("quarterly_theory", {})
    active_q = qt_ctx.get("active_quarter", "Q3_NY_AM")
    active_sub = qt_ctx.get("active_90m_sub_quarter", "Sub-3")
    active_micro = qt_ctx.get("active_22m_micro_cycle", "Micro-3")
    sub_role = qt_ctx.get("sub_quarter_role", "DISTRIBUTION_EXPANSION_DRIVE")
    micro_role = qt_ctx.get("micro_cycle_role", "MICRO_DIRECTIONAL_RUN")
    w_quarter = qt_ctx.get("weekly_quarter", {})
    m_quarter = qt_ctx.get("monthly_quarter", {})
    # 2. Fetch Intraday Price Action (VWAP and ATR)
    pa = session_intraday_intelligence(conn, sym, as_of=as_of)
    vwap = (pa.get("session_vwap") or pa.get("vwap")) if pa else None
    atr_14 = pa.get("atr_14") if pa else None
    if atr_14 is None or atr_14 <= 0.0:
        atr_14 = max(0.001, (pdh - pdl) * 0.5)

    volatility_ratio = pa.get("expansion_ratio") or pa.get("volatility_ratio", 1.0) if pa else 1.0
    vwap_state = pa.get("vwap_state", "NEUTRAL") if pa else "NEUTRAL"
    # 3. Fetch Fast Intraday Catalyst & Macro Swing Sentiment
    fast_cat = compute_intraday_catalyst_radar(conn, sym, window_hours=4, as_of=as_of)
    swing_sent = compute_asset_sentiment_radar(conn, sym, window_days=3, as_of=as_of)

    # 4. Fetch Domain 1 (Macro Engine Context)
    try:
        pillars = compute_pillars(conn)
        macro_regime_score = compute_regime_score(pillars)
        dalio_quadrant = compute_quadrant(pillars)
    except Exception:
        macro_regime_score = 0.0
        dalio_quadrant = "UNKNOWN"

    # Systemic Net Liquidity: SOMA Fed Balance Sheet (WALCL) - TGA (WTREGEN) - RRP (RRPONTSYD)
    walcl = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:WALCL' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    wtregen = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:WTREGEN' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    rrp = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:RRPONTSYD' ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    fed_bs_b = (float(walcl[0]) / 1000.0) if walcl else 7100.0
    tga_b = (float(wtregen[0]) / 1000.0) if wtregen else 750.0
    rrp_b = (float(rrp[0]) / 1000.0) if rrp else 300.0
    net_liq_b = round(fed_bs_b - tga_b - rrp_b, 2)
    friction_warnings = []
    tailwinds = []

    quadrant_alignment = "NEUTRAL"
    if "Disinflationary" in dalio_quadrant and sym in ("NQ1", "ES1"):
        quadrant_alignment = "BULLISH_EQUITIES_ALIGNED"
        tailwinds.append(
            "DALIO_REGIME: Disinflationary Growth is the optimal Goldilocks macro backdrop for tech multiples."
        )
    elif "Reflation" in dalio_quadrant and sym in ("CL1", "BZ1", "GC1"):
        quadrant_alignment = "BULLISH_COMMODITIES_ALIGNED"
        tailwinds.append("DALIO_REGIME: Reflation supports commodities and real assets.")
    elif "Stagflation" in dalio_quadrant and sym in ("NQ1", "ES1"):
        quadrant_alignment = "BEARISH_EQUITIES_ALIGNED"
        friction_warnings.append(
            "DALIO_REGIME_WARNING: Stagflation compresses equity valuation multiples."
        )
    real_yield_row = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:DFII10' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    real_yield_10y = float(real_yield_row[0]) if real_yield_row else None

    curve_row = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:T10Y2Y' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if curve_row and curve_row[0] is not None:
        yield_curve_spread = float(curve_row[0])
    else:
        dgs10_row = conn.execute(
            "SELECT value FROM raw_observations WHERE series_id='FRED:DGS10' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        dgs2_row = conn.execute(
            "SELECT value FROM raw_observations WHERE series_id='FRED:DGS2' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if dgs10_row and dgs2_row and dgs10_row[0] is not None and dgs2_row[0] is not None:
            yield_curve_spread = round(float(dgs10_row[0]) - float(dgs2_row[0]), 2)
        else:
            yield_curve_spread = None
    # Sahm Rule Recession Indicator
    sahm_row = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:SAHMREALTIME' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    sahm_val = float(sahm_row[0]) if sahm_row else None
    if sahm_val is not None and sahm_val >= 0.50:
        friction_warnings.append(
            f"SAHM_RULE_RECESSION_TRIGGER: Sahm Rule at {sahm_val} (>=0.50 triggers formal US recession indicator)."
        )

    # Treasury Auction Demand (10-Year Bid-to-Cover)
    try:
        auc_demand = fiscal.auction_demand(conn)
        auc_10y = auc_demand.get("terms", {}).get("10-Year", {})
        auc_pctl = auc_10y.get("percentile")
    except Exception:
        auc_pctl = None

    if auc_pctl is not None and auc_pctl <= 20.0 and sym in ("NQ1", "ES1"):
        friction_warnings.append(
            f"WEAK_TREASURY_AUCTION: 10Y Auction bid-to-cover at bottom {round(auc_pctl, 1)}% percentile, risks yield spikes."
        )
    # Fed Broad Trade-Weighted Dollar & Dollar Smile
    broad_d_row = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:DTWEXBGS' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    broad_dollar_val = float(broad_d_row[0]) if broad_d_row else None
    try:
        smile_regime = compute_dollar_smile(conn)
    except Exception:
        smile_regime = "UNKNOWN"

    if "STRONG" in smile_regime and sym in ("GC1", "SI1", "EURUSD", "GBPUSD"):
        friction_warnings.append(
            f"BROAD_DOLLAR_STRENGTH: Fed Broad Dollar (DTWEXBGS={broad_dollar_val}) at high with Dollar Smile strong."
        )
    elif "WEAK" in smile_regime and sym in ("GC1", "CL1"):
        tailwinds.append(
            f"GLOBAL_CYCLICAL_DOLLAR_WEAKNESS: Fed Broad Dollar (DTWEXBGS={broad_dollar_val}) weakening provides commodity tailwind."
        )

    # VIX Term Structure (Contango vs Backwardation)
    vix_res = vixterm.vix9d_ratio(conn)
    vix_state = vix_res.get("state", "NORMAL") if vix_res else "NORMAL"
    vix_ratio = vix_res.get("ratio") if vix_res else None
    # FedWatch Rate Expectations Outlook
    fw_row = conn.execute(
        """
        SELECT meeting_date, prob_ease, prob_hold, prob_hike, implied_rate
        FROM fedwatch_snapshots
        WHERE date = (SELECT MAX(date) FROM fedwatch_snapshots)
        ORDER BY meeting_date ASC LIMIT 1
        """
    ).fetchone()
    if fw_row:
        p_cut = round(fw_row[1] * 100, 1)
        p_hold = round(fw_row[2] * 100, 1)
        p_hike = round(fw_row[3] * 100, 1)
        fedwatch_fomc_outlook = {
            "meeting_date": fw_row[0],
            "prob_cut_pct": p_cut,
            "prob_hold_pct": p_hold,
            "prob_hike_pct": p_hike,
            "implied_rate_pct": round(fw_row[4], 2),
        }
        if p_cut >= 70.0 and sym in ("NQ1", "ES1", "YM1"):
            tailwinds.append(
                f"FEDWATCH_DOVISH: High market consensus ({p_cut}% probability) for rate cut at {fw_row[0]} FOMC meeting."
            )
        elif p_hike >= 20.0:
            friction_warnings.append(
                f"FEDWATCH_HAWKISH_PRICING: Non-zero hike probability ({p_hike}%) priced into {fw_row[0]} FOMC meeting."
            )
    else:
        fedwatch_fomc_outlook = "N/A (Awaiting FedWatch Snapshot Harvest)"

    # Recession Triangulation (Model, Survey, Labor)
    rec_snap = recession_snapshot(conn)
    if rec_snap:
        rec_sahm = rec_snap.get("sahm") if rec_snap.get("sahm") is not None else 0.0
        rec_model = round(rec_snap.get("model_pct") or 0.0, 1)
        rec_spf = round(rec_snap.get("anxious_pct") or 0.0, 1)
        elevated_count = (
            (1 if rec_sahm >= 0.50 else 0)
            + (1 if rec_model >= 30.0 else 0)
            + (1 if rec_spf >= 30.0 else 0)
        )
        recession_triangulation = {
            "state": (
                "ELEVATED_RECESSION_RISK"
                if elevated_count >= 2
                else ("WARNING" if elevated_count == 1 else "CALM")
            ),
            "elevated_gauges_count": elevated_count,
            "sahm_rule": rec_sahm,
            "cleve_yield_curve_model_pct": rec_model,
            "spf_survey_anxious_pct": rec_spf,
        }
        if elevated_count >= 2:
            friction_warnings.append(
                f"MACRO_RECESSION_TRIANGULATION_ALERT: {elevated_count}/3 independent recession gauges elevated."
            )
    else:
        recession_triangulation = "N/A (Recession Models Standby)"

    # Cleveland Fed Real Rate & Inflation Risk Premium (IRP)
    irp_snap = inflation_risk_premium(conn)
    if irp_snap:
        exp_inf_10y = round(irp_snap.get("expinf_10y") or 0.0, 2)
        exp_inf_1y = round(irp_snap.get("expinf_1y") or 0.0, 2)
        irp_bp = round(irp_snap.get("irp_10y_bp") or 0.0, 1)
        tips_liq_bp = round(irp_snap.get("tips_liq_10y_bp") or 0.0, 1)
        cleveland_fed_real_rate = {
            "expected_inflation_10y_pct": exp_inf_10y,
            "expected_inflation_1y_pct": exp_inf_1y,
            "inflation_risk_premium_bp": irp_bp,
            "tips_liquidity_premium_bp": tips_liq_bp,
        }
        if irp_bp > 30.0:
            friction_warnings.append(
                f"INFLATION_RISK_PREMIUM_ELEVATED: IRP at +{irp_bp} bps, market paying high inflation insurance premium."
            )
    else:
        cleveland_fed_real_rate = "N/A (Cleveland Fed Expectations Standby)"

    # Primary Dealer UST Inventory
    dlr_snap = dealers_snapshot(conn)
    if dlr_snap and "PDPOSGST-TOT" in dlr_snap:
        ust_dlr = dlr_snap["PDPOSGST-TOT"]
        primary_dealer_ust_inventory = {
            "inventory_b": round((ust_dlr.get("value_musd") or 0.0) / 1000.0, 1),
            "z_score": round(ust_dlr.get("z") or 0.0, 2),
            "delta_4w_pct": round(ust_dlr.get("delta_4w_pct") or 0.0, 1),
        }
    else:
        primary_dealer_ust_inventory = "N/A (NY Fed PD Positions Standby)"

    # Cross-Currency Basis Swap (Global USD Strain)
    try:
        xccy_rows = xccy.compute_xccy(conn)
    except Exception:
        xccy_rows = []
    if xccy_rows:
        front_x = xccy_rows[0]
        cross_currency_basis = {
            "contract": front_x.contract,
            "basis_spread_bp": round(front_x.basis_bps, 1),
            "interpretation": "NORMAL" if front_x.basis_bps > -20.0 else "USD_FUNDING_STRAIN",
        }
        if front_x.basis_bps < -25.0:
            friction_warnings.append(
                f"DOLLAR_LIQUIDITY_STRAIN: Cross-currency basis swap wide at {round(front_x.basis_bps, 1)} bps, global banks paying premium for USD."
            )
    else:
        cross_currency_basis = "N/A (XCCY Matrix Standby)"

    # 4b. Fetch Domain 2 (Institutional Flows & Positioning)
    opt_prod = OPTIONS_PRODUCT_MAP.get(sym)
    opt_snap = options.options_snapshot(conn, opt_prod) if opt_prod else None
    opt_pcr = opt_snap.get("pcr") if opt_snap else None
    opt_top_wall = opt_snap.get("top_wall") if opt_snap else None
    opt_max_pain = opt_snap.get("max_pain") if opt_snap else None

    # Monthly OPEX Pinning Risk
    opex_info = options.next_opex(as_of=target_dt)
    is_opex_week = opex_info.get("is_opex_week", False)
    days_to_opex = opex_info.get("days_to_opex", 99)

    cot_code = COT_CONTRACT_MAP.get(sym)
    cot_z = cot_signals._cot_zscore(conn, cot_code) if cot_code else None
    cot_sym = (
        "XAUUSD"
        if sym == "GC1"
        else ("XAGUSD" if sym == "SI1" else ("US500" if sym == "ES1" else sym))
    )
    cot_div = (
        cot_signals._price_positioning_divergence(conn, cot_sym, "CME", cot_code)
        if cot_code
        else None
    )
    if cot_div is None:
        cot_div = (
            "IN_RANGE_NEUTRAL (Inside 20W Range)" if cot_code else "N/A (No COT Contract Mapping)"
        )
    cot_quad = cot_signals._price_oi_quadrant(conn, cot_sym, "CME") if cot_code else None
    if cot_quad is None:
        cot_quad = "NEUTRAL_BALANCED" if cot_code else "N/A (No COT Contract Mapping)"
    cot_hedge = cot_signals._hedging_pressure(conn, cot_code) if cot_code else None
    if cot_hedge is None:
        cot_hedge = (
            "N/A (Financial Asset — Non-Commercial Categories Active)"
            if (
                cot_code
                and sym in ("NQ1", "ES1", "YM1", "BTCUSD", "ETHUSD", "EURUSD", "GBPUSD", "USDJPY")
            )
            else ("N/A (No COT Contract Mapping)" if not cot_code else "NEUTRAL_BALANCED")
        )
    btc_smart_money = cot_signals._btc_smart_money(conn) if sym == "BTCUSD" else None
    fx_turning_point = (
        cot_signals._fx_turning_point(conn, cot_code)
        if sym in ("EURUSD", "GBPUSD", "USDJPY") and cot_code
        else None
    )
    if sym in ("EURUSD", "GBPUSD", "USDJPY") and fx_turning_point is None:
        fx_turning_point = "IN_RANGE_NORMAL (No COT Extreme Turning Point)"
    silver_52wk_gate = cot_signals._silver_52wk_gate(conn) if sym == "SI1" else None

    if fx_turning_point and "EXTREME" in str(fx_turning_point):
        tailwinds.append(f"FX_TURNING_POINT_GATE: {fx_turning_point}")

    # Institutional ETF Flow Momentum
    etf_asset_map = {
        "GC1": "GOLD",
        "SI1": "SILVER",
        "BTCUSD": "BTC",
        "ETHUSD": "ETH",
    }
    etf_asset = etf_asset_map.get(sym)
    try:
        etf_mom = etf_flows.etf_flow_momentum(conn, etf_asset) if etf_asset else None
        if isinstance(etf_mom, dict):
            etf_mom = {k: (v if v is not None else 0.0) for k, v in etf_mom.items()}
    except Exception:
        etf_mom = None

    if etf_mom and etf_mom.get("flow_state") in ("INFLOW_SURGE", "ACCUMULATION"):
        tailwinds.append(
            f"ETF_FLOW_ACCUMULATION: Institutional ETF flow expanding ({etf_mom['flow_state']})."
        )
    elif etf_mom and etf_mom.get("flow_state") == "OUTFLOW_DRAIN":
        friction_warnings.append(
            f"ETF_FLOW_OUTFLOW: Institutional ETF flow draining ({etf_mom['flow_state']})."
        )
    if cot_div == "BEARISH_DIVERGENCE":
        friction_warnings.append(
            "COT_DISTRIBUTION_DIVERGENCE: Price at highs without institutional positioning confirmation."
        )
    if cot_quad == "NEW_MONEY_LONG":
        tailwinds.append(
            "INSTITUTIONAL_NEW_MONEY: Rising price backed by expanding Open Interest confirms trend."
        )
    elif cot_quad == "SHORT_COVERING":
        friction_warnings.append(
            "FRAGILE_RALLY: Price advance driven by short covering rather than new long buyers."
        )
    # Crypto Derivatives (Open Interest and Forced Liquidations)
    crypto_oi_usd = None
    if sym in ("BTCUSD", "ETHUSD"):
        c_inst = "BTC-USDT-SWAP" if sym == "BTCUSD" else "ETH-USDT-SWAP"
        c_oi_row = conn.execute(
            "SELECT value FROM crypto_derivatives WHERE instrument=? AND metric='open_interest_usd' ORDER BY ts_utc DESC LIMIT 1",
            (c_inst,),
        ).fetchone()
        crypto_oi_usd = float(c_oi_row[0]) if c_oi_row else None

        liq_sum = liquidation_summary(conn, c_inst, window_hours=24, as_of=target_dt)
        if liq_sum:
            crypto_liquidation_flow = {
                "state": liq_sum["state"],
                "imbalance_ratio": round(liq_sum["imbalance_ratio"], 3),
                "total_notional_usd": round(liq_sum["total_notional_usd"], 2),
                "long_flushed_usd": round(liq_sum["long_notional_usd"], 2),
                "short_squeezed_usd": round(liq_sum["short_notional_usd"], 2),
            }
            if liq_sum["state"] == "LONG_FLUSH":
                tailwinds.append(
                    f"CRYPTO_LIQUIDATION_FLUSH: Longs flushed ({liq_sum['state']}), leverage reset creates cleaner bounce potential."
                )
            elif liq_sum["state"] == "SHORT_SQUEEZE":
                tailwinds.append(
                    "CRYPTO_SHORT_SQUEEZE: Forced short liquidations propelling upward price acceleration."
                )
        else:
            crypto_liquidation_flow = "N/A (No Recent Liquidation Events)"
    else:
        crypto_liquidation_flow = "N/A (Crypto Asset Only)"

    # CME Futures Volume & Open Interest Flow Matrix
    fut_code = FUTURES_PRODUCT_MAP.get(sym)
    if fut_code:
        fut_flow = futures_flow_matrix(conn, fut_code, as_of=target_dt.strftime("%Y-%m-%d"))
        if fut_flow:
            futures_oi_flow = {
                "quadrant": fut_flow["quadrant"],
                "signal": fut_flow["signal"],
                "delta_oi_pct": fut_flow["delta_oi_pct"],
                "interpretation": fut_flow["interpretation"],
            }
            if fut_flow["quadrant"] == "NEW_LONGS":
                tailwinds.append(
                    f"FUTURES_ACCUMULATION: CME OI expanding (+{fut_flow['delta_oi_pct']}%) with rising price confirms institutional new buying ({fut_flow['quadrant']})."
                )
            elif fut_flow["quadrant"] == "SHORT_COVERING":
                friction_warnings.append(
                    "FUTURES_SHORT_COVERING: Rising price driven by short covering rather than new longs, rally vulnerable to fade."
                )
            elif fut_flow["quadrant"] == "NEW_SHORTS":
                friction_warnings.append(
                    f"FUTURES_DISTRIBUTION: CME OI expanding (+{fut_flow['delta_oi_pct']}%) with falling price confirms aggressive short selling ({fut_flow['quadrant']})."
                )
            elif fut_flow["quadrant"] == "LONG_LIQUIDATION":
                tailwinds.append(
                    "FUTURES_LONG_EXHAUSTION: Falling price driven by long liquidation rather than aggressive short entry."
                )
        else:
            futures_oi_flow = "N/A (Awaiting Daily CME Settlements)"
    else:
        futures_oi_flow = "N/A (CME Futures Metric Only)"

    # 4c. Fetch Domain 3 (High-Impact Event Risk in next 24h)
    next_event_row = conn.execute(
        """
        SELECT name, ts_utc, country FROM events
        WHERE (importance = 'HIGH' OR importance = 'high' OR importance = '3')
          AND ts_utc >= ? AND ts_utc <= ?
        ORDER BY ts_utc ASC LIMIT 1
        """,
        (
            target_dt.isoformat(timespec="seconds"),
            (target_dt + timedelta(hours=24)).isoformat(timespec="seconds"),
        ),
    ).fetchone()
    hours_to_event = (
        max(
            0.0,
            (datetime.fromisoformat(next_event_row[1]).astimezone(UTC) - target_dt).total_seconds()
            / 3600.0,
        )
        if next_event_row
        else None
    )

    # News Publication Velocity Spikes
    topic_map = {
        "CL1": "OIL",
        "BZ1": "OIL",
        "GC1": "GOLD",
        "SI1": "GOLD",
        "NQ1": "EQUITY",
        "ES1": "EQUITY",
        "YM1": "EQUITY",
        "SPY": "EQUITY",
        "BTCUSD": "CRYPTO",
        "ETHUSD": "CRYPTO",
    }
    target_topic = topic_map.get(sym)
    try:
        vel_res = news_velocity(conn, target_topic, as_of=target_dt) if target_topic else None
        vel_state = vel_res.get("state", "NORMAL") if vel_res else "NORMAL"
    except Exception:
        vel_state = "NORMAL"

    if vel_state == "NEWS_SPIKE":
        tailwinds.append(
            "NEWS_VELOCITY_SPIKE: Anomaly surge in news publication frequency detected before price move."
        )

    # 4d. Fetch Domain 4 Intermarket Microstructure & Market Breadth
    def _get_1h_chg(t_sym: str) -> float:
        b = conn.execute(
            "SELECT close FROM intraday_bars WHERE symbol=? ORDER BY bar_ts_utc DESC LIMIT 13",
            (t_sym,),
        ).fetchall()
        if len(b) >= 13 and b[-1][0]:
            return round(((b[0][0] - b[-1][0]) / b[-1][0]) * 100, 2)
        return 0.0

    tnx_1h_chg = _get_1h_chg("TNX")
    dxy_1h_chg = _get_1h_chg("DXY")
    smh_1h_chg = _get_1h_chg("SMH")
    spy_1h_chg = _get_1h_chg("SPY")
    semi_alpha = round(smh_1h_chg - spy_1h_chg, 2)

    # TPO VPOC vs TPOC Alignment
    if vpoc_tpoc_align.get("relationship") == "VPOC_ABOVE_TPOC":
        tailwinds.append(
            "VPOC_BUY_MIGRATION: Volume POC is above TPO POC, confirming institutional aggressive buy accumulation."
        )
    elif vpoc_tpoc_align.get("relationship") == "VPOC_BELOW_TPOC":
        friction_warnings.append(
            "VPOC_SELL_DISTRIBUTION: Volume POC formed below TPO POC, indicating institutional sell pressure."
        )

    # S&P 500 Constituent Breadth
    mb_row = conn.execute(
        "SELECT advances, declines FROM market_breadth ORDER BY ts_utc DESC LIMIT 1"
    ).fetchone()
    adv_ratio = round((mb_row[0] / max(1, mb_row[0] + mb_row[1])) * 100, 1) if mb_row else None

    # Multi-Domain Confluence, Friction & Gate Restrictions
    event_restriction = False

    # Event Risk Gate
    if next_event_row and hours_to_event is not None and hours_to_event <= 3.0:
        event_restriction = True
        friction_warnings.append(
            f"EVENT_RISK_HALT: High-impact '{next_event_row[0]}' in {round(hours_to_event, 1)}h. Pre-event breakout trades restricted."
        )

    # Volatility Circuit Breaker
    if vix_state == "BACKWARDATION":
        friction_warnings.append(
            f"VOLATILITY_CIRCUIT_BREAKER: VIX term structure in Backwardation (9d/spot={vix_ratio}). Longs require defensive sizing."
        )

    # OPEX Pinning Alert
    if is_opex_week:
        friction_warnings.append(
            f"OPEX_PINNING_ALERT: Monthly OPEX week ({days_to_opex}d to expiry). Magnetized to Max Pain ({opt_max_pain})."
        )

    # Market Breadth Divergence on Equities
    if sym in ("NQ1", "ES1") and adv_ratio is not None and adv_ratio < 40.0:
        friction_warnings.append(
            f"BREADTH_DIVERGENCE: Market breadth weak ({adv_ratio}% advancing). Rally lacks broad constituent backing."
        )
    elif sym in ("NQ1", "ES1") and adv_ratio is not None and adv_ratio > 65.0:
        tailwinds.append(
            f"BREADTH_CONFIRMATION: Broad market participation ({adv_ratio}% advancing). Confirms index strength."
        )
    # Yield Friction on Equities/Tech
    if sym in ("NQ1", "ES1") and tnx_1h_chg > 0.5:
        friction_warnings.append(
            f"YIELD_HEADWIND: 10Y Yield surging (+{tnx_1h_chg}% in 1h), creates valuation drag."
        )
    elif sym in ("NQ1", "ES1") and tnx_1h_chg < -0.5:
        tailwinds.append(
            f"YIELD_TAILWIND: 10Y Yield dropping ({tnx_1h_chg}% in 1h), provides duration relief."
        )

    # Dollar Friction on Gold & FX
    if sym in ("GC1", "SI1", "EURUSD", "GBPUSD") and dxy_1h_chg > 0.15:
        friction_warnings.append(
            f"DOLLAR_HEADWIND: US Dollar strengthening (+{dxy_1h_chg}% in 1h)."
        )
    elif sym in ("GC1", "SI1", "EURUSD", "GBPUSD") and dxy_1h_chg < -0.15:
        tailwinds.append(f"DOLLAR_TAILWIND: US Dollar softening ({dxy_1h_chg}% in 1h).")

    # Semiconductor Lead on NQ1
    if sym == "NQ1" and semi_alpha > 0.3:
        tailwinds.append(
            f"SEMI_LEADERSHIP: Chips outperforming market (+{semi_alpha}% alpha), supports tech breakout."
        )
    elif sym == "NQ1" and semi_alpha < -0.3:
        friction_warnings.append(
            f"SEMI_LAG: Chips lagging market ({semi_alpha}% alpha), cautions tech rally."
        )
    # 5. Extract Empirical Stats for this symbol
    emp = EMPIRICAL_BREAKOUT_STATS.get(sym, EMPIRICAL_BREAKOUT_STATS.get("NQ1", {}))
    cont_atr = emp.get("high_continuation_median_atr", 0.30)
    high_false_close_pct = emp.get("high_false_close_pct", 45.0)
    low_false_close_pct = emp.get("low_false_close_pct", 50.0)
    daily_atr = max(0.001, pdh - pdl, (atr_14 or 0.0) * 12)

    # 6. Build Actionable Scenarios (Separated into Intraday & Swing Horizons)
    intraday_scenarios = []
    swing_scenarios = []

    is_bullish_open_drive = open_type == "OPEN_DRIVE_BULLISH"
    is_bearish_open_drive = open_type == "OPEN_DRIVE_BEARISH"

    # [A] INTRADAY SCENARIO 1: Intraday Momentum Expansion
    if (fast_cat["net_stance_score"] >= 0.15 or is_bullish_open_drive) and (
        vwap is None or last_price >= vwap
    ):
        intra_target = (
            round(max(pdh + (0.20 * daily_atr), last_price + (cont_atr * daily_atr)), 2)
            + cfd_basis_offset
        )
        intra_inval = (
            round(
                max(
                    val if val else last_price - (0.20 * daily_atr),
                    vwap if vwap else last_price - (0.20 * daily_atr),
                ),
                2,
            )
            + cfd_basis_offset
        )
        trig_long = vah + cfd_basis_offset
        reward_long = abs(intra_target - trig_long)
        risk_long = max(0.01, abs(trig_long - intra_inval))
        rr_long = round(reward_long / risk_long, 2)
        if rr_long >= 1.5:
            intraday_scenarios.append(
                {
                    "id": "SCENARIO_INTRADAY_EXPANSION_LONG",
                    "horizon": "INTRADAY",
                    "title": "Intraday Trend Expansion Long (Catalyst Momentum + Value Acceptance)",
                    "direction": "LONG",
                    "trigger_condition": f"5m candle closes and holds above VAH ({vah}) with price staying above Session VWAP ({vwap})",
                    "trigger_price": vah,
                    "target_profit": intra_target,
                    "invalidation_level": intra_inval,
                    "risk_reward_ratio": rr_long,
                    "timing_gate": {
                        "recommended_quarter": "Q3_NY_AM",
                        "recommended_sub_quarter": "Sub-3 (DISTRIBUTION_EXPANSION_DRIVE) or Sub-4",
                        "recommended_micro_cycle": "Micro-3 (MICRO_DIRECTIONAL_RUN)",
                        "current_cycle": f"{active_q} | {active_sub} ({sub_role}) | {active_micro} ({micro_role})",
                        "is_optimal_window": bool(
                            active_sub in ("Sub-3", "Sub-4")
                            and active_micro in ("Micro-3", "Micro-4")
                        ),
                        "execution_notes": "Breakout momentum requires active volume expansion window (Sub-3 / Micro-3). Avoid executing during Sub-1/Sub-2 traps.",
                    },
                    "invalidation_rationale": "Loss of Session VWAP or close back inside Value Area rejects continuation.",
                    "empirical_support": {
                        "continuation_median_atr": cont_atr,
                        "target_derivation": f"max(PDH + 0.2*Daily_ATR, last_price + {cont_atr} * Daily_ATR)",
                        "open_type_gate": f"{open_type} ({open_conviction})",
                        "sample_weeks": emp.get("sample_weeks_high"),
                        "source": emp.get("source_doc"),
                    },
                }
            )
    elif (fast_cat["net_stance_score"] <= -0.15 or is_bearish_open_drive) and (
        vwap is None or last_price <= vwap
    ):
        intra_target = (
            round(min(pdl - (0.20 * daily_atr), last_price - (cont_atr * daily_atr)), 2)
            + cfd_basis_offset
        )
        intra_inval = (
            round(
                min(
                    vah if vah else last_price + (0.20 * daily_atr),
                    vwap if vwap else last_price + (0.20 * daily_atr),
                ),
                2,
            )
            + cfd_basis_offset
        )
        trig_short = val + cfd_basis_offset
        reward_short = abs(trig_short - intra_target)
        risk_short = max(0.01, abs(intra_inval - trig_short))
        rr_short = round(reward_short / risk_short, 2)
        if rr_short >= 1.5:
            intraday_scenarios.append(
                {
                    "id": "SCENARIO_INTRADAY_EXPANSION_SHORT",
                    "horizon": "INTRADAY",
                    "title": "Intraday Trend Expansion Short (Dovish/Bearish Catalyst + Value Acceptance)",
                    "direction": "SHORT",
                    "trigger_condition": f"5m candle closes and holds below VAL ({val}) with price staying below Session VWAP ({vwap})",
                    "trigger_price": val,
                    "target_profit": intra_target,
                    "invalidation_level": intra_inval,
                    "risk_reward_ratio": rr_short,
                    "timing_gate": {
                        "recommended_quarter": "Q3_NY_AM",
                        "recommended_sub_quarter": "Sub-3 (DISTRIBUTION_EXPANSION_DRIVE) or Sub-4",
                        "recommended_micro_cycle": "Micro-3 (MICRO_DIRECTIONAL_RUN)",
                        "current_cycle": f"{active_q} | {active_sub} ({sub_role}) | {active_micro} ({micro_role})",
                        "is_optimal_window": bool(
                            active_sub in ("Sub-3", "Sub-4")
                            and active_micro in ("Micro-3", "Micro-4")
                        ),
                        "execution_notes": "Breakout momentum requires active volume expansion window (Sub-3 / Micro-3). Avoid executing during Sub-1/Sub-2 traps.",
                    },
                    "invalidation_rationale": "Reclaim of Session VWAP or close back inside Value Area invalidates short.",
                    "empirical_support": {
                        "continuation_median_atr": emp.get("low_continuation_median_atr", cont_atr),
                        "target_derivation": f"min(PDL - 0.2*Daily_ATR, last_price - {cont_atr} * Daily_ATR)",
                        "open_type_gate": f"{open_type} ({open_conviction})",
                        "sample_weeks": emp.get("sample_weeks_low"),
                        "source": emp.get("source_doc"),
                    },
                }
            )
    # [B1] INTRADAY SCENARIO: Value Area 80% Rule Rotation (Inside Value Auction)
    if val and vah:
        if abs(last_price - val) <= (0.25 * daily_atr) and last_price >= (val - 0.05 * daily_atr):
            rot_target = round(vah + cfd_basis_offset, 2)
            rot_inval = round(val - (0.05 * daily_atr) + cfd_basis_offset, 2)
            rot_reward = abs(rot_target - last_price)
            rot_risk = max(0.01, abs(last_price - rot_inval))
            rr_rot = round(rot_reward / rot_risk, 2)
            if rr_rot >= 1.5:
                intraday_scenarios.append(
                    {
                        "id": "SCENARIO_INTRADAY_VAL_ROTATION_LONG",
                        "horizon": "INTRADAY",
                        "title": "Value Area 80% Rule Rotation Long (VAL Support to POC/VAH)",
                        "direction": "LONG",
                        "trigger_condition": f"Price respects and accepts above VAL ({val}); holds within prior Value Area",
                        "trigger_price": val,
                        "target_profit": rot_target,
                        "invalidation_level": rot_inval,
                        "risk_reward_ratio": rr_rot,
                        "timing_gate": {
                            "recommended_quarter": "Q3_NY_AM",
                            "recommended_sub_quarter": "Sub-1 or Sub-2 (Initial Range Defense)",
                            "recommended_micro_cycle": "Micro-1 or Micro-2",
                            "current_cycle": f"{active_q} | {active_sub} ({sub_role}) | {active_micro} ({micro_role})",
                            "is_optimal_window": bool(active_sub in ("Sub-1", "Sub-2", "Sub-3")),
                            "execution_notes": "Value Area rotation long triggered as price tests and holds VAL boundary.",
                        },
                        "invalidation_rationale": "Breakdown and acceptance below VAL turns thesis into breakdown expansion.",
                        "empirical_support": {
                            "rule": "Dalton 80% Rule of Value Area Rotation",
                            "target_magnet": "POC / Opposite Value Area High",
                            "source": "Auction Market Theory Mind over Markets",
                        },
                    }
                )
        elif abs(last_price - vah) <= (0.25 * daily_atr) and last_price <= (vah + 0.05 * daily_atr):
            rot_target_s = round(val + cfd_basis_offset, 2)
            rot_inval_s = round(vah + (0.05 * daily_atr) + cfd_basis_offset, 2)
            rot_reward_s = abs(last_price - rot_target_s)
            rot_risk_s = max(0.01, abs(rot_inval_s - last_price))
            rr_rot_s = round(rot_reward_s / rot_risk_s, 2)
            if rr_rot_s >= 1.5:
                intraday_scenarios.append(
                    {
                        "id": "SCENARIO_INTRADAY_VAH_ROTATION_SHORT",
                        "horizon": "INTRADAY",
                        "title": "Value Area 80% Rule Rotation Short (VAH Resistance to POC/VAL)",
                        "direction": "SHORT",
                        "trigger_condition": f"Price fails to break above VAH ({vah}); accepts back inside prior Value Area",
                        "trigger_price": vah,
                        "target_profit": rot_target_s,
                        "invalidation_level": rot_inval_s,
                        "risk_reward_ratio": rr_rot_s,
                        "timing_gate": {
                            "recommended_quarter": "Q3_NY_AM",
                            "recommended_sub_quarter": "Sub-1 or Sub-2 (Initial Range Defense)",
                            "recommended_micro_cycle": "Micro-1 or Micro-2",
                            "current_cycle": f"{active_q} | {active_sub} ({sub_role}) | {active_micro} ({micro_role})",
                            "is_optimal_window": bool(active_sub in ("Sub-1", "Sub-2", "Sub-3")),
                            "execution_notes": "Value Area rotation short triggered as price rejects VAH boundary.",
                        },
                        "invalidation_rationale": "Breakout and acceptance above VAH turns thesis into breakout expansion.",
                        "empirical_support": {
                            "rule": "Dalton 80% Rule of Value Area Rotation",
                            "target_magnet": "POC / Opposite Value Area Low",
                            "source": "Auction Market Theory Mind over Markets",
                        },
                    }
                )
    # [B2] INTRADAY SCENARIO: TPO Single Print Imbalance Repair Magnet
    if single_prints and abs(last_price - single_prints[0]["price_mid"]) <= (1.5 * atr_14):
        target_sp = round(single_prints[0]["price_mid"] + cfd_basis_offset, 2)
        sp_dir = "LONG" if target_sp > last_price else "SHORT"
        sp_inval = round((val if sp_dir == "LONG" else vah) + cfd_basis_offset, 2)
        reward_span = abs(target_sp - last_price)
        risk_span = max(0.01, abs(last_price - sp_inval))
        rr_sp = round(reward_span / risk_span, 2)
        if rr_sp >= 1.2:
            intraday_scenarios.append(
                {
                    "id": "SCENARIO_INTRADAY_SINGLE_PRINT_REPAIR",
                    "horizon": "INTRADAY",
                    "title": f"TPO Single Print Imbalance Repair (Bracket {single_prints[0]['bracket']})",
                    "direction": sp_dir,
                    "trigger_condition": f"Price tests imbalance void; fills toward Single Print at {target_sp}",
                    "trigger_price": last_price,
                    "target_profit": target_sp,
                    "invalidation_level": sp_inval,
                    "timing_gate": {
                        "recommended_quarter": "Q3_NY_AM",
                        "recommended_sub_quarter": "Sub-3 (DISTRIBUTION_EXPANSION_DRIVE)",
                        "recommended_micro_cycle": "Micro-1 or Micro-3",
                        "current_cycle": f"{active_q} | {active_sub} ({sub_role}) | {active_micro} ({micro_role})",
                        "is_optimal_window": bool(active_sub in ("Sub-3", "Sub-4")),
                        "execution_notes": "Single print voids act as rapid vacuum magnets as regular market volume floods in.",
                    },
                    "risk_reward_ratio": rr_sp,
                    "invalidation_rationale": "Reversal away from single print void invalidates repair thesis.",
                    "empirical_support": {
                        "rule": "Auction Market Theory Imbalance Repair Magnet",
                        "single_print_bracket": single_prints[0]["bracket"],
                        "source": "AMT Markets in Profile Liquidity Voids",
                    },
                }
            )

    # [B] INTRADAY SCENARIO 2: Liquidity Sweep / Failed Auction (Trap Setup)
    if last_price >= pdh * 0.998 and not is_bullish_open_drive:
        sweep_inval = round(pdh + (0.05 * daily_atr), 2) + cfd_basis_offset
        target_naked = (
            naked_poc_below if isinstance(naked_poc_below, int | float) else (poc if poc else pdc)
        )
        sweep_target = (
            round(
                target_naked if target_naked < last_price else (last_price - (0.35 * daily_atr)),
                2,
            )
            + cfd_basis_offset
        )
        trig_sweep_s = pdh + cfd_basis_offset
        reward_sweep_short = abs(trig_sweep_s - sweep_target)
        risk_sweep_short = max(0.01, abs(sweep_inval - trig_sweep_s))
        rr_sweep_short = round(reward_sweep_short / risk_sweep_short, 2)
        if rr_sweep_short >= 1.5:
            intraday_scenarios.append(
                {
                    "id": "SCENARIO_INTRADAY_SWEEP_SHORT",
                    "horizon": "INTRADAY",
                    "title": "PDH Liquidity Sweep / Bull Trap Reversal",
                    "direction": "SHORT",
                    "trigger_condition": f"Price spikes above PDH ({pdh}) but fails to sustain; 5m/15m candle closes back below {pdh}",
                    "trigger_price": pdh,
                    "target_profit": sweep_target,
                    "invalidation_level": sweep_inval,
                    "timing_gate": {
                        "recommended_quarter": "Q3_NY_AM",
                        "recommended_sub_quarter": "Sub-2 (MANIPULATION_TRAP_SETUP) or Sub-1",
                        "recommended_micro_cycle": "Micro-2 (MICRO_PIVOT_SWEEP)",
                        "current_cycle": f"{active_q} | {active_sub} ({sub_role}) | {active_micro} ({micro_role})",
                        "is_optimal_window": bool(
                            active_sub in ("Sub-1", "Sub-2")
                            or active_micro in ("Micro-1", "Micro-2")
                        ),
                        "execution_notes": "Sweep and liquidity trap reversals peak during Sub-2 Judah probe and Micro-2 liquidity grabs.",
                    },
                    "risk_reward_ratio": rr_sweep_short,
                    "invalidation_rationale": f"Price accepts and sustains above {sweep_inval} (PDH + 0.05*ATR) proves breakout.",
                    "empirical_support": {
                        "empirical_false_close_rate_pct": high_false_close_pct,
                        "target_magnet": f"Unretested Naked POC at {naked_poc_below}"
                        if naked_poc_below
                        else "Prior POC",
                        "confidence_interval_95": emp.get("high_false_close_ci95"),
                        "sample_weeks": emp.get("sample_weeks_high"),
                        "mechanism": "Nearly half (44%-54%) of high breakouts fail to close outside prior range",
                        "source": emp.get("source_doc"),
                    },
                }
            )
    elif last_price <= pdl * 1.002 and not is_bearish_open_drive:
        sweep_inval = round(pdl - (0.05 * daily_atr), 2) + cfd_basis_offset
        target_naked = (
            naked_poc_above if isinstance(naked_poc_above, int | float) else (poc if poc else pdc)
        )
        sweep_target = (
            round(
                target_naked if target_naked > last_price else (last_price + (0.35 * daily_atr)),
                2,
            )
            + cfd_basis_offset
        )
        trig_sweep_l = pdl + cfd_basis_offset
        reward_sweep_long = abs(sweep_target - trig_sweep_l)
        risk_sweep_long = max(0.01, abs(trig_sweep_l - sweep_inval))
        rr_sweep_long = round(reward_sweep_long / risk_sweep_long, 2)
        if rr_sweep_long >= 1.5:
            intraday_scenarios.append(
                {
                    "id": "SCENARIO_INTRADAY_SWEEP_LONG",
                    "horizon": "INTRADAY",
                    "title": "PDL Liquidity Sweep / Bear Trap Reversal",
                    "direction": "LONG",
                    "trigger_condition": f"Price pierces below PDL ({pdl}) but reclaims level; 5m/15m candle closes back above {pdl}",
                    "trigger_price": pdl,
                    "target_profit": sweep_target,
                    "invalidation_level": sweep_inval,
                    "timing_gate": {
                        "recommended_quarter": "Q3_NY_AM",
                        "recommended_sub_quarter": "Sub-2 (MANIPULATION_TRAP_SETUP) or Sub-1",
                        "recommended_micro_cycle": "Micro-2 (MICRO_PIVOT_SWEEP)",
                        "current_cycle": f"{active_q} | {active_sub} ({sub_role}) | {active_micro} ({micro_role})",
                        "is_optimal_window": bool(
                            active_sub in ("Sub-1", "Sub-2")
                            or active_micro in ("Micro-1", "Micro-2")
                        ),
                        "execution_notes": "Sweep and liquidity trap reversals peak during Sub-2 Judah probe and Micro-2 liquidity grabs.",
                    },
                    "risk_reward_ratio": rr_sweep_long,
                    "invalidation_rationale": f"Price breaks below {sweep_inval} (PDL - 0.05*ATR) confirms breakdown.",
                    "empirical_support": {
                        "empirical_false_close_rate_pct": low_false_close_pct,
                        "target_magnet": f"Unretested Naked POC at {naked_poc_above}"
                        if naked_poc_above
                        else "Prior POC",
                        "confidence_interval_95": emp.get("low_false_close_ci95"),
                        "sample_weeks": emp.get("sample_weeks_low"),
                        "mechanism": "Observed low false close frequency across historical database",
                        "source": emp.get("source_doc"),
                    },
                }
            )
    # [D] SWING SCENARIO 1: Multi-Day CVA Balance Expansion (Dalton 100% Measured Move)
    if cva_measured_long and last_price >= (vah or last_price):
        cva_target = round(cva_measured_long + cfd_basis_offset, 2)
        cva_inval = round(
            (levels.get("DYNAMIC_CVA_POC") or poc or (last_price - (0.35 * daily_atr)))
            + cfd_basis_offset,
            2,
        )
        trig_cva_l = (levels.get("DYNAMIC_CVA_VAH") or last_price) + cfd_basis_offset
        reward_cva_long = abs(cva_target - trig_cva_l)
        risk_cva_long = max(0.01, abs(trig_cva_l - cva_inval))
        rr_cva_long = round(reward_cva_long / risk_cva_long, 2)
        if rr_cva_long >= 1.5:
            swing_scenarios.append(
                {
                    "id": "SCENARIO_SWING_CVA_EXPANSION_LONG",
                    "horizon": "SWING",
                    "title": f"Multi-Day {cva_name} Breakout Expansion Long",
                    "direction": "LONG",
                    "trigger_condition": f"Daily bar accepts and sustains above Composite VAH ({levels.get('DYNAMIC_CVA_VAH')})",
                    "trigger_price": levels.get("DYNAMIC_CVA_VAH") or last_price,
                    "target_profit": cva_target,
                    "invalidation_level": cva_inval,
                    "risk_reward_ratio": rr_cva_long,
                    "timing_gate": {
                        "recommended_window": f"{w_quarter.get('weekday', 'Thursday')} ({w_quarter.get('quarter', 'Q4')}) | {m_quarter.get('quarter', 'Q1')}",
                        "current_cycle": f"Weekly {w_quarter.get('quarter', 'Q4')} ({w_quarter.get('theory_role', 'CONTINUATION')}) | Monthly {m_quarter.get('quarter', 'Q1')}",
                        "is_optimal_window": True,
                        "execution_notes": f"Swing trade aligns with {m_quarter.get('description', 'Monthly Cycle')}.",
                    },
                    "invalidation_rationale": "Loss of Composite Balance Area POC indicates failed breakout.",
                    "empirical_support": {
                        "rule": "Dalton 100% Measured Move of Balance Range",
                        "balance_days": ctx.get("dynamic_cva_days", 2),
                        "source": "Auction Market Theory Balance Progression",
                    },
                }
            )
    elif cva_measured_short and last_price <= (val or last_price):
        cva_target = round(cva_measured_short + cfd_basis_offset, 2)
        cva_inval = round(
            (levels.get("DYNAMIC_CVA_POC") or poc or (last_price + (0.35 * daily_atr)))
            + cfd_basis_offset,
            2,
        )
        trig_cva_s = (levels.get("DYNAMIC_CVA_VAL") or last_price) + cfd_basis_offset
        reward_cva_short = abs(trig_cva_s - cva_target)
        risk_cva_short = max(0.01, abs(cva_inval - trig_cva_s))
        rr_cva_short = round(reward_cva_short / risk_cva_short, 2)
        if rr_cva_short >= 1.5:
            swing_scenarios.append(
                {
                    "id": "SCENARIO_SWING_CVA_EXPANSION_SHORT",
                    "horizon": "SWING",
                    "title": f"Multi-Day {cva_name} Breakdown Expansion Short",
                    "direction": "SHORT",
                    "trigger_condition": f"Daily bar accepts and sustains below Composite VAL ({levels.get('DYNAMIC_CVA_VAL')})",
                    "trigger_price": levels.get("DYNAMIC_CVA_VAL") or last_price,
                    "target_profit": cva_target,
                    "invalidation_level": cva_inval,
                    "risk_reward_ratio": rr_cva_short,
                    "timing_gate": {
                        "recommended_window": f"{w_quarter.get('weekday', 'Thursday')} ({w_quarter.get('quarter', 'Q4')}) | {m_quarter.get('quarter', 'Q1')}",
                        "current_cycle": f"Weekly {w_quarter.get('quarter', 'Q4')} ({w_quarter.get('theory_role', 'CONTINUATION')}) | Monthly {m_quarter.get('quarter', 'Q1')}",
                        "is_optimal_window": True,
                        "execution_notes": f"Swing trade aligns with {m_quarter.get('description', 'Monthly Cycle')}.",
                    },
                    "invalidation_rationale": "Reclaim of Composite Balance Area POC indicates failed breakdown.",
                    "empirical_support": {
                        "rule": "Dalton 100% Measured Move of Balance Range",
                        "balance_days": ctx.get("dynamic_cva_days", 2),
                        "source": "Auction Market Theory Balance Progression",
                    },
                }
            )

    # [E] SWING SCENARIO 2: Weekly Value Migration & Naked POC Target
    if isinstance(naked_poc_below, int | float) and value_migration in (
        "LOWER_VALUE",
        "OVERLAPPING_LOWER",
    ):
        target_npoc_s = round(naked_poc_below + cfd_basis_offset, 2)
        inval_npoc_s = round((pdh or (last_price + (0.35 * daily_atr))) + cfd_basis_offset, 2)
        trig_npoc_s = (levels.get("WEEKLY_VWAP") or last_price) + cfd_basis_offset
        reward_npoc_s = abs(trig_npoc_s - target_npoc_s)
        risk_npoc_s = max(0.01, abs(inval_npoc_s - trig_npoc_s))
        rr_npoc_s = round(reward_npoc_s / risk_npoc_s, 2)
        if rr_npoc_s >= 1.5:
            swing_scenarios.append(
                {
                    "id": "SCENARIO_SWING_NAKED_POC_TARGET_SHORT",
                    "horizon": "SWING",
                    "title": f"Swing Value Migration to Naked POC ({naked_poc_below})",
                    "direction": "SHORT",
                    "trigger_condition": f"Value migration remains {value_migration}; price holds below Weekly VWAP ({levels.get('WEEKLY_VWAP')})",
                    "trigger_price": levels.get("WEEKLY_VWAP") or last_price,
                    "target_profit": target_npoc_s,
                    "invalidation_level": inval_npoc_s,
                    "risk_reward_ratio": rr_npoc_s,
                    "timing_gate": {
                        "recommended_window": f"{w_quarter.get('weekday', 'Thursday')} ({w_quarter.get('quarter', 'Q4')}) | {m_quarter.get('quarter', 'Q1')}",
                        "current_cycle": f"Weekly {w_quarter.get('quarter', 'Q4')} ({w_quarter.get('theory_role', 'CONTINUATION')}) | Monthly {m_quarter.get('quarter', 'Q1')}",
                        "is_optimal_window": True,
                        "execution_notes": f"Swing trade aligns with {m_quarter.get('description', 'Monthly Cycle')}.",
                    },
                    "invalidation_rationale": "Break above prior session high proves bullish reversal.",
                    "empirical_support": {
                        "target_type": "Virgin / Naked POC Magnet",
                        "value_migration": value_migration,
                        "source": "AMT Markets in Profile Unretested Auction Nodes",
                    },
                }
            )
    elif isinstance(naked_poc_above, int | float) and value_migration in (
        "HIGHER_VALUE",
        "OVERLAPPING_HIGHER",
    ):
        target_npoc_l = round(naked_poc_above + cfd_basis_offset, 2)
        inval_npoc_l = round((pdl or (last_price - (0.35 * daily_atr))) + cfd_basis_offset, 2)
        trig_npoc_l = (levels.get("WEEKLY_VWAP") or last_price) + cfd_basis_offset
        reward_npoc_l = abs(target_npoc_l - trig_npoc_l)
        risk_npoc_l = max(0.01, abs(trig_npoc_l - inval_npoc_l))
        rr_npoc_l = round(reward_npoc_l / risk_npoc_l, 2)
        if rr_npoc_l >= 1.5:
            swing_scenarios.append(
                {
                    "id": "SCENARIO_SWING_NAKED_POC_TARGET_LONG",
                    "horizon": "SWING",
                    "title": f"Swing Value Migration to Naked POC ({naked_poc_above})",
                    "direction": "LONG",
                    "trigger_condition": f"Value migration remains {value_migration}; price holds below Weekly VWAP ({levels.get('WEEKLY_VWAP')})",
                    "trigger_price": levels.get("WEEKLY_VWAP") or last_price,
                    "target_profit": target_npoc_l,
                    "invalidation_level": inval_npoc_l,
                    "risk_reward_ratio": rr_npoc_l,
                    "timing_gate": {
                        "recommended_window": f"{w_quarter.get('weekday', 'Thursday')} ({w_quarter.get('quarter', 'Q4')}) | {m_quarter.get('quarter', 'Q1')}",
                        "current_cycle": f"Weekly {w_quarter.get('quarter', 'Q4')} ({w_quarter.get('theory_role', 'CONTINUATION')}) | Monthly {m_quarter.get('quarter', 'Q1')}",
                        "is_optimal_window": True,
                        "execution_notes": f"Swing trade aligns with {m_quarter.get('description', 'Monthly Cycle')}.",
                    },
                    "invalidation_rationale": "Break below prior session low proves bearish reversal.",
                    "empirical_support": {
                        "target_type": "Virgin / Naked POC Magnet",
                        "value_migration": value_migration,
                        "source": "AMT Markets in Profile Unretested Auction Nodes",
                    },
                }
            )

    all_scenarios = intraday_scenarios + swing_scenarios
    now_utc = datetime.now(UTC).isoformat(timespec="seconds")
    out_dict = {
        "symbol": sym,
        "as_of": now_utc,
        "last_price": round(last_price, 4),
        "reference_levels": {
            k: (
                v
                if v is not None
                else (
                    "NONE_IN_25D_LOOKBACK (All-Time High / Blue Sky)"
                    if k == "NAKED_POC_ABOVE"
                    else (
                        "NONE_IN_25D_LOOKBACK (All-Time Low)"
                        if k == "NAKED_POC_BELOW"
                        else (
                            "FORMING_IN_SESSION"
                            if k.startswith(("ASIA_", "LONDON_"))
                            else "FORMING_IN_RTH"
                        )
                    )
                )
            )
            for k, v in levels.items()
        },
        "price_action": {
            "vwap": round(vwap, 4) if vwap else round(last_price, 4),
            "atr_14": round(atr_14, 4),
            "vwap_state": vwap_state,
            "volatility_ratio": round(volatility_ratio, 2),
        },
        "catalysts": {
            "intraday_fast_stance": fast_cat["stance"],
            "intraday_fast_score": fast_cat["net_stance_score"],
            "intraday_articles_4h": fast_cat.get("sample_count", 0),
            "active_channels": fast_cat.get("active_catalysts", {}),
            "top_quotes": (
                fast_cat.get("top_intraday_quotes", [])
                + [
                    q
                    for q in swing_sent.get("top_quotes", [])
                    if q["title"]
                    not in [x["title"] for x in fast_cat.get("top_intraday_quotes", [])]
                ]
            )[:3],
            "swing_macro_stance": swing_sent["stance"],
            "swing_macro_score": swing_sent["net_stance_score"],
            "swing_articles_3d": swing_sent.get("sample_count", 0),
            "multiday_macro_headlines": swing_sent.get("top_quotes", [])[:3],
            "macro_regime_score": round(macro_regime_score, 2),
        },
        "multi_domain": {
            "domain_1_macro": {
                "dalio_economic_quadrant": dalio_quadrant,
                "systemic_net_liquidity_b": net_liq_b,
                "quadrant_asset_alignment": quadrant_alignment,
                "tips_10y_real_yield": real_yield_10y,
                "yield_curve_spread_t10y2y": yield_curve_spread,
                "fed_broad_trade_weighted_dollar": broad_dollar_val,
                "dollar_smile_regime": smile_regime,
                "vix_term_structure_state": vix_state,
                "vix_9d_spot_ratio": vix_ratio,
                "sahm_rule_recession_indicator": sahm_val if sahm_val is not None else 0.0,
                "treasury_10y_auction_percentile": round(auc_pctl, 1)
                if auc_pctl
                else "N/A (No Recent 10Y Auction)",
                "fedwatch_fomc_outlook": fedwatch_fomc_outlook,
                "recession_triangulation": recession_triangulation,
                "cleveland_fed_real_rate": cleveland_fed_real_rate,
                "primary_dealer_ust_inventory": primary_dealer_ust_inventory,
                "cross_currency_basis": cross_currency_basis,
            },
            "domain_2_flows": {
                "options_pcr": round(opt_pcr, 3)
                if opt_pcr
                else (
                    "N/A (Awaiting Daily Settlement Publish)"
                    if opt_prod
                    else "N/A (No CME Options Settlement Feed)"
                ),
                "options_top_wall": opt_top_wall
                if opt_top_wall
                else (
                    "N/A (Awaiting Daily Settlement Publish)"
                    if opt_prod
                    else "N/A (No CME Options Settlement Feed)"
                ),
                "options_max_pain": opt_max_pain
                if opt_max_pain
                else (
                    "N/A (Awaiting Daily Settlement Publish)"
                    if opt_prod
                    else "N/A (No CME Options Settlement Feed)"
                ),
                "is_opex_week": is_opex_week,
                "days_to_opex": days_to_opex,
                "cot_positioning_3y_zscore": round(cot_z, 2)
                if cot_z is not None
                else "N/A (No COT Mapping)",
                "cot_price_positioning_divergence": cot_div,
                "price_oi_quadrant": cot_quad,
                "commercial_hedging_pressure": cot_hedge,
                "crypto_open_interest_usd": crypto_oi_usd
                if sym in ("BTCUSD", "ETHUSD")
                else "N/A (Crypto Asset Only)",
                "crypto_liquidation_flow": crypto_liquidation_flow,
                "futures_oi_flow": futures_oi_flow,
                "etf_flow_momentum": etf_mom
                if etf_mom is not None
                else (
                    "N/A (Accumulating Flow History: <5 daily observations)"
                    if etf_asset
                    else "N/A (Metals/Crypto Physical ETF Metric)"
                ),
                "btc_smart_money": btc_smart_money if sym == "BTCUSD" else "N/A (BTC Only)",
                "fx_turning_point_gate": fx_turning_point
                if sym in ("EURUSD", "GBPUSD", "USDJPY")
                else "N/A (FX Majors Only)",
                "silver_52wk_gate": silver_52wk_gate if sym == "SI1" else "N/A (Silver Only)",
            },
            "domain_3_news_events": {
                "fast_catalyst_stance": fast_cat["stance"],
                "fast_catalyst_score": fast_cat["net_stance_score"],
                "news_velocity_state": vel_state,
                "upcoming_high_impact_event": next_event_row[0]
                if next_event_row
                else "NONE_SCHEDULED_NEXT_24H",
                "hours_to_next_event": round(hours_to_event, 1)
                if hours_to_event is not None
                else "N/A (No Event Next 24H)",
            },
            "domain_4_intermarket_breadth": {
                "us_10y_yield_1h_chg_pct": tnx_1h_chg if tnx_1h_chg is not None else 0.0,
                "dxy_dollar_1h_chg_pct": dxy_1h_chg if dxy_1h_chg is not None else 0.0,
                "semi_alpha_vs_spy_pct": semi_alpha if semi_alpha is not None else 0.0,
                "sp500_advancing_breadth_pct": adv_ratio if adv_ratio is not None else 50.0,
            },
            "confluence_status": {
                "friction_warnings": friction_warnings,
                "tailwinds": tailwinds,
                "event_risk_halt": event_restriction,
                "alignment_state": (
                    "EVENT_HALT_REQUIRED"
                    if event_restriction
                    else (
                        "VOLATILITY_RESTRICTION"
                        if vix_state == "BACKWARDATION"
                        else (
                            "FRICTION_DETECTED"
                            if friction_warnings
                            else ("STRONG_CONFLUENCE" if tailwinds else "NEUTRAL_BALANCED")
                        )
                    )
                ),
            },
        },
        "amt_context": {
            "open_type": open_type,
            "open_conviction": open_conviction,
            "participant_activity": participant_activity,
            "value_migration": value_migration,
            "cva_name": cva_name,
            "cva_measured_move_long": cva_measured_long,
            "cva_measured_move_short": cva_measured_short,
            "nearest_naked_poc_above": naked_poc_above
            if naked_poc_above
            else "NONE_IN_25D_LOOKBACK (All-Time High / Blue Sky)",
            "nearest_naked_poc_below": naked_poc_below
            if naked_poc_below
            else "NONE_IN_25D_LOOKBACK (All-Time Low)",
            "multi_horizon_session_profiles": ctx.get("session_profiles", {}),
            "session_value_migration": ctx.get("session_value_migration", {}),
            "hierarchical_naked_pocs": ctx.get("hierarchical_naked_pocs", {}),
            "multi_desk_initial_balance": ctx.get("multi_desk_initial_balance", {}),
            "overnight_cva": ctx.get("overnight_cva", {}),
            "multi_horizon_time_acceptance": ctx.get("multi_horizon_time_acceptance", {}),
            "multi_horizon_open_types": ctx.get("multi_horizon_open_types", {}),
            "multi_horizon_cva_map": ctx.get("multi_horizon_cva_map", {}),
            "ipda_data_ranges": ctx.get("ipda_data_ranges", {}),
            "quarterly_theory": ctx.get("quarterly_theory", {}),
            "tpo_analytics": {
                "tpo_poc": tpo_poc,
                "tpo_vah": tpo_vah,
                "tpo_val": tpo_val,
                "vpoc_tpoc_alignment": vpoc_tpoc_align,
                "single_prints": single_prints,
                "single_prints_count": len(single_prints),
            },
        },
        "intraday_playbook": intraday_scenarios,
        "swing_playbook": swing_scenarios,
        "scenarios": all_scenarios,
        "provenance": {
            "levels_derived_from": ref["provenance"],
            "empirical_dataset": emp.get("source_doc"),
            "cfd_basis_offset": cfd_basis_offset,
            "calculated_at_utc": now_utc,
        },
    }

    # Record scenarios into playbook_scenarios tracker table
    record_playbook_scenarios(conn, out_dict, cfd_basis_offset=cfd_basis_offset)
    evaluate_active_playbooks(conn, as_of=target_dt)
    out_dict["performance_tracker"] = get_playbook_performance_metrics(conn, symbol=sym)

    return out_dict
