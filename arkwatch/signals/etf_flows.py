"""etf_flows.py — physical and spot ETF flow momentum indicators (GLD, SLV, BTC, ETH).

Calculates cumulative net flows (5-day and 20-day) and rolling z-scores to track
institutional accumulation vs distribution in precious metals and crypto.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..transforms.core import zscore


def _flows_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='flows_daily'"
    ).fetchone()
    return bool(row)


def etf_flow_momentum(
    conn: sqlite3.Connection,
    asset: str = "GOLD",
    *,
    as_of: str | None = None,
) -> dict | None:
    """Compute rolling 5-day and 20-day physical/spot ETF flow momentum."""
    if not _flows_ready(conn):
        return None

    asset_key = asset.upper()
    date_filter = f"WHERE date <= '{as_of}'" if as_of else ""

    if asset_key == "GOLD":
        col = "gld_tonnes"
        unit = "tonnes"
    elif asset_key == "SILVER":
        col = "slv_shares"
        unit = "shares"
    elif asset_key == "BTC":
        col = "btc_etf_musd"
        unit = "million_usd"
    elif asset_key == "ETH":
        col = "eth_etf_musd"
        unit = "million_usd"
    else:
        return None

    query = f"""
        SELECT date, {col}
        FROM flows_daily
        {date_filter}
        ORDER BY date DESC
        LIMIT 260
    """
    rows = conn.execute(query).fetchall()
    # Filter valid rows
    valid = [(r[0], float(r[1])) for r in rows if r[1] is not None][::-1]
    if len(valid) < 5:
        return None

    dates = [v[0] for v in valid]
    values = [v[1] for v in valid]
    latest_date = dates[-1]
    current_val = values[-1]

    if asset_key in ("GOLD", "SILVER"):
        # Cumulative tonnage/shares: flow is delta
        delta_5d = current_val - values[-5] if len(values) >= 5 else 0.0
        delta_20d = current_val - values[-20] if len(values) >= 20 else delta_5d

        # Rolling 5d delta series for z-score
        deltas_5d = [values[i] - values[i - 5] for i in range(5, len(values))]
        z = zscore(deltas_5d, window=min(len(deltas_5d), 120)) if len(deltas_5d) >= 10 else None

        if z is not None and z > 0.5:
            state = "ACCUMULATION"
        elif z is not None and z < -0.5:
            state = "DISTRIBUTION"
        else:
            state = "NEUTRAL"

        return {
            "asset": asset_key,
            "date": latest_date,
            "current_level": round(current_val, 2),
            "unit": unit,
            "flow_5d": round(delta_5d, 2),
            "flow_20d": round(delta_20d, 2),
            "flow_z": round(z, 2) if z is not None else None,
            "state": state,
        }

    # BTC / ETH: rows are already daily net flow in $ millions
    sum_5d = sum(values[-5:])
    sum_20d = sum(values[-min(20, len(values)):])

    rolling_5d_sums = [
        sum(values[max(0, i - 4) : i + 1]) for i in range(4, len(values))
    ]
    z = zscore(rolling_5d_sums, window=min(len(rolling_5d_sums), 60)) if len(rolling_5d_sums) >= 10 else None

    if sum_5d >= 300.0:
        state = "STRONG_INFLOW"
    elif sum_5d > 50.0:
        state = "MODERATE_INFLOW"
    elif sum_5d <= -300.0:
        state = "HEAVY_OUTFLOW"
    elif sum_5d < -50.0:
        state = "MODERATE_OUTFLOW"
    else:
        state = "BALANCED"

    return {
        "asset": asset_key,
        "date": latest_date,
        "current_daily_net_musd": round(current_val, 2),
        "unit": unit,
        "cum_flow_5d_musd": round(sum_5d, 2),
        "cum_flow_20d_musd": round(sum_20d, 2),
        "flow_z": round(z, 2) if z is not None else None,
        "state": state,
    }


def all_etf_flow_momentum(conn: sqlite3.Connection) -> dict[str, dict]:
    """Compute flow momentum across Gold, Silver, BTC, and ETH."""
    out = {}
    for asset in ("GOLD", "SILVER", "BTC", "ETH"):
        res = etf_flow_momentum(conn, asset)
        if res:
            out[asset] = res
    return out


def store_etf_flow_signals(conn: sqlite3.Connection) -> int:
    """Persist ETF flow momentum states to computed_signals."""
    data = all_etf_flow_momentum(conn)
    if not data:
        return 0

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[tuple] = []
    for asset, m in data.items():
        sig_id = f"flow_etf_{asset.lower()}"
        val = m.get("flow_5d") if "flow_5d" in m else m.get("cum_flow_5d_musd", 0.0)
        rows.append(
            (
                sig_id,
                m["date"],
                "etf_flow",
                now_iso,
                val,
                m["state"],
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
