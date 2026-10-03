"""pillars.py — pillars A-F, regime score, quadrant, dollar smile, identity checks."""

from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

from ..transforms.core import (
    annualize_3m,
    momentum,
    percentile_rank,
    state_direction,
    zscore,
)

# Inflation-overlay thresholds come from config (params_block_c.yaml), not
# hard-coded here
try:
    from ..config import load_params_block_c

    _PBC = load_params_block_c()
except Exception:
    _PBC = {}
REACCEL_LOW_PCT = float(_PBC.get("reaccel_low_pct", 3.0))
CONFIRM_PCT = float(_PBC.get("confirm_pct", 5.0))  # Neville-Harvey confirmation band
COOLING_PCT = float(_PBC.get("cooling_pct", 1.0))

# Signal-engine thresholds (regime labels among them) come from
# params_signals.yaml — the same file cot_signals/surprise/watcher read, so a
# threshold change lands everywhere at once. Imported by brief.py, cot_signals.py
# and qa/f4.py — the regime label must be identical on every surface.
try:
    from ..config import load_params_signals

    _PS = load_params_signals()
except Exception:
    _PS = {}
REGIME_RISK_ON = float(_PS.get("regime_risk_on", 0.3))
REGIME_RISK_OFF = float(_PS.get("regime_risk_off", -0.3))

WIB = ZoneInfo("Asia/Jakarta")
ET = ZoneInfo("America/New_York")

# Regime score weights per pillar
PILLAR_WEIGHTS = {"A": 0.20, "B": 0.20, "C": 0.15, "D": 0.15, "E": 0.15, "F": 0.15}


def _values(conn: sqlite3.Connection, series_id: str, limit: int = 1600) -> list[float]:
    # queries.py owns the SQL read pattern
    from ..queries import series_values

    return series_values(conn, series_id, limit)


def _latest(conn: sqlite3.Connection, series_id: str) -> tuple[str, float] | None:
    from ..queries import latest_observation

    return latest_observation(conn, series_id)


def _pct(v: float | None, fmt: str = "{:.2f}") -> str:
    return fmt.format(v) if v is not None else "N/A"


class _RealtimeReader:
    """Default reader: realtime values (via queries.py)."""

    def __init__(self, conn):
        self._conn = conn

    def values(self, sid: str, limit: int = 1600) -> list[float]:
        return _values(self._conn, sid, limit)

    def latest(self, sid: str):
        return _latest(self._conn, sid)


def _identity_checks(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Daily identity checks: DFII≈DGS-BE · Sahm · net-liq · 2-way slope."""
    results: list[tuple[str, str, str]] = []

    # 1. DFII10 ≈ DGS10 − T10YIE (±25bps)
    dffii = _latest(conn, "FRED:DFII10")
    dgs10 = _latest(conn, "FRED:DGS10")
    t10yie = _latest(conn, "FRED:T10YIE")
    if all([dffii, dgs10, t10yie]):
        implied_ry = dgs10[1] - t10yie[1]
        diff = abs(dffii[1] - implied_ry)
        ok = "✓" if diff <= 0.25 else f"✗ Δ={diff:.3f}"
        results.append(("DFII≈DGS−BE", ok, f"DFII10={dffii[1]:.2f} vs DGS−BE={implied_ry:.2f}"))

    # 2. Sahm formula (±0.05)
    sahm = _latest(conn, "FRED:SAHMREALTIME")
    if sahm:
        ok = "✓" if abs(sahm[1]) < 0.5 else f"⚠ {sahm[1]:.3f} (>0.5 trigger!)"
        results.append(("Sahm Rule", ok, f"value={sahm[1]:.3f}"))

    # 3. Net-liq identity: WALCL ≈ WRESBAL + RRP + TGA + other
    walcl = _latest(conn, "FRED:WALCL")
    wresbal = _latest(conn, "FRED:WRESBAL")
    if all([walcl, wresbal]):
        diff = walcl[1] - wresbal[1]
        results.append(
            (
                "BS vs Reserves",
                "✓" if diff > 0 else "✗",
                f"WALCL−WRESBAL={diff / 1000:.0f}B (=RRP+TGA+other)",
            )
        )

    # 4. 2s10s slope — report the actual state instead of a hardcoded check
    #    mark: an inverted curve must not render green. The par-Treasury path
    #    is not wired yet, so this stays a single-source check.
    dgs10_v = _latest(conn, "FRED:DGS10")
    dgs2_v = _latest(conn, "FRED:DGS2")
    if dgs10_v and dgs2_v:
        slope = dgs10_v[1] - dgs2_v[1]
        ok = "✓" if slope >= 0 else "⚠ INVERTED"
        results.append(("2s10s Slope", ok, f"{slope:+.2f}%"))

    return results


def compute_pillars(conn: sqlite3.Connection, *, reader=None) -> dict[str, dict]:
    """Compute z + state per all six pillars.

    `reader` (optional protocol with .values/.latest) enables point-in-time
    reads (vintage first-print, as-of) without duplicating the pillar logic;
    the default is the plain realtime reader."""
    if reader is None:
        reader = _RealtimeReader(conn)
    out: dict[str, dict] = {}

    # A — Policy
    dff_vals = reader.values("FRED:DFF")
    curve = reader.values("FRED:T10Y3M")
    dff_m = momentum(dff_vals, 20) if len(dff_vals) > 20 else None
    out["A"] = {
        "parts": ["FRED:DFF", "FRED:T10Y3M"],
        "label": "Policy",
        "state": state_direction(dff_m, 0.05),
        "detail": f"DFF {_pct(dff_vals[-1] if dff_vals else None)}%",
        "z": zscore(curve) if curve else None,
    }

    # B — Real Yield
    dfii = reader.values("FRED:DFII10")
    ry_m = momentum(dfii, 20) if len(dfii) > 20 else None
    # Threshold ±0.05pp = 5bp/20d, consistent with policy-rates pillar; a much smaller
    # threshold (0.001 = 0.1bp) leaves FLAT dead while the detail line
    # renders a contradictory 'RISING (+0bps)'.
    _RY_THR = 0.05
    if ry_m is not None:
        ry_state = "FALLING" if ry_m < -_RY_THR else ("RISING" if ry_m > _RY_THR else "FLAT")
    else:
        ry_state = "INSUFFICIENT"
    # ROUND-6 P0: NEGATED — rising real yields are a headwind for risk
    # assets (the brief's own XAUUSD line reads 'RY↑ headwind'), but the
    # raw z fed the POSITIVE-weighted score the other way: stress/RY-up
    # pushed the score toward RISK-ON (11/12 live labels biased). The
    # score convention is 'higher = more risk-on'; both B and F z must
    # express 'supportive of risk'. ROUND-7: guard on the z RESULT, not the
    # input list — zscore returns None below min_obs and unary-minus on
    # None crashed the Sunday f4 replay at 66/261 months.
    _zb = zscore(dfii)
    out["B"] = {
        "parts": ["FRED:DFII10"],
        "label": "RealYield",
        "state": ry_state,
        "detail": f"DFII10 {_pct(dfii[-1] if dfii else None)}% ({ry_m * 100:.0f}bps/20d)"
        if ry_m is not None
        else "N/A",
        "z": -_zb if _zb is not None else None,
    }

    # C — Inflation (overlay)
    cpi = reader.values("FRED:CPIAUCSL", 120)
    # 3m-annualized = ((1+m1)(1+m2)(1+m3))^4 - 1
    if len(cpi) >= 4:
        try:
            m1 = (cpi[-1] / cpi[-2] - 1) * 100
            m2 = (cpi[-2] / cpi[-3] - 1) * 100
            m3 = (cpi[-3] / cpi[-4] - 1) * 100
            ann3m = annualize_3m(m1 / 100, m2 / 100, m3 / 100) * 100
            # Level from 3m-ann; confirmation = 3m-ann OR YoY >= confirm_pct;
            # the render must name the measure that fired
            yoy = ((cpi[-1] / cpi[-13]) - 1) * 100 if len(cpi) >= 13 else None
            if ann3m >= CONFIRM_PCT:
                c_state = "REACCEL(high)"
            elif ann3m >= REACCEL_LOW_PCT:
                c_state = "REACCEL(low)"
            elif ann3m < COOLING_PCT:
                c_state = "COOLING"
            else:
                c_state = "STABLE"
            c_detail = f"3m-ann {ann3m:.1f}%"
            if yoy is not None:
                c_detail += f" · YoY {yoy:.1f}%"
                trig = []
                if ann3m >= CONFIRM_PCT:
                    trig.append("3m-ann")
                if yoy >= CONFIRM_PCT:
                    trig.append("YoY")
                if trig:
                    c_detail += f" ⚠CONFIRM({'/'.join(trig)})"
        except (ZeroDivisionError, IndexError):
            c_state, c_detail = "INSUFFICIENT", "N/A"
    else:
        c_state, c_detail = "INSUFFICIENT", "N/A"
    out["C"] = {
        "parts": ["FRED:CPIAUCSL"],
        "label": "Inflation",
        "state": c_state,
        "detail": c_detail,
        # Use the YoY rate for the inflation z-score, not the index level.
        # Level z-scores of monotonically increasing series are pinned near
        # +sqrt(3) regardless of actual inflation, creating a permanent +0.26
        # bias in the regime score.
        "z": (
            zscore([cpi[i] / cpi[i - 12] - 1 for i in range(12, len(cpi))], window=48)
            if len(cpi) >= 60
            else None
        ),  # monthly YoY: 5y = 48 valid obs
    }

    # D — Growth
    icsa = reader.values("FRED:ICSA", 300)
    ccsa = reader.values("FRED:CCSA", 300)
    d_parts = ["FRED:ICSA", "FRED:GDPNOW"]
    if ccsa:
        d_parts.append("FRED:CCSA")
    neworder = reader.values("FRED:NEWORDER", 120)
    if neworder:
        d_parts.append("FRED:NEWORDER")

    d_state = state_direction(momentum(icsa, 4), 2000) if icsa else "INSUFFICIENT"
    # falling claims = tight labor = good growth → invert the direction
    if d_state == "FALLING":
        d_state = "ACCELERATING"
    elif d_state == "RISING":
        d_state = "DECELERATING"
    gdpnow_v = reader.latest("FRED:GDPNOW")

    icsa_z = zscore([-v for v in icsa], window=260) if icsa else None
    ccsa_z = zscore([-v for v in ccsa], window=260) if ccsa else None
    if icsa_z is not None and ccsa_z is not None:
        d_z = round((icsa_z + ccsa_z) / 2.0, 4)
    elif icsa_z is not None:
        d_z = icsa_z
    elif ccsa_z is not None:
        d_z = ccsa_z
    else:
        d_z = None

    detail_parts = []
    if icsa:
        detail_parts.append(f"ICSA {icsa[-1] / 1000:.0f}K")
    if ccsa:
        detail_parts.append(f"CCSA {ccsa[-1] / 1000:.0f}K")
    if gdpnow_v:
        detail_parts.append(f"GDPNow {gdpnow_v[1]:.1f}%")
    d_detail = " · ".join(detail_parts) if detail_parts else "N/A"

    out["D"] = {
        "parts": d_parts,
        "label": "Growth",
        "state": d_state,
        "detail": d_detail,
        "z": d_z,
    }

    # E — Liquidity
    walcl = reader.values("FRED:WALCL", 300)
    dcpf3m = reader.values("FRED:DCPF3M", 300)
    e_parts = ["FRED:WALCL"]
    if dcpf3m:
        e_parts.append("FRED:DCPF3M")
    liq_m = momentum(walcl, 4) if len(walcl) > 4 else None  # 4 weeks
    if liq_m is not None:
        e_state = "EXPANDING" if liq_m > 0 else "CONTRACTING"
    else:
        e_state = "INSUFFICIENT"

    e_detail = f"BS ${walcl[-1] / 1_000_000:.2f}T" if walcl else "N/A"
    if dcpf3m:
        e_detail += f" · CP {dcpf3m[-1]:.2f}%"

    out["E"] = {
        "parts": e_parts,
        "label": "Liquidity",
        "state": e_state,
        "detail": e_detail,
        # Use the 4-week change (flow) for the z-score, not the WALCL level:
        # the trending (QT) level pins z near −1.5 constantly, a permanent
        # −0.22 bias in the regime score.
        "z": (
            zscore([walcl[i] - walcl[i - 4] for i in range(4, len(walcl))], window=260)
            if len(walcl) >= 64
            else None
        ),  # weekly: 5y = 260 obs
    }
    # F — Stress
    hy = reader.values("FRED:BAMLH0A0HYM2", 800)  # 3y window (BAML truncated)
    # VIX is not fetched here: no pillar uses it (the VIX z-score lives in
    # the watcher cooldown), and VIX still reaches the brief via the
    # overnight-changes section and the watcher.
    hy_pct = percentile_rank(hy, 756) if hy else None  # 3y percentile
    _zf = zscore(hy, 756)
    if hy_pct is not None:
        f_state = "CALM" if hy_pct < 30 else ("ELEVATED" if hy_pct < 70 else "STRESSED")
    else:
        f_state = "INSUFFICIENT"
    out["F"] = {
        "parts": ["FRED:BAMLH0A0HYM2"],
        "label": "Stress",
        "state": f_state,
        "detail": f"HY p{hy_pct:.0f}" if hy_pct is not None else "N/A",
        # ROUND-6 P0: NEGATED — widening HY spreads = credit stress = risk
        # OFF, but the raw z pushed the score toward RISK-ON (same class as
        # pillar B). z here expresses 'credit calm-ness' so the positive
        # weights read correctly. ROUND-7: guard on the z result (see B).
        "z": -_zf if _zf is not None else None,
    }

    return out


def compute_regime_score(pillars: dict[str, dict]) -> float:
    """Weighted average z → score −2…+2."""
    total_z = 0.0
    total_w = 0.0
    for blk, w in PILLAR_WEIGHTS.items():
        z = pillars.get(blk, {}).get("z")
        if z is not None:
            total_z += z * w
            total_w += w
    if total_w == 0:
        return 0.0
    score = total_z / total_w
    return max(-2.0, min(2.0, score))


def compute_quadrant(pillars: dict[str, dict]) -> str:
    growth = pillars.get("D", {}).get("state", "?")
    inflation = pillars.get("C", {}).get("state", "?")
    # FLAT/INSUFFICIENT growth must not fall through to "-" (contraction):
    # the quadrant would be misleading when data is lacking → N/A instead.
    if "INSUFFICIENT" in growth or "INSUFFICIENT" in inflation:
        return "N/A (insufficient data)"
    if "ACCELERATING" in growth or "NEUTRAL" in growth or "LOW" in growth:
        g = "+"
    elif "FLAT" in growth:
        return "N/A (growth flat)"
    else:
        g = "-"
    if "REACCEL" in inflation or "HIGH" in inflation:
        i = "+"
    else:
        i = "-"
    return {
        ("+", "+"): "Reflation (growth↑ inflation↑)",
        ("+", "-"): "Disinflationary Growth (growth↑ inflation↓)",
        ("-", "+"): "Stagflation (growth↓ inflation↑)",
        ("-", "-"): "Deflationary Contraction (growth↓ inflation↓)",
    }.get((g, i), "Mixed")


def compute_dollar_smile(conn: sqlite3.Connection) -> str:
    """Dollar 20d trend label + the input's AS-OF date.

    ROUND-6: DTWEXBGS publishes weekly-ish (Treasury H.10, lagged) — the
    label rendered from data frozen at 2026-09-11 for a week+ with no
    marker. Callers append '(as of MM-DD)' and a ⚠ when the leg goes stale;
    returning the ts makes the honesty mechanical instead of hoping each
    caller queries it separately."""
    from ..queries import latest_observation

    dtw = _values(conn, "FRED:DTWEXBGS", 60)
    lv = latest_observation(conn, "FRED:DTWEXBGS")
    asof = lv[0][5:] if lv else None  # MM-DD
    m20 = momentum(dtw, 20) if dtw and len(dtw) > 20 else None
    if m20 is None:
        return "N/A"
    pct_change = m20 / dtw[-21] * 100 if len(dtw) > 21 and dtw[-21] != 0 else 0
    label = "STRONG" if pct_change > 0.4 else ("WEAK" if pct_change < -0.4 else "FLAT")
    stale = ""
    if lv:
        from datetime import UTC, datetime

        age = (datetime.now(UTC).date() - datetime.fromisoformat(lv[0][:10]).date()).days
        if age > 12:  # H.10 weekly + holiday tolerance
            stale = " ⚠stale"
    return f"{label} (as of {asof}){stale}" if asof else label
