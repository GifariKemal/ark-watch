"""qa/backup.py: atomic VACUUM INTO, race-tolerant verify, nightly prune."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.qa import backup


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "live.db"
    db.get_conn(path, allow_init=True).close()
    return path


def _add_event(path, uid):
    c = sqlite3.connect(path)
    c.execute(
        "INSERT INTO events(event_uid, ts_utc, country, name, normalized_name)"
        " VALUES (?, '2026-10-01T12:00:00+00:00', 'US', 'X', 'X')",
        (uid,),
    )
    c.commit()
    c.close()


def test_failed_vacuum_keeps_previous_backup(live, tmp_path):
    dst = backup.backup(str(live))
    before = dst.read_bytes()
    garbage = tmp_path / "not-a-db.db"
    garbage.write_bytes(b"x" * 4096)
    with pytest.raises(sqlite3.DatabaseError):
        backup.backup(str(garbage))
    assert dst.read_bytes() == before
    assert list(backup.BACKUP_DIR.iterdir()) == [dst]  # no .tmp leftover


def test_verify_tolerates_live_growth_not_loss(live):
    _add_event(live, "e1")
    dst = backup.backup(str(live))
    _add_event(live, "e2")  # the daemon wrote after the snapshot
    backup.verify(str(live), dst)
    c = sqlite3.connect(live)
    c.execute("DELETE FROM events")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="COUNT mismatch events"):
        backup.verify(str(live), dst)


def test_main_prunes_old_1m_bars_only(live):
    now = datetime.now(UTC)
    old = (now - timedelta(days=4)).isoformat(timespec="seconds")
    new = (now - timedelta(days=1)).isoformat(timespec="seconds")
    c = sqlite3.connect(live)
    c.executemany(
        "INSERT INTO intraday_bars(symbol, bar_ts_utc, interval, source, close, fetched_at)"
        " VALUES ('SPY', ?, ?, 'YAHOO', 1.0, ?)",
        [(old, "1m", new), (new, "1m", new), (old, "5m", new)],
    )
    c.commit()
    c.close()
    assert backup.main(["--db", str(live)]) == 0
    c = sqlite3.connect(live)
    left = c.execute("SELECT interval, bar_ts_utc FROM intraday_bars ORDER BY 1, 2").fetchall()
    c.close()
    assert left == [("1m", new), ("5m", old)]
