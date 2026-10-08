"""soma_harvest.py — SOMA weekly job (Friday 06:00 WIB): per-CUSIP holdings + summary.

Thursday COB ET release -> Friday ~05:00 WIB availability -> 06:00 WIB harvest.
Layers: this module owns the FETCH layer only (soma_holdings + soma_summary).
All computed_signals soma.* writes belong to the signals layer
(arkwatch/signals/soma.py) — same split as cot_raw vs cot signals.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .. import db
from ..fetchers import nyfed, soma

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
DEFAULT_BACKFILL_WEEKS = 104  # 2 years of weekly snapshots
BACKFILL_PAUSE_S = 0.3  # pacing: polite pause between per-date requests


def store_soma_week(conn, holdings: list[dict]) -> tuple[int, dict]:
    """Compute summary from holdings and write both tables atomically.

    The week is stored as an atomic REPLACE: existing soma_holdings rows for
    the as-of date are deleted and re-inserted together with the derived
    summary, so a Fed restatement of the week refreshes BOTH tables in one
    transaction. (INSERT OR IGNORE would keep stale par values and ghost
    CUSIPs while the summary moved on — the tables would silently drift
    apart; verified 2026-09-03.)
    Returns (n_new_cusips, summary) — n_new counts CUSIPs not present in the
    previously stored week (0 on an idempotent re-run).
    """
    if not holdings:
        raise soma.SomaError("store_soma_week: empty holdings")
    as_of = holdings[0]["as_of_date"]
    mixed = {h["as_of_date"] for h in holdings}
    if mixed != {as_of}:
        raise soma.SomaError(f"store_soma_week: mixed as-of dates {sorted(mixed)}")
    summary = soma.compute_soma_summary(holdings, as_of)
    old_cusips = {
        r[0] for r in conn.execute("SELECT cusip FROM soma_holdings WHERE as_of_date=?", (as_of,))
    }

    payload = [
        (
            h["as_of_date"],
            h["cusip"],
            h["security_type"],
            h["maturity_date"],
            h["par_value"],
            h["pct_outstanding"],
            h["change_week"],
        )
        for h in holdings
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM soma_holdings WHERE as_of_date=?", (as_of,))
        conn.executemany(
            "INSERT INTO soma_holdings"
            "(as_of_date,cusip,security_type,maturity_date,par_value,pct_outstanding,change_week)"
            " VALUES (?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute(
            "INSERT OR REPLACE INTO soma_summary"
            "(as_of_date,total_par,bills,notes_bonds,tips,frn,weekly_change,"
            "rolling_off_7d,rolling_off_30d,rolling_off_90d,n_cusips,avg_maturity_years)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                summary["as_of_date"],
                summary["total_par"],
                summary["bills"],
                summary["notes_bonds"],
                summary["tips"],
                summary["frn"],
                summary["weekly_change"],
                summary["rolling_off_7d"],
                summary["rolling_off_30d"],
                summary["rolling_off_90d"],
                summary["n_cusips"],
                summary["avg_maturity_years"],
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len({h["cusip"] for h in holdings} - old_cusips), summary


def harvest_soma(conn, holdings: list[dict] | None = None) -> dict:
    """Fetch the latest week + compute summary + store both tables.

    holdings can be pre-fetched and injected (used by main() so the response is
    fetched exactly once — also makes the path testable without network).
    """
    if holdings is None:
        holdings = soma.fetch_soma_holdings()
    n_new, summary = store_soma_week(conn, holdings)
    out = {
        "as_of": summary["as_of_date"],
        "n_holdings": len(holdings),
        "n_new": n_new,
        "total_par": summary["total_par"],
        "weekly_change": summary["weekly_change"],
        "rolling_off_7d": summary["rolling_off_7d"],
        "rolling_off_30d": summary["rolling_off_30d"],
        "rolling_off_90d": summary["rolling_off_90d"],
    }
    print(
        f"  SOMA {out['as_of']}: {out['n_holdings']} CUSIPs ({out['n_new']} new) — "
        f"total ${out['total_par'] / 1e9:,.1f}B · buy ${out['weekly_change'] / 1e9:+,.1f}B · "
        f"roll-off 7/30/90d ${out['rolling_off_7d'] / 1e9:,.1f}B/"
        f"${out['rolling_off_30d'] / 1e9:,.1f}B/${out['rolling_off_90d'] / 1e9:,.1f}B"
    )
    return out


def store_agency_week(conn, holdings: list[dict]) -> dict:
    """Compute the agency summary and write both v9 tables atomically (same
    REPLACE semantics as store_soma_week — a Fed restatement refreshes holdings
    and summary together). Returns the summary dict."""
    if not holdings:
        raise nyfed.NyFedError("store_agency_week: empty holdings")
    as_of = holdings[0]["as_of_date"]
    mixed = {h["as_of_date"] for h in holdings}
    if mixed != {as_of}:
        raise nyfed.NyFedError(f"store_agency_week: mixed as-of dates {sorted(mixed)}")
    summary = nyfed.compute_agency_summary(holdings)
    payload = [
        (
            h["as_of_date"],
            h["cusip"],
            h["asset_type"],
            h["security_description"],
            h["term"],
            h["issuer"],
            h["current_face_value"],
            h["change_week"],
        )
        for h in holdings
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM soma_agency_holdings WHERE as_of_date=?", (as_of,))
        conn.executemany(
            "INSERT INTO soma_agency_holdings"
            "(as_of_date,cusip,asset_type,security_description,term,issuer,"
            "current_face_value,change_week) VALUES (?,?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute(
            "INSERT OR REPLACE INTO soma_agency_summary"
            "(as_of_date,mbs,cmbs,agency_debts,total) VALUES (?,?,?,?,?)",
            (
                summary["as_of_date"],
                summary["mbs"],
                summary["cmbs"],
                summary["agency_debts"],
                summary["total"],
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary


def harvest_agency(conn, holdings: list[dict]) -> dict:
    """Store pre-fetched agency rows as one week (mbs + cmbs + agency_debts).
    holdings is injected by main() so the response is fetched exactly once —
    same pattern as harvest_soma."""
    summary = store_agency_week(conn, holdings)
    print(
        f"  SOMA agency {summary['as_of_date']}: {summary['n_cusips']} CUSIPs — "
        f"MBS ${summary['mbs'] / 1e9:,.1f}B · CMBS ${summary['cmbs'] / 1e9:,.2f}B · "
        f"agency debts ${summary['agency_debts'] / 1e9:,.2f}B"
    )
    return summary


def harvest_wam(conn, as_of: str) -> list[dict]:
    """Official per-type weighted-average maturity (5 requests, tiny)."""
    rows = nyfed.fetch_wam(as_of)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO soma_wam(as_of_date,wam_type,years) VALUES (?,?,?)",
            [(r["as_of_date"], r["wam_type"], r["years"]) for r in rows],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    by_type = {r["wam_type"]: r["years"] for r in rows}
    print(
        f"  SOMA WAM {as_of}: all {by_type.get('all')}y · bills {by_type.get('bills')}y · "
        f"notesbonds {by_type.get('notesbonds')}y · tips {by_type.get('tips')}y · "
        f"frn {by_type.get('frn')}y"
    )
    return rows


def backfill_soma(conn, weeks: int = DEFAULT_BACKFILL_WEEKS) -> tuple[int, int]:
    """Backfill N weeks of historical per-CUSIP data (weeks already stored are
    skipped). Returns (weeks_stored, weeks_failed)."""
    from curl_cffi import requests as creq

    dates = soma.fetch_asof_dates()  # newest first
    have = {r[0] for r in conn.execute("SELECT DISTINCT as_of_date FROM soma_holdings")}
    todo = [d for d in dates if d not in have][:weeks]
    print(f"  backfill: {len(todo)} weeks to fetch ({weeks} requested, {len(have)} already stored)")
    session = creq.Session(impersonate="chrome")  # shared session for all weeks
    n_fail = 0
    for i, d in enumerate(todo):
        try:
            holdings = soma.fetch_soma_holdings(d, session=session)
            n_new, summary = store_soma_week(conn, holdings)
            if (i + 1) % 10 == 0 or i == len(todo) - 1:
                print(
                    f"  {i + 1}/{len(todo)}: {d} — {len(holdings)} CUSIPs, "
                    f"total ${summary['total_par'] / 1e9:,.1f}B ({n_new} new)"
                )
        except Exception as ex:
            n_fail += 1
            print(f"  ✗ {d}: {str(ex)[:100]}")
        time.sleep(BACKFILL_PAUSE_S)
    session.close()
    print(f"  backfill done: {len(todo) - n_fail} weeks stored, {n_fail} failed")
    return len(todo) - n_fail, n_fail


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch soma")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", help="fetch latest week + store holdings + summary")
    p_backfill = sub.add_parser("backfill", help="fetch N historical weeks")
    p_backfill.add_argument("--weeks", type=int, default=DEFAULT_BACKFILL_WEEKS)
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)

    from .fetch_log import log_collection

    if a.cmd == "harvest":
        print("=== SOMA per-CUSIP weekly harvest ===")
        err = None
        err_extra = None  # agency/WAM leg — logged separately, still fails the job
        first_row: dict | None = None
        n_holdings = 0
        try:
            holdings = soma.fetch_soma_holdings()
            first_row = holdings[0]
            n_holdings = len(holdings)
            harvest_soma(conn, holdings)
            as_of = holdings[0]["as_of_date"]
            # agency + WAM use the SAME as-of date chain as the tsy harvest
            agency_first: dict | None = None
            n_agency = 0
            try:
                agency = []
                for asset_type in nyfed.AGENCY_ENUMS:
                    agency += nyfed.fetch_agency_holdings(asset_type, as_of)
                agency_first = agency[0]
                n_agency = len(agency)
                harvest_agency(conn, agency)
            except Exception as ex:
                err_extra = str(ex)[:140]
                print(f"  ✗ agency: {ex}")
            log_collection(conn, "soma", "NYFED:SOMA_AGENCY", agency_first, n_agency, err=err_extra)
            try:
                harvest_wam(conn, as_of)
                log_collection(conn, "soma", "NYFED:SOMA_WAM", None, 5)
            except Exception as ex:
                err_extra = err_extra or str(ex)[:140]
                print(f"  ✗ wam: {ex}")
                log_collection(conn, "soma", "NYFED:SOMA_WAM", None, 0, err=str(ex)[:140])
        except Exception as ex:
            err = str(ex)[:140]
            print(f"  ✗ {ex}")
            # the agency/WAM legs never ran (they chain on the tsy as-of) —
            # log them as skipped so the audit trail shows why, not silence
            log_collection(
                conn,
                "soma",
                "NYFED:SOMA_AGENCY",
                None,
                0,
                err=f"skipped: tsy harvest failed ({err})",
            )
            log_collection(
                conn, "soma", "NYFED:SOMA_WAM", None, 0, err=f"skipped: tsy harvest failed ({err})"
            )
        log_collection(conn, "soma", "NYFED:SOMA_HOLDINGS", first_row, n_holdings, err=err)
        conn.close()
        return 1 if (err or err_extra) else 0

    print(f"=== SOMA backfill ({a.weeks} weeks) ===")
    try:
        stored, failed = backfill_soma(conn, a.weeks)
    except Exception as ex:
        print(f"  ✗ {ex}")
        log_collection(conn, "soma", "NYFED:SOMA_BACKFILL", None, 0, err=str(ex)[:140])
        conn.close()
        return 1
    # rows = weeks actually STORED (not requested) — fetch_log is this
    # project's persistent memory; a requested-but-failed count would be fiction
    log_collection(
        conn,
        "soma",
        "NYFED:SOMA_BACKFILL",
        None,
        stored,
        err=None if failed == 0 else f"{failed} weeks failed",
    )
    conn.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
