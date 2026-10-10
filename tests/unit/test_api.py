"""Unit tests for the unified Python API and data access layer (arkwatch/api.py)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from arkwatch import api, db


def _setup_test_db() -> sqlite3.Connection:
    return db.get_conn(":memory:", allow_init=True)


def test_get_regime_snapshot():
    conn = _setup_test_db()
    snap = api.get_regime_snapshot(conn)
    assert "regime_score" in snap
    assert "label" in snap
    assert snap["label"] in ("RISK-ON", "RISK-OFF", "NEUTRAL")
    assert "quadrant" in snap
    assert "dollar_smile" in snap
    assert "pillars" in snap
    assert set(snap["pillars"].keys()) == set("ABCDEF")


def test_get_crypto_intelligence():
    conn = _setup_test_db()
    intel = api.get_crypto_intelligence(conn, instrument="BTC-USDT-SWAP")
    assert intel["instrument"] == "BTC-USDT-SWAP"
    assert "summary_24h" in intel
    assert "summary_1h" in intel
    assert "cascade_detector" in intel


def test_get_options_intelligence():
    conn = _setup_test_db()
    intel = api.get_options_intelligence(conn)
    assert "gold" in intel
    assert "btc" in intel
    assert "opex" in intel
    assert "annual_opex_schedule" in intel
    assert intel["opex"]["is_opex_week"] in (True, False)
    assert len(intel["annual_opex_schedule"]) == 12


def test_get_energy_intelligence():
    conn = _setup_test_db()
    intel = api.get_energy_intelligence(conn)  # the energy job has not run yet
    assert set(intel["signals"]) == {"error"}
    assert intel["crack_321_history"] == []
    conn.executemany(
        "INSERT INTO computed_signals(signal_id, ts, run_id, computed_at, value)"
        " VALUES (?, ?, ?, '2026-10-09T06:25:00+00:00', ?)",
        [
            ("energy_crack_321", "2026-10-08", "energy", 30.0),
            ("energy_crack_321", "2026-10-09", "energy", 31.5),
            ("energy_wti_bwd", "2026-10-09", "energy", 0.4),
            ("energy_x", "2026-10-09", "other", 1.0),  # another job's row stays out
        ],
    )
    intel = api.get_energy_intelligence(conn)
    assert intel["signals"] == {
        "energy_crack_321": {"ts": "2026-10-09", "value": 31.5},
        "energy_wti_bwd": {"ts": "2026-10-09", "value": 0.4},
    }


def test_get_market_news_and_calendar():
    conn = _setup_test_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")

    # Seed test market_news
    conn.execute(
        "INSERT INTO market_news(news_id, published_at_utc, source, title, url, summary, symbols_json, cluster_id, relevance, novelty, fetched_at) "
        "VALUES ('n-1', ?, 'FMP', 'Fed signals rate pause', 'https://example.com/1', 'Summary', '[\"SPY\"]', 'c-1', 0.8, 1.0, ?)",
        (now, now),
    )

    # Seed test events
    conn.execute(
        "INSERT INTO events(event_uid, ts_utc, country, name, normalized_name, importance, actual, consensus, previous, indicator_key) "
        "VALUES ('ev-1', ?, 'US', 'CPI MoM', 'CPI MOM', 'high', 0.2, 0.3, 0.2, 'CPI')",
        (now,),
    )
    conn.commit()

    news = api.get_market_news(conn, limit=5)
    assert len(news) == 1
    assert news[0]["title"] == "Fed signals rate pause"
    assert news[0]["source"] == "FMP"
    assert news[0]["symbols"] == ["SPY"]

    cal = api.get_economic_calendar(conn, days_forward=1, days_backward=1)
    assert len(cal) == 1
    assert cal[0]["name"] == "CPI MoM"
    assert cal[0]["actual"] == 0.2


def test_on_demand_refresh_unknown_target():
    res = api.on_demand_refresh("invalid-target")
    assert res["status"] == "ERROR"
    assert "unknown refresh target" in res["error"]


def test_new_intelligence_getters():
    conn = _setup_test_db()
    ff = api.get_futures_flow_intelligence(conn)
    assert isinstance(ff, dict)

    etf = api.get_etf_flows_intelligence(conn)
    assert isinstance(etf, dict)

    nv = api.get_news_velocity_intelligence(conn)
    assert isinstance(nv, dict)

    intra = api.get_session_intraday_intelligence(conn, symbol="SPY")
    assert intra is None or "vwap_state" in intra


def test_on_demand_refresh_targets(tmp_path):
    db_file = tmp_path / "test.db"
    conn = db.get_conn(db_file, allow_init=True)
    conn.close()

    r1 = api.on_demand_refresh("crypto", db_path=db_file)
    assert r1["status"] == "OK"

    r2 = api.on_demand_refresh("calibrate", db_path=db_file)
    assert r2["status"] == "OK"

    r3 = api.on_demand_refresh("futures-flow", db_path=db_file)
    assert r3["status"] == "OK"

    r4 = api.on_demand_refresh("etf-flows", db_path=db_file)
    assert r4["status"] == "OK"

    r5 = api.on_demand_refresh("news-velocity", db_path=db_file)
    assert r5["status"] == "OK"


def test_writer_paths_refuse_a_missing_db_file(tmp_path):

    missing = tmp_path / "typo.db"
    with pytest.raises(FileNotFoundError):
        api.get_trading_playbook("NQ1", db_path=missing)
    with pytest.raises(FileNotFoundError):
        api.scan_opportunities(["NQ1"], db_path=missing)
    assert not missing.exists()  # no parallel DB silently initialized


def test_freshness_honors_the_per_series_ceiling():
    from datetime import date

    from arkwatch.api import _freshness

    today = date(2026, 10, 10)
    assert _freshness("2026-10-02", "D", today)["status"] == "late"  # 8 d > daily default 5
    weekly_release = _freshness("2026-10-02", "D", today, max_age_days=12)
    assert weekly_release["status"] == "fresh" and weekly_release["expected_lag_days"] == 12
