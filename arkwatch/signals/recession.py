"""recession.py — the recession triangulation: model / survey / rule.

Three INDEPENDENT recession gauges, one line:
  - CLEVE:RECPROB       — yield-curve MODEL probability (monthly)
  - PHILLY:ANXIOUS      — professional forecasters' SURVEY probability for
                          next quarter (mean probability of negative QoQ GDP)
  - FRED:SAHMREALTIME   — the Sahm RULE trigger (3m-avg unemployment vs its
                          12m low; >= 0.50 = recession onset historically)

Methodological independence is the point: a curve signal (market), a survey
signal (humans), and a labor signal (statistic) rarely fire together on
noise. All three being elevated simultaneously is the strongest single
macro warning this system can print.

Degradation: each leg renders alone; the whole line disappears when ALL are
missing. Freshness per frequency (M/Q/M windows from brief._STALE_DAYS).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from .brief import _STALE_DAYS

RECPROB = "CLEVE:RECPROB"
ANXIOUS = "PHILLY:ANXIOUS"
SAHM = "FRED:SAHMREALTIME"

# Elevation bars for the state buckets (how many gauges are 'on').
# [KEPUTUSAN: placeholder 2026-09-08 — 30% sits above the normal-range modes
# of both probability series; calibrate from the landed histories before any
# trigger consumes this.]
ELEVATION_PCT = 30.0

# ANXIOUS rows are dated by the TARGET quarter (one quarter AHEAD of the
# survey that produced them) — freshness must be measured against the
# effective observation date (survey ≈ target − 1 quarter), or a dead
# harvest keeps printing 'fresh' for months on a future-dated ts.
ANXIOUS_FORECAST_LEAD_DAYS = 92  # ~1 quarter

# Sahm publishes with the employment report (first Friday of M+1): a monthly
# row is already 31-38d old when it lands. The generic M window (45d) would
# suppress the leg most of each month — gate it on the Q window instead.
SAHM_STALE_DAYS = 120

# ANXIOUS gets its own quarterly gate (ronde-7): the blanket Q window was
# widened to 190d for slow quarterlies, but a survey 2+ cycles dead must
# still vanish from the triangulation — a quarterly survey at frontier ages
# ~1Q + publication ~4wk ≈ 122d; anything past ~130d is genuinely stale.
ANXIOUS_STALE_DAYS = 130


def _latest(conn: sqlite3.Connection, sid: str) -> tuple[str, float] | None:
    row = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? AND vintage_ts='realtime' "
        "AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return None if row is None else (str(row[0])[:10], float(row[1]))


def _age(ts: str) -> int:
    return (datetime.now(UTC).date() - date.fromisoformat(ts)).days


def recession_snapshot(conn: sqlite3.Connection) -> dict:
    """{model_pct, model_ts, anxious_pct, anxious_ts, sahm, sahm_ts,
    effective_ts} — any leg None when absent or stale. The ANXIOUS gate
    measures age against the EFFECTIVE observation date (survey ≈ target −
    1Q); effective_ts = the max effective date across live legs (used as the
    stored ts — never a future date)."""
    out: dict = {}
    m = _latest(conn, RECPROB)
    out["model_pct"] = None if m is None or _age(m[0]) > _STALE_DAYS["M"] else m[1]
    out["model_ts"] = None if m is None else m[0]
    a = _latest(conn, ANXIOUS)
    a_eff = (
        None
        if a is None
        else (date.fromisoformat(a[0]) - timedelta(days=ANXIOUS_FORECAST_LEAD_DAYS)).isoformat()
    )
    out["anxious_pct"] = None if a is None or _age(a_eff) > ANXIOUS_STALE_DAYS else a[1]
    out["anxious_ts"] = None if a is None else a[0]
    s = _latest(conn, SAHM)
    out["sahm"] = None if s is None or _age(s[0]) > SAHM_STALE_DAYS else s[1]
    out["sahm_ts"] = None if s is None else s[0]
    eff = [
        t
        for t, live in (
            (out["model_ts"], out["model_pct"] is not None),
            (a_eff, out["anxious_pct"] is not None),
            (out["sahm_ts"], out["sahm"] is not None),
        )
        if live and t
    ]
    out["effective_ts"] = max(eff) if eff else None
    return out


# Sahm-rule trigger level (the rule's canonical 0.50; [RISET: Sahm 2019 —
# not a tuning knob, do not calibrate]).
SAHM_TRIGGER = 0.50


def store_recession_signals(conn: sqlite3.Connection) -> int:
    """Persist 'recession_triangulation' (ts = the FRESHEST leg's date)."""
    snap = recession_snapshot(conn)
    if all(snap[k] is None for k in ("model_pct", "anxious_pct", "sahm")):
        return 0
    ts = snap["effective_ts"]
    if not ts or ts > datetime.now(UTC).date().isoformat():
        print(f"  ⚠ recession_triangulation: effective_ts {ts} is None or in the future — skipped")
        return 0
    # state: how many of the three independent gauges are elevated
    elevated = 0
    if snap["model_pct"] is not None and snap["model_pct"] >= ELEVATION_PCT:
        elevated += 1
    if snap["anxious_pct"] is not None and snap["anxious_pct"] >= ELEVATION_PCT:
        elevated += 1
    if snap["sahm"] is not None and snap["sahm"] >= SAHM_TRIGGER:
        elevated += 1
    state = ("QUIET", "ELEVATED_x1", "ELEVATED_x2", "ELEVATED_x3")[elevated]
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                "recession_triangulation",
                ts,
                now,
                now,
                snap["model_pct"],
                state,
                json.dumps(
                    {
                        "unit": "pct (model leg)",
                        "model_pct": snap["model_pct"],
                        "anxious_pct": snap["anxious_pct"],
                        "sahm": snap["sahm"],
                        "elevation_thresholds": (
                            f"model/anxious >={ELEVATION_PCT:.0f}, sahm >= {SAHM_TRIGGER}"
                        ),
                        "ts_convention": "effective observation dates (anxious = target − 1Q)",
                    }
                ),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return 1


def recession_brief_line(conn: sqlite3.Connection) -> str | None:
    """Render the triangulation (next to the Policy/Exp cluster).

    Recession: model 24.4% · anxious next-Q 20.0% · Sahm 0.42

    Each leg degrades alone; ts of each leg is NOT printed (the freshness
    windows already gated staleness — a stale leg simply vanishes).
    """
    snap = recession_snapshot(conn)
    segs = []
    if snap["model_pct"] is not None:
        # '12m' label: the curve-model probability is a ~12-months-ahead
        # reading — must not be compared like-for-like with 'anxious next-Q'
        segs.append(f"model 12m {snap['model_pct']:.0f}%")
    if snap["anxious_pct"] is not None:
        segs.append(f"anxious next-Q {snap['anxious_pct']:.0f}%")
    if snap["sahm"] is not None:
        flag = " ⚠TRIG" if snap["sahm"] >= SAHM_TRIGGER else ""
        segs.append(f"Sahm {snap['sahm']:.2f}{flag}")
    if not segs:
        return None
    return "Recession: " + " · ".join(segs)
