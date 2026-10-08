"""jobs_runner.py — executes on-demand jobs queued through the REST API.

The API only INSERTs `jobs` rows (status queued); the daemon owner calls
run_pending_jobs(conn) from its loop. Each job is a fresh subprocess
`python -m arkwatch <argv>` like the scheduled jobs, with a hard timeout.
Only kinds in JOB_KINDS ever run: the argv is fixed here, never taken from
the request (params_json is reserved and ignored).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# Allowlist = the daemon's idempotent fetch/compute jobs (SCHEDULE commands
# plus the interval-driven market/market-news). Excluded on purpose: brief/
# send (delivery), gdelt-retention --apply (deletes data), alfred/backfill
# (heavy maintenance), daemon/backup/export.
JOB_KINDS: dict[str, list[str]] = {
    "market": ["market"],
    "market-news": ["market-news"],
    "harvest": ["harvest"],
    "instruments-sweep": ["instruments", "sweep"],
    "energy": ["energy"],
    "calendar": ["calendar"],
    "surprise": ["surprise"],
    "cme": ["cme"],
    "f2": ["f2"],
    "fiscalx": ["fiscalx"],
    "nyfed-ops": ["nyfed", "ops"],
    "fedsurvey": ["fedsurvey"],
    "coverage": ["coverage"],
}
JOB_TIMEOUT_S = 1800


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def run_pending_jobs(conn: sqlite3.Connection, *, timeout: int = JOB_TIMEOUT_S) -> int:
    """Run every queued job oldest-first; returns how many were executed."""
    ran = 0
    while True:
        row = conn.execute(
            "SELECT id, kind FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return ran
        job_id, kind = row[0], row[1]
        # atomic claim: a concurrent runner loses the race and skips the row
        claimed = conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (_now(), job_id),
        ).rowcount
        conn.commit()  # no-op on db.get_conn (autocommit) connections
        if not claimed:
            continue
        status, result, error = "failed", None, None
        argv = JOB_KINDS.get(kind)
        if argv is None:
            error = f"kind '{kind}' is not allowlisted"
        else:
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "arkwatch", *argv],
                    cwd=str(ROOT),
                    timeout=timeout,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-3:]
                result = {"returncode": r.returncode, "tail": tail}
                if r.returncode == 0:
                    status = "done"
                else:
                    error = ((r.stderr or "").strip().splitlines() or ["no output"])[-1][:500]
            except subprocess.TimeoutExpired:
                error = f"timeout after {timeout}s"
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, result_json = ?, error = ? WHERE id = ?",
            (status, _now(), json.dumps(result) if result else None, error, job_id),
        )
        conn.commit()
        ran += 1
