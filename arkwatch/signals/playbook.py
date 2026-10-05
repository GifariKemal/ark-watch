"""playbook.py — Actionable Trading Playbook & Scenario Outlook Engine.

Synthesizes Auction Market Theory reference levels, intraday price action (VWAP & ATR),
fast news catalyst stances, and macro regime score into actionable if-then trading
scenarios with mathematically grounded target profits and invalidation levels.

Grounds all baseline breakout and trap probabilities on empirical historical studies
(docs/analysis/) with exact sample counts (N >= 500) and confidence intervals.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import cot_signals, options
from .intraday import session_intraday_intelligence
from .levels import compute_session_reference_levels
from .pillars import compute_pillars, compute_regime_score
from .sentiment import compute_asset_sentiment_radar, compute_intraday_catalyst_radar

OPTIONS_PRODUCT_MAP: dict[str, str] = {
    "GC1": "OG",
    "SI1": "SO",
    "BTCUSD": "BTC",
    "CL1": "LO",
    "ES1": "ES",
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
) -> dict[str, Any] | None:
    """Generate an actionable probabilistic trading playbook with target profits and invalidation levels."""
    sym = symbol.strip().upper()

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

    ctx = ref["auction_context"]
    open_type = ctx.get("open_type", "OPEN_IN_VALUE")
    open_conviction = ctx.get("open_conviction", "MODERATE_CONVICTION")
    participant_activity = ctx.get("participant_activity", "ROTATIONAL_AUCTION")
    value_migration = ctx.get("value_migration", "INSIDE_VALUE")
    # 2. Fetch Intraday Price Action (VWAP and ATR)
    pa = session_intraday_intelligence(conn, sym, as_of=as_of)
    vwap = pa.get("vwap") if pa else None
    atr_14 = pa.get("atr_14") if pa else None
    if atr_14 is None or atr_14 <= 0.0:
        atr_14 = max(0.001, (pdh - pdl) * 0.5)

    volatility_ratio = pa.get("volatility_ratio", 1.0) if pa else 1.0
    vwap_state = pa.get("vwap_state", "NEUTRAL") if pa else "NEUTRAL"

    # 3. Fetch Fast Intraday Catalyst & Macro Swing Sentiment
    fast_cat = compute_intraday_catalyst_radar(conn, sym, window_hours=4, as_of=as_of)
    swing_sent = compute_asset_sentiment_radar(conn, sym, window_days=3, as_of=as_of)

    # 4. Fetch Domain 1 (Macro Context)
    try:
        pillars = compute_pillars(conn)
        macro_regime_score = compute_regime_score(pillars)
    except Exception:
        macro_regime_score = 0.0

    real_yield_row = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:DFII10' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    real_yield_10y = float(real_yield_row[0]) if real_yield_row else None

    curve_row = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:T10Y2Y' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    yield_curve_spread = float(curve_row[0]) if curve_row else None

    # 4b. Fetch Domain 2 (Institutional Flows & Positioning)
    opt_prod = OPTIONS_PRODUCT_MAP.get(sym)
    opt_snap = options.options_snapshot(conn, opt_prod) if opt_prod else None
    opt_pcr = opt_snap.get("pcr") if opt_snap else None
    opt_top_wall = opt_snap.get("top_wall") if opt_snap else None
    opt_max_pain = opt_snap.get("max_pain") if opt_snap else None

    cot_code = COT_CONTRACT_MAP.get(sym)
    cot_z = cot_signals._cot_zscore(conn, cot_code) if cot_code else None

    # 4c. Fetch Domain 4 Intermarket Microstructure (Live 1h Δ)
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

    # Multi-Domain Confluence & Intermarket Friction Detection
    friction_warnings = []
    tailwinds = []

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

    # 6. Build Actionable Scenarios
    scenarios = []

    # Open Type Gate Filtering (Dalton Rule: "Never fade an Open Drive")
    is_bullish_open_drive = open_type == "OPEN_DRIVE_BULLISH"
    is_bearish_open_drive = open_type == "OPEN_DRIVE_BEARISH"

    # SCENARIO 1: Trend Expansion / Value Acceptance (Long or Short)
    # Target: Dalton CVA 100% Measured Move if available, fallback to continuation median ATR
    if (fast_cat["net_stance_score"] >= 0.15 or is_bullish_open_drive) and (
        vwap is None or last_price >= vwap
    ):
        target_p = round(
            cva_measured_long
            if (cva_measured_long and cva_measured_long > last_price)
            else max(pdh, last_price + (cont_atr * atr_14)),
            2,
        )
        inval_p = round(
            min(val if val else last_price, vwap if vwap else last_price - (0.5 * atr_14)), 2
        )
        scenarios.append(
            {
                "id": "SCENARIO_EXPANSION_LONG",
                "title": "Trend Expansion Long (Catalyst Momentum + Value Acceptance)",
                "direction": "LONG",
                "trigger_condition": (
                    f"5m candle closes and holds above VAH ({vah}) with price staying above Session VWAP ({vwap})"
                ),
                "target_profit": target_p,
                "invalidation_level": inval_p,
                "risk_reward_ratio": (
                    round((target_p - last_price) / max(0.01, (last_price - inval_p)), 2)
                    if last_price > inval_p
                    else 1.5
                ),
                "invalidation_rationale": "Loss of Session VWAP or close back inside Value Area rejects continuation.",
                "empirical_support": {
                    "continuation_median_atr": cont_atr,
                    "target_derivation": (
                        f"Dalton {cva_name} 100% Measured Move ({target_p})"
                        if cva_measured_long
                        else f"max(PDH, last_price + {cont_atr} * ATR_14)"
                    ),
                    "open_type_gate": f"{open_type} ({open_conviction})",
                    "sample_weeks": emp.get("sample_weeks_high"),
                    "source": emp.get("source_doc"),
                },
            }
        )
    elif (fast_cat["net_stance_score"] <= -0.15 or is_bearish_open_drive) and (
        vwap is None or last_price <= vwap
    ):
        target_p = round(
            cva_measured_short
            if (cva_measured_short and cva_measured_short < last_price)
            else min(pdl, last_price - (cont_atr * atr_14)),
            2,
        )
        inval_p = round(
            max(vah if vah else last_price, vwap if vwap else last_price + (0.5 * atr_14)), 2
        )
        scenarios.append(
            {
                "id": "SCENARIO_EXPANSION_SHORT",
                "title": "Trend Expansion Short (Dovish/Bearish Catalyst + Value Acceptance)",
                "direction": "SHORT",
                "trigger_condition": (
                    f"5m candle closes and holds below VAL ({val}) with price staying below Session VWAP ({vwap})"
                ),
                "target_profit": target_p,
                "invalidation_level": inval_p,
                "risk_reward_ratio": (
                    round((last_price - target_p) / max(0.01, (inval_p - last_price)), 2)
                    if inval_p > last_price
                    else 1.5
                ),
                "invalidation_rationale": "Reclaim of Session VWAP or close back inside Value Area invalidates short.",
                "empirical_support": {
                    "continuation_median_atr": emp.get("low_continuation_median_atr", cont_atr),
                    "target_derivation": (
                        f"Dalton {cva_name} 100% Measured Move ({target_p})"
                        if cva_measured_short
                        else f"min(PDL, last_price - {cont_atr} * ATR_14)"
                    ),
                    "open_type_gate": f"{open_type} ({open_conviction})",
                    "sample_weeks": emp.get("sample_weeks_low"),
                    "source": emp.get("source_doc"),
                },
            }
        )

    # SCENARIO 2: Liquidity Sweep / Failed Auction (Trap Setup)
    # If price tested near PDH or above PDH
    if last_price >= pdh * 0.998 and not is_bullish_open_drive:
        sweep_inval = round(pdh + (0.20 * atr_14), 2)
        # Primary target: Unretested Naked POC below, fallback to Prior POC/PDC
        sweep_target = round(naked_poc_below if naked_poc_below else (poc if poc else pdc), 2)
        scenarios.append(
            {
                "id": "SCENARIO_PDH_SWEEP_REVERSAL",
                "title": "PDH Liquidity Sweep / Bull Trap Reversal",
                "direction": "SHORT",
                "trigger_condition": (
                    f"Price spikes above PDH ({pdh}) but fails to sustain; 5m/15m candle closes back below {pdh}"
                ),
                "target_profit": sweep_target,
                "invalidation_level": sweep_inval,
                "risk_reward_ratio": (
                    round((last_price - sweep_target) / max(0.01, (sweep_inval - last_price)), 2)
                    if sweep_inval > last_price
                    else 2.0
                ),
                "invalidation_rationale": f"Price accepts and sustains above {sweep_inval} (PDH + 0.20*ATR) proves breakout.",
                "empirical_support": {
                    "empirical_false_close_rate_pct": high_false_close_pct,
                    "target_magnet": (
                        f"Unretested Naked POC at {naked_poc_below}"
                        if naked_poc_below
                        else "Prior POC"
                    ),
                    "confidence_interval_95": emp.get("high_false_close_ci95"),
                    "sample_weeks": emp.get("sample_weeks_high"),
                    "mechanism": "Nearly half (44%-54%) of high breakouts fail to close outside prior range",
                    "source": emp.get("source_doc"),
                },
            }
        )
    # If price tested near PDL or below PDL (Gate check: do not fade bearish open drive)
    elif last_price <= pdl * 1.002 and not is_bearish_open_drive:
        sweep_inval = round(pdl - (0.20 * atr_14), 2)
        # Primary target: Unretested Naked POC above, fallback to Prior POC/PDC
        sweep_target = round(naked_poc_above if naked_poc_above else (poc if poc else pdc), 2)
        scenarios.append(
            {
                "id": "SCENARIO_PDL_SWEEP_REVERSAL",
                "title": "PDL Liquidity Sweep / Bear Trap Reversal",
                "direction": "LONG",
                "trigger_condition": (
                    f"Price pierces below PDL ({pdl}) but reclaims level; 5m/15m candle closes back above {pdl}"
                ),
                "target_profit": sweep_target,
                "invalidation_level": sweep_inval,
                "risk_reward_ratio": (
                    round((sweep_target - last_price) / max(0.01, (last_price - sweep_inval)), 2)
                    if last_price > sweep_inval
                    else 2.0
                ),
                "invalidation_rationale": f"Price breaks below {sweep_inval} (PDL - 0.20*ATR) confirms breakdown.",
                "empirical_support": {
                    "empirical_false_close_rate_pct": low_false_close_pct,
                    "target_magnet": (
                        f"Unretested Naked POC at {naked_poc_above}"
                        if naked_poc_above
                        else "Prior POC"
                    ),
                    "confidence_interval_95": emp.get("low_false_close_ci95"),
                    "sample_weeks": emp.get("sample_weeks_low"),
                    "mechanism": "Observed low false close frequency across historical database",
                    "source": emp.get("source_doc"),
                },
            }
        )

    # SCENARIO 3: Rotational Digestion inside Value Area
    if not scenarios:
        scenarios.append(
            {
                "id": "SCENARIO_ROTATIONAL_VALUE",
                "title": "Rotational Digestion Inside Value Area",
                "direction": "NEUTRAL_RANGE",
                "trigger_condition": f"Price remains bracketed between VAL ({val}) and VAH ({vah})",
                "target_profit": round(poc, 2) if poc else last_price,
                "invalidation_level": round(vah, 2) if vah else last_price,
                "risk_reward_ratio": 1.0,
                "invalidation_rationale": "Sustained bar close outside VAH or VAL transitions market into trend state.",
                "empirical_support": {
                    "mechanism": "Auction Market Theory 70% volume containment",
                    "source": "AMT Market Profile",
                },
            }
        )

    now_utc = datetime.now(UTC).isoformat(timespec="seconds")
    return {
        "symbol": sym,
        "as_of": now_utc,
        "last_price": round(last_price, 4),
        "reference_levels": levels,
        "price_action": {
            "vwap": round(vwap, 4) if vwap else None,
            "atr_14": round(atr_14, 4),
            "vwap_state": vwap_state,
            "volatility_ratio": round(volatility_ratio, 2),
        },
        "catalysts": {
            "intraday_fast_stance": fast_cat["stance"],
            "intraday_fast_score": fast_cat["net_stance_score"],
            "active_channels": fast_cat.get("active_catalysts", {}),
            "top_quotes": fast_cat.get("top_intraday_quotes", [])[:2],
            "swing_macro_stance": swing_sent["stance"],
            "swing_macro_score": swing_sent["net_stance_score"],
            "macro_regime_score": round(macro_regime_score, 2),
        },
        "multi_domain": {
            "domain_1_macro": {
                "macro_regime_score": round(macro_regime_score, 2),
                "tips_10y_real_yield": real_yield_10y,
                "yield_curve_spread_t10y2y": yield_curve_spread,
            },
            "domain_2_flows": {
                "options_pcr": round(opt_pcr, 3) if opt_pcr else None,
                "options_top_wall": opt_top_wall,
                "options_max_pain": opt_max_pain,
                "cot_positioning_3y_zscore": round(cot_z, 2) if cot_z is not None else None,
            },
            "domain_4_intermarket": {
                "us_10y_yield_1h_chg_pct": tnx_1h_chg,
                "dxy_dollar_1h_chg_pct": dxy_1h_chg,
                "semi_alpha_vs_spy_pct": semi_alpha,
            },
            "confluence_status": {
                "friction_warnings": friction_warnings,
                "tailwinds": tailwinds,
                "alignment_state": (
                    "FRICTION_DETECTED"
                    if friction_warnings
                    else ("STRONG_CONFLUENCE" if tailwinds else "NEUTRAL_BALANCED")
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
            "nearest_naked_poc_above": naked_poc_above,
            "nearest_naked_poc_below": naked_poc_below,
        },
        "scenarios": scenarios,
        "provenance": {
            "levels_derived_from": ref["provenance"],
            "empirical_dataset": emp.get("source_doc"),
            "calculated_at_utc": now_utc,
        },
    }
