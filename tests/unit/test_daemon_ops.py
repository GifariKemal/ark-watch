"""daemon ops contracts for the container deploy: kernel-released lock,
atomic state writes, healthcheck exit codes, heartbeat during long jobs."""

import os
import sqlite3
import subprocess
import sys
import time
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


def test_healthcheck_opens_the_real_file_despite_uri_special_characters(tmp_path):
    # a raw "file:{path}" URI cuts the path at '#': the probe opened some other path and
    # reported a junk DB as healthy
    d = tmp_path / "data#1"
    d.mkdir()
    assert daemon.healthcheck(_beat(tmp_path / "hb", 10), _db(d / "good.db")) == 0
    junk = d / "arkwatch.db"
    junk.write_bytes(b"not a database at all" * 10)
    assert daemon.healthcheck(_beat(tmp_path / "hb2", 10), junk) == 1


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def test_daemon_runs_one_api_job_per_cycle_and_reaps_orphans(tmp_path, monkeypatch):
    from arkwatch import db as arkdb

    f = tmp_path / "arkwatch.db"
    conn = arkdb.get_conn(f, allow_init=True)
    now = datetime.now(UTC)
    start = now - timedelta(hours=3)  # daemon started 3h ago
    conn.executemany(
        "INSERT INTO jobs(id, kind, status, created_at, started_at) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "harvest", "running", _iso(start), _iso(start - timedelta(minutes=5))),  # prior run
            (2, "cme", "running", _iso(start), _iso(now - timedelta(hours=2))),  # past timeout
            (3, "f2", "running", _iso(now), _iso(now - timedelta(minutes=1))),  # still fine
            (4, "market", "queued", _iso(now), None),
            (5, "energy", "queued", _iso(now), None),
        ],
    )
    seen = []
    monkeypatch.setattr(daemon, "DB_PATH", f)
    monkeypatch.setattr(daemon, "_spawn", lambda argv, t: seen.append(argv[3:]) or (0, "ok", ""))
    daemon._run_api_jobs(_iso(start))
    rows = dict(conn.execute("SELECT id, status || ':' || COALESCE(error, '') FROM jobs"))
    conn.close()
    assert seen == [["market"]]  # one job per loop cycle, through the heartbeat-safe _spawn
    assert rows == {
        1: "failed:interrupted",
        2: "failed:interrupted",
        3: "running:",
        4: "done:",
        5: "queued:",
    }


def test_run_loop_drives_the_api_job_queue(tmp_path, monkeypatch):
    calls = []

    def stop(_s):
        raise SystemExit(0)

    monkeypatch.setenv("GIT_SHA", "test")
    monkeypatch.setattr(daemon, "LOCKFILE", tmp_path / "daemon.lock")
    monkeypatch.setattr(daemon.signal, "signal", lambda *a: None)
    for name, fn in {
        "_setup_logging": lambda: None,
        "_heartbeat": lambda: None,
        "_load_state": lambda: {},
        "_due_jobs": lambda *a: [],
        "_run_job": lambda *a: True,
        "_run_api_jobs": lambda start: calls.append(start),
    }.items():
        monkeypatch.setattr(daemon, name, fn)
    monkeypatch.setattr(daemon.time, "sleep", stop)
    with pytest.raises(SystemExit):
        daemon.run_loop()
    assert len(calls) == 1


def test_lanes_keep_running_while_the_main_loop_is_busy(monkeypatch):
    """The watcher and the market timeline must not wait for a slow main-loop job
    (2026-10-09: serial loop, watcher gap max 27 min, 87% market buckets)."""
    import threading

    calls = []
    monkeypatch.setattr(daemon, "_run_job", lambda cmd, desc: calls.append(cmd) or True)
    monkeypatch.setattr(daemon, "WATCH_INTERVAL_S", 0.01)
    monkeypatch.setattr(daemon, "MARKET_LANE_TICK_S", 0.01)
    stop = threading.Event()
    lanes = [
        threading.Thread(target=f, args=(stop,)) for f in (daemon._watch_lane, daemon._market_lane)
    ]
    for t in lanes:
        t.start()
    time.sleep(0.3)  # the main thread is "busy" all along
    stop.set()
    for t in lanes:
        t.join(timeout=2)
    assert not any(t.is_alive() for t in lanes)
    assert calls.count("watch") >= 5
    # same 5-minute bucket: market + scanner exactly once
    assert calls.count("market") == 1 and calls.count("scanner") == 1


def test_lane_survives_a_failing_job(monkeypatch):
    import threading

    stop = threading.Event()
    n = []

    def boom(cmd, desc):
        n.append(cmd)
        if len(n) >= 3:
            stop.set()
        raise RuntimeError("x")

    monkeypatch.setattr(daemon, "_run_job", boom)
    monkeypatch.setattr(daemon, "WATCH_INTERVAL_S", 0.01)
    daemon._watch_lane(stop)
    assert len(n) == 3


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="NTFS refuses concurrent os.replace onto one target; prod is Linux",
)
def test_atomic_write_from_many_threads(tmp_path):
    import threading

    target = tmp_path / "beat"
    errors = []

    def spam():
        try:
            for _ in range(200):
                daemon._atomic_write(target, "x")
        except Exception as ex:
            errors.append(ex)

    ts = [threading.Thread(target=spam) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and target.read_text() == "x"
    assert not list(tmp_path.glob("*.tmp"))
