"""polymarket.py - crowd probabilities for curated macro/geo topics.

Source: Polymarket Gamma API (free, no key). One call pulls the ~200 highest-volume
active markets; questions are matched to config/polymarket_queries.yaml topics and
the top markets per topic land in computed_signals as `polymarket:<slug>` with
value = probability of the first outcome (usually "Yes"), 0..1.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import re
import sqlite3
import time
from datetime import UTC, datetime

import requests

from ..config import _load_yaml
from ..qa import fetch_log

URL = "https://gamma-api.polymarket.com/markets"
PAGE = 100  # the Gamma API silently caps `limit` at 100
MAX_PAGES = 15
TIMEOUT = (5, 20)
MIN_VOLUME = 50_000.0
RETRY_AFTER_CAP_S = 10.0
# worth one more try; a 4xx (other than 429) or a junk 200 body will not change in 2 s
_TRANSIENT = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
_TOKEN = re.compile(r"[a-z0-9&]+")


class PolymarketError(RuntimeError):
    pass


def load_topics() -> dict:
    return _load_yaml("polymarket_queries.yaml")


def match_topic(question: str, cfg: dict) -> str | None:
    words = set(_TOKEN.findall(question.lower()))
    for t in cfg["topics"]:
        if any(set(_TOKEN.findall(k.lower())) <= words for k in t["keywords"]):
            return t["name"]
    return None


def _get_page(url: str | None, offset: int) -> list:
    params = {
        "active": "true",
        "closed": "false",
        "order": "volumeNum",
        "ascending": "false",
        "limit": PAGE,
        "offset": offset,
    }
    last, wait = "", 0.0
    for attempt in range(2):  # one retry, transient failures only
        if attempt:
            time.sleep(wait)
        wait = random.uniform(1.0, 2.0)  # jitter: hourly cron hosts should not retry in lockstep
        try:
            r = requests.get(url or URL, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
                last = "unexpected payload shape"
                break
            last = f"HTTP {r.status_code}"
            if r.status_code != 429 and r.status_code < 500:
                break
            # seconds form only; an HTTP-date falls back to the jitter
            with contextlib.suppress(KeyError, ValueError):
                wait = max(0.0, min(float(r.headers["Retry-After"]), RETRY_AFTER_CAP_S))
        except requests.RequestException as ex:  # includes an unparseable body
            last = f"{type(ex).__name__}: {ex}"
            if not isinstance(ex, _TRANSIENT):
                break
    raise PolymarketError(f"polymarket: {last}")


def fetch_markets(url: str | None = None, min_volume: float = MIN_VOLUME) -> list:
    """Walk pages by offset (the API caps `limit` at 100 and sorts by volume desc) until a short
    page or the last row is under min_volume; macro markets (Fed, recession) sit on later pages."""
    out: list = []
    for page in range(MAX_PAGES):
        rows = _get_page(url, page * PAGE)
        out += rows
        if len(rows) < PAGE:
            break
        try:
            if float(rows[-1].get("volumeNum") or 0) < min_volume:
                break
        except (TypeError, ValueError):
            break
        time.sleep(0.15)  # be polite
    return out


def _listish(v) -> list:
    out = json.loads(v) if isinstance(v, str) else v
    if not isinstance(out, list):
        raise ValueError("not a list")
    return out


def parse_market(m: dict) -> dict | None:
    """Normalised market, or None for a malformed row."""
    try:
        outcomes = _listish(m["outcomes"])
        prices = [float(p) for p in _listish(m["outcomePrices"])]
        row = {
            "slug": str(m["slug"]),
            "question": str(m["question"]),
            "outcomes": outcomes,
            "volume": float(m.get("volumeNum") or m.get("volume") or 0),
            "end_date": m.get("endDate"),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not outcomes or len(outcomes) != len(prices) or not 0 <= prices[0] <= 1:
        return None
    return {**row, "p": prices[0]}


def select(raw: list, cfg: dict) -> list[dict]:
    """Top-N markets per topic by volume, above each topic's min_volume."""
    min_vol = {t["name"]: float(t.get("min_volume", MIN_VOLUME)) for t in cfg["topics"]}
    picked: dict[str, list[dict]] = {}
    for m in raw:
        row = parse_market(m) if isinstance(m, dict) else None
        topic = row and match_topic(row["question"], cfg)
        if topic and row["volume"] >= min_vol[topic]:
            picked.setdefault(topic, []).append({**row, "topic": topic})
    top_n = int(cfg.get("top_n", 3))
    return [r for rs in picked.values() for r in sorted(rs, key=lambda r: -r["volume"])[:top_n]]


def store(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = datetime.now(UTC)
    computed = now.isoformat(timespec="seconds")
    ts = now.date().isoformat()  # one row per market per UTC day; hourly runs upsert it
    data = [
        (
            # third-party slug -> URL-path-safe id (signal ids appear in /v1/signals/{id})
            "polymarket:" + re.sub(r"[^a-z0-9-]", "-", r["slug"].lower())[:120],
            ts,
            computed,
            computed,
            r["p"],
            r["topic"],
            json.dumps(
                {
                    "question": r["question"],
                    "outcomes": r["outcomes"],
                    "volume": r["volume"],
                    "end_date": r["end_date"],
                    "topic": r["topic"],
                    "url": f"https://polymarket.com/market/{r['slug']}",
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


def run(conn: sqlite3.Connection) -> int | None:
    """Fetch, select, store; logs fetch_log. Returns rows stored, None on fetch ERROR."""
    try:
        topics = load_topics()
        floor = min(float(t.get("min_volume", MIN_VOLUME)) for t in topics["topics"])
        raw = fetch_markets(min_volume=floor)
    except PolymarketError as ex:
        fetch_log.log_collection(conn, "polymarket", "polymarket:markets", None, 0, err=str(ex))
        return None
    n = store(conn, select(raw, topics))
    fetch_log.log_collection(conn, "polymarket", "polymarket:markets", None, n)
    return n


def main(argv: list[str] | None = None) -> int:
    from .. import db
    from ..__main__ import _DEFAULT_DB

    p = argparse.ArgumentParser(prog="arkwatch polymarket")
    p.add_argument("--db", default=_DEFAULT_DB)
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)
    try:
        n = run(conn)
    finally:
        conn.close()
    print(f"=== polymarket: {'ERROR (see fetch_log)' if n is None else f'{n} markets stored'} ===")
    # ERROR is already in fetch_log; Polymarket is an extra source, so an outage must not page the owner
    return 0
