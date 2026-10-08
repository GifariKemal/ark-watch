"""daemon ops contracts for the container deploy: kernel-released lock,
atomic state writes, healthcheck exit codes, heartbeat during long jobs."""

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import daemon


def test_lock_is_exclusive_and_released_on_close(tmp_path):
    p = tmp_path / "daemon.lock"
    first = daemon._acquire_lock(p)
    assert first is not None
    assert daemon._acquire_lock(p) is None  # a live holder blocks a second daemon
    first.close()  # process death does the same: the kernel drops the lock
    again = daemon._acquire_lock(p)
    assert again is not None
    again.close()


def test_stale_pid_lockfile_does_not_block(tmp_path):
    # the old PID-file format: PID 1 "looks alive" forever inside Docker
    p = tmp_path / "daemon.lock"
    p.write_text("1")
    lock = daemon._acquire_lock(p)
    assert lock is not None
    lock.close()


def test_save_state_is_atomic(tmp_path, monkeypatch):
    p = tmp_path / "daemon_state.json"
    daemon._save_state({"0600-harvest@2026-10-08": "1"}, p)
    good = p.read_text()

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(daemon.os, "replace", boom)
    daemon._save_state({"0715-verify@2026-10-08": "1"}, p)  # logged, not raised
    assert p.read_text() == good  # never torn or truncated


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(x)")
    conn.close()
    return path


def _beat(path, age_s):
    path.write_text((datetime.now(UTC) - timedelta(seconds=age_s)).isoformat())
    return path


def test_healthcheck_exit_codes(tmp_path):
    db = _db(tmp_path / "arkwatch.db")
    assert daemon.healthcheck(_beat(tmp_path / "hb_ok", 10), db) == 0
    assert daemon.healthcheck(_beat(tmp_path / "hb_old", 400), db) == 1
    assert daemon.healthcheck(tmp_path / "missing", db) == 1
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 10)
    assert daemon.healthcheck(_beat(tmp_path / "hb", 10), junk) == 1
    assert daemon.healthcheck(_beat(tmp_path / "hb2", 10), tmp_path / "absent.db") == 1


def test_heartbeat_keeps_refreshing_during_long_job(monkeypatch):
    beats = []
    monkeypatch.setattr(daemon, "_heartbeat", lambda: beats.append(1))
    monkeypatch.setattr(daemon, "HEARTBEAT_EVERY_S", 0.2)
    code, out, _err = daemon._spawn(
        [sys.executable, "-c", "import time; time.sleep(1.5); print('done')"], 30
    )
    assert code == 0 and out.strip() == "done"
    assert len(beats) >= 3


def test_spawn_timeout_kills_the_job(monkeypatch):
    monkeypatch.setattr(daemon, "_heartbeat", lambda: None)
    monkeypatch.setattr(daemon, "HEARTBEAT_EVERY_S", 0.2)
    with pytest.raises(subprocess.TimeoutExpired):
        daemon._spawn([sys.executable, "-c", "import time; time.sleep(60)"], 0.5)


def test_stop_child_terminates_process_group():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=os.name != "nt",
    )
    daemon._stop_child(proc, grace_s=5)
    assert proc.poll() is not None
