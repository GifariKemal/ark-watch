"""futures_flow.py — CME futures volume, open interest, and price flow matrix (ΔP × ΔOI).

Classifies futures market dynamics into 4 institutional flow regimes:
  1. Price UP + OI UP   -> NEW_LONGS (Aggressive buyers accumulating; strong bull trend)
  2. Price UP + OI DOWN -> SHORT_COVERING (Sellers covering; fragile rally vulnerable to fade)
  3. Price DOWN + OI UP -> NEW_SHORTS (Aggressive short sellers entering; strong bear trend)
  4. Price DOWN + OI DOWN -> LONG_LIQUIDATION (Longs capitulating; potential reversal / bottom)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..fetchers import cme

TRACKED_FUTURES = ("GC", "SI", "HG", "CL", "ES", "NQ", "YM", "BTC")


def futures_flow_matrix(
    conn: sqlite3.Connection,
    product: str = "GC",
    *,
    as_of: str | None = None,
) -> dict | None:
    """Compute ΔPrice × ΔOpen Interest flow quadrant for a CME futures product."""
    code = product.upper()
    pid = cme.PRODUCTS.get(code)
    if not pid:
        return None

    # Query last 2 trade dates for front contract of this product
    date_filter = f"AND trade_date <= '{as_of}'" if as_of else ""
    query = f"""
        SELECT trade_date, month, settle, open_interest, volume
        FROM cme_settlements
        WHERE product_id = ? {date_filter}
        ORDER BY trade_date DESC, month ASC
        LIMIT 20
    """
    rows = conn.execute(query, (pid,)).fetchall()
    if not rows:
        return None

    # Group by trade_date, finding front month (earliest month) for each date
    dates = sorted(dict.fromkeys(r[0] for r in rows), reverse=True)
    if len(dates) < 2:
        return None

    t_curr = dates[0]
    t_prev = dates[1]

    # Front contract for current date
    front_curr = next((r for r in rows if r[0] == t_curr), None)
    # Match the SAME expiry month on prev date to avoid contract roll gap
    if not front_curr or front_curr[2] is None:
        return None

    month = front_curr[1]
    front_prev = next((r for r in rows if r[0] == t_prev and r[1] == month), None)

    # If same month not available on prev day, fall back to aggregate product OI from voi_daily
    if not front_prev or front_prev[2] is None:
        return None

    px_curr = float(front_curr[2])
    px_prev = float(front_prev[2])
    delta_p = px_curr - px_prev
    delta_p_pct = (delta_p / px_prev) * 100 if px_prev else 0.0

    # Total Open Interest comparison (prefer voi_daily for complete product aggregate, else front contract)
    voi_rows = conn.execute(
        "SELECT trade_date, oi, oi_diff FROM voi_daily "
        "WHERE product_id=? AND report_type='FUT' AND trade_date IN (?, ?) "
        "ORDER BY trade_date DESC",
        (pid, t_curr, t_prev),
    ).fetchall()

    if len(voi_rows) == 2 and voi_rows[0][1] and voi_rows[1][1]:
        oi_curr = float(voi_rows[0][1])
        oi_prev = float(voi_rows[1][1])
        delta_oi = float(voi_rows[0][2]) if voi_rows[0][2] is not None else (oi_curr - oi_prev)
    else:
        oi_curr = float(front_curr[3] or 0.0)
        oi_prev = float(front_prev[3] or 0.0)
        delta_oi = oi_curr - oi_prev

    delta_oi_pct = (delta_oi / oi_prev) * 100 if oi_prev else 0.0

    # Determine 4-quadrant regime
    p_up = delta_p > 0
    p_down = delta_p < 0
    oi_up = delta_oi > 0
    oi_down = delta_oi < 0

    if p_up and oi_up:
        quadrant = "NEW_LONGS"
        signal = "BULLISH_EXPANSION"
        interp = "Buyers accumulating new contracts; strong upward institutional momentum."
    elif p_up and oi_down:
        quadrant = "SHORT_COVERING"
        signal = "WEAK_RALLY"
        interp = "Price rally driven by short seller liquidation, not fresh buying; vulnerable to reversal."
    elif p_down and oi_up:
        quadrant = "NEW_SHORTS"
        signal = "BEARISH_EXPANSION"
        interp = "Sellers opening fresh short positions; strong downward institutional pressure."
    elif p_down and oi_down:
        quadrant = "LONG_LIQUIDATION"
        signal = "CAPITULATION_DUMP"
        interp = "Long holders closing / liquidating; potential selling exhaustion / bottom setup."
    else:
        quadrant = "NEUTRAL"
        signal = "CONSOLIDATION"
        interp = "Price and open interest changes negligible; indecision / consolidation."

    return {
        "product": code,
        "trade_date": t_curr,
        "contract_month": month,
        "settle": px_curr,
        "delta_price": round(delta_p, 4),
        "delta_price_pct": round(delta_p_pct, 3),
        "open_interest": oi_curr,
        "delta_oi": round(delta_oi, 1),
        "delta_oi_pct": round(delta_oi_pct, 2),
        "quadrant": quadrant,
        "signal": signal,
        "interpretation": interp,
    }


def all_futures_flow_matrix(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
) -> dict[str, dict]:
    """Compute flow matrix across all tracked CME futures products."""
    out = {}
    for code in TRACKED_FUTURES:
        res = futures_flow_matrix(conn, code, as_of=as_of)
        if res:
            out[code] = res
    return out


def store_futures_flow_signals(conn: sqlite3.Connection) -> int:
    """Persist daily CME futures flow matrix quadrants to computed_signals."""
    flows = all_futures_flow_matrix(conn)
    if not flows:
        return 0

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[tuple] = []
    for code, m in flows.items():
        sig_id = f"cme_flow_{code.lower()}"
        rows.append(
            (
                sig_id,
                m["trade_date"],
                "cme_flow",
                now_iso,
                m["delta_oi_pct"],
                m["quadrant"],
                json.dumps(m),
            )
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return len(rows)
