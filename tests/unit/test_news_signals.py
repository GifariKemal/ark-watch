"""Unit tests for news velocity and spike detector (signals/news.py)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from arkwatch import db
from arkwatch.signals import news


def _setup_db() -> sqlite3.Connection:
    return db.get_conn(":memory:", allow_init=True)


def test_news_velocity_quiet_when_empty():
    conn = _setup_db()
    res = news.news_velocity(conn, "OIL")
    assert res is not None
    assert res["state"] == "QUIET"


def test_news_velocity_detects_spike_on_burst():
    conn = _setup_db()
    now = datetime(2026, 10, 4, 18, 0, 0, tzinfo=UTC)

    # 1. Baseline: 1 article per day across 7 days
    base_rows = []
    for d in range(1, 8):
        t = now - timedelta(days=d)
        base_rows.append(
            (
                f"n-base-{d}",
                t.isoformat(),
                "FMP",
                "Crude oil market update",
                "{}",
                "c-1",
                0.5,
                1.0,
                now.isoformat(),
            )
        )
    conn.executemany(
        "INSERT INTO market_news(news_id, published_at_utc, source, title, symbols_json, cluster_id, relevance, novelty, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        base_rows,
    )

    # 2. Burst: 6 breaking oil articles in the last 30 minutes
    burst_rows = []
    for i in range(1, 7):
        t = now - timedelta(minutes=i * 4)
        burst_rows.append(
            (
                f"n-burst-{i}",
                t.isoformat(),
                "EODHD",
                f"Breaking oil supply shock headline {i}",
                "{}",
                f"c-b-{i}",
                0.8,
                1.0,
                now.isoformat(),
            )
        )
    conn.executemany(
        "INSERT INTO market_news(news_id, published_at_utc, source, title, symbols_json, cluster_id, relevance, novelty, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        burst_rows,
    )
    conn.commit()

    res = news.news_velocity(conn, "OIL", window_hours=2, baseline_days=7, as_of=now)
    assert res is not None
    assert res["recent_article_count"] == 6
    assert res["state"] == "NEWS_SPIKE"
    assert res["velocity_ratio"] >= 3.0
    assert len(res["top_headlines"]) > 0


def test_store_news_signals_persists():
    conn = _setup_db()
    now = datetime(2026, 10, 4, 18, 0, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO market_news(news_id, published_at_utc, source, title, symbols_json, cluster_id, relevance, novelty, fetched_at) "
        "VALUES ('n-1', ?, 'FMP', 'Oil prices surge', '{}', 'c-1', 0.8, 1.0, ?)",
        (now.isoformat(), now.isoformat()),
    )
    conn.commit()

    n = news.store_news_signals(conn)
    assert n > 0
    row = conn.execute(
        "SELECT signal_id, run_id FROM computed_signals WHERE signal_id='news_velocity_oil'"
    ).fetchone()
    assert row == ("news_velocity_oil", "news_velocity")
