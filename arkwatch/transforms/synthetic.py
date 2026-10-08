"""synthetic.py — synthetic XAGGBP & XAUGBP crosses.

EODHD does not list XAGGBP/XAUGBP, so they are derived from their components
in instrument_prices:
  XAGGBP = XAGUSD / GBPUSD
  XAUGBP = XAUUSD / GBPUSD
(division, not multiplication — sanity check: XAGUSD 66 / GBPUSD 1.36 gives
XAGGBP ≈ 48.5.)

Both legs are paired on the same timestamp. The metal leg uses EODHD; the
GBPUSD leg prefers EODHD and falls back to Yahoo.
"""

from __future__ import annotations

import contextlib
import sqlite3

SQL_CROSS = """
SELECT m.ts, m.close / g.close
FROM instrument_prices m
JOIN instrument_prices g
  ON g.symbol = 'GBPUSD' AND g.ts = m.ts
WHERE m.symbol = ? AND m.source = 'EODHD'
  AND g.source IN ('EODHD', 'YAHOO')
  AND m.close IS NOT NULL AND g.close IS NOT NULL AND g.close != 0
ORDER BY m.ts DESC, CASE g.source WHEN 'EODHD' THEN 0 ELSE 1 END
LIMIT 1
"""


def _compute_cross(conn: sqlite3.Connection, metal: str) -> dict:
    row = conn.execute(SQL_CROSS, (metal,)).fetchone()
    if row is None:
        raise LookupError(f"{metal}GBP: no same-date {metal} + GBPUSD pair in instrument_prices")
    return {"ts": row[0], "value": float(row[1])}


def compute_xaggbp(conn: sqlite3.Connection) -> dict:
    """XAGGBP = XAGUSD / GBPUSD → {ts, value}."""
    return _compute_cross(conn, "XAGUSD")


def compute_xaugbp(conn: sqlite3.Connection) -> dict:
    """XAUGBP = XAUUSD / GBPUSD → {ts, value}."""
    return _compute_cross(conn, "XAUUSD")


def compute_all_synthetic(conn: sqlite3.Connection) -> dict:
    """Both crosses at once → {'XAGGBP': {ts, value}, 'XAUGBP': {ts, value}}."""
    out = {}
    for metal in ("XAGUSD", "XAUUSD"):
        sym = f"{metal[:3]}GBP"
        with contextlib.suppress(LookupError):
            out[sym] = _compute_cross(conn, metal)
    return out
