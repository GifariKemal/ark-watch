"""horizon_backtest must derive every statistic from resolved playbook_scenarios rows."""

from __future__ import annotations

import sqlite3

from arkwatch import db
from arkwatch.qa import horizon_backtest


def _seed(conn: sqlite3.Connection, rows: list[tuple[str, str, str, str]]) -> None:
    """rows: (scenario_id, session_id, triggered_at_utc, state)."""
    for i, (sc, sess, trig, state) in enumerate(rows):
        r = 2.0 if state == "HIT_TARGET_WIN" else -1.0
        conn.execute(
            "INSERT INTO playbook_scenarios(scenario_uid, symbol, horizon, direction, scenario_id,"
            " title, trigger_condition, target_profit, invalidation_level, risk_reward_ratio,"
            " created_at_utc, session_id, state, triggered_at_utc, resolved_at_utc, entry_price,"
            " r_multiple, payload_json) VALUES (?, 'NQ1', 'INTRADAY', 'LONG', ?, 't', 'c',"
            " 1, 0, 2, ?, ?, ?, ?, ?, 100.0, ?, '{}')",
            (f"u{i}", sc, trig, sess, state, trig, trig, r),
        )


def _conn() -> sqlite3.Connection:
    return db.get_conn(":memory:", allow_init=True)


def test_hypothesis_grid_is_counted_honestly():
    grid = horizon_backtest.generate_hypotheses(["A", "B"])
    per_scenario = 1 + sum(len(v) for v in horizon_backtest.DIMENSIONS.values())
    assert len(grid) == 3 * per_scenario - 1  # "*" x ALL is the base rate itself
    assert len({h["id"] for h in grid}) == len(grid)


def test_no_data_means_insufficient_never_a_pvalue():
    out = horizon_backtest.run_horizon_backtest(_conn())
    assert out["n_trades"] == 0 and out["tested"] == 0 and out["results"] == []


def test_real_outcomes_drive_the_statistics():
    conn = _conn()
    rows = []
    # 40 sessions; scenario A wins in every NY-AM trigger, scenario B always loses
    for d in range(40):
        sess = f"2026-0{1 + d // 28}-{1 + d % 28:02d}"
        rows.append(("A", sess, f"{sess}T14:00:00+00:00", "HIT_TARGET_WIN"))  # 09:00 ET
        rows.append(("B", sess, f"{sess}T03:00:00+00:00", "HIT_STOP_LOSS"))  # 22:00 ET prior day
    _seed(conn, rows)
    out = horizon_backtest.run_horizon_backtest(conn, min_observations=30)
    assert out["n_trades"] == 80 and out["base_rate"] == 0.5
    by_id = {r["id"]: r for r in out["results"]}
    a = by_id["A|ALL"]
    assert a["status"] == "tested" and a["n"] == 40 and a["wins"] == 40
    assert a["n_eff"] == 40.0 and a["expectancy_r"] == 2.0
    assert a["win_rate_ci95"][0] > 0.9 and a["p_raw"] < 1e-6 and a["evidence"] == "supported"
    assert by_id["B|ALL"]["evidence"] == "no_edge" and by_id["B|ALL"]["p_raw"] == 1.0
    # cells with < min_observations real outcomes carry n but no p-value
    thin = by_id["A|weekday=MONDAY"]
    assert thin["status"] == "insufficient_data" and "p_raw" not in thin and thin["n"] < 30
    assert by_id["A|qt_quarter=Q3_NY_AM"]["n"] == 40
    assert by_id["B|qt_quarter=Q1_ASIA"]["n"] == 40
    # p-values differ across hypotheses (no hard-coded constants)
    assert len({r["p_raw"] for r in out["results"] if "p_raw" in r}) > 1


def test_unresolved_rows_are_ignored():
    conn = _conn()
    _seed(conn, [("A", "2026-01-02", "2026-01-02T14:00:00+00:00", "ACTIVE")])
    assert horizon_backtest.run_horizon_backtest(conn)["n_trades"] == 0


def test_every_filled_resolved_trade_counts_and_win_is_positive_r():
    """Breakeven/early-exit/time-exit/flip trades are stored as CANCELLED_EXPIRED with the
    real outcome in the payload: dropping them is survivorship bias."""
    conn = _conn()
    rows = [
        ("w", "HIT_TARGET_WIN", 100.0, 2.0),
        ("l", "HIT_STOP_LOSS", 100.0, -1.0),
        ("be", "CANCELLED_EXPIRED", 100.0, 0.5),  # partial then breakeven
        ("tx", "CANCELLED_EXPIRED", 100.0, -0.3),  # time exit
        ("nt", "CANCELLED_EXPIRED", None, 0.0),  # never filled: not a trade
        ("man", "CANCELLED_MANUAL", 100.0, 1.0),  # operator cancel: excluded
        ("act", "ACTIVE", 100.0, 0.0),
    ]
    ts = "2026-01-02T14:00:00+00:00"
    for uid, state, entry, r in rows:
        conn.execute(
            "INSERT INTO playbook_scenarios(scenario_uid, symbol, horizon, direction, scenario_id,"
            " title, trigger_condition, target_profit, invalidation_level, risk_reward_ratio,"
            " created_at_utc, session_id, state, triggered_at_utc, resolved_at_utc, entry_price,"
            " r_multiple, payload_json) VALUES (?, 'NQ1', 'INTRADAY', 'LONG', 'A', 't', 'c',"
            " 1, 0, 2, ?, '2026-01-02', ?, ?, ?, ?, ?, '{}')",
            (uid, ts, state, ts, ts, entry, r),
        )
    out = horizon_backtest.run_horizon_backtest(conn, min_observations=1)
    assert out["n_trades"] == 4
    assert out["base_rate"] == 0.5  # wins = r > 0: target win + breakeven-with-partial
