"""cme_harvest.py — daily gray-zone job (08:15 WIB): settlements + options + CVOL + VOI → DB.

All snapshots are append-only; retention is ~5 trading days, so the job must
not go more than 3 trading days without running. Options run AFTER the futures
settlements so the same-day underlying anchor is already in cme_settlements.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db
from ..fetchers import cme

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

OPTIONS_N_EXPIRATIONS = 6  # [KEPUTUSAN: expiry bulanan terdekat per produk/hari — est. 10-13k baris/hari utk 7 produk (PLAN-CME-OPTIONS §2.3); 2026-09-03]
OPTIONS_PAUSE_S = 0.3  # [KEPUTUSAN: pacing antar kontrak — sama dgn backfill SOMA; 2026-09-03]


def harvest_settlements(conn, products: list[str] | None = None) -> dict[str, int]:
    codes = products or list(cme.PRODUCTS.keys())
    out: dict[str, int] = {}
    for code in codes:
        time.sleep(0.3)  # ROUND-7: pacing — bursts of fresh TLS hands beg blocking
        try:
            rows = cme.fetch_settlements(code)
            # ROUND-3: snapshot the frontier BEFORE the save — reading it
            # after meant db_max >= fetched_max always and the gap walk was
            # unreachable dead code
            pid = cme.PRODUCTS[code]
            db_max_pre = conn.execute(
                "SELECT MAX(trade_date) FROM cme_settlements WHERE product_id=?",
                (pid,),
            ).fetchone()[0]
            n = _save_settlements(conn, rows)
            n += _reconcile_gaps(conn, code, rows, db_max_pre)
            # ROUND-10 queue: settlement walkback. ROUND-11: the trigger is
            # "the frontier did not advance", not "the fetch returned
            # nothing" — fetch_settlements never returns [] (it walks trade
            # dates and RAISES when all are empty), so the shipped
            # `if not rows` guard was unreachable dead code while the real
            # failure mode went unhandled: a healthy fetch of STALE-dated
            # rows (live 2026-09-19 01:16-04:40 UTC: three Saturday runs
            # returned the 09-17 strip for 0 new rows while Friday 09-18
            # was not yet published; the hole healed at 05:15 only via the
            # deploy-replay's ordinary fetch). One explicit retry for the
            # last completed trading day closes the window the scheduled
            # runs miss.
            fetched_max = max((r["trade_date"] for r in rows), default=None)
            if db_max_pre and (fetched_max is None or fetched_max <= db_max_pre):
                probe = datetime.now(UTC).date() - timedelta(days=1)
                while probe.weekday() >= 5:
                    probe -= timedelta(days=1)
                if probe.isoformat() > db_max_pre:
                    try:
                        retry = cme.fetch_settlements(
                            code,
                            trade_date=datetime(probe.year, probe.month, probe.day, tzinfo=UTC),
                        )
                        landed = [r for r in retry if r["trade_date"] == probe.isoformat()]
                        if landed:
                            n += _save_settlements(conn, landed)
                            print(f"  ↻ {code}: walkback recovered {len(landed)} rows @ {probe}")
                    except Exception as ex2:
                        print(f"  ⚠ {code} walkback retry: {str(ex2)[:70]}")
            out[code] = n
        except Exception as ex:
            out[code] = -1
            print(f"  ✗ settlements {code}: {str(ex)[:100]}")
    return out


def _reconcile_gaps(conn, code: str, rows: list[dict], db_max_pre: str | None = None) -> int:
    """ROUND-2 fix: permanent trade-date holes. CME retention is ~5 trading
    days — a missed harvest day is gone forever unless this pass explicitly
    re-fetches it (mechanism proven live by the 09-14 walkback recovery).
    Only dates that ACTUALLY land the requested trade_date are saved (the
    fetcher walks back on 404/holidays — a filtered landed==iso check keeps
    the prior trading day from double-writing)."""
    from datetime import date as _date
    from datetime import timedelta as _td

    if not rows:
        return 0
    pid = cme.PRODUCTS[code]
    fetched_max = max(r["trade_date"] for r in rows)
    # ROUND-3: callers pass the PRE-save frontier; only read the DB when the
    # caller did not (backward-compatible for direct calls)
    db_max = db_max_pre
    if db_max is None:
        db_max = conn.execute(
            "SELECT MAX(trade_date) FROM cme_settlements WHERE product_id=?", (pid,)
        ).fetchone()[0]
    if not db_max or db_max >= fetched_max:
        return 0
    d0, d1 = _date.fromisoformat(db_max), _date.fromisoformat(fetched_max)
    n = 0
    cur = d0
    while cur < d1:
        cur += _td(days=1)
        if cur.weekday() >= 5:
            continue
        iso = cur.isoformat()
        if conn.execute(
            "SELECT 1 FROM cme_settlements WHERE product_id=? AND trade_date=? LIMIT 1",
            (pid, iso),
        ).fetchone():
            continue
        try:
            gap_rows = cme.fetch_settlements(
                code, trade_date=datetime(cur.year, cur.month, cur.day, tzinfo=UTC)
            )
            landed = [r for r in gap_rows if r["trade_date"] == iso]
            if landed:
                n += _save_settlements(conn, landed)
        except Exception as ex:
            print(f"  ⚠ gap-fill {code} {iso}: {str(ex)[:70]}")
    if n:
        print(f"  ↻ {code}: gap-filled {n} rows {db_max}→{fetched_max}")
    return n


def _save_settlements(conn, rows: list[dict]) -> int:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    payload = [
        (
            r["trade_date"],
            r["product_id"],
            r["month"],
            r["settle"],
            r["volume"],
            r["open_interest"],
            now,
        )
        for r in rows
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO cme_settlements(trade_date,product_id,month,settle,volume,open_interest,fetched_at)"
            " VALUES (?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def harvest_cvol(conn) -> int:
    return harvest_cvol_from(conn, cme.fetch_cvol())


def harvest_cvol_from(conn, rows: list[dict]) -> int:
    payload = [
        (
            r["trade_date"],
            r["symbol"],
            r["cvol"],
            r["atm"],
            r["skew"],
            r["upvar"],
            r["dnvar"],
            r["convexity"],
        )
        for r in rows
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO cvol_snapshots(trade_date,symbol,cvol,atm,skew,upvar,dnvar,convexity)"
            " VALUES (?,?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def harvest_voi(conn, asset_classes: list[int] | None = None) -> tuple[int, str | None]:
    """ROUND-2 fix: iterate EVERY TradeDates entry (Preliminary + Final
    restatements — the old harvest read only entry [0], so Final restated
    OI never landed); per (trade_date, report_type) not yet stored."""
    ids = asset_classes or list(cme.VOI_ASSET_CLASSES.keys())
    total = 0
    voi_err: str | None = None
    try:
        entries = cme.fetch_voi_dates()
    except Exception as ex:
        return 0, str(ex)[:140]
    for ac_id in ids:
        for ent in entries:
            try:
                # ROUND-3 regression fix: NO global (trade_date, report_type)
                # pre-check — it keyed on date+type only, so after the FIRST
                # asset class stored a date, every other class (FX/Equity/
                # IR/Energy/Metals) skipped forever. The PK's INSERT OR
                # IGNORE is the dedup; entries are bounded (~10) by CME
                # retention, so re-fetching a stored entry is one cheap POST.
                rows = cme.fetch_voi(ac_id, td_raw=ent["td_raw"], report_type=ent["report_type"])
                payload = [
                    (
                        r["trade_date"],
                        r["product_id"],
                        r.get("report_type", "Preliminary"),
                        r["volume"],
                        r["oi"],
                        r["oi_diff"],
                    )
                    for r in rows
                ]
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.executemany(
                        "INSERT OR IGNORE INTO voi_daily"
                        "(trade_date,product_id,report_type,volume,oi,oi_diff)"
                        " VALUES (?,?,?,?,?,?)",
                        payload,
                    )
                    conn.execute("COMMIT")
                    total += len(payload)
                except Exception:
                    conn.execute("ROLLBACK")
                    continue
            except Exception as ex:
                voi_err = str(ex)[:140]
                print(f"  ✗ VOI class {ac_id}: {str(ex)[:90]}")
    return total, voi_err


def harvest_options(conn, products: list[str] | None = None) -> dict[str, int]:
    """Per-strike options settlements for the N nearest monthly expirations.

    Options append-only INSERT OR IGNORE per (trade_date, contract): unlike
    SOMA, CME does not restate a Final settlement, so a re-run is a no-op.
    The underlying anchor (cme_option_underlyings) is NOT in the options
    response — the single type='' row is the contract TOTAL (settle '-') on
    every product (live-verified 2026-09-03) — so it is joined from the
    futures strip the same job already harvested into cme_settlements.
    """
    from curl_cffi import requests as creq

    codes = products or list(cme.OPTIONS_PRODUCTS.keys())
    s = None  # one session shared across products/contracts (created lazily, closed at the end)
    out: dict[str, int] = {}
    for code in codes:
        try:
            if s is None:
                s = creq.Session(impersonate="chrome")
            pid = cme.OPTIONS_PRODUCTS[code]
            picked = cme.pick_expirations(
                cme.fetch_option_expirations(code, session=s), OPTIONS_N_EXPIRATIONS
            )
            n_rows = 0
            n_und = 0
            for contract_id, td_mmdd in picked:
                rows, td_iso = cme.fetch_option_settlements(code, contract_id, td_mmdd, session=s)
                n_rows += len(rows)
                if td_iso:
                    n_und += _save_underlying(conn, pid, code, contract_id, td_iso)
                _save_options(conn, rows)
                time.sleep(OPTIONS_PAUSE_S)
            out[code] = n_rows
            print(
                f"  ✓ options {code}: {len(picked)} expirations · {n_rows} rows · {n_und} underlyings"
            )
        except Exception as ex:
            out[code] = -1
            print(f"  ✗ options {code}: {str(ex)[:100]}")
    if s is not None:
        s.close()
    return out


def _save_options(conn, rows: list[dict]) -> int:
    payload = [
        (
            r["trade_date"],
            r["product_id"],
            r["product_code"],
            r["contract_id"],
            r["option_type"],
            r["strike"],
            r["settle"],
            r["volume"],
            r["open_interest"],
        )
        for r in rows
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO cme_options_settlements"
            "(trade_date,product_id,product_code,contract_id,option_type,strike,settle,volume,open_interest)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def _save_underlying(
    conn, option_pid: int, product_code: str, contract_id: str, trade_date: str
) -> int:
    """Anchor one options contract to its underlying FUTURE settle.

    Joined from cme_settlements (futures strip) by month label — serial option
    months without a listed future, or a missing futures day, are silently
    skipped: underlyings feed IV math only, positioning signals never read them.
    """
    fut_code = cme.OPTIONS_UNDERLYING.get(product_code)
    month = cme.futures_month_from_contract(contract_id)
    if not fut_code or not month or fut_code not in cme.PRODUCTS:
        return 0
    row = conn.execute(
        "SELECT settle FROM cme_settlements WHERE product_id=? AND month=? AND trade_date IN (?,?)",
        (cme.PRODUCTS[fut_code], month, trade_date, cme._mmddyyyy_from_iso(trade_date) or ""),
    ).fetchone()
    if not row or row[0] is None:
        return 0
    conn.execute(
        "INSERT OR REPLACE INTO cme_option_underlyings(trade_date,product_id,contract_id,settle)"
        " VALUES (?,?,?,?)",
        (trade_date, option_pid, contract_id, row[0]),
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch cme")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--skip-voi", action="store_true")
    p.add_argument("--skip-options", action="store_true")
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)

    print("=== CME settlements (14 products) ===")
    s = harvest_settlements(conn)
    ok_s = sum(1 for v in s.values() if v > 0)
    print(f"  {ok_s}/{len(s)} products populated")

    o: dict[str, int] = {}
    opts_err: str | None = None
    if not a.skip_options:
        print("=== CME options (7 products x 6 nearest expirations) ===")
        try:
            o = harvest_options(conn)
        except Exception as ex:
            opts_err = str(ex)[:140]
            print(f"  ✗ {ex}")

    print("=== CVOL snapshot ===")
    cvol_rows: list[dict] = []
    cvol_err: str | None = None
    try:
        cvol_rows = cme.fetch_cvol()
        n = harvest_cvol_from(conn, cvol_rows)
        print(f"  {n} symbols saved")
    except Exception as ex:
        cvol_err = str(ex)[:140]
        print(f"  ✗ {ex}")
    # fetch_log for the gray harvesters. Status derives only from
    # exceptions; partial failures are recorded in the error text; rows counts
    # products fetched (not new rows — an idempotent re-run reports 0 new).
    from .fetch_log import log_collection

    n_prod_fail = sum(1 for v in s.values() if v < 0)
    s_err = None
    if n_prod_fail:
        failed = [k for k, v in s.items() if v < 0]
        s_err = f"{n_prod_fail}/{len(s)} failed: {','.join(failed[:6])}"
    # ROUND-7 kill-switch: a fully-dead CME vendor (6 signals at once:
    # FedWatch/ECBWatch/XCCY/CVOL/VOI/options-PCR) must FAIL the job so the
    # daemon's job_failed alert fires — exit-0-with-empty was silent death
    all_dead = s and n_prod_fail == len(s)
    # ROUND-7 stale gate: the strip itself can lag; name it in fetch_log
    strip_td = conn.execute(
        "SELECT MAX(trade_date) FROM cme_settlements WHERE product_id=305"
    ).fetchone()[0]
    if strip_td:
        strip_age = (datetime.now(UTC).date() - datetime.fromisoformat(strip_td).date()).days
        if strip_age > 7:
            s_err = (s_err or "") + f" |ZQ strip stale {strip_age}d ({strip_td})"
    # ROUND-2: rows = ACTUAL inserted/gap-filled rows (the old len(s) logged
    # '14' forever — a dead product was indistinguishable from a healthy one)
    log_collection(
        conn,
        "cme",
        "CME:settlements",
        None,
        sum(v for v in s.values() if v > 0),
        err=s_err or None,
    )
    if not a.skip_options:
        n_opt_fail = sum(1 for v in o.values() if v < 0)
        if n_opt_fail:
            opts_err = opts_err or f"{n_opt_fail}/{len(o)} products failed"
        log_collection(
            conn, "cme", "CME:options", None, sum(v for v in o.values() if v > 0), err=opts_err
        )
    log_collection(
        conn, "cme", "CME:cvol", cvol_rows[0] if cvol_rows else None, len(cvol_rows), err=cvol_err
    )

    if not a.skip_voi:
        print("=== VOI (6 asset-class) ===")
        n, voi_err = harvest_voi(conn)
        print(f"  {n} rows saved")
        log_collection(conn, "cme", "CME:voi", None, n, err=voi_err)

    conn.close()
    # ROUND-7: propagate total vendor death to the daemon (job_failed alert)
    if all_dead:
        print("✗ CME: ALL settlement products failed — vendor kill-switch")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
