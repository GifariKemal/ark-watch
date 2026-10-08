"""expectations.py — Cleveland Fed inflation-expectation reads.

Data basis (registry, fetched by cleve.py from the expectations workbook):
  - CLEVE:EXPINF_1Y / EXPINF_10Y  — model expected inflation, monthly (pct)
  - CLEVE:REALRATE_1Y / REALRATE_10Y — model expected real rates, monthly (pct)
  - FRED:T10YIE — 10Y breakeven, DAILY (market)
  - FRED:DFII10 — 10Y TIPS real yield, DAILY (market)

Two QUANT reads, each a market-minus-model spread:
  - IRP (inflation risk premium proxy) = T10YIE − EXPINF_10Y. The breakeven
    prices expected inflation PLUS compensation for inflation uncertainty;
    the Cleveland model strips expectations using surveys+SWU+market. The
    residual ≈ what the market pays for inflation insurance.
  - TIPS liquidity premium = DFII10 − REALRATE_10Y. TIPS are LESS liquid
    than nominals, so TIPS yields sit ABOVE the model's "true" real rate by
    a liquidity premium (equivalently: TIPS PRICES are discounted). The
    spread ≈ that technical distortion — a POSITIVE value means TIPS are
    cheap vs fundamentals (historically a flight-to-quality footprint).

Mixed cadence is documented, not hidden: the market legs are daily, the
model legs monthly (published early following month). BOTH legs are
stale-checked — model > MODEL_STALE_DAYS (the brief _STALE_DAYS['M']
convention, imported — one source, no drift) and market legs > the D-window
— and each spread segment is suppressed when its own legs disagree on
freshness.

Degradation: every public function no-ops (None / 0) when a leg is missing
(observations absent, tables untouched) — pre-workbook DBs skip the layer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

from .brief import _STALE_DAYS

# State-bucket thresholds (bp) — display labels only, no alert consumes these
# yet; [KEPUTUSAN: placeholder 2026-09-04 — calibrate from the 536-month
# history's spread distribution before wiring any trigger].
IRP_PREMIUM_BP = 25.0  # above = PREMIUM
IRP_NEGATIVE_BP = -25.0  # below = NEGATIVE
TIPS_LIQ_FLOOR_BP = 10.0  # |spread| floor before the TIPS-liq read renders

# Shared staleness windows (single source: brief._STALE_DAYS) — model legs
# are M-frequency, market legs D-frequency.
MODEL_STALE_DAYS = _STALE_DAYS["M"]
MARKET_STALE_DAYS = _STALE_DAYS["D"]

EXPINF_10Y = "CLEVE:EXPINF_10Y"
EXPINF_1Y = "CLEVE:EXPINF_1Y"
REALRATE_10Y = "CLEVE:REALRATE_10Y"
IRP_10Y_MODEL = "CLEVE:IRP_10Y_MODEL"
T10YIE = "FRED:T10YIE"
DFII10 = "FRED:DFII10"


def _latest(conn: sqlite3.Connection, sid: str) -> tuple[str, float] | None:
    row = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? AND vintage_ts='realtime' "
        "AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return None if row is None else (row[0], float(row[1]))


def _age_days(ts: str) -> int:
    return (datetime.now(UTC).date() - date.fromisoformat(str(ts)[:10])).days


def inflation_risk_premium(conn: sqlite3.Connection) -> dict | None:
    """The two market-minus-model spreads, in basis points.

    {irp_10y_bp, tips_liq_10y_bp, expinf_1y, expinf_10y, model_ts,
    model_stale, market_stale, be_ts, dfii_ts} — None when the 10Y model
    leg is missing. A spread is None when ITS OWN legs are stale-mismatched:
    mixing a stale market leg with a fresh model month (or vice versa)
    would state a vintage-mismatched number (each spread requires BOTH its
    legs inside their freshness windows).
    """
    m10 = _latest(conn, EXPINF_10Y)
    if m10 is None:
        return None
    be = _latest(conn, T10YIE)
    rr = _latest(conn, REALRATE_10Y)
    dfii = _latest(conn, DFII10)
    e1 = _latest(conn, EXPINF_1Y)
    irp_model = _latest(conn, IRP_10Y_MODEL)
    model_stale = _age_days(m10[0]) > MODEL_STALE_DAYS
    be_fresh = be is not None and _age_days(be[0]) <= MARKET_STALE_DAYS
    dfii_fresh = dfii is not None and _age_days(dfii[0]) <= MARKET_STALE_DAYS
    irp = (
        None if (be is None or model_stale or not be_fresh) else round((be[1] - m10[1]) * 100.0, 1)
    )
    tips = (
        None
        if (rr is None or dfii is None or model_stale or not dfii_fresh)
        else round((dfii[1] - rr[1]) * 100.0, 1)
    )
    return {
        "irp_10y_bp": irp,
        "tips_liq_10y_bp": tips,
        # the model's OWN decomposition (different definition: no TIPS
        # technicals; pct -> bp for one-line comparability with irp_10y_bp)
        "irp_model_10y_bp": (
            None
            if irp_model is None or _age_days(irp_model[0]) > MODEL_STALE_DAYS
            else round(irp_model[1] * 100.0, 1)
        ),
        "expinf_1y": None if e1 is None else e1[1],
        "expinf_10y": m10[1],
        "model_ts": m10[0],
        "model_stale": model_stale,
        "market_stale": not (be_fresh and dfii_fresh),
        "be_pct": None if be is None else be[1],
        "be_ts": None if be is None else str(be[0])[:10],
        "dfii_pct": None if dfii is None else dfii[1],
        "dfii_ts": None if dfii is None else str(dfii[0])[:10],
    }


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def store_expectations_signals(conn: sqlite3.Connection) -> int:
    """Persist 'irp_10y' to computed_signals (audit trail; soma convention).

    ts = the MODEL leg's month (the slower cadence governs when the number
    changes — a new daily breakeven alone does not advance the row's date
    key, matching how the value is actually computed).
    """
    irp = inflation_risk_premium(conn)
    if irp is None or irp["irp_10y_bp"] is None:
        return 0
    if irp["irp_10y_bp"] > IRP_PREMIUM_BP:
        state = "PREMIUM"
    elif irp["irp_10y_bp"] < IRP_NEGATIVE_BP:
        state = "NEGATIVE"
    else:
        state = "FLAT"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                "irp_10y",
                irp["model_ts"],
                now,
                now,
                irp["irp_10y_bp"],
                state,
                json.dumps(
                    {
                        "unit": "bp",
                        "definition": "T10YIE − CLEVE:EXPINF_10Y",
                        "expinf_10y_pct": _r(irp["expinf_10y"], 2),
                        "expinf_1y_pct": _r(irp["expinf_1y"], 2),
                        "be_pct": _r(irp.get("be_pct"), 2),
                        "be_ts": irp.get("be_ts"),
                        "dfii_pct": _r(irp.get("dfii_pct"), 2),
                        "dfii_ts": irp.get("dfii_ts"),
                        "irp_model_10y_bp": irp.get("irp_model_10y_bp"),
                        "tips_liq_10y_bp": irp["tips_liq_10y_bp"],
                    }
                ),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return 1


def expectations_brief_line(conn: sqlite3.Connection) -> str | None:
    """Render the expectations segment (next to the Policy/CVOL cluster).

    Exp: 1y 2.4% · 10y 2.5% · IRP 10y +2bp · TIPS liq +14bp

    Each segment degrades on its own. Spread segments are suppressed (not
    printed stale) when their legs' vintages mismatch — the level reads
    still render; the model-month marker shows which vintage they carry.
    Returns None when the 10Y model leg is missing entirely.
    """
    e = inflation_risk_premium(conn)
    if e is None:
        return None
    segs = []
    if e["expinf_1y"] is not None:
        segs.append(f"1y {e['expinf_1y']:.1f}%")
    segs.append(f"10y {e['expinf_10y']:.1f}%")
    if e["irp_10y_bp"] is not None:
        segs.append(f"IRP 10y {e['irp_10y_bp']:+.0f}bp")
    # the model's own premium decomposition — a DIFFERENT definition (no TIPS
    # technicals); shown as 'mdl' so the two are never read as one number
    if e.get("irp_model_10y_bp") is not None:
        segs.append(f"IRP mdl {e['irp_model_10y_bp']:+.0f}bp")
    if e["tips_liq_10y_bp"] is not None and abs(e["tips_liq_10y_bp"]) >= TIPS_LIQ_FLOOR_BP:
        segs.append(f"TIPS liq {e['tips_liq_10y_bp']:+.0f}bp")
    if e["model_stale"]:
        segs.append(f"⚠(model {str(e['model_ts'])[:7]} stale)")
    return "Exp: " + " · ".join(segs)
