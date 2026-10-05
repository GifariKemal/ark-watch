"""Retain the current UTC week's collected GDELT records."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .. import db as _db
from .backup import BACKUP_DIR

TABLES = ("gdelt_mentions", "gdelt_gkg", "gdelt_events")
RETENTION_DAYS = 8
MARKET_NEWS_RETENTION_DAYS = int(os.environ.get("MARKET_NEWS_RETENTION_DAYS", "90"))


def _cutoff(now: datetime, days: int = RETENTION_DAYS) -> str:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    utc_now = now.astimezone(UTC)
    cutoff_dt = (utc_now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return cutoff_dt.isoformat(timespec="seconds")


def _summary(conn: sqlite3.Connection, table: str, cutoff: str) -> dict[str, int]:
    extra = (
        " AND NOT EXISTS (SELECT 1 FROM gdelt_mentions m "
        "WHERE m.event_id=gdelt_events.event_id AND m.fetched_at>=?)"
        if table == "gdelt_events"
        else ""
    )
    params = (cutoff, cutoff) if extra else (cutoff,)
    count, payload_bytes = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(COALESCE(length(raw_record_gzip), "
        f"length(raw_record_json))),0) FROM {table} "
        f"WHERE fetched_at < ?{extra}",
        params,
    ).fetchone()
    return {"rows": count, "payload_bytes": payload_bytes}


def _protected_events(conn: sqlite3.Connection, cutoff: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM gdelt_events e WHERE e.fetched_at < ? "
        "AND EXISTS (SELECT 1 FROM gdelt_mentions m "
        "WHERE m.event_id=e.event_id AND m.fetched_at>=?)",
        (cutoff, cutoff),
    ).fetchone()[0]


def _old_fetch_log_rows(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE ts < datetime('now', '-180 day')"
    ).fetchone()[0]


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _record_file_sizes(result: dict, path: Path) -> None:
    result["db_bytes_after"] = _file_size(path)
    result["wal_bytes_after"] = _file_size(Path(f"{path}-wal"))


def _verify_backup(db_path: Path, backup_path: Path) -> dict[str, int]:
    source = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    backup = sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)
    try:
        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("temporary pre-cleanup backup integrity check failed")
        counts = {}
        for table in ("raw_observations", "instrument_prices", "events", *TABLES):
            live_count = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            backup_count = backup.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if live_count != backup_count:
                raise ValueError(f"temporary pre-cleanup backup row count mismatch: {table}")
            counts[table] = live_count
        return counts
    finally:
        source.close()
        backup.close()


def _create_temporary_backup(db_path: Path, now: datetime) -> tuple[Path, int, dict]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUP_DIR / f".gdelt-retention-before-{stamp}-{uuid4().hex[:8]}.db"
    source = sqlite3.connect(db_path)
    source.execute("PRAGMA busy_timeout=30000")
    old_umask = os.umask(0o177)
    try:
        source.execute("VACUUM INTO ?", (str(destination),))
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)
        source.close()
    os.chmod(destination, 0o600)
    try:
        counts = _verify_backup(db_path, destination)
        return destination, destination.stat().st_size, counts
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def clean(
    db_path: str | Path,
    *,
    now: datetime | None = None,
    apply: bool = False,
) -> dict:
    """Preview or apply age-based GDELT deletion; preview is the default."""
    instant = now or datetime.now(UTC)
    cutoff = _cutoff(instant)
    path = Path(db_path).resolve()
    _require_database(path)
    db_bytes_before = _file_size(path)
    wal_bytes_before = _file_size(Path(f"{path}-wal"))
    conn = (
        _db.get_conn(path, allow_init=False)
        if apply
        else sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    )
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        news_cutoff = _cutoff(instant, days=MARKET_NEWS_RETENTION_DAYS)
        news_candidates = conn.execute(
            "SELECT COUNT(*) FROM market_news WHERE published_at_utc < ?", (news_cutoff,)
        ).fetchone()[0]
        result = {
            "applied": apply,
            "cutoff_utc": cutoff,
            "news_cutoff_utc": news_cutoff,
            "window": f"{RETENTION_DAYS}_days",
            "news_window": f"{MARKET_NEWS_RETENTION_DAYS}_days",
            "tables": {table: _summary(conn, table, cutoff) for table in TABLES},
            "protected_events": _protected_events(conn, cutoff),
            "fetch_log_candidates": _old_fetch_log_rows(conn),
            "market_news_candidates": news_candidates,
            "db_bytes_before": db_bytes_before,
            "wal_bytes_before": wal_bytes_before,
        }
        if not apply:
            _record_file_sizes(result, path)
            return result

        if (
            not any(item["rows"] for item in result["tables"].values())
            and not result["fetch_log_candidates"]
            and not result["market_news_candidates"]
        ):
            result["deleted"] = {table: 0 for table in TABLES}
            result["fetch_log_deleted"] = 0
            result["temporary_backup_bytes"] = 0
            result["temporary_backup_removed"] = True
            result["remaining"] = {table: item["rows"] for table, item in result["tables"].items()}
            result["quick_check"] = "not_run_no_deletions"
            _record_file_sizes(result, path)
            return result

        temporary_backup, backup_bytes, backup_counts = _create_temporary_backup(path, instant)
        result["temporary_backup_bytes"] = backup_bytes
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, backup_count in backup_counts.items():
                live_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if live_count != backup_count:
                    raise RuntimeError(f"database changed after backup verification: {table}")
            deleted = {}
            for table in TABLES:
                extra = (
                    " AND NOT EXISTS (SELECT 1 FROM gdelt_mentions m "
                    "WHERE m.event_id=gdelt_events.event_id AND m.fetched_at>=?)"
                    if table == "gdelt_events"
                    else ""
                )
                params = (cutoff, cutoff) if extra else (cutoff,)
                cursor = conn.execute(f"DELETE FROM {table} WHERE fetched_at < ?{extra}", params)
                deleted[table] = cursor.rowcount
            fetch_log = conn.execute("DELETE FROM fetch_log WHERE ts < datetime('now', '-180 day')")
            news_cutoff = _cutoff(instant, days=MARKET_NEWS_RETENTION_DAYS)
            news_del = conn.execute(
                "DELETE FROM market_news WHERE published_at_utc < ?", (news_cutoff,)
            )
            payload_del = conn.execute(
                "DELETE FROM market_news_payloads WHERE fetched_at < ?", (news_cutoff,)
            )
            deleted["market_news"] = news_del.rowcount
            deleted["market_news_payloads"] = payload_del.rowcount
            remaining = {table: _summary(conn, table, cutoff)["rows"] for table in TABLES}
            if any(remaining.values()):
                raise RuntimeError(f"GDELT retention postcondition failed: {remaining}")
            quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if quick_check != "ok":
                raise RuntimeError(f"database quick_check after GDELT cleanup: {quick_check}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        result["deleted"] = deleted
        result["remaining"] = remaining
        result["quick_check"] = quick_check
        result["fetch_log_deleted"] = fetch_log.rowcount
        result["freelist_pages"] = conn.execute("PRAGMA freelist_count").fetchone()[0]
        conn.close()
        conn = None
        temporary_backup.unlink()
        result["temporary_backup_removed"] = not temporary_backup.exists()
        _record_file_sizes(result, path)
        return result
    finally:
        if conn is not None:
            conn.close()


def _require_database(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "schema_migrations" not in names:
            raise ValueError("refusing to modify a database without ARK Watch schema")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version is None or version > _db.SCHEMA_VERSION:
            raise ValueError(f"unsupported ARK Watch schema version: {version}")
        for table in ("gdelt_events", "gdelt_mentions", "gdelt_gkg"):
            if table not in names:
                raise ValueError(f"ARK Watch database is missing {table}")
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not {"fetched_at", "raw_record_json", "raw_record_gzip"} <= columns:
                raise ValueError(f"ARK Watch table {table} is missing retention columns")
        return version
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arkwatch gdelt-retention")
    parser.add_argument("--db", default="data/arkwatch.db")
    parser.add_argument("--apply", action="store_true", help="delete eligible records")
    args = parser.parse_args(argv)
    result = clean(args.db, apply=args.apply)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
