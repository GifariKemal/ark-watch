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
    assert row[3] == 150.0    # pnl: 31300 - 31150
    assert row[4] > 0.0       # positive R-multiple
    assert row[5] == 200.0    # MFE: 31350 - 31150 = 200 pts

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

    conn.close()
