"""Scenario scorecard (evidence tiers) and open-book risk, on synthetic playbook rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from arkwatch import db
from arkwatch.signals import book_risk, scorecard

SC = "SCENARIO_INTRADAY_SWEEP_SHORT"


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "arkwatch.db", allow_init=True)
    yield c
    c.close()


def _row(c, uid, *, state, symbol="NQ1", direction="SHORT", sid=SC, horizon="INTRADAY",
         entry=None, r=0.0, session="2026-10-01", trigger=100.0, stop=101.0, target=98.0,
         created="2026-10-01T13:00:00+00:00", resolved=None, outcome=None):  # fmt: skip
    c.execute(
        "INSERT INTO playbook_scenarios(scenario_uid,symbol,horizon,direction,scenario_id,title,"
        "trigger_condition,trigger_price,target_profit,invalidation_level,risk_reward_ratio,"
        "created_at_utc,session_id,state,entry_price,r_multiple,resolved_at_utc,payload_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, symbol, horizon, direction, sid, "t", "cond", trigger, target, stop, 2.0, created,
         session, state, entry, r, resolved, json.dumps({"outcome": outcome or state})),
    )  # fmt: skip


def _trades(c, rs, *, sessions=None, **kw):
    for i, r in enumerate(rs):
        sess = sessions[i] if sessions else f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        _row(c, f"{kw.get('sid', SC)}-{kw.get('symbol', 'NQ1')}-{i}",
             state="HIT_TARGET_WIN" if r > 0 else "HIT_STOP_LOSS", entry=100.0, r=r,
             session=sess, resolved=f"{sess}T20:00:00+00:00", **kw)  # fmt: skip


def _only(c, **filters):
    out = scorecard.compute_scorecard(c, **filters)
    assert len(out["groups"]) == 1
    return out["groups"][0]


def test_asset_class_from_instruments_yaml():
    assert scorecard.asset_class("NQ1") == "equity_index"
    assert scorecard.asset_class("xauusd") == "metals"
    assert scorecard.asset_class("BTCUSD") == "crypto"
    assert scorecard.asset_class("NOPE") == "other"


def test_empty_db(conn):
    out = scorecard.compute_scorecard(conn)
    assert out["groups"] == []
    assert out["overall"]["n"] == 0 and out["overall"]["tier"] == "unvalidated"
    assert out["overall"]["win_rate"] is None and out["overall"]["expectancy_r"] is None
    assert "not a forecast" in out["disclaimer"]


def test_group_math_and_trade_definition(conn):
    _trades(conn, [2.0, 2.0, -1.0, -1.0, 0.0])
    # non-trades and open scenarios never count
    _row(conn, "nt", state="CANCELLED_EXPIRED", outcome="NO_TRIGGER")
    _row(conn, "open", state="ACTIVE", entry=100.0)
    g = _only(conn)
    assert (g["scenario_type"], g["direction"], g["horizon"], g["asset_class"]) == (
        SC,
        "SHORT",
        "INTRADAY",
        "equity_index",
    )
    assert g["n"] == 5 and g["wins"] == 2 and g["win_rate"] == 0.4
    lo, hi = g["win_rate_ci95"]
    assert 0.05 < lo < 0.4 < hi < 0.9
    assert g["expectancy_r"] == pytest.approx(0.4)
    assert g["avg_win_r"] == 2.0 and g["avg_loss_r"] == -1.0
    assert g["profit_factor"] == 2.0
    assert g["breakeven_win_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert g["tier"] == "unvalidated"
    assert g["last_updated"] == "2026-01-05T20:00:00+00:00"
    assert g["calibration"] is None and "probability" in g["calibration_reason"]
    # seeded bootstrap: identical on rerun
    assert (
        scorecard.compute_scorecard(conn)["groups"][0]["expectancy_ci95"] == (g["expectancy_ci95"])
    )


def test_grouping_and_filters(conn):
    _trades(conn, [1.0, -1.0])
    _trades(conn, [1.0], sid="SCENARIO_SWING_CVA_EXPANSION_LONG", direction="LONG",
            horizon="SWING", symbol="BTCUSD")  # fmt: skip
    out = scorecard.compute_scorecard(conn)
    assert len(out["groups"]) == 2 and out["overall"]["n"] == 3
    assert _only(conn, asset_class="crypto")["n"] == 1
    assert _only(conn, symbol="nq1", horizon="intraday", direction="short")["n"] == 2
    assert scorecard.compute_scorecard(conn, direction="LONG", asset_class="metals")["groups"] == []


@pytest.mark.parametrize(
    ("rs", "tier"),
    [
        ([2.0] * 10 + [-1.0] * 9, "unvalidated"),  # n = 19
        ([2.0] * 10 + [-1.0] * 10, "emerging"),  # n = 20
        ([2.0] * 60 + [-1.0] * 39, "emerging"),  # n = 99: never supported below 100
        ([2.0] * 60 + [-1.0] * 40, "supported"),  # 60% vs breakeven 33%, E = +0.8R
        ([1.0] * 50 + [-1.0] * 50, "emerging"),  # CI straddles breakeven 50%
        ([1.0] * 30 + [-1.0] * 70, "rejected"),  # Wilson upper < 50%
    ],
)
def test_tier_boundaries(conn, rs, tier):
    _trades(conn, rs)
    assert _only(conn)["tier"] == tier


def test_single_session_cluster_warning(conn):
    rs = [2.0, -1.0, 1.5, -1.0] * 8
    _trades(conn, rs, sessions=["2026-10-01"] * len(rs))
    g = _only(conn)
    assert g["n_sessions"] == 1 and g["effective_n"] == 1.0
    assert "1 session" in g["sample_warning"]
    lo, hi = g["expectancy_ci95"]
    assert lo == hi  # one cluster: the bootstrap cannot widen it, hence the warning


def test_spread_sessions_no_warning(conn):
    _trades(conn, [2.0, -1.0, 1.5, -1.0] * 8)
    assert _only(conn)["sample_warning"] is None


# --- book risk ------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


def test_book_risk_empty(conn):
    out = book_risk.compute_book_risk(conn, now=NOW)
    assert out["open_count"] == 0 and out["r_at_stake"] == 0
    assert out["flags"] == [] and out["veto_hints"] == []
    assert "1R" in out["r_at_stake_assumption"]


def test_book_risk_per_scenario_and_aggregates(conn):
    _row(conn, "a", state="ACTIVE", direction="LONG", entry=100.0, stop=98.0, target=106.0)
    _row(conn, "p", state="PENDING_TRIGGER", symbol="BTCUSD", direction="SHORT", trigger=200.0,
         stop=204.0, target=190.0, created="2026-10-01T12:00:00+00:00")  # fmt: skip
    _row(conn, "done", state="HIT_TARGET_WIN", entry=100.0, r=1.0)
    out = book_risk.compute_book_risk(conn, now=NOW)
    by = {s["scenario_uid"]: s for s in out["scenarios"]}
    assert set(by) == {"a", "p"}
    assert by["a"]["risk_pct"] == pytest.approx(2.0) and by["a"]["reward_to_risk"] == 3.0
    assert by["p"]["ref_price"] == 200.0 and by["p"]["risk_pct"] == pytest.approx(2.0)
    assert by["p"]["age_hours"] == 3.0 and by["p"]["cluster"] == "crypto"
    assert out["by_state"] == {"ACTIVE": 1, "PENDING_TRIGGER": 1}
    assert out["by_direction"] == {"LONG": 1, "SHORT": 1}
    assert out["by_asset_class"] == {"equity_index": 1, "crypto": 1}
    assert out["net_by_asset_class"]["crypto"] == {"long": 0, "short": 1, "net": -1}
    assert out["r_at_stake"] == 1
    assert out["clusters"]["us_equity_index"] == {"open": 1, "active_long": 1, "active_short": 0}
    assert out["flags"] == []


def test_flag_opposite_directions_same_symbol(conn):
    _row(conn, "l", state="PENDING_TRIGGER", direction="LONG", stop=99.0, target=104.0)
    _row(conn, "s", state="PENDING_TRIGGER", direction="SHORT")
    out = book_risk.compute_book_risk(conn, now=NOW)
    assert any("NQ1" in f and "opposite" in f for f in out["flags"])
    assert out["veto_hints"]


def test_flag_cluster_crowding(conn):
    for i, sym in enumerate(["NQ1", "ES1", "YM1", "US500"]):
        _row(conn, f"c{i}", state="ACTIVE", symbol=sym, direction="LONG", entry=100.0,
             stop=98.0, target=106.0)  # fmt: skip
    out = book_risk.compute_book_risk(conn, now=NOW)
    assert any("us_equity_index" in f and "4 ACTIVE LONG" in f for f in out["flags"])
    _row(conn, "c9", state="ACTIVE", symbol="BTCUSD", direction="LONG", entry=100.0, stop=98.0)
    assert sum("ACTIVE LONG" in f for f in book_risk.compute_book_risk(conn, now=NOW)["flags"]) == 1


def test_flag_tight_stop(conn):
    _row(conn, "tight-1", state="ACTIVE", direction="LONG", entry=100.0, stop=99.95, target=101.0)
    out = book_risk.compute_book_risk(conn, now=NOW)
    assert any("tight-1" in f and "0.1%" in f for f in out["flags"])


def test_flag_open_past_expiry(conn):
    _row(conn, "old", state="PENDING_TRIGGER", direction="LONG", stop=99.0, target=104.0,
         session="2026-09-20", created="2026-09-20T13:00:00+00:00")  # fmt: skip
    out = book_risk.compute_book_risk(conn, now=NOW)
    assert any("old" in f and "expiry" in f for f in out["flags"])
    assert next(s for s in out["scenarios"] if s["scenario_uid"] == "old")["expired"] is True
