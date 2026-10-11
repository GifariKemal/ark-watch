"""kalshi.py - Kalshi (CFTC-regulated) market probabilities beside Polymarket's, same topics.

Source: Kalshi trade-api v2, public market data (no key). For each curated macro series the
nearest open event is read and its most-traded markets land in computed_signals as
`kalshi:<ticker>` (value = Yes probability 0..1, state = topic), the shape /v1/odds reads.
Volume is in contracts (each pays $1), not USD.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime

import requests

from ..qa import fetch_log

URL = "https://api.elections.kalshi.com/trade-api/v2/events"
TIMEOUT = (5, 20)
# series -> (topic shared with polymarket_queries.yaml, markets kept from the nearest event)
SERIES = {
    "KXFEDDECISION": ("fed", 5),
    "KXRECSSNBER": ("recession", 1),
    "KXCPIYOY": ("inflation", 4),
}
MAX_SPREAD = 0.05  # a quote wider than this is not a price; fall back to the last trade


class KalshiError(RuntimeError):
    pass


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def probability(m: dict) -> float | None:
    """Mid of a tight Yes quote, else the last trade; None when neither is a usable price."""
    bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
    if bid is not None and ask is not None and ask > 0 and 0 <= ask - bid <= MAX_SPREAD:
        p = round((bid + ask) / 2, 4)
    else:
        p = _num(m.get("last_price_dollars"))
    return p if p is not None and 0 <= p <= 1 else None


def nearest_markets(events: list[dict], keep: int) -> list[dict]:
    """The open event closing first, its `keep` most-traded priced markets."""
    live = [e for e in events if any(m.get("status") == "active" for m in e.get("markets") or [])]
    if not live:
        return []
    ev = min(live, key=lambda e: min(m.get("close_time") or "9" for m in e["markets"]))
    rows = []
    for m in ev["markets"]:
        p = probability(m)
        if m.get("status") != "active" or p is None or not m.get("ticker"):
            continue
        rows.append(
            {
                "ticker": str(m["ticker"]),
                "series": str(ev.get("series_ticker") or m["ticker"].split("-")[0]),
                "question": f"{ev.get('title') or m.get('title')}: {m.get('yes_sub_title') or m.get('title')}",
                "p": p,
                "volume": _num(m.get("volume_fp")) or 0.0,
                "end_date": m.get("close_time"),
            }
        )
    return sorted(rows, key=lambda r: -r["volume"])[:keep]


def fetch_series(series: str) -> list[dict]:
    try:
        r = requests.get(
            URL,
            params={
                "series_ticker": series,
                "status": "open",
                "with_nested_markets": "true",
                "limit": 10,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as ex:
        raise KalshiError(f"kalshi {series}: {type(ex).__name__}") from None
    if r.status_code != 200:
        raise KalshiError(f"kalshi {series}: HTTP {r.status_code}")
    try:
        events = r.json()["events"]
    except (ValueError, KeyError, TypeError):
        raise KalshiError(f"kalshi {series}: unexpected payload") from None
    return events if isinstance(events, list) else []


def store(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = datetime.now(UTC)
    computed = now.isoformat(timespec="seconds")
    data = [
        (
            "kalshi:" + re.sub(r"[^a-z0-9-]", "-", r["ticker"].lower())[:120],
            now.date().isoformat(),  # one row per market per UTC day; reruns upsert it
            computed,
            computed,
            r["p"],
            r["topic"],
            json.dumps(
                {
                    "question": r["question"],
                    "outcomes": ["Yes", "No"],
                    "volume": r["volume"],
                    "volume_unit": "contracts",
                    "end_date": r["end_date"],
                    "topic": r["topic"],
                    "venue": "kalshi",
                    "url": f"https://kalshi.com/markets/{r['series'].lower()}",
                }
            ),
        )
        for r in rows
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            data,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(data)


def run(conn: sqlite3.Connection) -> int:
    """Fetch every series (network first, no lock held), store, log per series."""
    rows, errors = [], []
    for series, (topic, keep) in SERIES.items():
        try:
            rows += [{**r, "topic": topic} for r in nearest_markets(fetch_series(series), keep)]
        except KalshiError as ex:
            errors.append(str(ex))
    n = store(conn, rows) if rows else 0
    err = "; ".join(errors) or None
    # some series failed but others stored: DEGRADED, not a dead source
    status = "DEGRADED" if err and n else None
    fetch_log.log_collection(conn, "kalshi", "kalshi:events", None, n, err=err, status=status)
    return n


def main(argv: list[str] | None = None) -> int:
    from .. import db
    from ..__main__ import _DEFAULT_DB

    p = argparse.ArgumentParser(prog="arkwatch kalshi")
    p.add_argument("--db", default=_DEFAULT_DB)
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)
    try:
        n = run(conn)
    finally:
        conn.close()
    print(f"=== kalshi: {n} markets stored ===")
    return 0  # an extra source: an outage is in fetch_log and must not page the owner
