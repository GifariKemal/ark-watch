"""synthetic.py — synthetic XAGGBP & XAUGBP crosses.

EODHD does not list XAGGBP/XAUGBP, so they are derived from their components
in instrument_prices:
  XAGGBP = XAGUSD / GBPUSD
  XAUGBP = XAUUSD / GBPUSD
(division, not multiplication — sanity check: XAGUSD 66 / GBPUSD 1.36 gives
XAGGBP ≈ 48.5.)

Both legs are paired on the same timestamp. The metal leg prefers EODHD spot and,
with EODHD off, falls back to the COMEX front future from Yahoo (GC1 / SI1: spot plus
a small carry basis, so the brief names it). The GBPUSD leg prefers EODHD, then Yahoo.
"""

from __future__ import annotations

import contextlib
import sqlite3

SQL_CROSS = """
SELECT m.ts, m.close / g.close
FROM instrument_prices m
JOIN instrument_prices g
  ON g.symbol = 'GBPUSD' AND g.ts = m.ts
WHERE m.symbol = ? AND m.source = ?
  AND g.source IN ('EODHD', 'YAHOO')
  AND m.close IS NOT NULL AND g.close IS NOT NULL AND g.close != 0
ORDER BY m.ts DESC, CASE g.source WHEN 'EODHD' THEN 0 ELSE 1 END
LIMIT 1
"""


# metal -> legs in preference order: spot from EODHD, else the COMEX front future from Yahoo
METAL_LEGS = {
    "XAGUSD": (("XAGUSD", "EODHD"), ("SI1", "YAHOO")),
    "XAUUSD": (("XAUUSD", "EODHD"), ("GC1", "YAHOO")),
}


def _compute_cross(conn: sqlite3.Connection, metal: str) -> dict:
    for symbol, source in METAL_LEGS[metal]:
        row = conn.execute(SQL_CROSS, (symbol, source)).fetchone()
        if row is not None:
            return {"ts": row[0], "value": float(row[1]), "metal_leg": symbol}
    raise LookupError(f"{metal}GBP: no same-date {metal} (or its future) + GBPUSD pair")


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
