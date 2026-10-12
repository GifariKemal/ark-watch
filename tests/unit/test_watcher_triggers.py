"""Watcher triggers that had no test (2026-10-12 audit): each fires once on a seeded boundary
condition and stays quiet on the next cycle (its cooldown key holds)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.qa import watcher


@pytest.fixture()
def conn():
    c = db.get_conn(":memory:", allow_init=True)
    yield c
    c.close()


def _series(c, sid, values, *, start=date(2026, 1, 1)):
    c.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit, value_format,"
        " freq, primary_source) VALUES (?, ?, 'A', 0, 'x', 'x', 'D', 'FRED')",
        (sid, sid),
    )
    c.executemany(
        "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at)"
        " VALUES (?, ?, ?, 'FRED', 'x')",
        [(sid, (start + timedelta(days=i)).isoformat(), v) for i, v in enumerate(values)],
    )
    c.commit()


def _fires_once(c, name):
    assert name in watcher.check_all(c)
    assert name not in watcher.check_all(c)  # cooldown key holds on the next cycle


def test_hy_extreme_high_and_low(conn):
    _series(conn, "FRED:BAMLH0A0HYM2", [3.0 + i * 0.001 for i in range(200)])  # newest = widest
    _fires_once(conn, "hy_extreme_high")
    c2 = db.get_conn(":memory:", allow_init=True)
    _series(c2, "FRED:BAMLH0A0HYM2", [5.0 - i * 0.001 for i in range(200)])  # newest = tightest
    _fires_once(c2, "hy_extreme_low")
    c2.close()


def test_net_liq_reversal_needs_a_real_move(conn):
    step = watcher.NET_LIQ_MIN_ABS_B * 1000  # WALCL is $M, the deadband $B
    up = [6_600_000 + i * step for i in range(5)]
    _series(conn, "FRED:WALCL", up + [up[-1] - (i + 1) * step for i in range(4)])
    _fires_once(conn, "net_liq_reversal")


def test_net_liq_noise_inside_the_deadband_is_quiet(conn):
    _series(conn, "FRED:WALCL", [6_600_000 + (1 if i % 2 else -1) for i in range(9)])
    assert "net_liq_reversal" not in watcher.check_all(conn)


def test_gold_ry_divergence_on_the_futures_fallback(conn):
    """XAUUSD spot (EODHD) is off: GC1 carries the gold leg. A +2% gold move with real yields
    up 10bp fires (the % threshold used to be compared with a fraction: never fired)."""
    _series(conn, "FRED:DFII10", [1.80 + i * 0.005 for i in range(25)])
    today = datetime.now(UTC).date()
    conn.executemany(
        "INSERT INTO instrument_prices(symbol, ts, source, close) VALUES ('GC1', ?, 'YAHOO', ?)",
        [((today - timedelta(days=25 - i)).isoformat(), 2400 + i * 2.5) for i in range(25)],
    )
    conn.commit()
    _fires_once(conn, "gold_ry_divergence")


def test_cot_broad_divergence(conn):
    rows = []
    for code in ("099741", "133741", "13874+", "209742"):
        for cat, long_, short in (("lev", 100, 10), ("am", 10, 100)):  # opposite nets
            rows.append(("2026-10-06", code, "tff", "2026-10-09", cat, long_, short, "CFTC", "x"))
    conn.executemany(
        "INSERT INTO cot_raw(report_date, contract_code, report_type, release_ts, category,"
        " long, short, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    _fires_once(conn, "cot_broad_divergence")


def test_ecb_flip_and_high_conviction(conn):
    meeting = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
    conn.executemany(
        "INSERT INTO fedwatch_snapshots(date, meeting_date, source, prob_ease, prob_hold,"
        " prob_hike, implied_rate) VALUES (?, ?, 'diy_ecb', ?, ?, ?, 2.25)",
        [("2026-10-08", meeting, 0.1, 0.8, 0.1), ("2026-10-09", meeting, 0.02, 0.05, 0.93)],
    )
    conn.commit()
    fired = watcher.check_all(conn)
    assert {"ecb_watch_flip", "ecb_high_conviction"} <= set(fired)
    again = watcher.check_all(conn)
    assert "ecb_watch_flip" not in again and "ecb_high_conviction" not in again


def test_surprise_flip(conn, monkeypatch):
    from arkwatch.qa import surprise

    conn.execute(
        "INSERT INTO computed_signals(signal_id, ts, run_id, computed_at, value)"
        " VALUES ('esi', '2026-10-08', 'r', 'x', -0.40)"
    )
    conn.commit()

    def store_esi(c):
        c.execute(
            "INSERT OR REPLACE INTO computed_signals(signal_id, ts, run_id, computed_at, value)"
            " VALUES ('esi', '2026-10-09', 'r', 'x', 0.45)"
        )
        c.commit()
        return 0.45

    monkeypatch.setattr(surprise, "store_esi", store_esi)
    _fires_once(conn, "surprise_flip")


def test_soma_walcl_divergence(conn, monkeypatch):
    from arkwatch.signals import soma

    gap = {"dwalcl_b": 120.0, "dsoma_b": 10.0, "gap_b": 110.0, "as_of_date": "2026-10-08"}
    monkeypatch.setattr(soma, "soma_walcl_gap_alert", lambda c, threshold_b: gap)
    _fires_once(conn, "soma_walcl_divergence")


def test_vix_stress_shortens_the_cooldown(conn):
    """A VIX at stress level switches the windowed cooldown to the stress hours."""
    assert watcher._dynamic_cooldown_hours(conn) == watcher.COOLDOWN_HOURS_DEFAULT
    _series(conn, "FRED:VIXCLS", [15.0] * 60 + [watcher.VIX_STRESS_LEVEL + 10.0])
    assert watcher._dynamic_cooldown_hours(conn) == watcher.COOLDOWN_HOURS_STRESS
