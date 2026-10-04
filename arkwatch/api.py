"""api.py — unified public Python API and data access layer for ark-watch.

Designed as the clean programmatic bridge for website dashboards, REST/FastAPI
endpoints, and autonomous AI Agent Harnesses.

Key APIs:
  - get_regime_snapshot()
  - get_crypto_intelligence()
  - get_energy_intelligence()
  - get_options_intelligence()
  - get_market_news()
  - get_economic_calendar()
  - on_demand_refresh()
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "arkwatch.db"


@contextmanager
def _get_connection(
    conn: sqlite3.Connection | None = None, db_path: str | Path | None = None
) -> Generator[sqlite3.Connection, None, None]:
    if conn is not None:
        yield conn
    else:
        from . import db

        path = db_path or DEFAULT_DB
        connection = db.get_conn(path, allow_init=True)
        try:
            yield connection
        finally:
            connection.close()


def get_regime_snapshot(
    conn: sqlite3.Connection | None = None, db_path: str | Path | None = None
) -> dict:
    """Retrieve full macroeconomic regime intelligence: score, label, Dalio quadrant, Dollar smile, and 6 pillars."""
    from .signals.pillars import (
        compute_dollar_smile,
        compute_pillars,
        compute_quadrant,
        compute_regime_score,
    )

    with _get_connection(conn, db_path) as c:
        pillars = compute_pillars(c)
        score = compute_regime_score(pillars)
        label = "RISK-ON" if score > 0.3 else ("RISK-OFF" if score < -0.3 else "NEUTRAL")
        quadrant = compute_quadrant(pillars)
        dollar_smile = compute_dollar_smile(c)

        return {
            "regime_score": round(score, 4),
            "label": label,
            "quadrant": quadrant,
            "dollar_smile": dollar_smile,
            "pillars": {
                k: {
                    "label": v.get("label"),
                    "z_score": v.get("z"),
                    "state": v.get("state"),
                    "detail": v.get("detail"),
                    "parts": v.get("parts"),
                }
                for k, v in pillars.items()
            },
        }


def get_crypto_intelligence(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    instrument: str = "BTC-USDT-SWAP",
) -> dict:
    """Retrieve crypto market intelligence: 24h & 1h forced liquidations, imbalance ratio, and cascade alerts."""
    from .signals.crypto import liquidation_cascade_detector, liquidation_summary

    with _get_connection(conn, db_path) as c:
        sum_24h = liquidation_summary(c, instrument=instrument, window_hours=24)
        sum_1h = liquidation_summary(c, instrument=instrument, window_hours=1)
        cascade = liquidation_cascade_detector(c, instrument=instrument, window_hours=1)

        return {
            "instrument": instrument,
            "summary_24h": sum_24h,
            "summary_1h": sum_1h,
            "cascade_detector": cascade,
        }


def get_energy_intelligence(
    conn: sqlite3.Connection | None = None, db_path: str | Path | None = None
) -> dict:
    """Retrieve energy curve intelligence: 3:2:1 crack spread, gasoline crack, heating oil crack, WTI backwardation."""
    from .qa.energy import compute, compute_crack_321_history

    with _get_connection(conn, db_path) as c:
        try:
            current_signals = compute(c)
        except Exception as ex:
            current_signals = {"error": str(ex)}

        history_321 = compute_crack_321_history(c, limit=30)

        return {
            "signals": current_signals,
            "crack_321_history": [{"ts": ts, "crack_321": val} for ts, val in history_321],
        }


def get_options_intelligence(
    conn: sqlite3.Connection | None = None, db_path: str | Path | None = None
) -> dict:
    """Retrieve CME options positioning (PCR, Max Pain, OI walls) and OPEX calendar status."""
    from .signals.options import next_opex, opex_calendar, options_snapshot

    with _get_connection(conn, db_path) as c:
        gold_snap = options_snapshot(c, "OG")
        btc_snap = options_snapshot(c, "BTC")
        opex_info = next_opex()
        year = datetime.now(UTC).year

        return {
            "gold": gold_snap,
            "btc": btc_snap,
            "opex": opex_info,
            "annual_opex_schedule": opex_calendar(year),
        }


def get_market_news(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    limit: int = 20,
    source: str | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """Retrieve clustered, curated market news from FMP and EODHD ordered by recency and relevance."""
    with _get_connection(conn, db_path) as c:
        query = (
            "SELECT news_id, published_at_utc, source, title, url, summary, symbols_json, "
            "       cluster_id, relevance, novelty "
            "FROM market_news WHERE 1=1 "
        )
        params: list = []
        if source:
            query += "AND source = ? "
            params.append(source.upper())
        if symbol:
            query += "AND symbols_json LIKE ? "
            params.append(f"%{symbol}%")

        query += "ORDER BY published_at_utc DESC LIMIT ?"
        params.append(limit)

        rows = c.execute(query, params).fetchall()

        out = []
        for r in rows:
            try:
                syms = json.loads(r[6])
            except Exception:
                syms = []
            out.append(
                {
                    "news_id": r[0],
                    "published_at_utc": r[1],
                    "source": r[2],
                    "title": r[3],
                    "url": r[4],
                    "summary": r[5],
                    "symbols": syms,
                    "cluster_id": r[7],
                    "relevance": round(float(r[8]), 2),
                    "novelty": round(float(r[9]), 2),
                }
            )
        return out


def get_economic_calendar(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    days_forward: int = 7,
    days_backward: int = 3,
) -> list[dict]:
    """Retrieve recent and upcoming economic calendar events with consensus, actual, and surprise z-scores."""
    now = datetime.now(UTC)
    start = (now - timedelta(days=days_backward)).date().isoformat()
    end = (now + timedelta(days=days_forward)).date().isoformat()

    with _get_connection(conn, db_path) as c:
        rows = c.execute(
            "SELECT event_uid, ts_utc, country, name, importance, actual, consensus, previous, surprise_z "
            "FROM events "
            "WHERE substr(ts_utc, 1, 10) >= ? AND substr(ts_utc, 1, 10) <= ? "
            "ORDER BY ts_utc ASC",
            (start, end),
        ).fetchall()

        return [
            {
                "event_uid": r[0],
                "ts_utc": r[1],
                "country": r[2],
                "name": r[3],
                "importance": r[4],
                "actual": r[5],
                "consensus": r[6],
                "previous": r[7],
                "surprise_z": r[8],
            }
            for r in rows
        ]


def on_demand_refresh(target: str, db_path: str | Path | None = None) -> dict:
    """Trigger an on-demand data refresh or calculation outside the cron schedule.

    Supported targets: 'crypto', 'energy', 'market', 'market-news', 'calibrate'
    """
    path = str(db_path or DEFAULT_DB)

    if target == "crypto":
        from .signals.crypto import store_crypto_signals

        with _get_connection(None, path) as c:
            n = store_crypto_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "energy":
        from .qa.energy import compute

        with _get_connection(None, path) as c:
            out = compute(c)
        return {"target": target, "status": "OK", "signals": out}

    if target == "market":
        from .qa.market_timeline import run

        res = run(path)
        return {"target": target, "status": "OK", "timeline_results": res}

    if target == "market-news":
        from .qa.market_news import run

        res = run(path)
        return {"target": target, "status": "OK", "news_results": res}

    if target == "calibrate":
        from .qa.calibrate import audit_anchors

        with _get_connection(None, path) as c:
            audits = audit_anchors(c)
        return {
            "target": target,
            "status": "OK",
            "total_anchors": len(audits),
            "ok": sum(1 for a in audits if a.status == "OK"),
            "drifted": sum(1 for a in audits if a.status == "DRIFTED"),
        }

    return {"target": target, "status": "ERROR", "error": f"unknown refresh target '{target}'"}
