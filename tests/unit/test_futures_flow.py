"""Unit tests for CME futures flow matrix (signals/futures_flow.py)."""

from __future__ import annotations

import sqlite3

from arkwatch import db
from arkwatch.signals import futures_flow
from arkwatch.signals.futures_flow import futures_flow_matrix


def _setup_db() -> sqlite3.Connection:
    return db.get_conn(":memory:", allow_init=True)


def test_futures_flow_matrix_new_longs():
    conn = _setup_db()
    # Gold product_id = 437
    # Day 1: settle 2600, oi 10000
    # Day 2: settle 2650 (+50), oi 12000 (+2000) -> NEW_LONGS
    conn.executemany(
        "INSERT INTO cme_settlements(trade_date, product_id, month, settle, volume, open_interest) "
        "VALUES (?, 437, 'Z26', ?, 1000, ?)",
        [
            ("2026-09-20", 2600.0, 10000.0),
            ("2026-09-21", 2650.0, 12000.0),
        ],
    )
    conn.commit()

    res = futures_flow_matrix(conn, "GC")
    assert res is not None
    assert res["product"] == "GC"
    assert res["delta_price"] == 50.0
    assert res["delta_oi"] == 2000.0
    assert res["quadrant"] == "NEW_LONGS"
    assert res["signal"] == "BULLISH_EXPANSION"


def test_futures_flow_matrix_short_covering():
    conn = _setup_db()
    # Day 1: settle 2600, oi 10000
    # Day 2: settle 2650 (+50), oi 8000 (-2000) -> SHORT_COVERING
    conn.executemany(
        "INSERT INTO cme_settlements(trade_date, product_id, month, settle, volume, open_interest) "
        "VALUES (?, 437, 'Z26', ?, 1000, ?)",
        [
            ("2026-09-20", 2600.0, 10000.0),
            ("2026-09-21", 2650.0, 8000.0),
        ],
    )
    conn.commit()

    res = futures_flow_matrix(conn, "GC")
    assert res is not None
    assert res["quadrant"] == "SHORT_COVERING"
    assert res["signal"] == "WEAK_RALLY"


def test_futures_flow_matrix_new_shorts():
    conn = _setup_db()
    # Day 1: settle 2600, oi 10000
    # Day 2: settle 2550 (-50), oi 12000 (+2000) -> NEW_SHORTS
    conn.executemany(
        "INSERT INTO cme_settlements(trade_date, product_id, month, settle, volume, open_interest) "
        "VALUES (?, 437, 'Z26', ?, 1000, ?)",
        [
            ("2026-09-20", 2600.0, 10000.0),
            ("2026-09-21", 2550.0, 12000.0),
        ],
    )
    conn.commit()

    res = futures_flow_matrix(conn, "GC")
    assert res is not None
    assert res["quadrant"] == "NEW_SHORTS"
    assert res["signal"] == "BEARISH_EXPANSION"


def test_futures_flow_matrix_long_liquidation():
    conn = _setup_db()
    # Day 1: settle 2600, oi 10000
    # Day 2: settle 2550 (-50), oi 8000 (-2000) -> LONG_LIQUIDATION
    conn.executemany(
        "INSERT INTO cme_settlements(trade_date, product_id, month, settle, volume, open_interest) "
        "VALUES (?, 437, 'Z26', ?, 1000, ?)",
        [
            ("2026-09-20", 2600.0, 10000.0),
            ("2026-09-21", 2550.0, 8000.0),
        ],
    )
    conn.commit()

    res = futures_flow_matrix(conn, "GC")
    assert res is not None
    assert res["quadrant"] == "LONG_LIQUIDATION"
    assert res["signal"] == "CAPITULATION_DUMP"


def test_store_futures_flow_signals():
    conn = _setup_db()
    conn.executemany(
        "INSERT INTO cme_settlements(trade_date, product_id, month, settle, volume, open_interest) "
        "VALUES (?, 437, 'Z26', ?, 1000, ?)",
        [
            ("2026-09-20", 2600.0, 10000.0),
            ("2026-09-21", 2650.0, 12000.0),
        ],
    )
    conn.commit()

    n = futures_flow.store_futures_flow_signals(conn)
    assert n > 0
    row = conn.execute("SELECT signal_id, state FROM computed_signals WHERE signal_id='cme_flow_gc'").fetchone()
    assert row == ("cme_flow_gc", "NEW_LONGS")
