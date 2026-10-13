"""Every migration keeps the rows that exist before it (2026-10-12 audit, G9): the chain was
only tested on empty databases."""

from __future__ import annotations

import sqlite3

import pytest

from arkwatch import db

_DUMMY = {"INTEGER": 1, "REAL": 1.0, "BLOB": b"x"}


def _seed_every_table(conn: sqlite3.Connection) -> dict[str, int]:
    """One row per table where a generic row satisfies the constraints; returns counts."""
    counts = {}
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " AND name <> 'schema_migrations'"
    ).fetchall():
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        names = [c[1] for c in cols if not (c[5] and c[2].upper() == "INTEGER")]  # skip rowid PKs
        values = [_DUMMY.get(c[2].upper().split("(")[0], "x") for c in cols if c[1] in names]
        try:
            conn.execute(
                f'INSERT INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" * len(names))})',
                values,
            )
        except sqlite3.DatabaseError:
            continue  # a CHECK/FK the generic row cannot meet: that table is not checked
        counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return counts


@pytest.mark.parametrize("version", range(2, db.SCHEMA_VERSION + 1))
def test_migration_keeps_existing_rows(tmp_path, monkeypatch, version):
    path = tmp_path / "m.db"
    full = dict(db.MIGRATIONS)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")  # seeding only; the migration runs as in prod
    monkeypatch.setattr(db, "MIGRATIONS", {v: s for v, s in full.items() if v < version})
    db._apply_migrations(conn)
    before = _seed_every_table(conn)
    n_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert len(before) >= 0.7 * (n_tables - 1)  # the generic seed must cover most tables
    monkeypatch.setattr(db, "MIGRATIONS", full)
    db._apply_migrations(conn, from_version=version - 1)
    after = {
        t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        for t in before
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
    }
    conn.close()
    lost = {t: (before[t], n) for t, n in after.items() if n < before[t]}
    assert not lost, f"migration {version} dropped rows: {lost}"
