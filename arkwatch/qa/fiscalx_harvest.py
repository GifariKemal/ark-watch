"""fiscalx_harvest.py — fiscaldata expansion daily job (daemon `fiscalx`, 06:10 WIB):

auctions    full history once (auto when fd_auctions is empty, or --auctions-full)
            + 120-day daily window; INSERT OR REPLACE — announcement rows share
            (auction_date, cusip) with the results that land after the auction
            (an OR IGNORE would freeze the announcement nulls forever)
debt_tx     newest 14 record_dates (DTS rolling window, nyfed-ops convention)
interest/   newest 6 record_dates (monthly endpoints)
avg_rates
debt_to_penny is NOT here — FISCAL:DEBT_* are registry series owned by the
06:00 `harvest` job (qa/harvest.py routes them via fiscal.fetch_latest).
Per-dataset try/except + fetch_log: one failing dataset fails visibly without
killing the rest; exit 1 on ANY error so the daemon retries (all five legs
are cheap and idempotent).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .. import db
from ..fetchers import fiscal

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
DATASET_PAUSE_S = 0.3  # [KEPUTUSAN: pacing antar dataset — konvensi soma/nyfed]
AUCTIONS_WINDOW_DAYS = 120  # [KEPUTUSAN: jendela increment — menangkap announcement
# (record_date = issue date bisa MASA DEPAN) + hasil lelang terbaru; koreksi lama
# di luar jendela via --auctions-full (re-pull penuh, idempotent)]

AUCTION_COLS = (
    "auction_date, cusip, security_type, security_term, issue_date, maturity_date,"
    " price_per100, avg_med_yield, bid_to_cover, allocation_pct, auction_format"
)
TX_COLS = "record_date, transaction_type, security_market, security_type, amount_today"
EXP_COLS = (
    "record_date, expense_catg_desc, expense_group_desc, expense_type_desc, month_amt, fytd_amt"
)
RATE_COLS = "record_date, security_desc, security_type_desc, avg_interest_rate"

PUBLIC_ISSUES_CATG = "INTEREST EXPENSE ON PUBLIC ISSUES"  # headline FYTD filter —
# the GAS category is intragovernmental and must not enter the debt-service total


def _upsert(conn, table: str, cols: str, payload: list[tuple]) -> int:
    n = len(cols.split(","))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO {table}({cols}) VALUES ({','.join('?' * n)})",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(payload)


def store_auctions(conn, rows: list[dict]) -> int:
    parsed = [p for p in (fiscal.parse_auction_row(r) for r in rows) if p]
    # announcement rows (all-null numerics) sort FIRST, the fullest results row
    # LAST → on a PK collision the results overwrite the announcement nulls
    # (same lifecycle as fed_operations Announced→Results)
    parsed.sort(
        key=lambda p: sum(
            v is not None
            for v in (p["price_per100"], p["avg_med_yield"], p["bid_to_cover"], p["allocation_pct"])
        )
    )
    cols = [c.strip() for c in AUCTION_COLS.split(",")]
    return _upsert(conn, "fd_auctions", AUCTION_COLS, [tuple(p[c] for c in cols) for p in parsed])


def store_debt_transactions(conn, rows: list[dict]) -> int:
    parsed = [p for p in (fiscal.parse_debt_tx_row(r) for r in rows) if p]
    cols = [c.strip() for c in TX_COLS.split(",")]
    return _upsert(
        conn, "fd_debt_transactions", TX_COLS, [tuple(p[c] for c in cols) for p in parsed]
    )


def store_interest_expense(conn, rows: list[dict]) -> int:
    parsed = [p for p in (fiscal.parse_interest_expense_row(r) for r in rows) if p]
    cols = [c.strip() for c in EXP_COLS.split(",")]
    return _upsert(
        conn, "fd_interest_expense", EXP_COLS, [tuple(p[c] for c in cols) for p in parsed]
    )


def store_avg_rates(conn, rows: list[dict]) -> int:
    parsed = [p for p in (fiscal.parse_avg_rate_row(r) for r in rows) if p]
    cols = [c.strip() for c in RATE_COLS.split(",")]
    return _upsert(conn, "fd_avg_rates", RATE_COLS, [tuple(p[c] for c in cols) for p in parsed])


def net_marketable_today(rows: list[dict]) -> tuple[str, float] | None:
    """(latest record_date, Σ issues − Σ redemptions, Marketable) in $M — the
    day's marketable supply pulse. Input = parsed debt-tx rows."""
    if not rows:
        return None
    latest = max(r["record_date"] for r in rows)
    net = 0.0
    for r in rows:
        if r["record_date"] != latest or r["security_market"] != "Marketable":
            continue
        sign = 1.0 if r["transaction_type"] == "Issues" else -1.0
        net += sign * (r["amount_today"] or 0.0)
    return latest, net


def fytd_public_issues(rows: list[dict]) -> tuple[str, float]:
    """(latest record_date, Σ fytd_amt over the public-issues category) in raw
    USD — the debt-service headline. Input = parsed interest-expense rows."""
    latest = max(r["record_date"] for r in rows)
    total = sum(
        r["fytd_amt"] or 0.0
        for r in rows
        if r["record_date"] == latest and r["expense_catg_desc"] == PUBLIC_ISSUES_CATG
    )
    return latest, total


def _latest_10y(conn) -> tuple[str, float] | None:
    row = conn.execute(
        "SELECT auction_date, bid_to_cover FROM fd_auctions"
        " WHERE security_term='10-Year' AND bid_to_cover IS NOT NULL"
        " ORDER BY auction_date DESC LIMIT 1"
    ).fetchone()
    return (row[0], row[1]) if row else None


def harvest_auctions(conn, full: bool = False) -> int:
    from .fetch_log import log_collection

    err = None
    n = 0
    first_raw: dict | None = None
    try:
        empty = conn.execute("SELECT 1 FROM fd_auctions LIMIT 1").fetchone() is None
        if full or empty:
            rows = fiscal.fetch_auctions()
            mode = "full history"
        else:
            rows = fiscal.fetch_auctions_window(AUCTIONS_WINDOW_DAYS)
            mode = f"window {AUCTIONS_WINDOW_DAYS}d"
        first_raw = rows[0] if rows else None  # fields-restricted row — key set
        # is constant across full/window pulls, so the schema_fp cannot flip
        n = store_auctions(conn, rows)
        span = conn.execute(
            "SELECT MIN(auction_date), MAX(auction_date), COUNT(*) FROM fd_auctions"
        ).fetchone()
        ten = _latest_10y(conn)
        ten_s = f" · latest 10Y {ten[0]} btc {ten[1]:.2f}" if ten else ""
        print(
            f"  auctions ({mode}): {n} rows upserted — table {span[2]:,} rows"
            f" {span[0]} → {span[1]}{ten_s}"
        )
    except Exception as ex:
        err = str(ex)[:140]
        print(f"  ✗ auctions: {ex}")
    log_collection(conn, "fiscalx", "FISCAL:AUCTIONS", first_raw, n, err=err)
    return 1 if err else 0


def harvest_debt_transactions(conn) -> int:
    from .fetch_log import log_collection

    err = None
    n = 0
    first_raw: dict | None = None
    try:
        rows = fiscal.fetch_debt_transactions()
        first_raw = rows[0] if rows else None
        n = store_debt_transactions(conn, rows)
        net = net_marketable_today([p for p in (fiscal.parse_debt_tx_row(r) for r in rows) if p])
        if net:
            print(
                f"  debt transactions (14d): {n} rows upserted @ {net[0]}"
                f" · net marketable {net[1]:+,.0f} $M"
            )
        else:
            print(f"  debt transactions (14d): {n} rows upserted")
    except Exception as ex:
        err = str(ex)[:140]
        print(f"  ✗ debt transactions: {ex}")
    log_collection(conn, "fiscalx", "FISCAL:DEBT_TX", first_raw, n, err=err)
    return 1 if err else 0


def harvest_interest_expense(conn) -> int:
    from .fetch_log import log_collection

    err = None
    n = 0
    first_raw: dict | None = None
    try:
        rows = fiscal.fetch_interest_expense()
        first_raw = rows[0] if rows else None
        n = store_interest_expense(conn, rows)
        parsed = [p for p in (fiscal.parse_interest_expense_row(r) for r in rows) if p]
        if parsed:
            latest, total = fytd_public_issues(parsed)
            print(
                f"  interest expense (6m): {n} rows upserted — FYTD public issues"
                f" ${total / 1e12:.3f}T @ {latest}"
            )
        else:
            print(f"  interest expense (6m): {n} rows upserted")
    except Exception as ex:
        err = str(ex)[:140]
        print(f"  ✗ interest expense: {ex}")
    log_collection(conn, "fiscalx", "FISCAL:INTEREST_EXPENSE", first_raw, n, err=err)
    return 1 if err else 0


def harvest_avg_rates(conn) -> int:
    from .fetch_log import log_collection

    err = None
    n = 0
    first_raw: dict | None = None
    try:
        rows = fiscal.fetch_avg_rates()
        first_raw = rows[0] if rows else None
        n = store_avg_rates(conn, rows)
        tm = conn.execute(
            "SELECT record_date, avg_interest_rate FROM fd_avg_rates"
            " WHERE security_desc='Total Marketable' ORDER BY record_date DESC LIMIT 1"
        ).fetchone()
        tm_s = f" · Total Marketable {tm[1]:.3f}% @ {tm[0]}" if tm else ""
        print(f"  avg rates (6m): {n} rows upserted{tm_s}")
    except Exception as ex:
        err = str(ex)[:140]
        print(f"  ✗ avg rates: {ex}")
    log_collection(conn, "fiscalx", "FISCAL:AVG_RATES", first_raw, n, err=err)
    return 1 if err else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch fiscalx")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--auctions-full", action="store_true", help="force the full auctions backfill")
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)
    print("=== fiscaldata expansion harvest ===")
    rc = harvest_auctions(conn, full=a.auctions_full)
    time.sleep(DATASET_PAUSE_S)
    rc |= harvest_debt_transactions(conn)
    time.sleep(DATASET_PAUSE_S)
    rc |= harvest_interest_expense(conn)
    time.sleep(DATASET_PAUSE_S)
    rc |= harvest_avg_rates(conn)
    conn.close()
    n_err = bin(rc).count("1")
    print(f"=== fiscalx done: {4 - n_err}/4 datasets OK (exit {rc}) ===")
    return rc


if __name__ == "__main__":
    sys.exit(main())
