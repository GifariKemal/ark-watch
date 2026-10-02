"""alfred.py — weekly job: DB maintenance + vintage audit.

Revision detection is absorbed by the daily harvest (realtime upsert + a
vintage snapshot whenever a value changes). This weekly job performs
maintenance instead:
  1. PRAGMA wal_checkpoint(TRUNCATE) — shrink the WAL file
  2. ANALYZE — refresh query-planner statistics
  3. foreign_key_check + integrity_check — detect corruption
  4. Report vintage rows created this week (evidence FRED revisions are captured)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db
from ..fetchers.fred import fetch_observations
from . import fetch_log

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

CLASS_A_VINTAGE_SERIES: tuple[str, ...] = (
    "PAYEMS",
    "UNRATE",
    "CPIAUCSL",
    "CPILFESL",
    "PCEPILFE",
    "INDPRO",
    "RSAFS",
    "ICSA",
    "M2SL",
)


def maintenance(conn) -> dict:
    out = {}
    out["wal_checkpoint"] = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    conn.execute("ANALYZE")
    conn.commit()
    out["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    out["fk_violations"] = len(fk)
    since = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    out["vintage_rows_week"] = conn.execute(
        "SELECT COUNT(*) FROM raw_observations WHERE vintage_ts != 'realtime' AND fetched_at >= ?",
        (since,),
    ).fetchone()[0]
    return out


def harvest_alfred_vintages(
    conn: sqlite3.Connection,
    series: list[str] | tuple[str, ...] | None = None,
    *,
    start: str = "2016-01-01",
    end: str | None = None,
) -> dict[str, int]:
    """Harvest ALFRED first-print vintages (output_type=4) for Class A macro series.

    Stores rows in raw_observations with vintage_ts='first' and release_ts=realtime_start.
    """
    target_series = series or CLASS_A_VINTAGE_SERIES
    counts: dict[str, int] = {}
    end_date = end or datetime.now(UTC).date().isoformat()
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    registered = {r[0] for r in conn.execute("SELECT series_id FROM series_registry").fetchall()}

    for raw_sid in target_series:
        sid = raw_sid[5:] if raw_sid.startswith("FRED:") else raw_sid
        full_sid = f"FRED:{sid}"
        if full_sid not in registered:
            print(f"  ⚠ {full_sid}: not found in series_registry — skipped")
            continue
        t0 = time.time()
        try:
            obs = fetch_observations(
                sid,
                output_type=4,
                realtime_start=start,
                realtime_end=end_date,
                sort="asc",
            )
        except Exception as ex:
            dur_ms = int((time.time() - t0) * 1000)
            fetch_log.log(
                conn,
                "alfred",
                f"{full_sid}:VINTAGE",
                "ERROR",
                0,
                err=str(ex)[:160],
                duration_ms=dur_ms,
            )
            counts[full_sid] = -1
            print(f"  ✗ {full_sid}: {ex}")
            continue

        valid_rows = [
            (
                full_sid,
                o["ts"],
                o.get("realtime_start") or "na",
                float(o["value"]),
                "first",
                "FRED",
                None,
                now_iso,
            )
            for o in obs
            if o.get("value") is not None
        ]

        if valid_rows:
            conn.executemany(
                "INSERT INTO raw_observations"
                "(series_id, ts, release_ts, value, vintage_ts, source, precision_k, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(series_id, ts, source, vintage_ts) DO UPDATE SET"
                "  release_ts=excluded.release_ts,"
                "  value=excluded.value,"
                "  fetched_at=excluded.fetched_at",
                valid_rows,
            )
            conn.commit()

        dur_ms = int((time.time() - t0) * 1000)
        fetch_log.log(
            conn, "alfred", f"{full_sid}:VINTAGE", "OK", len(valid_rows), duration_ms=dur_ms
        )
        counts[full_sid] = len(valid_rows)
        print(f"  ✓ {full_sid}: {len(valid_rows)} vintage rows ({start}..{end_date}) in {dur_ms}ms")

    return counts


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(prog="arkwatch alfred")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument(
        "--vintages",
        action="store_true",
        help="Harvest ALFRED first-print vintages for Class A macro series",
    )
    p.add_argument("--series", nargs="*", help="Specific series IDs to harvest")
    p.add_argument(
        "--start", default="2016-01-01", help="Start date for vintage window (default: 2016-01-01)"
    )
    p.add_argument("--end", default=None, help="End date for vintage window")
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)
    if a.vintages:
        print("=== ALFRED first-print vintage harvest ===")
        results = harvest_alfred_vintages(conn, a.series, start=a.start, end=a.end)
        conn.close()
        failed = [s for s, c in results.items() if c < 0]
        return 1 if failed else 0

    r = maintenance(conn)
    conn.close()
    print("=== weekly maintenance ===")
    print(f"  wal_checkpoint: {r['wal_checkpoint']} (0=ok, 1=retry, -1=busy)")
    print(f"  integrity: {r['integrity']}")
    print(f"  fk_violations: {r['fk_violations']}")
    print(f"  vintage rows last 7 days: {r['vintage_rows_week']} (>0 = FRED revisions captured ✓)")
    ok = r["integrity"] == "ok" and r["fk_violations"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
