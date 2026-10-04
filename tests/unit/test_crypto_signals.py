"""Unit tests for crypto liquidation analytics (signals/crypto.py)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from arkwatch import db
from arkwatch.signals import crypto

def _setup_db() -> sqlite3.Connection:
    return db.get_conn(":memory:", allow_init=True)


def test_liquidation_summary_missing_table_graceful():
    conn = sqlite3.connect(":memory:")
    # No crypto_liquidations table
    assert crypto.liquidation_summary(conn) is None
    assert crypto.store_crypto_signals(conn) == 0


def test_liquidation_summary_imbalance_calculation():
    conn = _setup_db()
    now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)

    # Seed 3 long liquidations ($600k total) and 1 short liquidation ($100k total)
    conn.executemany(
        "INSERT INTO crypto_liquidations(event_uid, ts_utc, source, instrument, position_side, price, size, notional_usd, raw_json, fetched_at) "
        "VALUES (?, ?, 'OKX', 'BTC-USDT-SWAP', ?, 60000.0, 1.0, ?, '{}', ?)",
        [
            ("ev-1", (now - timedelta(hours=2)).isoformat(), "long", 300000.0, now.isoformat()),
            ("ev-2", (now - timedelta(hours=1)).isoformat(), "long", 300000.0, now.isoformat()),
            ("ev-3", (now - timedelta(minutes=30)).isoformat(), "short", 100000.0, now.isoformat()),
        ],
    )
    conn.commit()

    summary = crypto.liquidation_summary(conn, "BTC-USDT-SWAP", window_hours=24, as_of=now)
    assert summary is not None
    assert summary["long_notional_usd"] == 600000.0
    assert summary["short_notional_usd"] == 100000.0
    assert summary["total_notional_usd"] == 700000.0
    assert summary["long_count"] == 2
    assert summary["short_count"] == 1
    # Imbalance: (600k - 100k) / 700k = +0.7143 > 0.40 -> LONG_FLUSH
    assert summary["imbalance_ratio"] == 0.7143
    assert summary["state"] == "LONG_FLUSH"


def test_cascade_detector_fires_capitulation_on_spike():
    conn = _setup_db()
    now = datetime(2026, 10, 4, 15, 0, 0, tzinfo=UTC)

    # 1. Seed 30 hours of quiet baseline (~$10k per hour)
    baseline_rows = []
    for h in range(1, 31):
        t = now - timedelta(hours=h)
        baseline_rows.append(
            (f"b-{h}", t.isoformat(), "OKX", "BTC-USDT-SWAP", "long", 60000.0, 0.1, 10000.0, "{}", now.isoformat())
        )
    conn.executemany(
        "INSERT INTO crypto_liquidations(event_uid, ts_utc, source, instrument, position_side, price, size, notional_usd, raw_json, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        baseline_rows,
    )

    # 2. Inject a massive liquidation spike in the current 1-hour window ($1M long liquidation)
    conn.execute(
        "INSERT INTO crypto_liquidations(event_uid, ts_utc, source, instrument, position_side, price, size, notional_usd, raw_json, fetched_at) "
        "VALUES ('spike-1', ?, 'OKX', 'BTC-USDT-SWAP', 'long', 58000.0, 17.24, 1000000.0, '{}', ?)",
        ((now - timedelta(minutes=10)).isoformat(), now.isoformat()),
    )
    conn.commit()

    detector = crypto.liquidation_cascade_detector(conn, "BTC-USDT-SWAP", window_hours=1, lookback_days=30, as_of=now)
    assert detector is not None
    assert detector["signal"] == "LIQUIDATION_CAPITULATION"
    assert detector["z_score"] is not None
    assert detector["z_score"] >= 2.5


def test_store_crypto_signals_persists_to_computed_signals():
    conn = _setup_db()
    now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)

    conn.execute(
        "INSERT INTO crypto_liquidations(event_uid, ts_utc, source, instrument, position_side, price, size, notional_usd, raw_json, fetched_at) "
        "VALUES ('ev-store', ?, 'OKX', 'BTC-USDT-SWAP', 'short', 60000.0, 5.0, 300000.0, '{}', ?)",
        ((now - timedelta(hours=1)).isoformat(), now.isoformat()),
    )
    conn.commit()

    n = crypto.store_crypto_signals(conn)
    assert n > 0

    rows = conn.execute(
        "SELECT signal_id, value, state, inputs_json FROM computed_signals WHERE signal_id LIKE 'crypto_liq_%'"
    ).fetchall()
    assert len(rows) >= 2
    ids = {r[0] for r in rows}
    assert "crypto_liq_24h_btc" in ids


def test_compute_cvd_calculation():
    conn = _setup_db()
    now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)

    conn.executemany(
        "INSERT INTO crypto_trade_flow_1m"
        "(minute_utc, instrument, source, trade_count, buy_count, sell_count, buy_contracts, sell_contracts, buy_notional_usd, sell_notional_usd, buy_normalized_count, sell_normalized_count, first_trade_ts, last_trade_ts, first_trade_id, last_trade_id, fetched_at) "
        "VALUES (?, 'BTC-USDT-SWAP', 'OKX', 10, 6, 4, 1.0, 1.0, ?, ?, 6, 4, 't1', 't2', 'id1', 'id2', ?)",
        [
            ((now - timedelta(minutes=2)).isoformat(), 600000.0, 400000.0, now.isoformat()),
            ((now - timedelta(minutes=1)).isoformat(), 700000.0, 300000.0, now.isoformat()),
        ],
    )
    conn.commit()

    cvd = crypto.compute_cvd(conn, "BTC-USDT-SWAP", window_hours=1, as_of=now)
    assert cvd is not None
    assert cvd["total_buy_usd"] == 1300000.0
    assert cvd["total_sell_usd"] == 700000.0
    assert cvd["net_delta_usd"] == 600000.0
    assert cvd["state"] == "AGGRESSIVE_BUYING"
    assert cvd["points_count"] == 2
