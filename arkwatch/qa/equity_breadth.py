"""S&P 500 constituent breadth from live FMP quotes."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime

import requests

from .. import db as _db
from .fetch_log import log_collection


def _constituents(key: str) -> list[str]:
    for url in (
        "https://financialmodelingprep.com/stable/sp500-constituent",
        "https://financialmodelingprep.com/api/v3/sp500_constituent",
    ):
        response = requests.get(url, params={"apikey": key}, timeout=(10, 45))
        if response.status_code == 200 and isinstance(response.json(), list):
            symbols = sorted(
                {str(row.get("symbol") or "").strip() for row in response.json()} - {""}
            )
            if len(symbols) >= 450:
                return symbols
    raise RuntimeError("FMP S&P 500 constituent list unavailable or incomplete")


def _quotes(key: str, symbols: list[str]) -> list[dict]:
    import time

    out = []
    for start in range(0, len(symbols), 100):
        batch = ",".join(symbols[start : start + 100])
        payload = None
        for attempt in range(2):
            try:
                response = requests.get(
                    f"https://financialmodelingprep.com/api/v3/quote/{batch}",
                    params={"apikey": key},
                    timeout=(10, 60),
                )
                response.raise_for_status()
                payload = response.json()
                break
            except Exception:
                if attempt == 0:
                    time.sleep(2)
                else:
                    raise
        if isinstance(payload, list):
            out.extend(payload)
    return out


def run(db_path: str = "data/arkwatch.db") -> dict:
    key = os.environ.get("FMP_API_KEY", "")
    if not key:
        raise RuntimeError("FMP_API_KEY not set")
    symbols = _constituents(key)
    quotes = _quotes(key, symbols)
    by_symbol = {row.get("symbol"): row for row in quotes if row.get("symbol")}
    stamp = datetime.now(UTC).replace(second=0, microsecond=0).isoformat(timespec="seconds")
    components = []
    returns = []
    for symbol in symbols:
        row = by_symbol.get(symbol)
        if not row:
            continue
        price, previous = row.get("price"), row.get("previousClose")
        if price is None or not previous:
            continue
        change = float(price) / float(previous) - 1
        market_cap = float(row.get("marketCap") or 0)
        components.append((stamp, symbol, "FMP", change, float(price), market_cap))
        returns.append((change, market_cap))
    if len(components) < 450:
        raise RuntimeError(f"FMP breadth coverage too low: {len(components)}/{len(symbols)}")
    advances = sum(change > 0 for change, _ in returns)
    declines = sum(change < 0 for change, _ in returns)
    total_cap = sum(cap for _, cap in returns)
    cap_return = sum(change * cap for change, cap in returns) / total_cap if total_cap else None
    conn = _db.get_conn(db_path, allow_init=True)
    conn.executemany(
        "INSERT OR REPLACE INTO equity_breadth_components VALUES (?,?,?,?,?,?)", components
    )
    conn.execute(
        "INSERT OR REPLACE INTO market_breadth VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            stamp,
            "SP500_CONSTITUENTS",
            "FMP",
            advances,
            declines,
            len(returns) - advances - declines,
            advances / len(returns),
            sum(change for change, _ in returns) / len(returns),
            cap_return,
            json.dumps({"coverage": len(components), "universe": len(symbols)}),
        ),
    )
    log_collection(conn, "equity_breadth", "FMP:SP500", by_symbol.get(symbols[0]), len(components))
    conn.close()
    return {"components": len(components), "advances": advances, "declines": declines}


def main(argv=None):
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="arkwatch breadth")
    parser.add_argument("--db", default="data/arkwatch.db")
    print(run(parser.parse_args(argv).db))
    return 0
