"""vixterm.py — short-tenor vol term structure: VIX9D/VIX spot ratio.

The 9-day/spot ratio isolates where stress is CONCENTRATED on the vol curve:
  ratio > 1  (VIX9D > VIX)  — front-end backwardation: acute event stress
  ratio << 1 (< ~0.75)        — steep contango: calm, carry-friendly regime
Rising-but-below-1 is the classic pre-stress tell (front end wakes first).

Degradation: pre-revival DBs (no CBOE:VIX9D rows) return None everywhere; a
leg without a same-day pair also degrades (never a mixed-vintage number).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

VIX9D = "CBOE:VIX9D"
VIX = "FRED:VIXCLS"

# Display/state bands (spot-ratio). [KEPUTUSAN: placeholder 2026-09-08 —
# calibrate from the 2011→ history's ratio distribution before any trigger
# consumes this; 1.0 = the backwardation line itself is structural.]
BACKWARDATION = 1.0
STEEP_CONTANGO = 0.75

# Daily series: >5d old = the harvest broke (the brief D-window convention).
STALE_DAYS = 5


def _latest(conn: sqlite3.Connection, sid: str) -> tuple[str, float] | None:
    row = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? AND vintage_ts='realtime' "
        "AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return None if row is None else (str(row[0])[:10], float(row[1]))


def _age(ts: str) -> int:
    return (datetime.now(UTC).date() - date.fromisoformat(ts)).days


def _value_at(conn: sqlite3.Connection, sid: str, ts: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id=? AND vintage_ts='realtime' "
        "AND ts=? AND value IS NOT NULL",
        (sid, ts),
    ).fetchone()
    return None if row is None else float(row[0])


def vix9d_ratio(conn: sqlite3.Connection) -> dict | None:
    """{ratio, vix9d, vix, ts} — None when either leg is missing or stale.

    COMMON-DATE join (the expectations.py vintage-mismatch precedent), SYMMETRIC:
    both legs are read at ts = the OLDER leg's date — whichever leg leads has
    its value re-read back to that date (round-1's fix only joined the
    numerator, leaving the mirror direction — FRED newer than the CDN —
    stating a mixed ratio AND overwriting the good stored row). Either leg
    without a row at the common date → None, never a mixed-vintage number.
    """
    v9 = _latest(conn, VIX9D)
    vx = _latest(conn, VIX)
    if v9 is None or vx is None or _age(v9[0]) > STALE_DAYS or _age(vx[0]) > STALE_DAYS:
        return None
    ts = min(v9[0], vx[0])  # the older leg = the common date both must quote
    v9_val = v9[1] if v9[0] == ts else _value_at(conn, VIX9D, ts)
    vx_val = vx[1] if vx[0] == ts else _value_at(conn, VIX, ts)
    if v9_val is None or vx_val is None or vx_val <= 0:
        return None
    ratio_val = round(v9_val / vx_val, 3)
    return {
        "ratio": ratio_val,
        "state": _state(ratio_val),
        "vix9d": v9_val,
        "vix": vx_val,
        "ts": ts,
        "vix9d_ts": ts,  # source-of-value dates (the common date)
        "vix_ts": ts,
    }


def _state(ratio: float) -> str:
    if ratio >= BACKWARDATION:
        return "BACKWARDATION"
    if ratio < STEEP_CONTANGO:
        return "STEEP_CONTANGO"
    return "NORMAL"


def store_vixterm_signals(conn: sqlite3.Connection) -> int:
    r = vix9d_ratio(conn)
    if r is None:
        return 0
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                "vix9d_vix_ratio",
                r["ts"],
                now,
                now,
                r["ratio"],
                _state(r["ratio"]),
                json.dumps(
                    {
                        "unit": "ratio",
                        "vix9d": r["vix9d"],
                        "vix": r["vix"],
                        "vix9d_ts": r["vix9d_ts"],
                        "vix_ts": r["vix_ts"],
                        "bands": f"backwardation >= {BACKWARDATION}, steep < {STEEP_CONTANGO}",
                    }
                ),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return 1


def vixterm_brief_line(conn: sqlite3.Connection) -> str | None:
    """'VIX term: 9d/spot 0.74 (NORMAL)' — rendered near the Vol/Exp cluster."""
    r = vix9d_ratio(conn)
    if r is None:
        return None
    return f"VIX term: 9d/spot {r['ratio']:.2f} ({_state(r['ratio'])})"
