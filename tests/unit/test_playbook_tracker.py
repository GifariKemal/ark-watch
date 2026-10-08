"""test_playbook_tracker.py — unit tests for automated playbook lifecycle and outcome tracking."""

import json
from datetime import UTC, datetime, timedelta

from arkwatch import db
from arkwatch.signals import playbook_tracker


def test_record_playbook_scenarios_and_cfd_offset(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)

    playbook_payload = {
        "symbol": "NQ1",
        "as_of": "2026-10-05T14:00:00+00:00",
        "last_price": 31000.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": "SCENARIO_INTRADAY_EXPANSION_LONG",
                "horizon": "INTRADAY",
                "title": "Long Expansion",
                "direction": "LONG",
                "trigger_condition": "5m close above VAH",
                "trigger_price": 31100.0,
                "target_profit": 31300.0,
                "invalidation_level": 31000.0,
                "risk_reward_ratio": 2.0,
            }
        ],
    }

    # Record scenario with +10.0 CFD basis offset: levels are tracked against the symbol's own
    # bars, so they are stored in bar price space and the offset is only kept for reference
    # (it used to be added a second time on top of playbook.py's offset).
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload, cfd_basis_offset=10.0)
    assert len(uids) == 1
    assert "NQ1-2026-10-05-INTRADAY-SCENARIO_INTRADAY_EXPANSION_LONG" in uids[0]

    row = conn.execute(
        "SELECT target_profit, invalidation_level, cfd_basis_offset, state FROM playbook_scenarios WHERE scenario_uid = ?",
        (uids[0],),
    ).fetchone()
    assert row[0] == 31300.0  # bar space, no offset
    assert row[1] == 31000.0
    assert row[2] == 10.0
    assert row[3] == "PENDING_TRIGGER"

    conn.close()


def test_evaluate_active_playbooks_win_lifecycle(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t0_iso = t0.isoformat(timespec="seconds")

    playbook_payload = {
        "symbol": "NQ1",
        "as_of": t0_iso,
        "last_price": 31000.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": "SCENARIO_EXPANSION_LONG",
                "horizon": "INTRADAY",
                "title": "Long Expansion",
                "direction": "LONG",
                "trigger_condition": "close above 31100",
                "trigger_price": 31100.0,
                "target_profit": 31300.0,
                "invalidation_level": 31000.0,
                "risk_reward_ratio": 2.0,
            }
        ],
    }
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload)

    # Insert subsequent bars:
    # Bar 1: Triggers at 31150 (ACTIVE)
    # Bar 2: Rallies to 31350 (Hits target 31300 -> WIN)
    t1_iso = (t0 + timedelta(minutes=5)).isoformat(timespec="seconds")
    t2_iso = (t0 + timedelta(minutes=10)).isoformat(timespec="seconds")

    bars = [
        ("NQ1", t1_iso, "5m", "YAHOO", 31080.0, 31160.0, 31070.0, 31150.0, 500.0, t1_iso),
        ("NQ1", t2_iso, "5m", "YAHOO", 31150.0, 31350.0, 31140.0, 31320.0, 800.0, t2_iso),
    ]
    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    conn.commit()

    stats = playbook_tracker.evaluate_active_playbooks(conn, as_of=t2_iso)
    assert stats["activated"] == 1
    assert stats["resolved_wins"] == 1

    row = conn.execute(
        "SELECT state, entry_price, exit_price, pnl_points, r_multiple, mfe_points FROM playbook_scenarios WHERE scenario_uid = ?",
        (uids[0],),
    ).fetchone()
    assert row[0] == "HIT_TARGET_WIN"
    assert row[1] == 31100.0  # entry filled at trigger price (Option 1)
    assert row[2] == 31300.0  # target exit
    assert row[3] == 150.0  # blended pnl: 50% at +1.0R (50 pts) + 50% at +2.0R (100 pts) = 150 pts
    assert row[4] == 1.5  # 1.50R realized
    assert row[5] == 250.0  # MFE: 31350 - 31100 = 250 pts

    # Test performance metrics
    perf = playbook_tracker.get_playbook_performance_metrics(conn, symbol="NQ1")
    assert perf["total_scenarios"] == 1
    assert perf["wins"] == 1
    assert perf["win_rate_pct"] == 100.0
    assert perf["avg_mfe"] == 250.0

    conn.close()


def test_evaluate_active_playbooks_loss_lifecycle(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t0_iso = t0.isoformat(timespec="seconds")

    playbook_payload = {
        "symbol": "CL1",
        "as_of": t0_iso,
        "last_price": 90.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": "SCENARIO_LONG",
                "horizon": "INTRADAY",
                "title": "Oil Long",
                "direction": "LONG",
                "trigger_condition": "above 90.5",
                "trigger_price": 90.5,
                "target_profit": 92.0,
                "invalidation_level": 89.0,
                "risk_reward_ratio": 1.0,
            }
        ],
    }
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload)

    # Bar 1 triggers at 90.8
    # Bar 2 drops to 88.5 (Hits stop 89.0 -> LOSS)
    t1_iso = (t0 + timedelta(minutes=5)).isoformat(timespec="seconds")
    t2_iso = (t0 + timedelta(minutes=10)).isoformat(timespec="seconds")

    bars = [
        ("CL1", t1_iso, "5m", "YAHOO", 90.0, 91.0, 89.9, 90.8, 100.0, t1_iso),
        ("CL1", t2_iso, "5m", "YAHOO", 90.8, 90.9, 88.5, 88.7, 200.0, t2_iso),
    ]
    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    conn.commit()

    stats = playbook_tracker.evaluate_active_playbooks(conn, as_of=t2_iso)
    assert stats["activated"] == 1
    assert stats["resolved_losses"] == 1

    row = conn.execute(
        "SELECT state, pnl_points, mae_points FROM playbook_scenarios WHERE scenario_uid = ?",
        (uids[0],),
    ).fetchone()
    assert row[0] == "HIT_STOP_LOSS"
    assert row[1] < 0.0  # negative pnl
    assert row[2] == 2.0  # MAE: 90.5 - 88.5 = 2.0


def test_evaluate_counterfactual_outcomes(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t0_iso = t0.isoformat(timespec="seconds")

    # Create stopped-out trade
    playbook_payload = {
        "symbol": "NQ1",
        "as_of": t0_iso,
        "last_price": 31000.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": "SCENARIO_LONG_STOPPED",
                "horizon": "INTRADAY",
                "title": "Long Stopped",
                "direction": "LONG",
                "trigger_condition": "above 31050",
                "trigger_price": 31050.0,
                "target_profit": 31300.0,
                "invalidation_level": 30950.0,
                "risk_reward_ratio": 2.5,
            }
        ],
    }
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload)

    # Activation bar at 14:05 (31060), Stop hit bar at 14:10 (30940)
    t1 = (t0 + timedelta(minutes=5)).isoformat(timespec="seconds")
    t2 = (t0 + timedelta(minutes=10)).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("NQ1", t1, "5m", "YAHOO", 31040.0, 31070.0, 31030.0, 31060.0, 100.0, t1),
            ("NQ1", t2, "5m", "YAHOO", 31060.0, 31060.0, 30940.0, 30940.0, 200.0, t2),
        ],
    )
    conn.commit()
    playbook_tracker.evaluate_active_playbooks(conn, as_of=t2)

    # Add forward bars 2 hours later where price crashes to 30700 (proving stop loss saved capital)
    forward_bars = [
        (
            "NQ1",
            (t0 + timedelta(hours=1, minutes=m)).isoformat(timespec="seconds"),
            "5m",
            "YAHOO",
            30900.0,
            30910.0,
            30700.0,
            30720.0,
            100.0,
            "now",
        )
        for m in range(0, 60, 5)
    ]
    conn.executemany(
        "INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        forward_bars,
    )
    conn.commit()

    # Run counterfactual audit 2.5h later
    t_audit = t0 + timedelta(hours=2, minutes=30)
    cf_stats = playbook_tracker.evaluate_counterfactual_outcomes(
        conn, as_of=t_audit, forward_hours=2
    )
    assert cf_stats["audited"] == 1
    assert cf_stats["good_stop_loss"] == 1

    row = conn.execute(
        "SELECT payload_json FROM playbook_scenarios WHERE scenario_uid = ?", (uids[0],)
    ).fetchone()
    data = json.loads(row[0])
    assert "counterfactual_audit" in data
    assert "GOOD_STOP_LOSS" in data["counterfactual_audit"]["verdict"]

    conn.close()


def test_evaluate_active_playbooks_breakeven_ratchet(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t0_iso = t0.isoformat(timespec="seconds")

    playbook_payload = {
        "symbol": "NQ1",
        "as_of": t0_iso,
        "last_price": 31000.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": "SCENARIO_EXPANSION_LONG",
                "horizon": "INTRADAY",
                "title": "Long Expansion",
                "direction": "LONG",
                "trigger_condition": "close above 31100",
                "trigger_price": 31100.0,
                "target_profit": 31300.0,
                "invalidation_level": 31000.0,  # Risk is 100 pts (31100 -> 31000)
                "risk_reward_ratio": 2.0,
            }
        ],
    }
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload)

    # Bar 1 triggers at 31100
    # Bar 2 rallies to 31220 (+120 pts MFE >= 100 pts risk -> Breakeven ratchet activated)
    # Bar 3 drops back to 31090 (breaches entry 31100 -> HIT_BREAKEVEN)
    t1_iso = (t0 + timedelta(minutes=5)).isoformat(timespec="seconds")
    t2_iso = (t0 + timedelta(minutes=10)).isoformat(timespec="seconds")
    t3_iso = (t0 + timedelta(minutes=15)).isoformat(timespec="seconds")

    bars = [
        ("NQ1", t1_iso, "5m", "YAHOO", 31050.0, 31110.0, 31040.0, 31100.0, 500.0, t1_iso),
        ("NQ1", t2_iso, "5m", "YAHOO", 31100.0, 31220.0, 31090.0, 31200.0, 600.0, t2_iso),
        ("NQ1", t3_iso, "5m", "YAHOO", 31200.0, 31210.0, 31080.0, 31090.0, 700.0, t3_iso),
    ]
    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    conn.commit()

    stats = playbook_tracker.evaluate_active_playbooks(conn, as_of=t3_iso)
    assert stats["activated"] == 1
    assert stats["resolved_wins"] == 1  # Banked +0.50R profit on first half

    row = conn.execute(
        "SELECT state, entry_price, exit_price, pnl_points, r_multiple, payload_json FROM playbook_scenarios WHERE scenario_uid = ?",
        (uids[0],),
    ).fetchone()
    # breakeven exit after a partial is its own outcome, no longer relabelled as a target hit
    assert row[0] == "CANCELLED_EXPIRED"
    assert json.loads(row[5])["outcome"] == "HIT_BREAKEVEN"
    assert row[1] == 31100.0  # entry
    assert row[2] == 31100.0  # breakeven exit
    assert row[3] == 50.0  # +50 pts net profit banked
    assert row[4] == 0.5  # +0.50R realized

    payload = json.loads(row[5])
    events = [e["event"] for e in payload["decision_log"]]
    assert "PARTIAL_TP_50" in events
    assert "HIT_BREAKEVEN" in events
    conn.close()


def test_evaluate_active_playbooks_opposing_invalidation(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t0_iso = t0.isoformat(timespec="seconds")

    playbook_payload = {
        "symbol": "ES1",
        "as_of": t0_iso,
        "last_price": 5000.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": "SCENARIO_LONG",
                "horizon": "INTRADAY",
                "title": "ES Long",
                "direction": "LONG",
                "trigger_condition": "above 5020",
                "trigger_price": 5020.0,
                "target_profit": 5050.0,
                "invalidation_level": 4980.0,
                "risk_reward_ratio": 1.5,
            },
            {
                "id": "SCENARIO_SHORT",
                "horizon": "INTRADAY",
                "title": "ES Short",
                "direction": "SHORT",
                "trigger_condition": "below 4980",
                "trigger_price": 4980.0,
                "target_profit": 4940.0,
                "invalidation_level": 5010.0,
                "risk_reward_ratio": 1.5,
            },
        ],
    }
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload)
    assert len(uids) == 2

    # Bar 1 drops to 4975:
    # 1. Triggers SHORT (since 4975 <= 4980) -> SCENARIO_SHORT becomes ACTIVE
    # 2. SCENARIO_LONG had invalidation at 4980, and price dropped to 4975 -> invalidated / cancelled superseded!
    t1_iso = (t0 + timedelta(minutes=5)).isoformat(timespec="seconds")
    bars = [
        ("ES1", t1_iso, "5m", "YAHOO", 4995.0, 4998.0, 4970.0, 4975.0, 500.0, t1_iso),
    ]
    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    conn.commit()

    stats = playbook_tracker.evaluate_active_playbooks(conn, as_of=t1_iso)
    assert stats["activated"] == 1

    states = dict(
        conn.execute(
            "SELECT scenario_id, state FROM playbook_scenarios WHERE scenario_uid IN (?, ?)",
            (uids[0], uids[1]),
        ).fetchall()
    )
    assert states["SCENARIO_SHORT"] == "ACTIVE"
    assert states["SCENARIO_LONG"] == "CANCELLED_EXPIRED"
    long_payload = conn.execute(
        "SELECT payload_json FROM playbook_scenarios WHERE scenario_uid = ?", (uids[0],)
    ).fetchone()[0]
    assert json.loads(long_payload)["outcome"] == "INVALIDATED_PRE_ENTRY"

    conn.close()


def test_evaluate_active_playbooks_early_vwap_exit(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t0_iso = t0.isoformat(timespec="seconds")

    playbook_payload = {
        "symbol": "NQ1",
        "as_of": t0_iso,
        "last_price": 31000.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": "SCENARIO_EXPANSION_LONG",
                "horizon": "INTRADAY",
                "title": "Long Expansion",
                "direction": "LONG",
                "trigger_condition": "close above 31100",
                "trigger_price": 31100.0,
                "target_profit": 31300.0,
                "invalidation_level": 31000.0,
                "risk_reward_ratio": 2.0,
            }
        ],
    }
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload)

    # Bar 1: Triggers at 31100 (high=31120, close=31110)
    # Bar 2: Rallies to 31250 (Hits +1.0R -> locks 50% partial TP at 31200)
    # Bar 3: Drops below VWAP (close=31150)
    # Bar 4: Closes below VWAP second time (close=31140) -> EARLY_FULL_TP
    t1_iso = (t0 + timedelta(minutes=5)).isoformat(timespec="seconds")
    t2_iso = (t0 + timedelta(minutes=10)).isoformat(timespec="seconds")
    t3_iso = (t0 + timedelta(minutes=15)).isoformat(timespec="seconds")
    t4_iso = (t0 + timedelta(minutes=20)).isoformat(timespec="seconds")

    bars = [
        ("NQ1", t1_iso, "5m", "YAHOO", 31050.0, 31120.0, 31040.0, 31110.0, 100.0, t1_iso),
        ("NQ1", t2_iso, "5m", "YAHOO", 31110.0, 31250.0, 31100.0, 31240.0, 1000.0, t2_iso),
        ("NQ1", t3_iso, "5m", "YAHOO", 31240.0, 31240.0, 31140.0, 31150.0, 100.0, t3_iso),
        ("NQ1", t4_iso, "5m", "YAHOO", 31150.0, 31160.0, 31130.0, 31140.0, 100.0, t4_iso),
    ]
    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    conn.commit()

    stats = playbook_tracker.evaluate_active_playbooks(conn, as_of=t4_iso)
    assert stats["activated"] == 1
    assert stats["resolved_wins"] == 1

    row = conn.execute(
        "SELECT state, exit_price, pnl_points, r_multiple, payload_json FROM playbook_scenarios WHERE scenario_uid = ?",
        (uids[0],),
    ).fetchone()
    assert row[0] == "CANCELLED_EXPIRED"  # early exit is not a target hit
    assert row[1] == 31140.0  # exited early at bar 4 close
    assert row[2] > 50.0  # locked partial + early exit profit
    assert row[3] > 0.5  # > 0.5R secured

    payload = json.loads(row[4])
    events = [e["event"] for e in payload["decision_log"]]
    assert "PARTIAL_TP_50" in events
    assert "EARLY_FULL_TP" in events
    assert payload["outcome"] == "EARLY_FULL_TP"

    conn.close()


# --- golden tests: fill/exit realism on synthetic OHLC bars ---------------------------------

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)  # session 2026-10-05 closes 21:00 UTC (17:00 EDT)


def _ts(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _record(conn, *scenarios, as_of=None, symbol="NQ1"):
    """scenarios: (id, direction, trigger, stop, target)."""
    payload = {
        "symbol": symbol,
        "as_of": as_of or _ts(0),
        "last_price": 100.0,
        "reference_levels": {"active_session_current": "2026-10-05"},
        "scenarios": [
            {
                "id": sid,
                "horizon": "INTRADAY",
                "title": sid,
                "direction": d,
                "trigger_condition": "test",
                "trigger_price": trig,
                "target_profit": tgt,
                "invalidation_level": stop,
                "risk_reward_ratio": 2.0,
            }
            for sid, d, trig, stop, tgt in scenarios
        ],
    }
    return playbook_tracker.record_playbook_scenarios(conn, payload)


def _bars(conn, *rows, symbol="NQ1"):
    """rows: (minutes, open, high, low, close)."""
    conn.executemany(
        "INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close,"
        " volume, fetched_at) VALUES (?, ?, '5m', 'YAHOO', ?, ?, ?, ?, 100.0, 'now')",
        [(symbol, _ts(m), o, h, lo, c) for m, o, h, lo, c in rows],
    )
    conn.commit()


def _row(conn, uid):
    r = conn.execute(
        "SELECT state, entry_price, exit_price, r_multiple, mfe_points, mae_points, payload_json"
        " FROM playbook_scenarios WHERE scenario_uid = ?",
        (uid,),
    ).fetchone()
    return {
        "state": r[0],
        "entry": r[1],
        "exit": r[2],
        "r": r[3],
        "mfe": r[4],
        "mae": r[5],
        "outcome": json.loads(r[6]).get("outcome"),
        "events": [e["event"] for e in json.loads(r[6])["decision_log"]],
    }


def test_stop_and_target_in_same_bar_stop_wins(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    (uid,) = _record(conn, ("L", "LONG", 100.0, 95.0, 110.0))
    _bars(conn, (5, 99.0, 101.0, 98.0, 100.5), (10, 100.0, 111.0, 94.0, 100.0))
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(10))
    r = _row(conn, uid)
    assert (r["state"], r["outcome"], r["exit"], r["r"]) == (
        "HIT_STOP_LOSS",
        "HIT_STOP_LOSS",
        95.0,
        -1.0,
    )


def test_gap_through_trigger_and_gap_through_stop_fill_at_open(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    (uid,) = _record(conn, ("L", "LONG", 100.0, 95.0, 120.0))
    _bars(conn, (5, 102.0, 103.0, 101.5, 102.5), (10, 93.0, 94.0, 92.0, 93.0))
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(10))
    r = _row(conn, uid)
    assert r["entry"] == 102.0  # gapped above trigger -> filled at open
    assert r["exit"] == 93.0  # gapped below stop -> filled at open
    assert r["r"] == round(-9.0 / 7.0, 2)  # R against the risk from the actual fill


def test_activation_bar_favourable_extreme_is_not_credited(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    (uid,) = _record(conn, ("L", "LONG", 100.0, 95.0, 105.0))
    _bars(conn, (5, 99.0, 106.0, 99.0, 104.0))  # high may print before the 100 fill
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(5))
    r = _row(conn, uid)
    assert (r["state"], r["mfe"]) == ("ACTIVE", 0.0)


def test_active_scenario_is_rescanned_from_triggered_at(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    (uid,) = _record(conn, ("L", "LONG", 100.0, 95.0, 110.0))
    _bars(
        conn,
        (5, 97.0, 99.0, 95.5, 98.0),  # pre-trigger dip must not count as MAE
        (10, 99.0, 100.5, 99.0, 100.2),  # activation
    )
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(10))
    _bars(conn, (15, 100.2, 101.0, 99.8, 100.5))
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(15))
    r = _row(conn, uid)
    assert r["state"] == "ACTIVE"
    assert r["mae"] == 1.0  # activation-bar low 99.0 vs fill 100.0, not the 95.5 pre-trigger low
    assert r["mfe"] == 1.0
    assert r["events"].count("TRIGGERED_ACTIVE") == 1


def test_session_expiry_time_exit_and_no_trigger(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    uid_a, uid_p = _record(
        conn, ("A", "LONG", 100.0, 95.0, 110.0), ("P", "LONG", 150.0, 95.0, 170.0)
    )
    _bars(
        conn,
        (5, 99.0, 100.5, 99.0, 100.2),
        (415, 100.5, 101.5, 100.4, 101.0),  # 20:55 UTC, last bar of the session
        (450, 101.0, 120.0, 100.9, 119.0),  # 21:30 UTC, after the session close
    )
    stats = playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(480))
    a, p = _row(conn, uid_a), _row(conn, uid_p)
    assert (a["outcome"], a["exit"], a["r"]) == ("TIME_EXIT", 101.0, 0.2)
    assert p["outcome"] == "NO_TRIGGER"
    assert p["entry"] is None
    assert stats["resolved_wins"] == 1


def test_flip_is_counted_as_a_loss_in_metrics(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    (uid_long,) = _record(conn, ("L", "LONG", 100.0, 95.0, 110.0))
    _bars(conn, (5, 99.5, 100.5, 99.5, 100.2))
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(5))
    (uid_short,) = _record(conn, ("S", "SHORT", 98.0, 103.0, 90.0), as_of=_ts(6))
    _bars(conn, (10, 99.5, 99.6, 97.5, 97.8))
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(10))

    lng, sht = _row(conn, uid_long), _row(conn, uid_short)
    assert (lng["outcome"], lng["exit"], lng["r"]) == ("FLIPPED", 98.0, -0.4)
    assert sht["state"] == "ACTIVE"
    assert sht["entry"] == 98.0

    perf = playbook_tracker.get_playbook_performance_metrics(conn, symbol="NQ1")
    assert perf["completed_trades"] == 1
    assert perf["losses"] == 1
    assert perf["expectancy_r"] == -0.4
    assert perf["active"] == 1


def test_activation_cancels_only_opposite_direction_pending(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    uid_l1, uid_l2, uid_s = _record(
        conn,
        ("L1", "LONG", 100.0, 95.0, 110.0),
        ("L2", "LONG", 104.0, 99.0, 115.0),
        ("S", "SHORT", 90.0, 106.0, 80.0),
    )
    _bars(conn, (5, 99.5, 100.5, 99.5, 100.2))
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(5))
    assert _row(conn, uid_l1)["state"] == "ACTIVE"
    assert _row(conn, uid_l2)["state"] == "PENDING_TRIGGER"
    assert _row(conn, uid_s)["outcome"] == "SUPERSEDED"


def test_news_shock_has_no_look_ahead_and_r_uses_original_risk(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    (uid,) = _record(conn, ("L", "LONG", 100.0, 90.0, 120.0))
    _bars(conn, (5, 99.5, 100.5, 99.5, 100.2), (10, 100.0, 100.0, 94.0, 96.0))
    t_news = _ts(8)
    conn.execute(
        "INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id,"
        " relevance, novelty, fetched_at, published_at_utc) VALUES ('shock', 'RSS_FED', 'Shock',"
        " 'u', 's', '[]', 'c', 1.0, 1.0, ?, ?)",
        (t_news, t_news),
    )
    conn.execute(
        "INSERT INTO news_intelligence (news_id, asset, stance, magnitude, confidence,"
        " macro_channel, impact_horizon, evidence_level, evidence_quote, transmission_rationale,"
        " created_at, published_at_utc) VALUES ('shock', 'NQ1', 'BEARISH', 0.8, 0.9,"
        " 'RATES_POLICY', 'INTRADAY_VOLATILITY', 'OBSERVED', 'q', 'r', ?, ?)",
        (t_news, t_news),
    )
    conn.commit()

    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(10))
    r = _row(conn, uid)
    assert r["state"] == "ACTIVE"  # bar low 94 printed before the shock was known
    assert "NEWS_SHOCK" in r["events"]

    _bars(conn, (15, 96.0, 96.5, 94.5, 95.0))
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(15))
    r = _row(conn, uid)
    assert (r["outcome"], r["exit"]) == ("HIT_STOP_LOSS", 95.0)  # tightened stop after the shock
    assert r["r"] == -0.5  # against the original 10-point risk, not the tightened 5


def test_performance_metrics_trades_ci_and_non_trades(tmp_path):
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    rows = [
        ("w1", "HIT_TARGET_WIN", 100.0, 2.0, "HIT_TARGET_WIN"),
        ("w2", "HIT_TARGET_WIN", 100.0, 1.0, "HIT_TARGET_WIN"),
        ("w3", "CANCELLED_EXPIRED", 100.0, 0.5, "HIT_BREAKEVEN"),
        ("l1", "HIT_STOP_LOSS", 100.0, -1.0, "HIT_STOP_LOSS"),
        ("l2", "CANCELLED_EXPIRED", 100.0, -0.3, "TIME_EXIT"),
        ("n1", "CANCELLED_EXPIRED", None, 0.0, "INVALIDATED_PRE_ENTRY"),
        ("p1", "PENDING_TRIGGER", None, 0.0, None),
    ]
    conn.executemany(
        "INSERT INTO playbook_scenarios (scenario_uid, symbol, horizon, direction, scenario_id,"
        " title, trigger_condition, target_profit, invalidation_level, risk_reward_ratio,"
        " created_at_utc, session_id, state, entry_price, r_multiple, mfe_points, mae_points,"
        " payload_json) VALUES (?, 'NQ1', 'INTRADAY', 'LONG', ?, 't', 't', 1, 1, 1,"
        " '2026-10-05', '2026-10-05', ?, ?, ?, 2.0, 1.0, ?)",
        [(u, u, st, e, r, json.dumps({"outcome": o})) for u, st, e, r, o in rows],
    )
    conn.commit()

    perf = playbook_tracker.get_playbook_performance_metrics(conn)
    assert perf["completed_trades"] == 5
    assert (perf["wins"], perf["losses"], perf["breakevens"]) == (3, 2, 0)
    assert perf["win_rate_pct"] == 60.0
    assert perf["win_rate_ci95_pct"] == [23.1, 88.2]  # Wilson score interval, 3/5
    assert perf["expectancy_r"] == 0.44
    assert perf["avg_r_multiple"] == 0.44
    assert perf["profit_factor"] == 2.69  # 3.5R / 1.3R
    assert perf["avg_mfe"] == 2.0
    assert perf["non_trades"] == {"INVALIDATED_PRE_ENTRY": 1}
    assert perf["invalidated"] == 1
    assert perf["pending"] == 1
