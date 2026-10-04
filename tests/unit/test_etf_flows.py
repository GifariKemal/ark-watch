"""Unit tests for physical and spot ETF flow momentum (signals/etf_flows.py)."""

from __future__ import annotations

import sqlite3

from arkwatch import db
from arkwatch.signals import etf_flows


def _setup_db() -> sqlite3.Connection:
    return db.get_conn(":memory:", allow_init=True)


def test_etf_flow_momentum_gold():
    conn = _setup_db()
    # Seed 6 days of gold tonnes increasing
    days = [f"2026-09-{i:02d}" for i in range(1, 10)]
    rows = [(d, 900.0 + i * 2.0) for i, d in enumerate(days)]

    conn.executemany(
        "INSERT INTO flows_daily(date, gld_tonnes) VALUES (?, ?)",
        rows,
    )
    conn.commit()

    intel = etf_flows.etf_flow_momentum(conn, "GOLD")
    assert intel is not None
    assert intel["asset"] == "GOLD"
    assert intel["flow_5d"] > 0
    assert intel["state"] in ("ACCUMULATION", "NEUTRAL")


def test_etf_flow_momentum_btc():
    conn = _setup_db()
    # Seed positive BTC ETF net flow
    days = [f"2026-09-{i:02d}" for i in range(1, 10)]
    rows = [(d, 150.0) for d in days]

    conn.executemany(
        "INSERT INTO flows_daily(date, btc_etf_musd) VALUES (?, ?)",
        rows,
    )
    conn.commit()

    intel = etf_flows.etf_flow_momentum(conn, "BTC")
    assert intel is not None
    assert intel["asset"] == "BTC"
    assert intel["cum_flow_5d_musd"] == 750.0
    assert intel["state"] == "STRONG_INFLOW"
