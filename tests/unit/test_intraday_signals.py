"""Unit tests for intraday VWAP and volatility expansion (signals/intraday.py)."""

from __future__ import annotations

import sqlite3

from arkwatch import db
from arkwatch.signals import intraday


def _setup_db() -> sqlite3.Connection:
    return db.get_conn(":memory:", allow_init=True)


def test_session_intraday_vwap_and_expansion():
    conn = _setup_db()
    # Seed 5-minute bars for SPY on 2026-10-02
    bars = [
        ("2026-10-02T13:30:00+00:00", 500.0, 502.0, 499.0, 501.0, 1000.0),
        ("2026-10-02T13:35:00+00:00", 501.0, 503.0, 500.0, 502.0, 1000.0),
        ("2026-10-02T13:40:00+00:00", 502.0, 508.0, 501.0, 507.0, 3000.0),  # large range + volume expansion
    ]

    conn.executemany(
        "INSERT INTO intraday_bars(bar_ts_utc, open, high, low, close, volume, symbol, source, interval, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'SPY', 'YAHOO', '5m', '2026-10-02T13:45:00+00:00')",
        bars,
    )
    conn.commit()

    intel = intraday.session_intraday_intelligence(conn, "SPY")
    assert intel is not None
    assert intel["symbol"] == "SPY"
    assert intel["trade_date"] == "2026-10-02"
    assert intel["close"] == 507.0
    assert intel["session_vwap"] > 500.0
    assert intel["vwap_state"] == "ABOVE_VWAP"
    assert intel["bars_in_session"] == 3


def test_intraday_signals_store_persists():
    conn = _setup_db()
    conn.execute(
        "INSERT INTO intraday_bars(bar_ts_utc, open, high, low, close, volume, symbol, source, interval, fetched_at) "
        "VALUES ('2026-10-02T13:30:00+00:00', 500.0, 502.0, 499.0, 501.0, 1000.0, 'SPY', 'YAHOO', '5m', '2026-10-02T13:30:00+00:00')",
    )
    conn.commit()

    n = intraday.store_intraday_signals(conn)
    assert n > 0
    row = conn.execute("SELECT signal_id, state FROM computed_signals WHERE signal_id='intraday_vwap_spy'").fetchone()
    assert row == ("intraday_vwap_spy", "ABOVE_VWAP")
