"""Manual database backup command: VACUUM INTO + verification.

Verification opens the resulting file, runs integrity_check, and compares the
row COUNTs of key tables. Retention: 30 dailies + 12 monthlies (monthly = the
first backup of each month). Copying the live DB file directly is forbidden
(a live SQLite file in WAL mode may copy inconsistently).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
# Docker: ARKWATCH_DATA_DIR=/data keeps backups on the volume next to the DB.
# Local only - offsite is Litestream's job, no upload hook here.
_DATA_ENV = os.environ.get("ARKWATCH_DATA_DIR")
DEFAULT_DB = (Path(_DATA_ENV) if _DATA_ENV else _ROOT / "data") / "arkwatch.db"
BACKUP_DIR = Path(_DATA_ENV) / "backups" if _DATA_ENV else _ROOT / "backups"
KEY_TABLES = ("raw_observations", "instrument_prices", "events")
# 1m bars are a same-day view (market_timeline refetches range=1d); every
# signal/level/backtest reader pins interval='5m'
INTRADAY_1M_RETENTION_DAYS = 3


def backup(db_path: str = str(DEFAULT_DB)) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    dst = BACKUP_DIR / f"arkwatch-{today}.db"
    # VACUUM INTO a tmp name (outside rotate()'s *.db glob), then an atomic
    # rename: a failed VACUUM keeps today's previous backup instead of none
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)  # VACUUM INTO refuses an existing file (crash leftover)
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            conn.close()
        # ROUND-7: intrinsic 600 — the nightly artifact carries every live
        # credential-adjacent table; harden.sh swept the old ones but every NEW
        # backup was still born 644 (umask default)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def verify(db_path: str, backup_path: Path) -> None:
    src = sqlite3.connect(db_path)
    bak = sqlite3.connect(str(backup_path))
    try:
        chk = bak.execute("PRAGMA integrity_check").fetchone()[0]
        if chk != "ok":
            raise RuntimeError(f"backup integrity_check: {chk}")
        for t in KEY_TABLES:
            a = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            b = bak.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            # the daemon keeps writing after the VACUUM snapshot: live may
            # only have grown; fewer live rows than the backup is a real gap
            if b > a:
                raise RuntimeError(f"COUNT mismatch {t}: live={a} backup={b}")
    finally:
        src.close()
        bak.close()


def rotate() -> None:
    """Retention: last 30 dailies + the first backup of each month (max 12 months).

    The first backup of a month is preserved so monthly history survives the
    30-day daily rotation.
    """
    files = sorted(BACKUP_DIR.glob("arkwatch-*.db"))
    # Monthly = the FIRST backup of each month (smallest date per YYYYMM)
    first_of_month: dict[str, Path] = {}
    for f in files:
        stem = f.stem  # arkwatch-YYYYMMDD-HHMMSS or arkwatch-YYYYMMDD
        ym = stem[9:15]
        if ym.isdigit() and len(ym) == 6:
            first_of_month.setdefault(ym, f)
    keep_monthly = set(first_of_month[ym] for ym in sorted(first_of_month)[-12:])
    dailies = [f for f in files if f not in keep_monthly]
    for f in dailies[:-30]:
        f.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch backup")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    dst = backup(a.db)
    verify(a.db, dst)
    rotate()
    # ROUND-6: fetch_log is the one operational table that grew unbounded
    # (~300-900 rows/day incl. retries) with no consumer beyond ~7d of
    # health checks — prune past 180d nightly with the backup. Each DELETE
    # autocommits on its own (short write lock, the daemon keeps running).
    try:
        _c = sqlite3.connect(a.db, isolation_level=None)
        try:
            _c.execute("PRAGMA busy_timeout=30000")
            cur = _c.execute("DELETE FROM fetch_log WHERE ts < datetime('now', '-180 day')")
            print(f"  fetch_log pruned: -{cur.rowcount} rows (>180d)")
            cutoff = datetime.now(UTC) - timedelta(days=INTRADAY_1M_RETENTION_DAYS)
            cur = _c.execute(
                "DELETE FROM intraday_bars WHERE interval = '1m' AND bar_ts_utc < ?",
                (cutoff.isoformat(timespec="seconds"),),
            )
            print(f"  intraday 1m pruned: -{cur.rowcount} rows (>{INTRADAY_1M_RETENTION_DAYS}d)")
            _c.execute("PRAGMA optimize")  # refresh planner stats where they drifted
        finally:
            _c.close()
    except Exception as ex:  # pruning must never fail the backup
        print(f"  ⚠ prune/optimize skipped: {str(ex)[:80]}")
    print(
        f"=== backup OK: {dst.name} ({dst.stat().st_size / 1e6:.1f} MB, "
        f"integrity ✓, key-table COUNTs match) ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
