"""daemon.py — in-app scheduler: one process, jobs = isolated subprocesses.

A 30-second loop checks the WIB clock and runs due jobs as subprocesses. Each
job is a fresh Python process (crashes/leaks do not spread). The heartbeat
file is refreshed every loop; if it goes stale for more than 5 minutes, the
daemon is dead or hung. A lockfile prevents a second instance (duplicate
briefs).
Run: python -m arkwatch daemon
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT = ROOT / "data" / "daemon_heartbeat"
LOCKFILE = ROOT / "data" / "daemon.lock"
LOG_DIR = ROOT / "logs"
WIB = ZoneInfo("Asia/Jakarta")
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")

# Schedule entries: (hour, minute, day, job_cmd, description)
# day: daily | monday..saturday | sunday — literal day tokens as used below;
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
    (6, 10, "daily", "fiscalx", "Fiscaldata expansion: auctions + DTS transactions + interest expense/rates"),
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
    (7, 0, "daily", "brief", "Generate brief + outbox (skip if Saturday edition already published)"),
    (7, 5, "daily", "send", "Send brief via Telegram (PAUSED holder — see _run_job guard)"),
    (7, 15, "daily", "verify", "Truth gate"),
    (8, 15, "daily", "cme", "CME settlements + CVOL + VOI (gray harvester)"),
    (8, 30, "daily", "f2", "COT + flows (Bybit/Farside/PBoC/LBMA/TIC/LME) + FedWatch"),
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
    (21, 15, "sunday", "f4 backfill-first", "First-print vintage heal (ALFRED truth)"),
    (21, 30, "sunday", "f4 replay", "Point-in-time regime replay refresh"),
    (21, 50, "sunday", "calibrate", "Golden anchors quarterly calibration & drift audit"),
    # ROUND-11: the dot plot refreshes 4x/year with SEP meetings — a quarterly
    # cadence job re-fetches all vintages (idempotent; the web is the source)
    (5, 0, "sunday", "backfill --source sep", "FOMC dot plot refresh (quarterly cadence)"),
    # NY Fed research expansion (2026-09-19): HHDC/MCT/LW/GSCPI/HPW rewrite
    # whole histories — the daily window can't see revisions older than its
    # floor, so a weekly full-history re-ingest lands them as vintage rows
    (5, 10, "sunday", "backfill --source nyfedresearch", "NY Fed research full-history refresh (revisions)"),
    # FRB charge-off/delinquency — same whole-history re-release pattern
    (5, 15, "sunday", "backfill --source frb", "Fed Board charge-off refresh (revisions)"),
    # Fed surveys + reports: SLOOS/Beige Book/SCOOS/FSR/Minutes/Press Conf.
    # Weekly check (quarterly/monthly sources — "unchanged" is the normal
    # outcome ~95% of days; new data triggers fetch + NLP + store).
    # ALSO runs daily: press conf + minutes land on FOMC days, not Sundays.
    (6, 50, "daily", "fedsurvey", "Fed surveys + FOMC comms (SLOOS/BeigeBook/Minutes/PressConf NLP)"),
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

# D-023 data-first phase (owner 2026-09-13): GENERATION must keep running
# (the brief pipeline persists five audit-trail store_* families), only the
# OUTBOUND delivery is held. Resume delivery by emptying this set.
PAUSED_JOBS = {"send"}

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
    # stderr mirror only for interactive runs — under systemd the unit's
    # StandardError=append doubles EVERY line into logs/daemon-err.log which
    # nothing rotates (audit round-2: 800KB duplicate log after ~2 months)
    import sys as _sys

    if _sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("daemon: %(message)s"))
        logger.addHandler(sh)


def _heartbeat():
    HEARTBEAT.parent.mkdir(exist_ok=True)
    HEARTBEAT.write_text(datetime.now(UTC).isoformat(timespec="seconds"))


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
        if day == "daily" or DAY_MAP.get(day) == wd:
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
    try:
        from . import db as _db
        from .qa.watcher import _fire

        conn = _db.get_conn(ROOT / "data" / "arkwatch.db")
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
    if cmd in PAUSED_JOBS:
        logger.info(f"⏸ {cmd} paused (D-023 data-first) — {desc}")
        return True
    t0 = time.monotonic()
    logger.info(f"▶ {cmd} — {desc}")
    # heartbeat inside the wrapper too: a job >5 min (verify measured
    # 221-269s, harvest 368s) leaves the loop-top heartbeat stale, one step
    # from tripping the 5-min staleness contract (D-018c)
    _heartbeat()
    try:
        r = subprocess.run(
            [sys.executable, "-m", "arkwatch"] + cmd.split(),
            cwd=str(ROOT),
            timeout=1800,  # 30-minute hard kill
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        dt = time.monotonic() - t0
        if r.returncode == 0:
            tail = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
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
        err = (r.stderr or r.stdout or "").strip().splitlines()
        # ROUND-9: pages must quote the FAILING line, not the last — a
        # verify failure blamed LME:CA_STOCKS (healthy, last row printed)
        # while the sick series hid mid-stream
        fail_lines = [
            ln for ln in err if any(m in ln for m in ("✗", "ERROR", "Error", "Traceback"))
        ]
        tail = (" | ".join(fail_lines[-2:]) if fail_lines else (err[-1] if err else "no output"))[:200]
        logger.error(f"✗ {cmd} ({dt:.0f}s) exit={r.returncode} — {tail}")
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


STATE_PATH = ROOT / "data" / "daemon_state.json"


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
            if k.endswith(f"@{today}") and v == "1"
        }
    except Exception:
        return {}


def _save_state(d: dict[str, str], path: Path | None = None) -> None:
    """Persist the day-state — successes only, mirroring _load_state."""
    import json as _json

    try:
        succ = {k: v for k, v in d.items() if v == "1"}
        (path or STATE_PATH).write_text(_json.dumps(succ))
    except Exception as ex:
        logger.warning(f"state persist failed: {ex}")


def run_loop():
    # Lockfile prevents duplicate instances
    if LOCKFILE.exists():
        try:
            pid = int(LOCKFILE.read_text().strip())
            import os

            os.kill(pid, 0)  # raises if the process is gone
            print(f"another daemon is alive (pid {pid}) — exit")
            return
        except (ValueError, OSError, ImportError):
            pass  # stale lock → take over
    LOCKFILE.parent.mkdir(exist_ok=True)
    LOCKFILE.write_text(str(__import__("os").getpid()))

    _setup_logging()
    # ROUND-10: stamp the code revision at start + every log rotation — the
    # soak lens was misled by an assumed-HEAD daemon (the restart only
    # landed the new code at 10:37 WIB while the 08:30 job ran pre-fix)
    import subprocess as _sp

    try:
        _head = _sp.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
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
    last_watch = 0.0
    last_market_bucket = ""
    last_news_bucket = ""
    try:
        while True:
            _heartbeat()
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

            # Watcher: run every ~60 seconds
            if time.monotonic() - last_watch >= WATCH_INTERVAL_S:
                last_watch = time.monotonic()
                _run_job("watch", "Alert watcher")

            market_bucket = now_wib.strftime("%Y%m%d%H") + f"{now_wib.minute // MARKET_INTERVAL_MINUTES:02d}"
            if now_wib.minute % MARKET_INTERVAL_MINUTES == 0 and market_bucket != last_market_bucket:
                last_market_bucket = market_bucket
                _run_job("market", "Five-minute cross-asset timeline")

            news_interval = _market_news_interval(now_wib)
            news_bucket = now_wib.strftime("%Y%m%d%H") + f"{now_wib.minute // news_interval:02d}"
            if now_wib.minute % news_interval == 0 and news_bucket != last_news_bucket:
                last_news_bucket = news_bucket
                _run_job("market-news", "Cross-source catalyst news")
                _run_job("breadth", "S&P 500 constituent breadth")
                _run_job("crypto", "Crypto liquidation analytics")
            # Rotate logs at the UTC date change (the filename convention is
            # UTC). CYCLE-counting drifted: a daemon started at 20:18 rotated
            # at 20:18 daily, so yesterday's filename kept receiving today's
            # runs (live: daemon-20260916.log carried all of 09-17).
            if datetime.now(UTC).date() != log_day:
                log_day = datetime.now(UTC).date()
                _setup_logging()
            time.sleep(30)
    finally:
        LOCKFILE.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch daemon")
    p.add_argument("--once", action="store_true", help="run the loop once then exit (for testing)")
    a = p.parse_args(argv)
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
