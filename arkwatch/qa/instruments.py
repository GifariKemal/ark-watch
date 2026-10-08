"""instruments.py — backfill + daily sweep of instrument prices.

Primary per class: EODHD for spot metals & the crypto backtest; Yahoo for
futures/indexes/DXY/majors. Both write to instrument_prices (distinct sources
coexist; cross-validation of the two sources happens elsewhere).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from .. import db as _db
from ..config import _load_yaml, missing_env

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
RETRY_PAUSE_S = 5.0  # pause before the single retry pass of errored symbols
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
}


def instruments() -> list[dict]:
    return _load_yaml("instruments.yaml")["instruments"]


def insert_prices(conn, symbol: str, source: str, rows: list[dict]) -> int:
    """Upsert: latest NON-NULL value wins per column.

    2026-09-17 anomaly-audit P1: INSERT OR IGNORE permanently froze whatever
    landed first — (a) NULL-close partial rows (GC1/SI1/HG1/PL1 @2026-09-10)
    blocked their own final bars forever, (b) intraday Yahoo bars swept while
    a market was still trading (ES1 carried 8 permanently-wrong closes) could
    never be corrected by the final print. The upsert overwrites stored
    columns only where the incoming payload is non-NULL, so a later final bar
    heals a partial row while a partial payload never clobbers stored values.
    Fully-empty rows (all OHLCV NULL) are dropped at payload build.
    """
    payload = [
        (
            symbol,
            r["ts"],
            source,
            r.get("open"),
            r.get("high"),
            r.get("low"),
            r.get("close"),
            r.get("volume"),
        )
        for r in rows
        if any(r.get(k) is not None for k in ("open", "high", "low", "close", "volume"))
    ]
    if not payload:
        return 0
    # ROUND-10: tombstone filter — the persistent-wedge repair NULLs a
    # poison close, but the vendor keeps serving it and the COALESCE upsert
    # (first NON-NULL wins) refilled it every sweep: a treadmill of
    # detect→NULL→refill. Quarantined (symbol, ts, source) rows are skipped
    # at payload build so the poison never re-lands.
    try:
        tomb = {
            (r[0], r[1], r[2])
            for r in conn.execute("SELECT symbol, ts, source FROM price_quarantine").fetchall()
        }
    except Exception:
        tomb = set()  # pre-v17 DB (no table) — no quarantine yet
    payload = [p for p in payload if (p[0], p[1], p[2]) not in tomb]
    if not payload:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.executemany(
            "INSERT INTO instrument_prices(symbol,ts,source,open,high,low,close,volume,adjusted)"
            " VALUES (?,?,?,?,?,?,?,?,0)"
            " ON CONFLICT(symbol,ts,source) DO UPDATE SET"
            "  open=COALESCE(excluded.open, instrument_prices.open),"
            "  high=COALESCE(excluded.high, instrument_prices.high),"
            "  low=COALESCE(excluded.low, instrument_prices.low),"
            "  close=COALESCE(excluded.close, instrument_prices.close),"
            "  volume=COALESCE(excluded.volume, instrument_prices.volume)",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def fetch_eodhd_daily(token: str, ticker: str, *, days: int | None = None) -> list[dict]:
    p = {"api_token": token, "fmt": "json"}
    if days:
        p["from"] = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
    r = requests.get(f"https://eodhd.com/api/eod/{ticker}", params=p, headers=UA, timeout=(10, 60))
    if r.status_code != 200:
        raise RuntimeError(f"EODHD {ticker}: HTTP {r.status_code}")
    return [
        {
            "ts": x["date"][:10],
            "open": x.get("open"),
            "high": x.get("high"),
            "low": x.get("low"),
            "close": x.get("close"),
            "volume": x.get("volume"),
        }
        for x in r.json()
    ]


def backfill(
    db_path: str = str(DEFAULT_DB),
    *,
    only: str | None = None,
    yahoo: bool = True,
    eodhd: bool = True,
) -> dict[str, int]:
    import os

    from ..fetchers import yahoo as yh

    conn = _db.get_conn(db_path, allow_init=True)
    out: dict[str, int] = {}
    tok = os.environ.get("EODHD_API_TOKEN", "")
    if skip := missing_env("EODHD_API_TOKEN"):
        eodhd = False
        print(f"  · EODHD skipped ({skip})")
    for ins in instruments():
        sym = ins["symbol"]
        if only and sym != only:
            continue
        if eodhd and ins.get("eodhd"):
            try:
                out[f"{sym}|EODHD"] = insert_prices(
                    conn, sym, "EODHD", fetch_eodhd_daily(tok, ins["eodhd"])
                )
            except Exception as ex:
                out[f"{sym}|EODHD"] = -1
                print(f"  ✗ {sym} EODHD: {str(ex)[:90]}")
        if yahoo and ins.get("yahoo"):
            try:
                out[f"{sym}|YAHOO"] = insert_prices(
                    conn, sym, "YAHOO", yh.fetch_daily(ins["yahoo"])
                )
            except Exception as ex:
                out[f"{sym}|YAHOO"] = -1
                print(f"  ✗ {sym} YAHOO: {str(ex)[:90]}")
    conn.close()
    return out


def _cross_validate(conn, db_path: str) -> int:
    """Post-write wedge check (ROUND-2: XPTUSD stored a +5.45% wrong-cut
    close vs its futures leg with no detector). A wedge >2% spot-vs-futures
    (normal carry <1%) or >0.75% between dual-source twins (different EOD
    cutoffs) is almost certainly a vendor wrong-cut — name it in fetch_log
    so the morning health check surfaces it; the healing upsert replaces
    the bad row once the vendor serves the final."""
    wedges: list[str] = []
    for spot, fut in (("XAUUSD", "GC1"), ("XAGUSD", "SI1"), ("XPTUSD", "PL1")):
        # ROUND-3: upper bound < today — a still-FORMING intraday bar diverges
        # from its futures leg by construction (false positive on first run)
        # ROUND-9: threshold 1.5% (was 2%) — live, XAG/XPT wedged at
        # 1.62%/1.57% while the futures legs matched CME settles EXACTLY,
        # i.e. a genuine vendor wrong-cut passed SILENTLY under the 2% gate.
        rows = conn.execute(
            "SELECT a.ts, a.close, b.close FROM instrument_prices a"
            " JOIN instrument_prices b ON b.symbol=? AND b.source='YAHOO' AND b.ts=a.ts"
            " WHERE a.symbol=? AND a.source='EODHD' AND a.close IS NOT NULL"
            " AND b.close IS NOT NULL AND a.ts >= date('now','-4 day')"
            " AND a.ts < date('now')"
            " ORDER BY a.ts DESC LIMIT 4",
            (fut, spot),
        ).fetchall()
        for ts, s_close, f_close in rows:
            if f_close:
                w = abs(s_close / f_close - 1)
                if w > 0.015:
                    wedges.append(f"{spot}/{fut} {ts} {w:.2%}")
                    # ROUND-9 persistent-wedge REPAIR: the healing upsert
                    # only works when the vendor eventually serves the
                    # final — live, EODHD kept serving the same wrong close
                    # forever (09-16 metals ~1-2.6% off, futures legs
                    # CME-exact). A wedge this size on a CLOSED bar has no
                    # innocent explanation (carry <1%): NULL the spot close
                    # AND tombstone it. ROUND-10 correction of the ROUND-9
                    # claim: the COALESCE upsert is first-NON-NULL-wins — an
                    # incoming vendor value ALWAYS refills a stored NULL, so
                    # NULL alone was a refill treadmill (observed live:
                    # detect→NULL→refill every sweep). The quarantine row
                    # makes insert_prices skip the poison (symbol, ts,
                    # source) forever, independent of any lookback window.
                    conn.execute(
                        "UPDATE instrument_prices SET close=NULL"
                        " WHERE symbol=? AND source='EODHD' AND ts=?",
                        (spot, ts),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO price_quarantine(symbol, ts, source, reason)"
                        " VALUES (?,?,?,?)",
                        (spot, ts, "EODHD", f"xval wedge {w:.2%} vs {fut}"),
                    )
                    conn.commit()
                    print(f"  ⚠ xval REPAIR+QUARANTINE: {spot} {ts} (wedge {w:.2%})")
    for sym in ("BTCUSD", "ETHUSD", "VIX", "US500", "US30", "US100", "DXY"):
        rows = conn.execute(
            "SELECT a.ts, a.close, b.close FROM instrument_prices a"
            " JOIN instrument_prices b ON b.symbol=a.symbol AND b.source='YAHOO' AND b.ts=a.ts"
            " WHERE a.symbol=? AND a.source='EODHD' AND a.close IS NOT NULL"
            " AND b.close IS NOT NULL AND a.ts >= date('now','-4 day')"
            " AND a.ts < date('now')"
            " ORDER BY a.ts DESC LIMIT 4",
            (sym,),
        ).fetchall()
        for ts, e_close, y_close in rows:
            if y_close:
                w = abs(e_close / y_close - 1)
                if w > 0.0075:
                    wedges.append(f"{sym} EODHD/YAHOO {ts} {w:.2%}")
    if wedges:
        from .fetch_log import log_collection

        log_collection(
            conn,
            "instruments",
            "INSTRUMENTS:XVAL",
            None,
            0,
            err=f"{len(wedges)} wedge(s): " + "; ".join(wedges[:4])[:160],
        )
        print(f"  ⚠ xval: {len(wedges)} wedge(s): {'; '.join(wedges[:4])}")
    return len(wedges)


def sweep(db_path: str = str(DEFAULT_DB)) -> dict[str, int]:
    """Daily sweep: only the last 7 days per symbol (lightweight)."""
    import os

    from ..fetchers import yahoo as yh
    from .fetch_log import log_collection

    conn = _db.get_conn(db_path, allow_init=True)
    out: dict[str, int] = {}
    tok = os.environ.get("EODHD_API_TOKEN", "")
    if skip := missing_env("EODHD_API_TOKEN"):  # once per run, not an ERROR per symbol
        log_collection(conn, "instruments", "EODHD", None, 0, err=skip, status="SKIPPED")
    start_ts = int(time.time()) - 7 * 86400

    def fetch(src: str, ins: dict, final: bool) -> None:
        sym = ins["symbol"]
        try:
            if src == "EODHD":
                rows = fetch_eodhd_daily(tok, ins["eodhd"], days=7)
            else:
                rows = yh.fetch_daily(ins["yahoo"], start_ts=start_ts)
            out[f"{sym}|{src}"] = insert_prices(conn, sym, src, rows)
            log_collection(
                conn, "instruments", f"{sym}:{src}", rows[0] if rows else None, len(rows)
            )
        except Exception as ex:
            out[f"{sym}|{src}"] = -1
            if final:  # a first-pass error is only logged if the retry fails too
                log_collection(conn, "instruments", f"{sym}:{src}", None, 0, err=str(ex))

    work = [
        (src, ins)
        for ins in instruments()
        for src in ("EODHD", "YAHOO")
        if ins.get(src.lower()) and not (src == "EODHD" and skip)
    ]
    for src, ins in work:
        fetch(src, ins, final=False)
    # ONE retry pass: a transient SOCKS/proxy error (VPS 2026-10-09:
    # BTCUSD:YAHOO SOCKSHTTPSConnectionPool Max retries) must not fail the sweep
    if retry := [(s, i) for s, i in work if out[f"{i['symbol']}|{s}"] < 0]:
        time.sleep(RETRY_PAUSE_S)
        for src, ins in retry:
            fetch(src, ins, final=True)
    _cross_validate(conn, db_path)
    conn.close()
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch instruments")
    p.add_argument("mode", choices=["backfill", "sweep"])
    p.add_argument("--only", help="a single symbol only (e.g. XAUUSD)")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    result = backfill(a.db, only=a.only) if a.mode == "backfill" else sweep(a.db)
    ok = sum(1 for v in result.values() if v > 0)
    fail = [k for k, v in result.items() if v < 0]
    print(f"=== instruments {a.mode}: {ok} symbol-sources populated · {len(fail)} failed ===")
    for k, v in sorted(result.items()):
        if v >= 0:
            print(f"  {k:<22} {v:>7,}")
    for k in fail:
        print(f"  ✗ {k}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
