"""Regime backtest: lagged regime inputs, base-rate null, walk-forward split."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from arkwatch import db
from arkwatch.qa import backtest

DAYS = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(60)]
SPIKE = DAYS[40]  # the only day VIX prints > 25


def _conn():
    conn = db.get_conn(":memory:", allow_init=True)
    for sid, val in (("FRED:VIXCLS", 15.0), ("FRED:BAMLH0A0HYM2", 3.0), ("FRED:DFII10", 1.0)):
        conn.execute(
            "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit,"
            " value_format, freq, primary_source) VALUES (?, ?, 'A', 0, 'x', 'x', 'D', 'FRED')",
            (sid, sid),
        )
        for d in DAYS:
            v = 30.0 if sid == "FRED:VIXCLS" and d == SPIKE else val
            conn.execute(
                "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at)"
                " VALUES (?, ?, ?, 'FRED', 'x')",
                (sid, d, v),
            )
    # XAUUSD rises every day except DAYS[41] -> DAYS[42]
    px = 100.0
    for i, d in enumerate(DAYS):
        px += -5.0 if i == 42 else 1.0
        conn.execute(
            "INSERT INTO instrument_prices(symbol, ts, source, close) VALUES"
            " ('XAUUSD', ?, 'EODHD', ?)",
            (d, px),
        )
    return conn


def test_regime_is_lagged_to_what_was_known():
    conn = _conn()
    # same-day (look-ahead) labeling: the spike day's own return is an up-day
    assert backtest.run_backtest(conn, lag_days=0)["STRESS"]["XAUUSD"]["wins"] == 1
    # lag 1: the spike value is only usable for the close(d+1) -> close(d+2) return
    stress = backtest.run_backtest(conn, lag_days=1)["STRESS"]["XAUUSD"]
    assert stress["n"] == 1 and stress["wins"] == 0


def test_fdr_guardrail_uses_base_rate_and_by():
    results = {
        "A": {"X": {"n": 20, "wins": 19}},
        "B": {"X": {"n": 20, "wins": 19}},
    }
    out = backtest._fdr_guardrail(results)
    # base rate of up-days for X is 0.95 -> 19/20 is no edge at all
    assert all(o["p_raw"] > 0.3 for o in out)
    assert all(not o["significant_after_fdr"] for o in out)
    assert out[0]["win_rate_ci95"][0] == pytest.approx(0.7639, abs=1e-3)


def test_walk_forward_chooses_thresholds_on_train_only():
    # synthetic obs: (date, inst, ret, vix, hy, dfii_mom); VIX drifts upward
    obs = [(f"d{i:03d}", "X", 1.0 if i % 2 else -1.0, float(i), 1.0, 0.0) for i in range(100)]
    res = backtest.walk_forward(obs, n_blocks=4)
    first = res["folds"][0]
    assert first["train"] == ("d000", "d019") and first["test"] == ("d020", "d039")
    # VIX threshold = 80th pct of the 20 training values (0..19), not the full sample
    assert first["thresholds"][0] == pytest.approx(15.2)
    n_oos = sum(c["n"] for r in res["results"].values() for c in r.values())
    assert n_oos == 80  # the first block is never scored
