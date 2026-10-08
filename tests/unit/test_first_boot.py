"""First boot on an empty volume (VPS deploy 2026-10-09): the daemon creates the
schema before any job, and CAL:* series derived from an empty events table
report 'no data yet' instead of 'unable to open database file'."""

from __future__ import annotations

import pytest

from arkwatch import daemon, db
from arkwatch.fetchers import caldist
from arkwatch.qa import harvest as hv
from arkwatch.qa import verify_sources as vs

CAL_SID = "CAL:" + next(iter(caldist.FAMILIES))


@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    path = tmp_path / "vol" / "arkwatch.db"
    monkeypatch.setattr(caldist, "DEFAULT_DB", path)
    return path


def test_daemon_start_initializes_schema(empty_db, monkeypatch):
    monkeypatch.setattr(daemon, "DB_PATH", empty_db)
    monkeypatch.setattr(daemon, "run_loop", lambda: None)
    assert daemon.main([]) == 0
    c = db.get_conn(empty_db, read_only=True)
    assert db._schema_version(c) == db.SCHEMA_VERSION
    # registry synced too: every job's raw_observations FK target exists (f2 LME)
    assert c.execute("SELECT 1 FROM series_registry WHERE series_id='LME:CA_STOCKS'").fetchone()
    c.close()


def test_family_rows_missing_or_empty_db_is_empty(empty_db):
    assert caldist.family_rows("anything") == []  # no file yet
    db.get_conn(empty_db, allow_init=True).close()
    assert caldist.family_rows("anything") == []  # schema, no events
    with pytest.raises(caldist.NoDataYet):
        caldist.fetch_latest(CAL_SID)


def test_verify_cal_no_data_yet_is_not_a_violation(empty_db):
    rep = vs.verify(registry=[{"series_id": CAL_SID, "block": "A"}], anchors=[])
    assert rep.violations == []
    assert rep.rows[0].note == "no data yet"


def test_harvest_cal_no_data_yet_is_empty_not_error(empty_db, monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(hv, "load_registry", lambda: [{"series_id": CAL_SID, "block": "A"}])
    monkeypatch.setattr("arkwatch.qa.backfill.sync_registry", lambda conn: None)
    ok, fail, _ = hv.harvest(str(empty_db))
    c = db.get_conn(empty_db)
    assert c.execute("SELECT status, error FROM fetch_log").fetchall() == [("EMPTY", "no data yet")]
    c.close()
    assert fail == 0
