"""Offline tests for caldist weekly-family aggregation (energy channel tier 3)."""

from __future__ import annotations

import sqlite3

from arkwatch.fetchers import caldist

SCHEMA = """
CREATE TABLE events (event_uid TEXT PRIMARY KEY, ts_utc TEXT, release_ts TEXT,
 country TEXT, name TEXT, normalized_name TEXT, importance TEXT,
 consensus REAL, consensus_source TEXT, actual REAL, actual_source TEXT,
 previous REAL, surprise_z REAL, is_curated INTEGER, indicator_key TEXT)
"""


def _db(tmp_path, rows):
    p = tmp_path / "ev.db"
    conn = sqlite3.connect(p)
    conn.execute(SCHEMA)
    conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(p)


def _ev(uid, ts, name, actual):
    return (
        uid,
        ts,
        None,
        "US",
        name,
        name,
        "medium",
        None,
        None,
        actual,
        "FMP",
        None,
        None,
        0,
        "TEST FAM",
    )


class TestAggModes:
    def test_sum_with_twins_and_week_tokens(self, tmp_path):
        rows = [
            _ev("1", "2026-08-06", "TEST FAM JUL 30", -4.45),
            _ev("1b", "2026-08-06", "TEST FAM", -4.45),  # token-less TV twin
            _ev("2", "2026-08-13", "TEST FAM AUG 06", 17.42),
            _ev("3", "2026-08-20", "TEST FAM AUG 13", 4.40),
            _ev("4", "2026-08-27", "TEST FAM AUG 20", 0.10),
            _ev("5", "2026-09-03", "TEST FAM AUG 27", -4.45),  # Aug-week in Sep release
        ]
        db_path = _db(tmp_path, rows)
        out = caldist.family_rows("TEST FAM", "week_ending", "sum", db_path=db_path)
        by = {r["ts"]: r["value"] for r in out}
        assert by["2026-07-01"] == -4.45  # week-ending Jul 30, twin counted once
        assert by["2026-08-01"] == round(17.42 + 4.40 + 0.10 - 4.45, 3)
        assert "2026-09-01" not in by  # nothing week-ended in Sep

    def test_last_mode_takes_latest_release(self, tmp_path):
        rows = [
            _ev("1", "2026-08-07", "TEST FAM", 440.0),
            _ev("2", "2026-08-21", "TEST FAM", 450.0),
            _ev("3", "2026-08-28", "TEST FAM", 448.0),
        ]
        db_path = _db(tmp_path, rows)
        out = caldist.family_rows("TEST FAM", "release_month", "last", db_path=db_path)
        assert out == [{"ts": "2026-08-01", "value": 448.0}]

    def test_max_mode_token_months(self, tmp_path):
        rows = [
            _ev("1", "2026-08-07", "TEST FAM JUL", 48.5),
            _ev("2", "2026-08-21", "TEST FAM AUG", 49.0),
        ]
        db_path = _db(tmp_path, rows)
        out = caldist.family_rows("TEST FAM", "m_minus_1", "max", db_path=db_path)
        assert out == [
            {"ts": "2026-07-01", "value": 48.5},
            {"ts": "2026-08-01", "value": 49.0},
        ]
