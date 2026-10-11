"""watcher scenario_events: one push per fill and per close, nothing from the backlog."""

from datetime import UTC, datetime, timedelta

from arkwatch import db
from arkwatch.qa import watcher
from arkwatch.signals import playbook_tracker

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)


def _ts(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _trade(tmp_path, *bars):
    conn = db.get_conn(tmp_path / "s.db", allow_init=True)
    payload = {
        "symbol": "NQ1",
        "as_of": _ts(0),
        "last_price": 100.0,
        "session_id": "2026-10-05",
        "scenarios": [
            {
                "id": "L",
                "horizon": "INTRADAY",
                "title": "VAH reclaim",
                "direction": "LONG",
                "trigger_condition": "test",
                "trigger_price": 100.0,
                "target_profit": 110.0,
                "invalidation_level": 95.0,
                "risk_reward_ratio": 2.0,
            }
        ],
    }
    (uid,) = playbook_tracker.record_playbook_scenarios(conn, payload, cfd_basis_offset=-12.5)
    conn.executemany(
        "INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close,"
        " volume, fetched_at) VALUES ('NQ1', ?, '5m', 'YAHOO', ?, ?, ?, ?, 100.0, 'now')",
        [(_ts(m), o, h, lo, c) for m, o, h, lo, c in bars],
    )
    playbook_tracker.evaluate_active_playbooks(conn, as_of=_ts(30))
    return conn, uid


def test_fill_then_close_each_push_once_with_levels(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    conn, uid = _trade(tmp_path, (5, 99.5, 100.5, 99.5, 100.2), (10, 100.2, 111.0, 100.0, 110.5))
    ev = watcher.scenario_events(conn, now=T0 + timedelta(hours=1))
    assert [(e[0], e[1]) for e in ev] == [
        ("scenario_active", f"scenario_active@{uid}"),
        ("scenario_closed", f"scenario_closed@{uid}"),
    ]
    assert ev[0][2] == "NQ1 LONG filled at 100.00 (CFD 87.50), intraday: VAH reclaim"
    assert "Stop 95.00, target 110.00, R:R 2.0" in ev[0][3]
    assert ev[1][2].startswith("NQ1 LONG closed HIT_TARGET_WIN at 110.00, +")
    # the watcher dedups per scenario: a second pass enqueues nothing
    for e in ev:
        assert watcher._fire(conn, e[0], *e[2:], cooldown_key=e[1])
        assert not watcher._fire(conn, e[0], *e[2:], cooldown_key=e[1])
    assert conn.execute("SELECT COUNT(*) FROM alert_deliveries").fetchone() == (2,)


def test_open_trade_pushes_only_the_fill(tmp_path):
    conn, _ = _trade(tmp_path, (5, 99.5, 100.5, 99.5, 100.2), (10, 100.2, 103.0, 99.0, 102.5))
    ev = watcher.scenario_events(conn, now=T0 + timedelta(hours=1))
    assert [e[0] for e in ev] == ["scenario_active"]


def test_backlog_outside_the_window_stays_quiet(tmp_path):
    conn, _ = _trade(tmp_path, (5, 99.5, 100.5, 99.5, 100.2), (10, 100.2, 111.0, 100.0, 110.5))
    late = T0 + timedelta(hours=watcher.SCENARIO_EVENT_WINDOW_H + 1)
    assert watcher.scenario_events(conn, now=late) == []
