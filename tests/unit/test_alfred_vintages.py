"""Unit tests for ALFRED first-print vintage harvesting (D-005)."""

from __future__ import annotations

import sqlite3

from arkwatch import db
from arkwatch.qa import alfred


def _setup_test_db() -> sqlite3.Connection:
    conn = db.get_conn(":memory:", allow_init=True)
    # Seed series_registry so foreign keys pass
    for sid in alfred.CLASS_A_VINTAGE_SERIES:
        conn.execute(
            "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit, value_format, freq, primary_source) "
            "VALUES (?, ?, 'D', 1, 'count', '{:,.0f}', 'M', 'FRED')",
            (f"FRED:{sid}", sid),
        )
    conn.commit()
    return conn


def test_harvest_alfred_vintages_inserts_first_print_and_release_ts(monkeypatch):
    conn = _setup_test_db()

    sample_obs = [
        {"ts": "2024-01-01", "value": 157700.0, "realtime_start": "2024-02-02"},
        {"ts": "2024-02-01", "value": 157808.0, "realtime_start": "2024-03-08"},
    ]

    def mock_fetch(series_id, **kwargs):
        assert series_id == "PAYEMS"
        assert kwargs.get("output_type") == 4
        return sample_obs

    monkeypatch.setattr(alfred, "fetch_observations", mock_fetch)

    counts = alfred.harvest_alfred_vintages(conn, ["PAYEMS"], start="2024-01-01")
    assert counts["FRED:PAYEMS"] == 2

    rows = conn.execute(
        "SELECT series_id, ts, release_ts, value, vintage_ts, source FROM raw_observations "
        "WHERE series_id='FRED:PAYEMS' ORDER BY ts ASC"
    ).fetchall()

    assert len(rows) == 2
    assert rows[0] == ("FRED:PAYEMS", "2024-01-01", "2024-02-02", 157700.0, "first", "FRED")
    assert rows[1] == ("FRED:PAYEMS", "2024-02-01", "2024-03-08", 157808.0, "first", "FRED")

    log_entry = conn.execute(
        "SELECT fetcher, target, status, rows FROM fetch_log WHERE target='FRED:PAYEMS:VINTAGE'"
    ).fetchone()
    assert log_entry == ("alfred", "FRED:PAYEMS:VINTAGE", "OK", 2)


def test_harvest_alfred_vintages_updates_existing_placeholder_row(monkeypatch):
    conn = _setup_test_db()

    # Pre-seed legacy placeholder row with release_ts='na'
    conn.execute(
        "INSERT INTO raw_observations(series_id, ts, release_ts, value, vintage_ts, source, fetched_at) "
        "VALUES ('FRED:PAYEMS', '2024-01-01', 'na', 999.0, 'first', 'FRED', '2024-01-01T00:00:00+00:00')"
    )
    conn.commit()

    sample_obs = [
        {"ts": "2024-01-01", "value": 157700.0, "realtime_start": "2024-02-02"},
    ]

    monkeypatch.setattr(alfred, "fetch_observations", lambda _s, **_kw: sample_obs)

    counts = alfred.harvest_alfred_vintages(conn, ["PAYEMS"])
    assert counts["FRED:PAYEMS"] == 1

    row = conn.execute(
        "SELECT release_ts, value FROM raw_observations WHERE series_id='FRED:PAYEMS' AND ts='2024-01-01'"
    ).fetchone()

    # Verified: placeholder 'na' updated to real release_ts '2024-02-02'
    assert row[0] == "2024-02-02"
    assert row[1] == 157700.0


def test_harvest_alfred_vintages_handles_fetch_error_and_logs(monkeypatch):
    conn = _setup_test_db()

    def mock_fail(_sid, **_kw):
        raise RuntimeError("API rate limit exceeded")

    monkeypatch.setattr(alfred, "fetch_observations", mock_fail)

    counts = alfred.harvest_alfred_vintages(conn, ["PAYEMS"])
    assert counts["FRED:PAYEMS"] == -1

    log_entry = conn.execute(
        "SELECT status, error FROM fetch_log WHERE target='FRED:PAYEMS:VINTAGE'"
    ).fetchone()
    assert log_entry[0] == "ERROR"
    assert "rate limit" in log_entry[1]


def test_main_cli_vintages_flag(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    conn = db.get_conn(db_file, allow_init=True)
    conn.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit, value_format, freq, primary_source) "
        "VALUES ('FRED:PAYEMS', 'PAYEMS', 'D', 1, 'count', '{:,.0f}', 'M', 'FRED')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        alfred,
        "fetch_observations",
        lambda _s, **_kw: [{"ts": "2024-01-01", "value": 157700.0, "realtime_start": "2024-02-02"}],
    )

    exit_code = alfred.main(["--db", str(db_file), "--vintages", "--series", "PAYEMS"])
    assert exit_code == 0
