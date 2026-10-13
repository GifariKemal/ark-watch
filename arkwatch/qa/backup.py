"""Nightly database backup: VACUUM INTO + verification + rotation + table retention.

Verification opens the resulting file, runs integrity_check, and compares the
row COUNTs of key tables. Retention: 7 dailies + 6 monthlies (monthly = the
first backup of each month). Every backup is a full copy on the SAME volume,
so the count stays small; offsite is Litestream's job. Copying the live DB
file directly is forbidden (a live SQLite file in WAL mode may copy
inconsistently).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..db import default_db_path

_ROOT = Path(__file__).resolve().parent.parent.parent
# Docker: ARKWATCH_DATA_DIR=/data keeps backups on the volume next to the DB.
# Local only - offsite is Litestream's job, no upload hook here.
_DATA_ENV = os.environ.get("ARKWATCH_DATA_DIR")
DEFAULT_DB = default_db_path()
BACKUP_DIR = Path(_DATA_ENV) / "backups" if _DATA_ENV else _ROOT / "backups"
KEY_TABLES = ("raw_observations", "instrument_prices", "events")
DAILIES, MONTHLIES = 7, 6
# Crash leftovers outside the arkwatch-*.db glob: gdelt_retention's pre-cleanup snapshot and a
# backup .tmp whose run was killed (a redeploy mid-VACUUM; backup() only clears TODAY's .tmp)
ORPHAN_GLOBS, ORPHAN_MAX_AGE_S = (".gdelt-retention-before-*.db", "arkwatch-*.db.tmp"), 86400
# (table, time column, keep days, extra filter). 2026-10-12 audit: the OKX stream grew the DB
# ~60-85 MB/day with no retention at all. Longest reader per table:
# - intraday 1m: same-day view (market_timeline refetches range=1d); readers pin interval='5m'
# - fetch_log: health views read <= 8 days; the 5-minute market/okx rows are 90% of the volume
# - orderbook snapshots, raw trade batches, trade events: no reader beyond MAX(ts) (recovery
#   highwater) - a raw archive
# - liquidations: cascade detector baseline 30 days; flow_1m: CVD 24 h
# - cme options: OI walls/max pain read the latest date, the PCR band the trailing history
RETENTION = (
    ("fetch_log", "ts", 180, ""),
    ("fetch_log", "ts", 30, " AND fetcher IN ('market', 'okx_market')"),
    ("intraday_bars", "bar_ts_utc", 3, " AND interval = '1m'"),
    ("crypto_orderbook_snapshots", "ts_utc", 7, ""),
    ("crypto_trade_raw_batches", "batch_ts_utc", 7, ""),
    ("crypto_trade_events", "ts_utc", 7, ""),
    ("crypto_liquidations", "ts_utc", 35, ""),
    ("crypto_trade_flow_1m", "minute_utc", 180, ""),
    ("crypto_derivatives", "ts_utc", 180, ""),
    ("cme_options_settlements", "trade_date", 400, ""),
)
PRUNE_CHUNK = 5000  # short write transactions: okxws and the lanes keep writing meanwhile


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
        # verify BEFORE the rename: a bad copy must never replace today's good one
        verify(db_path, tmp)
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


def rotate(now: float | None = None) -> None:
    """Retention: last DAILIES dailies + the first backup of each month (max MONTHLIES).

    The first backup of a month is preserved so monthly history survives the
    daily rotation. Also sweeps gdelt_retention snapshots a hard kill left behind.
    """
    cutoff = (now if now is not None else datetime.now(UTC).timestamp()) - ORPHAN_MAX_AGE_S
    for f in (f for g in ORPHAN_GLOBS for f in BACKUP_DIR.glob(g)):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
    files = sorted(BACKUP_DIR.glob("arkwatch-*.db"))
    # Monthly = the FIRST backup of each month (smallest date per YYYYMM)
    first_of_month: dict[str, Path] = {}
    for f in files:
        stem = f.stem  # arkwatch-YYYYMMDD-HHMMSS or arkwatch-YYYYMMDD
        ym = stem[9:15]
        if ym.isdigit() and len(ym) == 6:
            first_of_month.setdefault(ym, f)
    keep_monthly = set(first_of_month[ym] for ym in sorted(first_of_month)[-MONTHLIES:])
    dailies = [f for f in files if f not in keep_monthly]
    for f in dailies[:-DAILIES]:
        f.unlink(missing_ok=True)


def prune(db_path: str, now: datetime | None = None) -> dict[str, int]:
    """Apply RETENTION in PRUNE_CHUNK-row transactions; returns deleted rows per table."""
    now = now or datetime.now(UTC)
    deleted: dict[str, int] = {}
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        for table, col, days, extra in RETENTION:
            cut = now - timedelta(days=days)
            cutoff = (
                cut.date().isoformat() if col == "trade_date" else cut.isoformat(timespec="seconds")
            )
            sql = (
                f"DELETE FROM {table} WHERE rowid IN"  # table/col/extra are internal literals
                f" (SELECT rowid FROM {table} WHERE {col} < ?{extra} LIMIT {PRUNE_CHUNK})"
            )
            n = 0
            while True:
                rc = conn.execute(sql, (cutoff,)).rowcount
                n += rc
                if rc < PRUNE_CHUNK:
                    break
            deleted[table] = deleted.get(table, 0) + n
        conn.execute("PRAGMA optimize")  # refresh planner stats where they drifted
    finally:
        conn.close()
    return deleted


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch backup")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    dst = backup(a.db)
    rotate()
    try:
        pruned = prune(a.db)
        msg = ", ".join(f"{t} -{n}" for t, n in pruned.items() if n)
        print(f"  pruned: {msg or 'none'}")
    except Exception as ex:  # pruning must never fail the backup
        print(f"  ⚠ prune skipped: {str(ex)[:80]}")
    print(
        f"=== backup OK: {dst.name} ({dst.stat().st_size / 1e6:.1f} MB, "
        f"integrity ✓, key-table COUNTs match) ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
