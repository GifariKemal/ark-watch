"""api.py — unified public Python API and data access layer for ark-watch.

Designed as the clean programmatic bridge for website dashboards, REST/FastAPI
endpoints, and autonomous AI Agent Harnesses.

Key APIs:
  - get_regime_snapshot()
  - get_crypto_intelligence()
  - get_energy_intelligence()
  - get_options_intelligence()
  - get_market_news()
  - get_news_intelligence()
  - get_asset_sentiment_radar()
  - get_intraday_catalyst_radar()
  - get_all_sentiment_radars()
  - get_session_levels()
  - get_trading_playbook()
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
    """Retrieve crypto market intelligence: 24h & 1h forced liquidations, imbalance ratio, cascade alerts, and CVD."""
    from .signals.crypto import compute_cvd, liquidation_cascade_detector, liquidation_summary

    with _get_connection(conn, db_path) as c:
        sum_24h = liquidation_summary(c, instrument=instrument, window_hours=24)
        sum_1h = liquidation_summary(c, instrument=instrument, window_hours=1)
        cascade = liquidation_cascade_detector(c, instrument=instrument, window_hours=1)
        cvd = compute_cvd(c, instrument=instrument, window_hours=24)

        return {
            "instrument": instrument,
            "summary_24h": sum_24h,
            "summary_1h": sum_1h,
            "cascade_detector": cascade,
            "cvd_24h": cvd,
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


def get_futures_flow_intelligence(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    product: str | None = None,
) -> dict:
    """Retrieve CME futures flow matrix (ΔPrice × ΔOpen Interest) regimes across products."""
    from .signals.futures_flow import all_futures_flow_matrix, futures_flow_matrix

    with _get_connection(conn, db_path) as c:
        if product:
            res = futures_flow_matrix(c, product)
            return {product.upper(): res} if res else {}
        return all_futures_flow_matrix(c)


def get_session_intraday_intelligence(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    symbol: str = "SPY",
) -> dict | None:
    """Retrieve intraday session VWAP and 5-minute ATR volatility expansion."""
    from .signals.intraday import session_intraday_intelligence

    with _get_connection(conn, db_path) as c:
        return session_intraday_intelligence(c, symbol=symbol)


def get_etf_flows_intelligence(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    asset: str | None = None,
) -> dict:
    """Retrieve physical and spot ETF cumulative flow momentum (GOLD, SILVER, BTC, ETH)."""
    from .signals.etf_flows import all_etf_flow_momentum, etf_flow_momentum

    with _get_connection(conn, db_path) as c:
        if asset:
            res = etf_flow_momentum(c, asset)
            return {asset.upper(): res} if res else {}
        return all_etf_flow_momentum(c)


def get_news_velocity_intelligence(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    topic: str | None = None,
) -> dict:
    """Retrieve market news flow velocity and breaking catalyst spike alerts."""
    from .signals.news import all_news_velocity, news_velocity

    with _get_connection(conn, db_path) as c:
        if topic:
            res = news_velocity(c, topic)
            return {topic.upper(): res} if res else {}
        return all_news_velocity(c)


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


def get_news_intelligence(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    asset: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Retrieve structured multi-dimensional macro stances with evidence quotes."""
    with _get_connection(conn, db_path) as c:
        if asset:
            from .signals.sentiment import _normalize_asset

            norm = _normalize_asset(asset) or asset.upper()
            query = (
                "SELECT ni.news_id, ni.asset, ni.stance, ni.magnitude, ni.confidence, "
                "ni.macro_channel, ni.impact_horizon, ni.evidence_level, "
                "ni.evidence_quote, ni.transmission_rationale, ni.published_at_utc, "
                "m.source, m.title, m.url "
                "FROM news_intelligence ni "
                "JOIN market_news m ON m.news_id = ni.news_id "
                "WHERE ni.asset = ? "
                "ORDER BY ni.published_at_utc DESC LIMIT ?"
            )
            rows = c.execute(query, (norm, limit)).fetchall()
        else:
            query = (
                "SELECT ni.news_id, ni.asset, ni.stance, ni.magnitude, ni.confidence, "
                "ni.macro_channel, ni.impact_horizon, ni.evidence_level, "
                "ni.evidence_quote, ni.transmission_rationale, ni.published_at_utc, "
                "m.source, m.title, m.url "
                "FROM news_intelligence ni "
                "JOIN market_news m ON m.news_id = ni.news_id "
                "ORDER BY ni.published_at_utc DESC LIMIT ?"
            )
            rows = c.execute(query, (limit,)).fetchall()

        return [
            {
                "news_id": r[0],
                "asset": r[1],
                "stance": r[2],
                "magnitude": float(r[3]),
                "confidence": float(r[4]),
                "macro_channel": r[5],
                "impact_horizon": r[6],
                "evidence_level": r[7],
                "evidence_quote": r[8],
                "transmission_rationale": r[9],
                "published_at_utc": r[10],
                "source": r[11],
                "title": r[12],
                "url": r[13],
            }
            for r in rows
        ]


def get_asset_sentiment_radar(
    asset: str,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    window_days: int = 3,
) -> dict:
    """Retrieve time-decayed composite sentiment radar and active catalysts for an asset."""
    from .signals.sentiment import compute_asset_sentiment_radar

    with _get_connection(conn, db_path) as c:
        return compute_asset_sentiment_radar(c, asset, window_days=window_days)


def get_all_sentiment_radars(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    window_days: int = 3,
) -> dict[str, dict]:
    """Retrieve sentiment radars for all tracked assets in the macro book."""
    from .signals.sentiment import compute_all_asset_radars

    with _get_connection(conn, db_path) as c:
        return compute_all_asset_radars(c, window_days=window_days)


def get_intraday_catalyst_radar(
    asset: str,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    window_hours: int = 4,
) -> dict:
    """Retrieve fast-decaying intraday catalyst radar for active trading session."""
    from .signals.sentiment import compute_intraday_catalyst_radar

    with _get_connection(conn, db_path) as c:
        return compute_intraday_catalyst_radar(c, asset, window_hours=window_hours)


def get_session_levels(
    symbol: str,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    as_of: datetime | str | None = None,
) -> dict | None:
    """Retrieve Auction Market Theory reference levels (PDH, PDL, PDC, VAH, VAL, POC, ONH, ONL, OR)."""
    from .signals.levels import compute_session_reference_levels

    with _get_connection(conn, db_path) as c:
        return compute_session_reference_levels(c, symbol, as_of=as_of)


def get_trading_playbook(
    symbol: str,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    as_of: datetime | str | None = None,
    cfd_basis_offset: float = 0.0,
) -> dict | None:
    """Retrieve actionable if-then trading playbook with target profits and invalidation levels."""
    from .signals.playbook import generate_trading_playbook

    with _get_connection(conn, db_path) as c:
        return generate_trading_playbook(c, symbol, as_of=as_of, cfd_basis_offset=cfd_basis_offset)


def get_playbook_performance(
    symbol: str | None = None,
    horizon: str | None = None,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """Retrieve historical playbook win/loss performance metrics, profit factor, and MFE/MAE."""
    from .signals.playbook_tracker import get_playbook_performance_metrics

    with _get_connection(conn, db_path) as c:
        return get_playbook_performance_metrics(c, symbol=symbol, horizon=horizon)


def scan_opportunities(
    symbols: list[str] | tuple[str, ...] | None = None,
    as_of: datetime | str | None = None,
    min_rr: float = 1.5,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Continuous Opportunity Scanner: Scans the tracked book and returns active/imminent trade opportunities."""
    from .signals.playbook_tracker import scan_market_opportunities

    with _get_connection(conn, db_path) as c:
        return scan_market_opportunities(c, symbols=symbols, as_of=as_of, min_rr=min_rr)


def evaluate_counterfactuals(
    as_of: datetime | str | None = None,
    forward_hours: int = 2,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """Audit whether past stop-losses and early exits were justified (Saved Capital vs Whipsaw Stop)."""
    from .signals.playbook_tracker import evaluate_counterfactual_outcomes

    with _get_connection(conn, db_path) as c:
        return evaluate_counterfactual_outcomes(c, as_of=as_of, forward_hours=forward_hours)


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

    Supported targets: 'crypto', 'energy', 'market', 'market-news', 'sentiment', 'calibrate'
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

    if target == "futures-flow":
        from .signals.futures_flow import store_futures_flow_signals

        with _get_connection(None, path) as c:
            n = store_futures_flow_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "intraday":
        from .signals.intraday import store_intraday_signals

        with _get_connection(None, path) as c:
            n = store_intraday_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "etf-flows":
        from .signals.etf_flows import store_etf_flow_signals

        with _get_connection(None, path) as c:
            n = store_etf_flow_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "news-velocity":
        from .signals.news import store_news_signals

        with _get_connection(None, path) as c:
            n = store_news_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "sentiment":
        from .signals.sentiment import extract_news_intelligence, store_asset_radars

        with _get_connection(None, path) as c:
            n = extract_news_intelligence(c, limit=15)
            radars = store_asset_radars(c)
        return {
            "target": target,
            "status": "OK",
            "articles_processed": n,
            "radars_updated": len(radars),
        }

    return {"target": target, "status": "ERROR", "error": f"unknown refresh target '{target}'"}
