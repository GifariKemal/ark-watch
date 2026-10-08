"""dealers.py — Primary Dealer Positions Survey signals (NY Fed pd endpoints).

Data basis (migration v9, harvested Thursdays per PLAN-NYFED-FULL §1.6):
pd_positions — curated keyids from the 1,539-series weekly survey, Wednesday
positions in $MILLIONS (value_musd), carrying methodology series-breaks
(SBP2001/SBP2013/SBN2024).

Units: the table stores $millions; $B figures are converted at the boundary
(M_TO_B = 1e-3, the soma.py WALCL convention). z is dimensionless.

Population = primary dealers — the cash-Treasury intermediaries we already
know from repo-specials and the OI-wall. That makes this the THIRD
positioning angle in the brief (COT = futures crowd, Opt = per-strike
options, DLR = dealer balance sheet) and the one that reads balance-sheet
capacity: dealers shedding inventory fast = intermediation tightening.

Methodology breaks: the survey restated its methodology in 2001/2013/2024 —
levels are NOT comparable across a break. z therefore uses ONLY the
observations of the CURRENT seriesbreak (the break of the latest row), min
DEALER_Z_MIN_OBS obs else z=None (honest None, never a cross-break mix). The
4-week Δ is likewise searched within the same break — a restatement must not
fabricate a jump.

Degradation: every public function no-ops ({}, 0, None) when pd_positions is
missing or empty (_soma_tables_ready convention — pre-v9 DBs skip the layer).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

# Thresholds come from params_signals.yaml; the literals are only the
# unreadable-config fallback (watcher/options.py pattern). Parsed inside the
# guard so a bad yaml value cannot raise at import time and silently disable
# the trigger.
try:
    from ..config import load_params_signals

    _PS = load_params_signals()
    DEALER_STRESS_Z = float(_PS.get("dealer_stress_z", 1.5))
except Exception:
    _PS = {}
    DEALER_STRESS_Z = 1.5

# pd_positions stores $millions → $B at the boundary (WALCL convention).
M_TO_B = 1e-3

# Curated survey keyids (the fetch layer harvests this same list — 5 of the
# 1,539 series; financing PDFTR/PDFTD variants can join later). Dict order =
# brief display order: UST first (the headline leg + the stress trigger).
PD_KEYIDS: dict[str, str] = {
    "PDPOSGST-TOT": "UST",  # Treasuries ex-TIPS
    "PDPOSMBS-TOT": "MBS",
    "PDPOSCS-TOT": "Corp",
    "PDPOSFGS-TOT": "Agency",  # agencies ex-MBS
    "PDPOSSMGO-TOT": "Muni",
}
UST_KEYID = "PDPOSGST-TOT"

# z window = all observations of the current seriesbreak. 26 obs ≈ half a
# year of weekly surveys — below that a z would mostly measure the window's
# own noise (cot_z_min_weeks uses 60 for the same honesty, but the survey's
# current break SBN2024 is itself young; 26 keeps the trigger armed without
# stretching across a break).
DEALER_Z_MIN_OBS = 26

# computed_signals state boundary (a LEANING, deliberately looser than the
# ⚠ flag's dealer_stress_z): UST z below −1 = dealers drawing down inventory
# = risk-off leaning; above +1 = risk-on leaning.
DEALER_STATE_Z = 1.0

# The 4-week Δ compares the latest obs with the one closest to 28 days back
# (weekly cadence → ±1 week tolerance; a fixed 4-obs offset would silently
# become 5 weeks after one missed harvest).
DELTA_4W_TARGET_DAYS = 28
DELTA_4W_MIN_DAYS = 21
DELTA_4W_MAX_DAYS = 35


def _pd_table_ready(conn: sqlite3.Connection) -> bool:
    """True when pd_positions exists (pre-v9 DBs skip the whole layer)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pd_positions'"
    ).fetchone()
    return bool(row and row[0] == 1)


def _z_within(values: list[float]) -> float | None:
    """z of the LAST value vs the window (population σ, watcher VIX-z style)."""
    if len(values) < DEALER_Z_MIN_OBS:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = var**0.5
    return None if std == 0 else (values[-1] - mean) / std


def _delta_4w(
    window: list[tuple[str, str | None, float]],
) -> tuple[float | None, float | None, float | None]:
    """(delta_4w, delta_4w_pct, base) of the latest obs vs the obs closest to
    28 days back, within the same break. (None, None, None) when no obs sits
    in the ±1-week 4-week band (young series / missed harvests)."""
    latest_d, _b, latest_v = window[-1]
    latest = date.fromisoformat(latest_d)
    best: tuple[int, float] | None = None  # (|gap − 28|, base value)
    for asof, _bb, v in window[:-1]:
        gap = (latest - date.fromisoformat(asof)).days
        if DELTA_4W_MIN_DAYS <= gap <= DELTA_4W_MAX_DAYS:
            dist = abs(gap - DELTA_4W_TARGET_DAYS)
            if best is None or dist < best[0]:
                best = (dist, v)
    if best is None:
        return None, None, None
    base = best[1]
    delta = latest_v - base
    # abs(base): a net-SHORT class (base < 0) must not flip the percentage's
    # sign — 'short shrinking toward zero' is a POSITIVE move, and the
    # dealer_stress trigger reads this pct
    pct = None if base == 0 else delta / abs(base) * 100.0
    return delta, pct, base


def dealers_snapshot(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-keyid positioning snapshot at the keyid's latest survey week.

    {keyid: {asofdate, seriesbreak, value_musd, z, delta_4w_musd,
             delta_4w_pct, base_4w_musd, n_obs}}

    - z is computed WITHIN the current seriesbreak only (window = all obs of
      that break; < DEALER_Z_MIN_OBS obs → z=None) and never mixes breaks.
    - delta_4w is likewise searched within the same break (a restatement must
      not fabricate a jump); pct uses the 4w-ago value as the base.
    - n_obs = the within-break window size feeding the z.

    Returns {} when pd_positions is missing or empty (degradable, pre-v9).
    """
    if not _pd_table_ready(conn):
        return {}
    out: dict[str, dict] = {}
    for keyid in PD_KEYIDS:
        rows = conn.execute(
            "SELECT asofdate, seriesbreak, value_musd FROM pd_positions "
            "WHERE keyid=? AND value_musd IS NOT NULL ORDER BY asofdate",
            (keyid,),
        ).fetchall()
        if not rows:
            continue
        cur_break = rows[-1][1]
        # Python-side groupby on the break: SQL 'seriesbreak = ?' would drop
        # NULL-break rows (a NULL break is its own group here, not a mix).
        window = [r for r in rows if r[1] == cur_break]
        values = [r[2] for r in window]
        delta, pct, base = _delta_4w(window)
        out[keyid] = {
            "asofdate": rows[-1][0],
            "seriesbreak": cur_break,
            "value_musd": rows[-1][2],
            "z": _z_within(values),
            "delta_4w_musd": delta,
            "delta_4w_pct": pct,
            "base_4w_musd": base,
            "n_obs": len(window),
        }
    return out


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def store_dealer_signals(conn: sqlite3.Connection) -> int:
    """Persist the weekly dealer signal to computed_signals (audit trail).

    One row, signal_id='dealer_positions', ts = the latest asofdate across
    keyids → natural weekly dedup via INSERT OR REPLACE (store_options
    convention). value = UST z (None when under the min-obs floor), state =
    the risk leaning (UST z < −DEALER_STATE_Z → 'RISK_OFF' …), inputs_json
    carries every keyid's value/z/Δ4w (per-keyid detail lives in the JSON —
    the schema stores one value per signal_id, soma convention).

    Returns 0 when there is no pd_positions data — a no-op, not an error.
    """
    snap = dealers_snapshot(conn)
    if not snap:
        return 0
    ts = max(s["asofdate"] for s in snap.values())
    now = datetime.now(UTC).isoformat(timespec="seconds")
    ust = snap.get(UST_KEYID)
    z = None if ust is None else ust.get("z")
    if z is None:
        state = "N/A"
    elif z < -DEALER_STATE_Z:
        state = "RISK_OFF"  # dealers drawing down UST inventory fast
    elif z > DEALER_STATE_Z:
        state = "RISK_ON"
    else:
        state = "NEUTRAL"
    keyids = {}
    for keyid, label in PD_KEYIDS.items():
        s = snap.get(keyid)
        if not s:
            continue
        keyids[label] = {
            "asofdate": s.get("asofdate"),
            "value_b": _r(None if s["value_musd"] is None else s["value_musd"] * M_TO_B),
            "z": _r(s.get("z")),
            "delta_4w_b": _r(
                None if s.get("delta_4w_musd") is None else s["delta_4w_musd"] * M_TO_B
            ),
            "delta_4w_pct": _r(s.get("delta_4w_pct"), 2),
            "n_obs": s.get("n_obs"),
            "seriesbreak": s.get("seriesbreak"),
        }
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                "dealer_positions",
                ts,
                now,
                now,
                _r(z),
                state,
                json.dumps({"unit": "z", "keyids": keyids}),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return 1


def dealers_brief_line(conn: sqlite3.Connection, stress_z: float | None = None) -> str | None:
    """Render the one-line dealer positioning segment (next to the Opt: line).

    DLR: UST $436B z-1.7 ⚠ · MBS $116B z+0.4 · Corp $88B z+0.2 (as of 08-19)

    Segments render only for keyids with data; z renders only when the
    within-break window cleared DEALER_Z_MIN_OBS (honest omission, not z=0);
    ⚠ when |z| ≥ dealer_stress_z (the z-based BRIEF flag — the watcher's
    dealer_stress trigger separately reads delta_4w_pct vs its own threshold).
    A keyid whose survey week lags the freshest keyid by >8d is marked
    'stale' (a partially failed Thursday harvest must not pass last week's
    number off as current — soma stale-suffix convention). Values are $B,
    converted from $M at the boundary; a net-short class renders '-$12B'.
    Returns None when there is no pd_positions data (pre-v9 DB or missed
    Thursday harvest — degradable).
    """
    zflag = DEALER_STRESS_Z if stress_z is None else stress_z
    snap = dealers_snapshot(conn)
    if not snap:
        return None
    newest = max(s["asofdate"] for s in snap.values())
    segs: list[str] = []
    for keyid, label in PD_KEYIDS.items():
        s = snap.get(keyid)
        if not s:
            continue
        v_b = s["value_musd"] * M_TO_B
        seg = f"{label} {'-' if v_b < 0 else ''}${abs(v_b):.0f}B"
        if s.get("z") is not None:
            seg += f" z{s['z']:+.1f}"
            if abs(s["z"]) >= zflag:
                seg += " ⚠"
        age = (date.fromisoformat(newest) - date.fromisoformat(s["asofdate"])).days
        if age > 8:  # weekly survey: >8d behind the freshest keyid = left behind
            seg += " (stale)"
        segs.append(seg)
    if not segs:
        return None
    return f"DLR: {' · '.join(segs)} (as of {newest[5:]})"
