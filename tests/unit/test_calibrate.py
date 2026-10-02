"""Unit tests for automated quarterly calibration of golden anchors (D-010)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from arkwatch import db
from arkwatch.qa import calibrate


def _setup_db() -> sqlite3.Connection:
    conn = db.get_conn(":memory:", allow_init=True)
    conn.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit, value_format, freq, primary_source) "
        "VALUES ('FRED:DGS10', '10Y Yield', 'A', 0, 'pct', 'pct', 'D', 'FRED')"
    )
    conn.commit()
    return conn


def test_audit_anchors_detects_ok_and_drifted():
    conn = _setup_db()
    # Seed matching observation for 2026-08-27
    conn.execute(
        "INSERT INTO raw_observations(series_id, ts, release_ts, value, vintage_ts, source, fetched_at) "
        "VALUES ('FRED:DGS10', '2026-08-27', '2026-08-27', 4.67, 'realtime', 'FRED', '2026-08-27T20:00:00+00:00')"
    )
    conn.commit()

    test_anchors = [
        {"series_id": "DGS10", "anchor_date": "2026-08-27", "expected": 4.67, "tolerance": 0.02},
        {
            "series_id": "DGS10",
            "anchor_date": "2026-08-27",
            "expected": 4.00,
            "tolerance": 0.02,
        },  # drifted
    ]

    audits = calibrate.audit_anchors(conn, test_anchors, today=date(2026, 9, 1))
    assert len(audits) == 2
    assert audits[0].status == "OK"
    assert audits[0].drift == 0.0
    assert audits[1].status == "DRIFTED"
    assert audits[1].drift == pytest.approx(0.67)


def test_audit_anchors_detects_expired():
    conn = _setup_db()
    conn.execute(
        "INSERT INTO raw_observations(series_id, ts, release_ts, value, vintage_ts, source, fetched_at) "
        "VALUES ('FRED:DGS10', '2026-01-01', '2026-01-01', 4.00, 'realtime', 'FRED', '2026-01-01T20:00:00+00:00')"
    )
    conn.commit()

    old_anchor = [
        {"series_id": "DGS10", "anchor_date": "2026-01-01", "expected": 4.00, "tolerance": 0.02}
    ]
    # Evaluated on 2026-06-01 -> 151 days old > 90d
    audits = calibrate.audit_anchors(conn, old_anchor, max_age_days=90, today=date(2026, 6, 1))
    assert len(audits) == 1
    assert audits[0].status == "EXPIRED"
    assert audits[0].age_days == 151


def test_audit_anchors_detects_unseen():
    conn = _setup_db()
    anchor = [
        {"series_id": "DGS10", "anchor_date": "2026-05-01", "expected": 4.50, "tolerance": 0.02}
    ]
    audits = calibrate.audit_anchors(conn, anchor, today=date(2026, 5, 2))
    assert len(audits) == 1
    assert audits[0].status == "UNSEEN"


def test_propose_fresh_anchor():
    conn = _setup_db()
    # Seed observation 20 days before evaluation date
    conn.execute(
        "INSERT INTO raw_observations(series_id, ts, release_ts, value, vintage_ts, source, fetched_at) "
        "VALUES ('FRED:DGS10', '2026-08-10', '2026-08-10', 4.55, 'realtime', 'FRED', '2026-08-10T20:00:00+00:00')"
    )
    conn.commit()

    entry = {
        "series_id": "FRED:DGS10",
        "unit": "pct",
        "value_format": "pct",
    }

    cand = calibrate.propose_fresh_anchor(conn, entry, today=date(2026, 9, 1))
    assert cand is not None
    assert cand["series_id"] == "DGS10"
    assert cand["anchor_date"] == "2026-08-10"
    assert cand["expected"] == 4.55
    assert cand["tolerance"] == 0.02


def test_calibrate_cli(tmp_path, capsys):
    db_file = tmp_path / "test.db"
    conn = db.get_conn(db_file, allow_init=True)
    conn.close()

    exit_code = calibrate.main(["--db", str(db_file), "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "summary" in data
    assert "audits" in data
