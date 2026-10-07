"""test_playbook_tracker.py — unit tests for automated playbook lifecycle and outcome tracking."""

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

    # Record scenario with +10.0 CFD basis offset
    uids = playbook_tracker.record_playbook_scenarios(conn, playbook_payload, cfd_basis_offset=10.0)
    assert len(uids) == 1
    assert "NQ1-2026-10-05-INTRADAY-SCENARIO_INTRADAY_EXPANSION_LONG" in uids[0]

    row = conn.execute(
        "SELECT target_profit, invalidation_level, cfd_basis_offset, state FROM playbook_scenarios WHERE scenario_uid = ?",
        (uids[0],),
    ).fetchone()
    assert row[0] == 31310.0  # 31300 + 10
    assert row[1] == 31010.0  # 31000 + 10
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
    assert row[1] == 31150.0  # entry
    assert row[2] == 31300.0  # target exit
    assert row[3] == 150.0  # pnl: 31300 - 31150
    assert row[4] > 0.0  # positive R-multiple
    assert row[5] == 200.0  # MFE: 31350 - 31150 = 200 pts

    # Test performance metrics
    perf = playbook_tracker.get_playbook_performance_metrics(conn, symbol="NQ1")
    assert perf["total_scenarios"] == 1
    assert perf["wins"] == 1
    assert perf["win_rate_pct"] == 100.0
    assert perf["avg_mfe"] == 200.0

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
    assert row[2] == 2.3  # MAE: 90.8 - 88.5 = 2.3


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

    import json

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
    assert stats["resolved_breakeven"] == 1

    row = conn.execute(
        "SELECT state, entry_price, exit_price, pnl_points, r_multiple, payload_json FROM playbook_scenarios WHERE scenario_uid = ?",
        (uids[0],),
    ).fetchone()
    assert row[0] == "CANCELLED_EXPIRED"
    assert row[1] == 31100.0  # entry
    assert row[2] == 31100.0  # breakeven exit
    assert row[3] == 0.0  # 0 pts pnl
    assert row[4] == 0.0  # 0R
    import json

    payload = json.loads(row[5])
    assert payload["decision_log"][-1]["event"] == "HIT_BREAKEVEN"

    conn.close()
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

    conn.close()
