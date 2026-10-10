"""daemon.py — in-app scheduler: one process, jobs = isolated subprocesses.

A 30-second loop checks the WIB clock and runs due jobs as subprocesses; the
alert watcher and the 5-minute market timeline run in two lane threads so a
slow job never delays them. Each job is a fresh Python process (crashes/leaks do not spread). The heartbeat
file is refreshed every loop; if it goes stale for more than 5 minutes, the
daemon is dead or hung (`python -m arkwatch healthcheck`). An OS advisory
lock on daemon.lock prevents a second instance (duplicate briefs); the kernel
drops it when the holder dies, so a restart never sees a phantom owner.
Run: python -m arkwatch daemon
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
# Docker sets ARKWATCH_DATA_DIR=/data: DB, state, logs and backups on the volume
_DATA_ENV = os.environ.get("ARKWATCH_DATA_DIR")
DATA_DIR = Path(_DATA_ENV) if _DATA_ENV else ROOT / "data"
DB_PATH = DATA_DIR / "arkwatch.db"
HEARTBEAT = DATA_DIR / "daemon_heartbeat"
LOCKFILE = DATA_DIR / "daemon.lock"
LOG_DIR = DATA_DIR / "logs" if _DATA_ENV else ROOT / "logs"
LOG_RETENTION_DAYS = 14
HEALTH_MAX_AGE_S = 300  # the 5-minute staleness contract (D-018c)
HEARTBEAT_EVERY_S = 30.0  # heartbeat cadence while a job subprocess runs
WIB = ZoneInfo("Asia/Jakarta")
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")

# Schedule entries: (hour, minute, day, job_cmd, description)
# day: daily | hourly | monday..saturday | sunday — literal day tokens as used below
# (hourly: runs every hour at `minute`, the hour field is ignored);
# weekday names accept both short and long forms (both must map to the same
# weekday key)
SCHEDULE = [
    (3, 45, "saturday", "f2", "Saturday: COT post-release (Fri 15:30 ET) BEFORE brief"),
    (4, 15, "saturday", "brief", "Saturday positioning special brief"),
    #   ^ RE-ENABLED 2026-09-13 (review ronde-2): pausing GENERATION killed
    #   the five store_* audit trails (computed_signals froze) — generation
    #   must run for the data phase; only DELIVERY stays paused (send below)
    (6, 0, "daily", "harvest", "Increment harvest for all active registry series"),
    (6, 0, "friday", "soma harvest", "SOMA per-CUSIP weekly harvest"),
    # fiscaldata publishes DTS ~afternoon ET → a 06:10 WIB run catches yesterday;
    # sits after the 06:00 registry harvest (owns FISCAL:DEBT_*) and before the
    # 07:00 brief
    (
        6,
        10,
        "daily",
        "fiscalx",
        "Fiscaldata expansion: auctions + DTS transactions + interest expense/rates",
    ),
    (6, 20, "daily", "instruments sweep", "Last 7 days of prices"),
    # energy channel 2026-09-21: curve signals after the price sweep (CL2
    # month-resolution + cracks + Brent-WTI spot spread)
    (6, 25, "daily", "energy", "Energy curve: WTI backwardation + cracks + Brent-WTI"),
    (6, 30, "daily", "nyfed ops", "Desk operations tsy/ambs + fxs swap-line watch"),
    (6, 40, "daily", "calendar", "Union-4 calendar"),
    (6, 45, "daily", "surprise", "σ engine + surprise_z + ESI"),
    # pd BEFORE the 07:00 brief (same slot, list order = launch order): the survey
    # release lands Wed night ET = Thu ~06:00 WIB, so the brief sees fresh data
    (7, 0, "thursday", "nyfed pd", "Primary Dealer Positions Survey (release Wed night ET)"),
    (
        7,
        0,
        "daily",
        "brief",
        "Generate brief + outbox (skip if Saturday edition already published)",
    ),
    (
        7,
        5,
        "daily",
        "send",
        "Send brief to configured channels (paused when none, see _paused_jobs)",
    ),
    (7, 15, "daily", "verify", "Truth gate"),
    (8, 15, "daily", "cme", "CME settlements + CVOL + VOI (gray harvester)"),
    (8, 30, "daily", "f2", "COT + flows (Bybit/Farside/PBoC/LBMA/TIC/LME) + FedWatch"),
    # nightly local snapshot (VACUUM INTO + verify + rotation); offsite copies
    # are Litestream's job. Listed before gdelt-retention: same-slot order =
    # launch order, so Sunday's purge runs after the night's backup
    (23, 30, "daily", "backup", "Nightly DB backup + verify + rotation"),
    (23, 45, "sunday", "gdelt-retention --apply", "Purge GDELT data outside the current UTC week"),
    (22, 0, "sunday", "alfred", "Weekly maintenance + vintage audit"),
    # ROUND-4: the blindness class that started this whole audit (calendar
    # families with actuals but no series) must be checked by the daemon, not
    # by an accidental question — weekly, before the alfred maintenance
    (21, 0, "sunday", "coverage", "Registry lint + calendar-family gap detector"),
    # ROUND-6: the point-in-time substrate needs scheduled consumers — the
    # replay was frozen at a single 09-02 run while the vintage feed kept
    # writing; backfill-first heals freeze-window first-print holes (ALFRED
    # truth, upsert repairs fetch-day stamps)
    (21, 15, "sunday", "alfred --vintages", "First-print vintage heal via ALFRED API"),
    (21, 30, "sunday", "f4 replay", "Point-in-time regime replay refresh"),
    (21, 50, "sunday", "calibrate", "Golden anchors quarterly calibration & drift audit"),
    # ROUND-11: the dot plot refreshes 4x/year with SEP meetings — a quarterly
    # cadence job re-fetches all vintages (idempotent; the web is the source)
    (5, 0, "sunday", "backfill --source sep", "FOMC dot plot refresh (quarterly cadence)"),
    # NY Fed research expansion (2026-09-19): HHDC/MCT/LW/GSCPI/HPW rewrite
    # whole histories — the daily window can't see revisions older than its
    # floor, so a weekly full-history re-ingest lands them as vintage rows
    (
        5,
        10,
        "sunday",
        "backfill --source nyfedresearch",
        "NY Fed research full-history refresh (revisions)",
    ),
    # FRB charge-off/delinquency — same whole-history re-release pattern
    (5, 15, "sunday", "backfill --source frb", "Fed Board charge-off refresh (revisions)"),
    # Fed surveys + reports: SLOOS/Beige Book/SCOOS/FSR/Minutes/Press Conf.
    # Weekly check (quarterly/monthly sources — "unchanged" is the normal
    # outcome ~95% of days; new data triggers fetch + NLP + store).
    # ALSO runs daily: press conf + minutes land on FOMC days, not Sundays.
    (
        6,
        50,
        "daily",
        "fedsurvey",
        "Fed surveys + FOMC comms (SLOOS/BeigeBook/Minutes/PressConf NLP)",
    ),
    (0, 20, "hourly", "polymarket", "Polymarket crowd probabilities (macro/geo topics)"),
]
# The watcher is a recurring 60-second task, not part of SCHEDULE — the daemon
# runs it as its own subprocess each cycle
WATCH_INTERVAL_S = 60
MARKET_INTERVAL_MINUTES = 5
MARKET_NEWS_INTERVAL_MINUTES = 60
MARKET_ACTIVE_INTERVAL_MINUTES = 15
RETRY_DELAY_S = 600


def _market_news_interval(now: datetime) -> int:
    london = now.astimezone(LONDON)
    new_york = now.astimezone(NEW_YORK)
    active = (
        london.weekday() < 5
        and new_york.weekday() < 5
        and london.weekday() == new_york.weekday()
        and (london.hour, london.minute) >= (8, 0)
        and (new_york.hour, new_york.minute) < (16, 0)
    )
    return MARKET_ACTIVE_INTERVAL_MINUTES if active else MARKET_NEWS_INTERVAL_MINUTES


def _paused_jobs() -> set[str]:
    """D-023 data-first hold (owner 2026-09-13): generation always runs, OUTBOUND
    delivery (`send`) runs only once at least one channel (ntfy, Discord,
    Telegram) is configured. Re-read every call: env changes need no code edit."""
    from .senders.base import active_channels

    return set() if active_channels() else {"send"}


PING_EVERY_S = 300  # dead-man ping cadence (healthchecks.io-style HEALTHCHECK_PING_URL)

DAY_MAP = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

logger = logging.getLogger("arkwatch.daemon")


def _setup_logging():
    """(Re)install handlers — clear old ones first: re-adding on rotation would
    duplicate lines (2×, 3×, …) and leak file handles."""
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    fh = logging.FileHandler(LOG_DIR / f"daemon-{today}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    # stdout mirror always: `docker logs` is the primary view in the container
    # (the old systemd StandardError=append duplication was on stderr)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.addHandler(sh)
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for old in LOG_DIR.glob("daemon-*.log"):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < cutoff:
                old.unlink()


def _atomic_write(path: Path, text: str) -> None:
    """tmp + os.replace: a crash mid-write never leaves a torn file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # per-thread tmp name: the watch/market lanes and the main loop all write the heartbeat
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _heartbeat():
    _atomic_write(HEARTBEAT, datetime.now(UTC).isoformat(timespec="seconds"))


def healthcheck(heartbeat: Path | None = None, db_path: Path | None = None) -> int:
    """Docker HEALTHCHECK: 0 = heartbeat fresh (<5 min) and DB readable."""
    import sqlite3

    try:
        beat = datetime.fromisoformat((heartbeat or HEARTBEAT).read_text().strip())
        age = (datetime.now(UTC) - beat).total_seconds()
        # as_uri() percent-encodes '#', '?', '%' (a raw f"file:{path}" cut the path at '#')
        uri = Path(db_path or DB_PATH).resolve().as_uri()
        conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except Exception as ex:
        print(f"unhealthy: {type(ex).__name__}: {ex}")
        return 1
    if age > HEALTH_MAX_AGE_S:
        print(f"unhealthy: heartbeat {age:.0f}s old")
        return 1
    print(f"ok: heartbeat {age:.0f}s old")
    return 0


def _ping(suffix: str = ""):
    """Fire-and-forget GET to HEALTHCHECK_PING_URL (+suffix, e.g. '/fail') in a daemon
    thread: never raises, never blocks the loop. The URL is a secret: never logged.
    Returns the thread (tests join it) or None when unset."""
    import threading
    import urllib.request

    url = os.environ.get("HEALTHCHECK_PING_URL", "").strip()
    if not url:
        return None

    def _get():
        try:
            urllib.request.urlopen(url.rstrip("/") + suffix, timeout=5).close()
        except Exception as ex:
            logger.warning(f"healthcheck ping failed: {type(ex).__name__}")

    t = threading.Thread(target=_get, daemon=True)
    t.start()
    return t


def _maybe_ping(last: float, now: float | None = None) -> float:
    """Ping at most once per PING_EVERY_S, only while healthcheck() passes (same notion
    as the Docker HEALTHCHECK); returns the new last-check time."""
    now = time.monotonic() if now is None else now
    if now - last < PING_EVERY_S:
        return last
    with contextlib.redirect_stdout(io.StringIO()):  # healthcheck() prints its verdict
        healthy = healthcheck() == 0
    if healthy:
        _ping()
    return now


def _acquire_lock(path: Path | None = None):
    """Non-blocking OS advisory lock; returns the open handle (hold it for the
    process lifetime) or None when another live process holds it. The kernel
    releases it on death: the old PID file + os.kill(pid, 0) check looked
    alive forever in Docker (PID 1 after a restart) and crash-looped."""
    path = path or LOCKFILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")  # noqa: SIM115 - held until release/exit
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        if os.name == "nt":
            proc.send_signal(sig)  # SIGTERM -> TerminateProcess
        else:
            os.killpg(proc.pid, sig)


def _stop_child(proc: subprocess.Popen, grace_s: float = 20.0) -> None:
    """SIGTERM the job's process group, SIGKILL after the grace period."""
    if proc.poll() is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
        proc.wait()


def _spawn(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    """Run one job subprocess in its own process group, refreshing the
    heartbeat every HEARTBEAT_EVERY_S so a long job (harvest ~6 min) never
    looks like a hung daemon. Raises TimeoutExpired past timeout_s. The
    finally reaps the group on every exit path, including SIGTERM unwinding
    through here as SystemExit."""
    deadline = time.monotonic() + timeout_s
    with subprocess.Popen(
        argv,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
    ) as proc:
        try:
            while True:
                left = deadline - time.monotonic()
                try:
                    out, err = proc.communicate(timeout=max(min(HEARTBEAT_EVERY_S, left), 0.01))
                    return proc.returncode, out, err
                except subprocess.TimeoutExpired:
                    _heartbeat()
                    if left <= HEARTBEAT_EVERY_S:
                        raise
        finally:
            _stop_child(proc)


def _due_jobs(now_wib, last_run: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return [(job_cmd, description, key)] jobs that are due and not yet run.

    The key is PER SCHEDULE ENTRY (hhmm+cmd+date): a cmd+date key would let a
    Saturday 03:45 f2 run block the daily 08:30 f2 (same cmd), leaving
    Saturday flows/COT unrefreshed. The key is built here so it matches
    run_loop exactly."""
    due = []
    wd = now_wib.weekday()  # 0=Monday
    hhmm = now_wib.hour * 100 + now_wib.minute
    for h, m, day, cmd, desc in SCHEDULE:
        if day == "hourly":  # the hour field is ignored: this hour only, never a replay
            h = now_wib.hour
        if day in ("daily", "hourly") or DAY_MAP.get(day) == wd:
            target = h * 100 + m
            if hhmm >= target:
                key = f"{h:02d}{m:02d}-{cmd}@{now_wib.date()}"
                if key not in last_run:
                    due.append((cmd, desc, key))
    return due


def _alert_job_failed(cmd: str, detail: str) -> None:
    """ROUND-4: job failures lived only in the log file — the nightly backup
    hard-failed for two nights with zero visibility while the on-disk backup
    rotted. Route failures through the watcher's outbox.

    ROUND-5: the key is PER-JOB PER-DAY permanent — the round-4 shared
    windowed key meant (a) a second failing job within 6h was silently
    eaten (Sunday coverage 21:00 would mask alfred 22:00 + backup 23:30 —
    the exact class this routing exists to surface), (b) near-daily
    transient 5xx pages crossed the spam tripwire falsely. Dated-per-job:
    retry echoes suppressed, next-day episodes re-page, jobs never mask
    each other, tripwire counts stay 1/key/day."""
    _ping("/fail")
    try:
        from . import db as _db
        from .qa.watcher import _fire

        conn = _db.get_conn(DB_PATH)
        _fire(
            conn,
            "job_failed",
            f"Daemon job '{cmd}' failed: {detail[:110]}",
            "A scheduled job failed (see logs/daemon-*.log + fetch_log for detail)",
            f"Run `python -m arkwatch {cmd}` on the server to diagnose",
            # WIB-dated (audit round-2): the operational day is WIB — a UTC
            # key let a second failure episode of the same job inside one WIB
            # day stay silenced across the UTC-date flip (and vice versa)
            cooldown_key=f"job_failed@{cmd}@{datetime.now(WIB).date().isoformat()}",
        )
        conn.close()
    except Exception as ex:  # the alert must never break the loop
        logger.error(f"job-failure alert itself failed: {ex}")


def _run_job(cmd: str, desc: str) -> bool:
    if cmd in _paused_jobs():
        logger.info(f"⏸ {cmd} paused (no delivery channel configured) — {desc}")
        return True
    t0 = time.monotonic()
    logger.info(f"▶ {cmd} — {desc}")
    # heartbeat inside the wrapper too: a job >5 min (verify measured
    # 221-269s, harvest 368s) would leave the loop-top heartbeat stale (D-018c);
    # _spawn keeps refreshing it while the job runs
    _heartbeat()
    try:
        cmd_timeout = 3600 if "gdelt-retention" in cmd else 1800
        code, stdout, stderr = _spawn([sys.executable, "-m", "arkwatch"] + cmd.split(), cmd_timeout)
        dt = time.monotonic() - t0
        if code == 0:
            tail = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
            if cmd == "gdelt-retention --apply":
                result = next((ln for ln in reversed(tail) if ln.startswith("{")), None)
                if result is None:
                    logger.error("gdelt-retention-result missing structured output")
                    return False
                logger.info(f"gdelt-retention-result {result}")
                return True
            # last FEW lines, not the last one: exit-0 jobs print per-source
            # ⚠ warnings mid-run (a 3-week Farside freeze was invisible
            # because the summary only kept f2's final LME line — D-021)
            summary = " | ".join(tail[-3:]) if tail else "OK"
            logger.info(f"✓ {cmd} ({dt:.0f}s) — {summary[:240]}")
            return True
        err = (stderr or stdout or "").strip().splitlines()
        # ROUND-9: pages must quote the FAILING line, not the last — a
        # verify failure blamed LME:CA_STOCKS (healthy, last row printed)
        # while the sick series hid mid-stream
        fail_lines = [
            ln for ln in err if any(m in ln for m in ("✗", "ERROR", "Error", "Traceback"))
        ]
        tail = (" | ".join(fail_lines[-2:]) if fail_lines else (err[-1] if err else "no output"))[
            :200
        ]
        logger.error(f"✗ {cmd} ({dt:.0f}s) exit={code} — {tail}")
        _alert_job_failed(cmd, tail)
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"✗ {cmd} TIMEOUT 30min — hard-kill")
        _alert_job_failed(cmd, "TIMEOUT 30min")
        return False
    except Exception as ex:
        logger.error(f"✗ {cmd} — daemon exception: {ex}")
        _alert_job_failed(cmd, str(ex))
        return False


def _run_api_jobs(daemon_start: str) -> None:
    """One API-queued job per loop cycle, through _spawn (heartbeat stays fresh). First
    reap 'running' rows left by a previous daemon or older than timeout + grace: this
    daemon runs jobs synchronously, so none of its own can be running right now."""
    try:
        from . import db as _db
        from .qa import jobs_runner

        cutoff = datetime.now(UTC) - timedelta(seconds=jobs_runner.JOB_TIMEOUT_S + 300)
        conn = _db.get_conn(DB_PATH)
        try:
            # started before max(start, cutoff) == started before start OR before cutoff
            stale = max(daemon_start, cutoff.isoformat(timespec="seconds"))
            if n := jobs_runner.reap_running_jobs(conn, stale):
                logger.warning(f"reaped {n} interrupted API job(s)")
            jobs_runner.run_pending_jobs(conn, run=_spawn, limit=1)
        finally:
            conn.close()
    except Exception as ex:  # the queue must never break the loop
        logger.error(f"API job queue failed: {ex}")


STATE_PATH = DATA_DIR / "daemon_state.json"
# First boot on a brand-new volume would idle until the next scheduled slot:
# run the data chain once, in dependency order (never `send`)
BOOTSTRAP_MARKER = "bootstrapped"
# set BEFORE the jobs run: an interrupted bootstrap (redeploy mid-run) must resume on the
# next boot even though the half-filled DB no longer looks empty
BOOTSTRAP_STARTED = "bootstrap_started"
BOOTSTRAP_JOBS = (
    "harvest",
    "calendar",
    "instruments sweep",
    "fiscalx",
    "nyfed ops",
    "energy",
    "surprise",
    "cme",
    "f2",
    "fedsurvey",
    "brief",
)


def _bootstrap(state: dict[str, str]) -> None:
    """Run BOOTSTRAP_JOBS on a brand-new volume (no marker AND raw_observations empty),
    or resume them when an earlier run was interrupted (BOOTSTRAP_STARTED set, marker
    not: a redeploy mid-run leaves a half-filled DB that no longer looks empty). A
    populated DB without markers is just marked done. Jobs go through _run_job
    (logging/heartbeat/timeouts); a failed job does not abort the rest. SIGTERM unwinds
    as SystemExit (_on_signal) and leaves the completion marker unset."""
    if BOOTSTRAP_MARKER in state:
        return
    import sqlite3

    try:  # read-only probe: never creates a file, never breaks the loop
        uri = Path(DB_PATH).resolve().as_uri()
        with contextlib.closing(sqlite3.connect(f"{uri}?mode=ro", uri=True)) as conn:
            empty = conn.execute("SELECT 1 FROM raw_observations LIMIT 1").fetchone() is None
    except sqlite3.Error as ex:
        logger.error(f"bootstrap probe failed: {ex}")
        return
    if empty or BOOTSTRAP_STARTED in state:
        state[BOOTSTRAP_STARTED] = "1"
        _save_state(state)
        logger.info(f"first boot: bootstrap {len(BOOTSTRAP_JOBS)} job(s)")
        for cmd in BOOTSTRAP_JOBS:
            _run_job(cmd, "first-boot bootstrap")
    state[BOOTSTRAP_MARKER] = "1"
    _save_state(state)


def _load_state(path: Path | None = None) -> dict[str, str]:
    """Today's SUCCEEDED jobs from the persisted day-state file.

    ROUND-10: last_run survives restarts (a deploy-restart used to replay
    every already-succeeded daily job and page the owner for a clean
    harvest). Date-scoped — yesterday's keys are pruned on load.

    ROUND-11: only success ("1") entries restore. A FAILED job's key is
    deliberately absent so a restart re-runs it once: the 10-minute retry
    schedule is in-memory and dies with the process (live 2026-09-19
    05:26 UTC: a deploy 64 seconds into verify's retry killed it, while
    the persisted already-ran key cancelled the day's only automatic
    recovery — the failure stood until the next day's schedule)."""
    import json as _json

    try:
        raw = _json.loads((path or STATE_PATH).read_text())
        today = datetime.now(WIB).date().isoformat()
        return {
            k: v
            for k, v in raw.items()
            if (k.endswith(f"@{today}") or k in (BOOTSTRAP_MARKER, BOOTSTRAP_STARTED)) and v == "1"
        }
    except Exception:
        return {}


def _save_state(d: dict[str, str], path: Path | None = None) -> None:
    """Persist the day-state — successes only, mirroring _load_state."""
    import json as _json

    try:
        succ = {k: v for k, v in d.items() if v == "1"}
        _atomic_write(path or STATE_PATH, _json.dumps(succ))
    except Exception as ex:
        logger.warning(f"state persist failed: {ex}")


MARKET_LANE_TICK_S = 5.0


def _watch_lane(stop: threading.Event) -> None:
    """Alert watcher every WATCH_INTERVAL_S in its own thread. In the old serial loop a slow
    job blocked it: the LLM sentiment job runs up to 15 min, verify ~4 min (2026-10-09:
    watcher gap max 27 min, 170 late runs)."""
    while not stop.is_set():
        try:
            _run_job("watch", "Alert watcher")
        except Exception as ex:  # the lane must outlive any single failure
            logger.error(f"watch lane: {ex}")
        stop.wait(WATCH_INTERVAL_S)


def _market_lane(stop: threading.Event) -> None:
    """5-minute market timeline + scanner in their own thread (2026-10-09: only 87% of the
    buckets ran behind slow jobs). Fires on a NEW bucket, not on minute % 5 == 0, so a run
    that crosses the boundary minute no longer skips the whole bucket. Jobs are
    subprocesses; SQLite WAL takes the concurrent writers (busy_timeout 10 s)."""
    last_bucket = ""
    while not stop.is_set():
        try:
            now_wib = datetime.now(WIB)
            bucket = (
                now_wib.strftime("%Y%m%d%H") + f"{now_wib.minute // MARKET_INTERVAL_MINUTES:02d}"
            )
            if bucket != last_bucket:
                last_bucket = bucket
                _run_job("market", "Five-minute cross-asset timeline")
                _run_job("scanner", "Opportunity scanner and playbook tracker")
        except Exception as ex:
            logger.error(f"market lane: {ex}")
        stop.wait(MARKET_LANE_TICK_S)


def _on_signal(signum, _frame):
    # unwind as SystemExit: _spawn's finally reaps the running job group,
    # run_loop's finally releases the lock (an inline flag would wait out
    # a 30-min job or the 30s sleep)
    raise SystemExit(0)


def run_loop():
    lock = _acquire_lock()
    if lock is None:
        print(f"another daemon holds {LOCKFILE} — exit")
        return
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    _setup_logging()
    # ROUND-10: stamp the code revision at start + every log rotation — the
    # soak lens was misled by an assumed-HEAD daemon (the restart only
    # landed the new code at 10:37 WIB while the 08:30 job ran pre-fix).
    # The image has no git: the build stamps GIT_SHA instead.
    _head = os.environ.get("GIT_SHA", "")
    if not _head:
        try:
            _head = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except Exception:
            _head = "?"
    logger.info(f"=== daemon start @ {_head} ===")
    # ROUND-10 queue: last_run persists across restarts (a deploy-restart
    # used to replay every already-succeeded daily job and page the owner
    # for a clean harvest). Date-scoped file — yesterday's keys are pruned;
    # failures stay unpersisted so a restart re-runs them once (see
    # _load_state).
    last_run = _load_state()
    if last_run:
        logger.info(f"state restored: {len(last_run)} job(s) already ran today — no replay")
    next_retry: dict[str, float] = {}  # cmd → monotonic retry time (non-blocking)
    log_day = datetime.now(UTC).date()
    last_ping = float("-inf")
    _start = datetime.now(WIB)  # first news run at the next boundary, not at every deploy
    last_news_bucket = _start.strftime("%Y%m%d%H") + (
        f"{_start.minute // _market_news_interval(_start):02d}"
    )
    stop_lanes = threading.Event()
    lanes = [
        threading.Thread(target=fn, args=(stop_lanes,), name=fn.__name__, daemon=True)
        for fn in (_watch_lane, _market_lane)
    ]
    daemon_start = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        _bootstrap(last_run)
        for lane in lanes:
            lane.start()
        while True:
            _heartbeat()
            last_ping = _maybe_ping(last_ping)
            now_wib = datetime.now(WIB)

            # Retry failed jobs (NON-BLOCKING: an inline sleep would freeze the
            # heartbeat and delay every job/watcher cycle)
            for rkey, t in list(next_retry.items()):
                if time.monotonic() >= t:
                    cmd = rkey.split("-", 1)[1].split("@")[0]
                    desc = f"RETRY {cmd}"
                    del next_retry[rkey]
                    if _run_job(cmd, desc):
                        # ROUND-11: upgrade the day-state — the original
                        # failure left the key unpersisted; a healed retry
                        # must close it or a later restart would replay a
                        # job that already recovered
                        last_run[rkey] = "1"
                        _save_state(last_run)
                    else:
                        logger.error(
                            f"{cmd}: retry failed — manual attention needed "
                            f"(the next SCHEDULED run of this job is the natural "
                            f"fallback: daily jobs tomorrow, weekly jobs next week)"
                        )

            # Scheduled jobs — per-entry key (built by _due_jobs, identical format)
            for cmd, desc, key in _due_jobs(now_wib, last_run):
                ok = _run_job(cmd, desc)
                # ROUND-11: "0" marks failed-in-this-process (blocks the
                # 30-second loop from re-firing it) but is NOT persisted —
                # a restart re-runs a failed job exactly once
                last_run[key] = "1" if ok else "0"
                _save_state(last_run)
                if not ok:
                    next_retry[key] = time.monotonic() + RETRY_DELAY_S
                    logger.warning(f"  retry {cmd} in {RETRY_DELAY_S // 60} minutes")

            news_interval = _market_news_interval(now_wib)
            news_bucket = now_wib.strftime("%Y%m%d%H") + f"{now_wib.minute // news_interval:02d}"
            # a new bucket, not minute % N == 0: a busy loop at :00 used to lose the hour
            if news_bucket != last_news_bucket:
                last_news_bucket = news_bucket
                _run_job("market-news", "Cross-source catalyst news")
                _run_job("sentiment", "Multi-asset news intelligence radar")
                _run_job("breadth", "S&P 500 constituent breadth")
                _run_job("crypto", "Crypto liquidation analytics")
            _run_api_jobs(daemon_start)
            # Rotate logs at the UTC date change (the filename convention is
            # UTC). CYCLE-counting drifted: a daemon started at 20:18 rotated
            # at 20:18 daily, so yesterday's filename kept receiving today's
            # runs (live: daemon-20260916.log carried all of 09-17).
            if datetime.now(UTC).date() != log_day:
                log_day = datetime.now(UTC).date()
                _setup_logging()
            time.sleep(30)
    finally:
        stop_lanes.set()
        for lane in lanes:
            if lane.is_alive():
                lane.join(timeout=25)  # a running market job may finish inside the 60 s grace
        logger.info("=== daemon stop ===")
        # closing releases the lock; the file stays (unlinking a locked path
        # races a starting successor onto a different inode)
        lock.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch daemon")
    p.add_argument("--once", action="store_true", help="run the loop once then exit (for testing)")
    a = p.parse_args(argv)
    from . import db as _db
    from .qa.backfill import sync_registry

    # first boot on an empty volume: schema AND registry (the raw_observations
    # FK target) exist before any job / api read; a deploy = restart = resync
    conn = _db.get_conn(DB_PATH, allow_init=True)
    sync_registry(conn)
    conn.close()
    if a.once:
        _setup_logging()
        _heartbeat()
        now_wib = datetime.now(WIB)
        for cmd, desc, _key in _due_jobs(now_wib, {}):
            _run_job(cmd, desc)
        logger.info("=== daemon --once done ===")
        return 0
    run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
