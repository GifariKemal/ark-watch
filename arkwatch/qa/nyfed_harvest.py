"""nyfed_harvest.py — NY Fed jobs outside the registry increment (migration v9):

ops    (daily 06:30 WIB)  desk outright operations tsy/ambs (announcements
                          latest + results last/14) + fxs swap-line watch
pd     (Thu 07:00 WIB)    Primary Dealer Positions Survey — full history per
                          curated keyid, INSERT OR IGNORE (release Wed night ET)
rates  (manual)           increment every registered NYFED: series — the
                          production path for these is the 06:00 `harvest` job
                          (they are ordinary registry series via ROUTES); this
                          subcommand exists for seeding + live verification.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import db
from ..fetchers import nyfed
from . import fetch_log

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
OPS_COLUMNS = (
    "operation_id, family, operation_date, settlement_date, operation_type, direction,"
    " maturity_start, maturity_end, status, amount, details_json"
)


def store_fed_operations(conn, rows: list[dict]) -> int:
    """INSERT OR REPLACE keyed (operation_id, family): an announcement is
    overwritten by the same operation's results row once it settles (rows must
    be ordered announcements-first — the harvester guarantees that)."""
    payload = [
        (
            r["operation_id"],
            r["family"],
            r["operation_date"],
            r["settlement_date"],
            r["operation_type"],
            r["direction"],
            r["maturity_start"],
            r["maturity_end"],
            r["status"],
            r["amount"],
            r["details_json"],
        )
        for r in rows
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO fed_operations({OPS_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(payload)


def store_pd_positions(conn, rows: list[dict]) -> int:
    """Append-only insert. The PK (asofdate, keyid) makes the FIRST seriesbreak
    win: a future methodology break (SBP20xx) landing on overlapping dates can
    never silently overwrite SBN2024 values — z-scores must not cross breaks.
    Returns the number of NEW rows."""
    payload = [(r["asofdate"], r["keyid"], r["seriesbreak"], r["value_musd"]) for r in rows]
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO pd_positions(asofdate,keyid,seriesbreak,value_musd)"
            " VALUES (?,?,?,?)",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def harvest_ops(conn) -> int:
    print("=== NY Fed desk operations + fxs ===")
    err = None
    n = 0
    first_row: dict | None = None
    n_fxs_ops = 0
    n_cp = 0
    from .fetch_log import log_collection

    try:
        rows: list[dict] = []
        for family in ("tsy", "ambs"):
            # announcements first — a results row for the same operation_id must
            # win the INSERT OR REPLACE (lifecycle: Announced -> Results)
            rows += nyfed.fetch_announcements(family)
            rows += nyfed.fetch_operations(family)
        # Store the two CORE feeds BEFORE the fxs leg: fxs has never been seen
        # live (feed normally empty) and must not be able to discard
        # already-fetched desk data.
        rows = [r for r in rows if r.get("operation_id") and r.get("operation_date")]
        first_row = next(
            (r for r in rows if r["family"] == "tsy" and r.get("status") == "Results"),
            rows[0] if rows else None,
        )
        n = store_fed_operations(conn, rows)
        print(f"  fed_operations: {n} rows upserted")
    except Exception as ex:
        err = str(ex)[:140]
        print(f"  ✗ {ex}")
    log_collection(conn, "nyfed", "NYFED:FED_OPS", first_row, n, err=err)

    # fxs leg isolated: swap-line watch + counterparty metadata — a failure
    # here is visible but must not affect the exit code of the core feeds
    cp_err: str | None = None
    try:
        fxs = nyfed.fetch_fxs_latest()
        fxs_rows = [
            r for r in fxs["operations"] if r.get("operation_id") and r.get("operation_date")
        ]
        if fxs_rows:
            n_fxs_ops = store_fed_operations(conn, fxs_rows)
            print(f"  ⚠ fxs operations NON-EMPTY ({n_fxs_ops}), dollar-funding event")
        n_cp = len(fxs["counterparties"])
    except Exception as ex:
        cp_err = str(ex)[:140]
        print(f"  ✗ fxs: {ex}")
    # counterparty list = static standby banks — fetch metadata, not a table
    log_collection(conn, "nyfed", "NYFED:FXS_CP", None, n_cp, err=cp_err)
    return 1 if err else 0


def harvest_pd(conn) -> int:
    print("=== NY Fed Primary Dealer Positions Survey ===")
    err = None
    total = 0
    n_new = 0
    first_row: dict | None = None
    try:
        for keyid in nyfed.PD_KEYIDS:
            rows = nyfed.fetch_pd_series(keyid)
            if first_row is None:
                first_row = rows[0]
            n_new += store_pd_positions(conn, rows)
            total += len(rows)
            latest = rows[-1]["asofdate"]
            # value_musd may legitimately be None (missing latest print) — the
            # display must not crash the harvest of the remaining keyids
            v = rows[-1]["value_musd"]
            v_str = "n/a" if v is None else f"${v / 1e3:,.1f}B"
            print(f"  {keyid:15s}: {len(rows):4d} obs → {latest} ({v_str})")
        print(f"  pd_positions: +{n_new} new rows ({total} total fetched)")
    except Exception as ex:
        err = str(ex)[:140]
        print(f"  ✗ {ex}")
    from .fetch_log import log_collection

    log_collection(conn, "nyfed", "NYFED:PD", first_row, total, err=err)
    return 1 if err else 0


def harvest_rates(conn) -> int:
    from .backfill import sync_registry

    sync_registry(conn)  # same contract as the 06:00 harvest: YAML is the truth
    sids = [
        r[0]
        for r in conn.execute(
            "SELECT series_id FROM series_registry WHERE series_id LIKE 'NYFED:%'"
            " AND active=1 ORDER BY series_id"
        )
    ]
    print(f"=== NY Fed registry series increment ({len(sids)} series) ===")
    fail = 0
    for sid in sids:
        try:
            cur = nyfed.fetch_latest(sid)
            n = db.insert_observations(conn, [(sid, cur["ts"], cur["value"], "NYFED")])
            print(f"  {sid:18s} {cur['ts']} = {cur['value']}" + (" (new)" if n else ""))
        except Exception as ex:
            fail += 1
            print(f"  ✗ {sid}: {str(ex)[:120]}")
    return fetch_log.exit_code(len(sids) - fail, fail)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch nyfed")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ops", help="desk operations tsy/ambs + fxs (daily 06:30 WIB)")
    sub.add_parser("pd", help="Primary Dealer Positions Survey (Thu 07:00 WIB)")
    sub.add_parser("rates", help="increment all registered NYFED: series (manual seed)")
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)
    if a.cmd == "ops":
        rc = harvest_ops(conn)
    elif a.cmd == "pd":
        rc = harvest_pd(conn)
    else:
        rc = harvest_rates(conn)
    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
