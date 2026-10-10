"""Random-entry null of the playbook scorecard, on synthetic 5m bars."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from arkwatch import db
from arkwatch.signals import playbook_tracker, random_entry, scorecard

SC = "SCENARIO_INTRADAY_VAL_ROTATION_LONG"
T0 = "13:00:00+00:00"  # scenario creation; the session expires 17:00 ET = 21:00 UTC


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "arkwatch.db", allow_init=True)
    yield c
    c.close()


def _session(i: int) -> str:
    return (datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat()


def _bars(c, session: str, ohlc: list[tuple], sym: str = "NQ1") -> None:
    t = datetime.fromisoformat(f"{session}T{T0}")
    c.executemany(
        "INSERT INTO intraday_bars(symbol,bar_ts_utc,interval,source,open,high,low,close,volume,"
        "fetched_at) VALUES (?,?,'5m','t',?,?,?,?,1.0,'x')",
        [
            (sym, (t + timedelta(minutes=5 * i)).isoformat(timespec="seconds"), *b)
            for i, b in enumerate(ohlc)
        ],
    )


def drift(n: int = 97) -> list[tuple]:
    """Every bar rallies 2.5%: any long entry hits a +2% target on the next bar."""
    out, p = [], 100.0
    for _ in range(n):
        out.append((p, p * 1.03, p * 0.9995, p * 1.025))
        p *= 1.025
    return out


def zigzag(n: int = 97) -> list[tuple]:
    """No drift and too little range to reach a 1% stop or a 2% target: entries time out."""
    out = []
    for i in range(n):
        o, c = 100 + 0.3 * math.sin(i), 100 + 0.3 * math.sin(i + 1)
        out.append((o, max(o, c) + 0.05, min(o, c) - 0.05, c))
    return out


def _trades(c, rs: list[float], bars_fn, *, sid=SC) -> None:
    for i, r in enumerate(rs):
        s = _session(i)
        _bars(c, s, bars_fn())
        c.execute(
            "INSERT INTO playbook_scenarios(scenario_uid,symbol,horizon,direction,scenario_id,"
            "title,trigger_condition,trigger_price,target_profit,invalidation_level,"
            "risk_reward_ratio,created_at_utc,session_id,state,entry_price,r_multiple,"
            "resolved_at_utc,payload_json) VALUES (?,'NQ1','INTRADAY','LONG',?,'t','c',100.0,"
            "102.0,99.0,2.0,?,?,?,100.0,?,?,?)",
            (f"u{i}", sid, f"{s}T{T0}", s, "HIT_TARGET_WIN" if r > 0 else "HIT_STOP_LOSS", r,
             f"{s}T20:00:00+00:00", json.dumps({"outcome": "x"})),
        )  # fmt: skip


def _group(c) -> dict:
    (g,) = scorecard.compute_scorecard(c)["groups"]
    return g


def test_real_edge_beats_random(conn):
    _trades(conn, [1.5] * 20, zigzag)
    g = _group(conn)
    assert g["null_n"] == 20 and g["null_draws"] == random_entry.K_MAX
    assert abs(g["null_mean_r"]) < 0.2
    assert g["null_p"] < 0.05 and g["beats_random"] is True


def test_pure_drift_does_not_beat_random(conn):
    _trades(conn, [1.5] * 20, drift)
    g = _group(conn)
    assert g["null_mean_r"] == pytest.approx(1.5)
    assert g["null_p"] == 1.0 and g["beats_random"] is False


def test_supported_capped_when_not_beating_random(conn):
    _trades(conn, [2.0] * 60 + [-1.0] * 40, drift)  # 'supported' on its own numbers
    g = _group(conn)
    assert g["beats_random"] is False and g["tier"] == "emerging"


def test_deterministic(conn):
    _trades(conn, [1.5, -1.0] * 10, zigzag)
    g = _group(conn)
    assert {k: g[k] for k in random_entry.NO_NULL} == {
        k: _group(conn)[k] for k in random_entry.NO_NULL
    }


def test_small_groups_and_overall_have_no_null(conn):
    _trades(conn, [1.5] * 19, zigzag)
    out = scorecard.compute_scorecard(conn)
    for s in (out["groups"][0], out["overall"]):
        assert {k: s[k] for k in random_entry.NO_NULL} == random_entry.NO_NULL


def test_cost_cap(conn, monkeypatch):
    _trades(conn, [1.5] * 20, zigzag)
    calls = []
    real = random_entry._simulate
    monkeypatch.setattr(random_entry, "_simulate", lambda *a: calls.append(1) or real(*a))
    monkeypatch.setattr(scorecard, "MAX_WALKS", 500)
    assert _group(conn)["null_draws"] == 25 and len(calls) == 500
    calls.clear()
    monkeypatch.setattr(scorecard, "MAX_WALKS", 300)  # 15 draws per trade < K_MIN: skipped
    assert _group(conn)["beats_random"] is None and not calls


@pytest.mark.parametrize("seed", range(6))
def test_walk_matches_tracker_resolution(conn, seed):
    """A random entry at bar j resolves exactly like a tracker scenario filled at bar j open."""
    rng = np.random.default_rng(seed)
    flat = [(100.0, 100.3, 99.7, 100.0)] * 10
    p, walk = 101.0, []
    for _ in range(87):
        c = p + rng.normal(0, 0.6)
        walk.append(
            (p, max(p, c) + abs(rng.normal(0, 0.3)), min(p, c) - abs(rng.normal(0, 0.3)), c)
        )
        p = c
    s = _session(0)
    _bars(conn, s, flat + walk)
    conn.execute(
        "INSERT INTO playbook_scenarios(scenario_uid,symbol,horizon,direction,scenario_id,title,"
        "trigger_condition,trigger_price,target_profit,invalidation_level,risk_reward_ratio,"
        "created_at_utc,session_id,state,payload_json) VALUES ('p','NQ1','INTRADAY','LONG',?,"
        "'t','c',100.5,103.0,99.5,2.0,?,?,'PENDING_TRIGGER','{}')",
        (SC, f"{s}T{T0}", s),
    )
    playbook_tracker.evaluate_active_playbooks(conn, as_of=datetime(2026, 9, 2, tzinfo=UTC))
    entry, stop, target, r = conn.execute(
        "SELECT entry_price, invalidation_level, target_profit, r_multiple"
        " FROM playbook_scenarios WHERE scenario_uid = 'p'"
    ).fetchone()
    assert entry == 101.0 and r is not None
    bars = playbook_tracker._bars(conn, "NQ1", f"{s}T{T0}", f"{s}T21:00:00+00:00")
    got = random_entry.walk_r("LONG", (entry - stop) / entry, (target - entry) / entry, bars, 10)
    assert got == r
