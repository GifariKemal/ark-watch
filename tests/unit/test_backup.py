"""qa/backup.py: atomic VACUUM INTO, race-tolerant verify, nightly prune."""

from __future__ import annotations

import os
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


def test_prune_applies_table_retention_in_chunks(live, monkeypatch):
    monkeypatch.setattr(backup, "PRUNE_CHUNK", 2)  # force several chunks
    now = datetime.now(UTC)
    old = [(now - timedelta(days=8, minutes=i)).isoformat(timespec="seconds") for i in range(5)]
    new = (now - timedelta(days=1)).isoformat(timespec="seconds")
    c = sqlite3.connect(live)
    c.executemany(
        "INSERT INTO crypto_orderbook_snapshots(ts_utc, source, instrument, raw_json, fetched_at)"
        " VALUES (?, 'OKX', 'BTC-USDT-SWAP', '{}', ?)",
        [(ts, new) for ts in [*old, new]],
    )
    c.executemany(
        "INSERT INTO fetch_log(ts, fetcher, target, status) VALUES (?, ?, 't', 'OK')",
        [(old[0], "market"), (old[0], "fred"), ((now - timedelta(days=40)).isoformat(), "market")],
    )
    c.commit()
    c.close()
    deleted = backup.prune(str(live), now)
    assert deleted["crypto_orderbook_snapshots"] == 5
    c = sqlite3.connect(live)
    assert c.execute("SELECT ts_utc FROM crypto_orderbook_snapshots").fetchall() == [(new,)]
    # market rows keep 30 days, every other fetcher 180
    assert sorted(c.execute("SELECT fetcher FROM fetch_log").fetchall()) == [("fred",), ("market",)]
    c.close()


def test_rotate_keeps_dailies_monthlies_and_sweeps_old_orphans(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setattr(backup, "BACKUP_DIR", d)
    days = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(0, 300, 3)]
    for day in days:
        (d / f"arkwatch-{day:%Y%m%d}.db").write_bytes(b"x")
    stale = d / ".gdelt-retention-before-20260101T000000Z-aaaa.db"
    fresh = d / ".gdelt-retention-before-20260102T000000Z-bbbb.db"
    killed = d / "arkwatch-20260101.db.tmp"  # a backup killed mid-VACUUM on another day
    for f in (stale, fresh, killed):
        f.write_bytes(b"x")
    os.utime(stale, (1_000, 1_000))
    os.utime(killed, (1_000, 1_000))
    backup.rotate(now=fresh.stat().st_mtime + 60)
    left = sorted(f.name for f in d.glob("arkwatch-*.db"))
    monthly = {f"arkwatch-{min(x for x in days if x.month == m):%Y%m%d}.db" for m in range(5, 11)}
    assert left[-backup.DAILIES :] == [f"arkwatch-{x:%Y%m%d}.db" for x in days[-backup.DAILIES :]]
    assert set(left) == monthly | set(left[-backup.DAILIES :])
    assert not stale.exists() and not killed.exists() and fresh.exists()
