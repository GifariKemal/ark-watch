"""Lossless storage helpers for the duplicate raw GDELT payloads."""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import zlib
from pathlib import Path

from . import db as _db

TABLE_KEYS = {
    "gdelt_events": "event_id",
    "gdelt_mentions": "observation_id",
    "gdelt_gkg": "record_id",
}


def backup_database(source: str | Path, destination: str | Path) -> dict:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ValueError("backup destination must differ from source")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if destination_path.exists():
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination_path)
    try:
        source_conn.backup(target_conn, pages=4096, sleep=0.25)
        integrity = target_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"backup integrity check failed: {integrity}")
        return {"path": str(destination_path), "bytes": destination_path.stat().st_size}
    finally:
        target_conn.close()
        source_conn.close()


def _require_arkwatch_database(path: str | Path) -> int:
    db_path = Path(path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "schema_migrations" not in names:
            raise ValueError("refusing to modify a database without ARK Watch schema_migrations")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version is None or version > _db.SCHEMA_VERSION:
            raise ValueError(f"unsupported ARK Watch database schema version: {version}")
        required = {
            "gdelt_events": {"event_id", "raw_record_json"},
            "gdelt_mentions": {"observation_id", "raw_record_json"},
            "gdelt_gkg": {"record_id", "raw_record_json"},
        }
        for table, columns in required.items():
            if table not in names:
                raise ValueError(f"ARK Watch database is missing required table {table}")
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise ValueError(f"ARK Watch table {table} is missing required GDELT columns")
        return version
    finally:
        conn.close()


def compress_record(raw_json: str) -> bytes:
    return gzip.compress(raw_json.encode("utf-8"), compresslevel=6, mtime=0)


def restore_record(raw_json: str | None, raw_gzip: bytes | None) -> str:
    if raw_gzip:
        return gzip.decompress(raw_gzip).decode("utf-8")
    return raw_json or ""


def verify_storage(conn: sqlite3.Connection, *, batch_size: int = 500) -> dict:
    verification = {}
    for table, key in TABLE_KEYS.items():
        cursor = conn.execute(
            f"SELECT {key},raw_record_json,raw_record_gzip FROM {table} ORDER BY {key}"
        )
        summary = {"rows": 0, "text_rows": 0, "gzip_rows": 0, "gzip_bytes": 0}
        while rows := cursor.fetchmany(batch_size):
            for row_key, raw_json, raw_gzip in rows:
                has_text = bool(raw_json)
                has_gzip = bool(raw_gzip)
                if has_text == has_gzip:
                    raise ValueError(f"GDELT payload representation invalid for {table}:{row_key}")
                try:
                    restored = restore_record(raw_json, raw_gzip)
                    json.loads(restored)
                except (EOFError, OSError, UnicodeDecodeError, ValueError, zlib.error) as ex:
                    raise ValueError(f"GDELT payload invalid for {table}:{row_key}") from ex
                summary["rows"] += 1
                summary["text_rows"] += int(has_text)
                summary["gzip_rows"] += int(has_gzip)
                summary["gzip_bytes"] += len(raw_gzip) if raw_gzip else 0
        verification[table] = summary
    return verification


def verify_database(path: str | Path) -> dict:
    db_path = Path(path).resolve()
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        return verify_storage(conn)
    finally:
        conn.close()


def compact_database(
    path: str | Path, *, batch_size: int = 500, apply: bool = False, vacuum: bool = False
) -> dict:
    if not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if vacuum and not apply:
        raise ValueError("vacuum requires apply")
    if apply:
        _require_arkwatch_database(path)
        conn = _db.get_conn(path, allow_init=True)
    else:
        db_path = Path(path).resolve()
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    result = {"applied": apply, "tables": {}}
    try:
        for table, key in TABLE_KEYS.items():
            last_key = None
            condition = "raw_record_gzip IS NULL AND raw_record_json <> ''"
            pending = conn.execute(
                f"SELECT {key}, raw_record_json FROM {table} "
                f"WHERE {condition} ORDER BY {key} LIMIT ?",
                (batch_size,),
            ).fetchall()
            table_result = {"rows": 0, "raw_bytes": 0, "gzip_bytes": 0}
            result["tables"][table] = table_result
            while pending:
                updates = []
                for row_key, raw_json in pending:
                    raw_gzip = compress_record(raw_json)
                    if restore_record(None, raw_gzip) != raw_json:
                        raise ValueError(f"GDELT payload round-trip failed for {table}:{row_key}")
                    table_result["rows"] += 1
                    table_result["raw_bytes"] += len(raw_json.encode("utf-8"))
                    table_result["gzip_bytes"] += len(raw_gzip)
                    updates.append((raw_gzip, "" if apply else raw_json, row_key))
                last_key = pending[-1][0]
                if apply:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.executemany(
                            f"UPDATE {table} SET raw_record_gzip=?, raw_record_json=? WHERE {key}=?",
                            updates,
                        )
                        placeholders = ",".join("?" for _ in pending)
                        stored = {
                            row_key: (raw_json, raw_gzip)
                            for row_key, raw_json, raw_gzip in conn.execute(
                                f"SELECT {key},raw_record_json,raw_record_gzip FROM {table} "
                                f"WHERE {key} IN ({placeholders})",
                                [row[0] for row in pending],
                            )
                        }
                        for row_key, raw_json in pending:
                            stored_text, stored_gzip = stored[row_key]
                            if stored_text or restore_record(stored_text, stored_gzip) != raw_json:
                                raise ValueError(
                                    f"stored GDELT payload verification failed for {table}:{row_key}"
                                )
                        conn.execute("COMMIT")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
                if not apply:
                    condition = (
                        "raw_record_gzip IS NULL AND raw_record_json <> '' AND " + key + " > ?"
                    )
                pending = conn.execute(
                    f"SELECT {key}, raw_record_json FROM {table} WHERE {condition} ORDER BY {key} LIMIT ?",
                    (last_key, batch_size) if not apply else (batch_size,),
                ).fetchall()
        if vacuum:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0]:
                raise RuntimeError(
                    "WAL checkpoint is busy; stop all services using the database first"
                )
            conn.execute("VACUUM")
        result["verification"] = verify_storage(conn, batch_size=batch_size)
    finally:
        conn.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arkwatch gdelt-storage")
    parser.add_argument("--db", default="data/arkwatch.db")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--apply", action="store_true", help="compress existing raw payloads in place"
    )
    parser.add_argument("--backup", help="new SQLite backup path required with --apply")
    parser.add_argument(
        "--verify-only", action="store_true", help="audit stored GDELT raw payloads"
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="reclaim free pages; requires all services using the database to be stopped",
    )
    args = parser.parse_args(argv)
    if args.verify_only:
        if args.apply or args.backup or args.vacuum:
            parser.error("--verify-only cannot be combined with write options")
        print(verify_database(args.db))
        return 0
    if args.apply != bool(args.backup):
        parser.error("--apply requires a new --backup path; --backup is not used in preview mode")
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply")
    if args.backup:
        _require_arkwatch_database(args.db)
        backup = backup_database(args.db, args.backup)
        print({"backup": backup})
    print(
        compact_database(args.db, batch_size=args.batch_size, apply=args.apply, vacuum=args.vacuum)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
