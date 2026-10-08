"""Offline unit tests for the db/ layer: schema bootstrap, migration guard, storage contracts."""

from __future__ import annotations

import sqlite3

import pytest

from arkwatch import db


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


def test_init_creates_all_tables_and_version(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "series_registry",
        "raw_observations",
        "instrument_prices",
        "events",
        "indicator_stats",
        "indicator_sigma_snapshots",
        "indicator_sigma_vintages",
        "cot_raw",
        "flows_daily",
        "flows_periodic",
        "cme_settlements",
        "cvol_snapshots",
        "voi_daily",
        "fedwatch_snapshots",
        "computed_signals",
        "fetch_log",
        "brief_log",
        "brief_deliveries",
        "alert_deliveries",
        "schema_migrations",
    }
    assert expected <= tables, f"missing tables: {expected - tables}"
    # Regression guard: golden_anchors and cot_snapshots were dropped as ghost
    # tables and must never reappear.
    assert "golden_anchors" not in tables and "cot_snapshots" not in tables
    assert (
        conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == db.SCHEMA_VERSION
    )


def test_pragma_pack(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_gdelt_gzip_migration_preserves_existing_raw_json(tmp_path):
    path = tmp_path / "gdelt-v27.db"
    conn = db.get_conn(path, allow_init=True)
    raw_json = '{"EventCode":"042"}'
    conn.execute(
        "INSERT INTO gdelt_events (event_id,event_date,added_at_utc,fetched_at,raw_record_json) "
        "VALUES (?,?,?,?,?)",
        ("event-1", "20260925", "2026-09-25T00:00:00+00:00", "now", raw_json),
    )
    conn.execute(
        "INSERT INTO indicator_stats(indicator,as_of,sigma,n_obs,window,low_conf) "
        "VALUES ('TEST','2026-09-01',1.0,40,'5y-excl10MAD',0)"
    )
    conn.execute(
        "INSERT INTO computed_signals"
        "(signal_id,ts,run_id,computed_at,value,state,inputs_json) "
        "VALUES ('esi','2026-09-01','2026-09-01T23:59:00+00:00',"
        "'2026-09-01T23:59:00+00:00',0,'NEUTRAL','{}')"
    )
    conn.execute(
        "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
        "actual,consensus,surprise_z,indicator_key) VALUES "
        "('event-1','2026-09-01T12:00:00+00:00','2026-09-01T12:00:00+00:00',"
        "'US','Test','Test',11,10,1,'TEST')"
    )
    for table in ("gdelt_events", "gdelt_mentions", "gdelt_gkg"):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN raw_record_gzip")
    conn.execute("DROP TABLE gdelt_feed_state")
    conn.execute("DROP TABLE indicator_sigma_vintages")
    conn.execute("DROP TABLE indicator_sigma_snapshots")
    for column in (
        "sigma_vintage",
        "sigma_snapshot_id",
        "sigma_n_obs",
        "sigma_window",
        "sigma_low_conf",
    ):
        conn.execute(f"ALTER TABLE events DROP COLUMN {column}")
    # undo v33's non-idempotent parts too (the replay re-runs 28..latest)
    conn.execute("ALTER TABLE series_registry DROP COLUMN locked_by_ui")
    conn.execute("ALTER TABLE playbook_scenarios DROP COLUMN note")
    conn.execute("DELETE FROM schema_migrations WHERE version>=28")
    conn.close()

    migrated = db.get_conn(path, allow_init=True)
    assert (
        migrated.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        == db.SCHEMA_VERSION
    )
    assert migrated.execute("SELECT raw_record_json FROM gdelt_events").fetchone()[0] == raw_json
    assert "raw_record_gzip" in {
        row[1] for row in migrated.execute("PRAGMA table_info(gdelt_events)")
    }
    assert migrated.execute("SELECT COUNT(*) FROM gdelt_feed_state").fetchone()[0] == 0
    sigma_snapshot = migrated.execute(
        "SELECT snapshot_id,n_indicators FROM indicator_sigma_snapshots"
    ).fetchone()
    assert sigma_snapshot == ("2026-09-01T23:59:00+00:00", 1)
    assert migrated.execute(
        "SELECT sigma_vintage,sigma_snapshot_id,sigma_n_obs FROM events WHERE event_uid='event-1'"
    ).fetchone() == (1.0, sigma_snapshot[0], 40)
    sigma_snapshot = migrated.execute(
        "SELECT snapshot_id,n_indicators FROM indicator_sigma_snapshots"
    ).fetchone()
    assert sigma_snapshot == ("2026-09-01T23:59:00+00:00", 1)
    assert migrated.execute(
        "SELECT sigma_vintage,sigma_snapshot_id,sigma_n_obs FROM events WHERE event_uid='event-1'"
    ).fetchone() == (1.0, sigma_snapshot[0], 40)
    migrated.close()


def test_guard_rejects_older_schema(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    # Bootstrap with a minimal migration registry: v1 must itself create
    # schema_migrations, and SCHEMA_VERSION is patched to match the registry.
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        {
            1: "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL); CREATE TABLE t1(a);",
            2: "CREATE TABLE t2(b);",
        },
    )
    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    c = db.get_conn(p, allow_init=True)
    assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
    c.close()
    # Rewind to a genuine v1 database: drop the v2 table and rewrite the version
    # table via DELETE+INSERT (a bulk UPDATE would collide with the PK).
    fc = sqlite3.connect(p)
    fc.execute("DROP TABLE t2")
    fc.execute("DELETE FROM schema_migrations")
    fc.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-08-31')")
    fc.commit()
    fc.close()
    with pytest.raises(RuntimeError, match="migration"):
        db.get_conn(p)
    # allow_init=True only replays missed migrations on top of intact state
    # (v2 does not conflict with anything v1 already created).
    c2 = db.get_conn(p, allow_init=True)
    assert c2.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
    n2 = c2.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='t2'").fetchone()[0]
    assert n2 == 1
    c2.close()


def test_guard_rejects_non_arkwatch_db(tmp_path):
    foreign = tmp_path / "foreign.db"
    fc = sqlite3.connect(foreign)
    fc.execute("CREATE TABLE x(a)")
    fc.commit()
    fc.close()
    with pytest.raises(RuntimeError, match="not an ark-watch DB"):
        db.get_conn(foreign)


def test_insert_observations_append_only_and_dedup(conn):
    conn.execute(
        "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,primary_source)"
        " VALUES ('FRED:DGS10','10Y','A',0,'pct','pct','D','FRED')"
    )
    rows = [("FRED:DGS10", "2026-08-27", 4.67, "FRED", "2026-08-31T00:00:00+00:00")]
    assert db.insert_observations(conn, rows) == 1
    assert db.insert_observations(conn, rows) == 0  # duplicates are skipped (idempotent)
    # A revision becomes a new vintage row, never an UPDATE (append-only contract).
    rows2 = [
        (
            "FRED:DGS10",
            "2026-08-27",
            4.68,
            "FRED",
            "2026-09-01T00:00:00+00:00",
            "na",
            "2026-09-01T00:00:00+00:00",
            4,
        )
    ]
    assert db.insert_observations(conn, rows2) == 1
    n = conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0]
    assert n == 2
