"""intraday.py — intraday technical intelligence: session VWAP, ATR volatility expansion.

Processes 5-minute bars (intraday_bars) to provide institutional trend benchmarks:
  - Session VWAP: Volume-Weighted Average Price & price vs VWAP spread.
  - Volatility Expansion: 5m candle range vs 14-period ATR (>2.0x = breakout / expansion).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def _intraday_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='intraday_bars'"
    ).fetchone()
    return bool(row)


def session_intraday_intelligence(
    conn: sqlite3.Connection,
    symbol: str = "SPY",
    *,
    as_of: datetime | str | None = None,
) -> dict | None:
    """Compute Session VWAP and 5-minute ATR volatility expansion for an instrument."""
    if not _intraday_ready(conn):
        return None

    date_filter = ""
    if as_of:
        date_str = as_of[:10] if isinstance(as_of, str) else as_of.strftime("%Y-%m-%d")
        date_filter = f"AND substr(bar_ts_utc, 1, 10) = '{date_str}'"

    query = f"""
        SELECT bar_ts_utc, open, high, low, close, COALESCE(volume, 0.0)
        FROM intraday_bars
        WHERE symbol = ? {date_filter}
        ORDER BY bar_ts_utc ASC
    """
    rows = conn.execute(query, (symbol,)).fetchall()
    if not rows:
        return None

    # Group by trading session date (take the latest session date)
    sessions = sorted(dict.fromkeys(r[0][:10] for r in rows))
    latest_session = sessions[-1]
    session_rows = [r for r in rows if r[0][:10] == latest_session]

    if not session_rows:
        return None

    # Compute Session VWAP
    cum_vol = 0.0
    cum_pv = 0.0
    true_ranges = []
    prev_close = None
    for _ts, _o, h, low_val, c, v in session_rows:
        high_v, low_v, close_v, vol_v = float(h), float(low_val), float(c), float(v)
        typical_price = (high_v + low_v + close_v) / 3.0
        # If volume is 0 (like cash indices), fallback to equal weight
        eff_vol = vol_v if vol_v > 0 else 1.0
        cum_pv += typical_price * eff_vol
        cum_vol += eff_vol

        # True Range calculation
        if prev_close is None:
            tr = high_v - low_v
        else:
            tr = max(high_v - low_v, abs(high_v - prev_close), abs(low_v - prev_close))
        true_ranges.append(tr)
        prev_close = close_v

    latest_bar = session_rows[-1]
    latest_close = float(latest_bar[4])
    latest_high = float(latest_bar[2])
    latest_low = float(latest_bar[3])
    latest_range = latest_high - latest_low

    vwap = cum_pv / cum_vol if cum_vol > 0 else latest_close
    vwap_spread = ((latest_close - vwap) / vwap) * 100.0 if vwap else 0.0
    vwap_state = "ABOVE_VWAP" if latest_close >= vwap else "BELOW_VWAP"

    # 14-period ATR (or shorter if session just started)
    atr_window = min(14, len(true_ranges))
    atr_14 = sum(true_ranges[-atr_window:]) / atr_window if atr_window > 0 else latest_range
    expansion_ratio = latest_range / atr_14 if atr_14 > 0 else 1.0

    vol_state = "VOLATILITY_EXPANSION" if expansion_ratio >= 2.0 else "NORMAL"

    return {
        "symbol": symbol,
        "trade_date": latest_session,
        "latest_bar_ts": latest_bar[0],
        "close": round(latest_close, 4),
        "session_vwap": round(vwap, 4),
        "vwap_spread_pct": round(vwap_spread, 3),
        "vwap_state": vwap_state,
        "latest_range": round(latest_range, 4),
        "atr_14": round(atr_14, 4),
        "expansion_ratio": round(expansion_ratio, 2),
        "volatility_state": vol_state,
        "bars_in_session": len(session_rows),
    }


def all_session_intraday(
    conn: sqlite3.Connection,
    symbols: list[str] | tuple[str, ...] | None = None,
) -> dict[str, dict]:
    """Compute intraday VWAP and volatility intelligence across primary instruments."""
    target_syms = symbols or ("SPY", "NQ1", "ES1", "BTCUSD", "ETHUSD", "CL1", "GC1")
    out = {}
    for sym in target_syms:
        res = session_intraday_intelligence(conn, sym)
        if res:
            out[sym] = res
    return out


def store_intraday_signals(conn: sqlite3.Connection) -> int:
    """Persist session VWAP and volatility metrics to computed_signals."""
    data = all_session_intraday(conn)
    if not data:
        return 0

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[tuple] = []
    for sym, m in data.items():
        sig_id = f"intraday_vwap_{sym.lower()}"
        rows.append(
            (
                sig_id,
                m["trade_date"],
                "intraday",
                now_iso,
                m["vwap_spread_pct"],
                m["vwap_state"],
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
