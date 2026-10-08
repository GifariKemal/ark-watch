"""queries.py — shared read queries over the raw tables.

One owner for cross-module read SQL so shared filters (e.g.
vintage_ts='realtime') are not re-implemented per caller. Write paths stay
in db.py (insert/upsert) + the job modules (domain-specific).
"""

from __future__ import annotations

import sqlite3


def latest_observation(conn: sqlite3.Connection, series_id: str) -> tuple[str, float] | None:
    """(ts, value) of the latest realtime observation — the vintage filter
    excludes historical ALFRED vintages."""
    row = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? "
        "AND vintage_ts='realtime' ORDER BY ts DESC LIMIT 1",
        (series_id,),
    ).fetchone()
    return (row[0], row[1]) if row else None


def series_values(conn: sqlite3.Connection, series_id: str, limit: int = 1600) -> list[float]:
    """REALTIME value series ordered ASCENDING (ready for z/momentum)."""
    rows = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id=? AND vintage_ts='realtime'"
        " ORDER BY ts DESC LIMIT ?",
        (series_id, limit),
    ).fetchall()
    return [r[0] for r in reversed(rows) if r[0] is not None]


def flows_latest(conn: sqlite3.Connection, days: int = 1) -> list[tuple]:
    """Latest flows_daily rows (several days for funding/EOD lookback)."""
    return conn.execute("SELECT * FROM flows_daily ORDER BY date DESC LIMIT ?", (days,)).fetchall()


def cot_category_window(
    conn: sqlite3.Connection, contract_code: str, category: str, limit: int = 156
) -> list[tuple]:
    """Latest `limit` futures-only cot_raw rows per category — ordered ASCENDING."""
    rows = conn.execute(
        "SELECT report_date, long, short FROM cot_raw "
        "WHERE contract_code=? AND category=? AND long IS NOT NULL AND long > 0 "
        "AND report_type NOT LIKE '%_c' "
        "ORDER BY report_date DESC LIMIT ?",
        (contract_code, category, limit),
    ).fetchall()
    return rows[::-1]
