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

import base64
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "arkwatch.db"


@contextmanager
def _get_connection(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    *,
    write: bool = False,
) -> Generator[sqlite3.Connection, None, None]:
    """Readers get a read-only connection (never creates/migrates the DB);
    only the explicit write paths (on_demand_refresh, counterfactual audit)
    open a writer."""
    if conn is not None:
        yield conn
    else:
        from . import db

        path = db_path or DEFAULT_DB
        connection = (
            db.get_conn(path, allow_init=True) if write else db.get_conn(path, read_only=True)
        )
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
        return list_news(c, source=source, symbol=symbol, limit=limit)["items"]


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

    # generation records scenarios and evaluates trackers, so it needs a writer
    # (the HTTP server never calls this; it serves stored scenarios read-only)
    with _get_connection(conn, db_path, write=True) as c:
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

    with _get_connection(conn, db_path, write=True) as c:  # scanning records + evaluates
        return scan_market_opportunities(c, symbols=symbols, as_of=as_of, min_rr=min_rr)


def evaluate_counterfactuals(
    as_of: datetime | str | None = None,
    forward_hours: int = 2,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """Audit whether past stop-losses and early exits were justified (Saved Capital vs Whipsaw Stop)."""
    from .signals.playbook_tracker import evaluate_counterfactual_outcomes

    with _get_connection(conn, db_path, write=True) as c:
        return evaluate_counterfactual_outcomes(c, as_of=as_of, forward_hours=forward_hours)


def get_economic_calendar(
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    days_forward: int = 7,
    days_backward: int = 3,
) -> list[dict]:
    """Retrieve recent and upcoming economic calendar events with consensus, actual, and surprise z-scores."""
    with _get_connection(conn, db_path) as c:
        return list_calendar(c, days_forward, days_backward, limit=100_000)["items"]


def on_demand_refresh(target: str, db_path: str | Path | None = None) -> dict:
    """Trigger an on-demand data refresh or calculation outside the cron schedule.

    Supported targets: 'crypto', 'energy', 'market', 'market-news', 'sentiment', 'calibrate'
    """
    path = str(db_path or DEFAULT_DB)

    if target == "crypto":
        from .signals.crypto import store_crypto_signals

        with _get_connection(None, path, write=True) as c:
            n = store_crypto_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "energy":
        from .qa.energy import compute

        with _get_connection(None, path, write=True) as c:
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

        with _get_connection(None, path, write=True) as c:
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

        with _get_connection(None, path, write=True) as c:
            n = store_futures_flow_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "intraday":
        from .signals.intraday import store_intraday_signals

        with _get_connection(None, path, write=True) as c:
            n = store_intraday_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "etf-flows":
        from .signals.etf_flows import store_etf_flow_signals

        with _get_connection(None, path, write=True) as c:
            n = store_etf_flow_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "news-velocity":
        from .signals.news import store_news_signals

        with _get_connection(None, path, write=True) as c:
            n = store_news_signals(c)
        return {"target": target, "status": "OK", "signals_updated": n}

    if target == "sentiment":
        from .signals.sentiment import extract_news_intelligence, store_asset_radars

        with _get_connection(None, path, write=True) as c:
            n = extract_news_intelligence(c, limit=15)
            radars = store_asset_radars(c)
        return {
            "target": target,
            "status": "OK",
            "articles_processed": n,
            "radars_updated": len(radars),
        }

    return {"target": target, "status": "ERROR", "error": f"unknown refresh target '{target}'"}


# ---------------------------------------------------------------------------
# Generic read layer (REST server). Every function takes an open connection
# and returns JSON-friendly dicts; list endpoints use keyset pagination
# {items, next_cursor} where the cursor is the opaque last sort key.
# ---------------------------------------------------------------------------

# Expected max age (days) of the newest observation per registry freq —
# generous on purpose (period-start ts + publication lag + weekends/holidays).
FRESHNESS_LAG_DAYS = {"D": 5, "W": 14, "M": 75, "Q": 200, "A": 550, "E": 550}
CANCELLABLE_STATES = ("PENDING_TRIGGER", "ACTIVE")


class ApiConflict(Exception):
    """The target exists but its current state forbids the requested change."""


class ApiNotFound(Exception):
    """The addressed row does not exist."""


class ApiBadRequest(Exception):
    """Malformed client input the HTTP layer could not validate (e.g. cursor)."""


def _rows(c: sqlite3.Connection, sql: str, params: list | tuple = ()) -> list[dict]:
    cur = c.cursor()
    cur.row_factory = sqlite3.Row  # dict-by-name regardless of the caller's factory
    return [dict(r) for r in cur.execute(sql, params).fetchall()]


def _encode_cursor(values: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def _decode_cursor(cursor: str, n: int) -> list:
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        raise ApiBadRequest("invalid cursor") from None
    if not (isinstance(values, list) and len(values) == n):
        raise ApiBadRequest("invalid cursor")
    if not all(v is None or isinstance(v, (str, int, float)) for v in values):
        raise ApiBadRequest("invalid cursor")
    return values


def _page(
    c: sqlite3.Connection,
    inner_sql: str,
    params: list,
    keys: list[str],
    *,
    cursor: str | None,
    limit: int,
    desc: bool = False,
) -> dict:
    """Keyset page over `inner_sql` ordered by `keys` (unique together).
    The subquery wrapper lets keys be output aliases; SQLite flattens it, so
    the inner WHERE keeps its index."""
    sql = f"SELECT * FROM ({inner_sql}) WHERE 1=1"
    params = list(params)
    if cursor:
        op = "<" if desc else ">"
        sql += f" AND ({', '.join(keys)}) {op} ({', '.join('?' * len(keys))})"
        params += _decode_cursor(cursor, len(keys))
    sql += " ORDER BY " + ", ".join(f"{k} DESC" if desc else k for k in keys) + " LIMIT ?"
    rows = _rows(c, sql, [*params, limit + 1])
    items = rows[:limit]
    more = len(rows) > limit
    return {
        "items": items,
        "next_cursor": _encode_cursor([items[-1][k] for k in keys]) if more else None,
    }


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _freshness(last_ts: str | None, freq: str | None, today: date) -> dict:
    lag = FRESHNESS_LAG_DAYS.get((freq or "").upper(), 30)
    if not last_ts:
        return {"expected_lag_days": lag, "age_days": None, "status": "never"}
    try:
        age = (today - date.fromisoformat(last_ts[:10])).days
    except ValueError:
        return {"expected_lag_days": lag, "age_days": None, "status": "stale"}
    status = "fresh" if age <= lag else ("late" if age <= 2 * lag else "stale")
    return {"expected_lag_days": lag, "age_days": age, "status": status}


def _parse_json(text: str | None):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


_SERIES_COLS = (
    "series_id, name, block, tier, unit, value_format, freq, primary_source, "
    "secondary_source, active, calendar_family, locked_by_ui"
)


def list_series(
    c: sqlite3.Connection,
    *,
    block: str | None = None,
    active: bool | None = None,
    freq: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict:
    where, params = "", []
    if block:
        where += " AND block = ?"
        params.append(block)
    if active is not None:
        where += " AND active = ?"
        params.append(int(active))
    if freq:
        where += " AND freq = ?"
        params.append(freq.upper())
    inner = f"SELECT {_SERIES_COLS} FROM series_registry WHERE 1=1{where}"
    return _page(c, inner, params, ["series_id"], cursor=cursor, limit=limit)


def series_detail(c: sqlite3.Connection, series_id: str) -> dict | None:
    rows = _rows(
        c,
        "SELECT r.*, (SELECT MAX(ts) FROM raw_observations o WHERE o.series_id = r.series_id)"
        " AS last_ts, (SELECT COUNT(*) FROM raw_observations o WHERE o.series_id = r.series_id"
        " AND o.vintage_ts = 'realtime') AS n_obs FROM series_registry r WHERE r.series_id = ?",
        (series_id,),
    )
    if not rows:
        return None
    out = rows[0]
    out["freshness"] = _freshness(out["last_ts"], out["freq"], datetime.now(UTC).date())
    return out


def get_observations(
    c: sqlite3.Connection,
    series_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    vintage: str = "realtime",
    cursor: str | None = None,
    limit: int = 500,
) -> dict:
    """release_ts 'na' (the column's 'unknown' sentinel) is surfaced as null —
    never compare it as a date: 'na' sorts AFTER every ISO timestamp."""
    where, params = "", [series_id, vintage]
    if start:
        where += " AND ts >= ?"
        params.append(start)
    if end:
        where += " AND ts <= ?"
        params.append(end)
    inner = (
        "SELECT ts, source, value, NULLIF(release_ts, 'na') AS release_ts, vintage_ts"
        f" FROM raw_observations WHERE series_id = ? AND vintage_ts = ?{where}"
    )
    return _page(c, inner, params, ["ts", "source"], cursor=cursor, limit=limit)


def list_signals(
    c: sqlite3.Connection, *, prefix: str | None = None, cursor: str | None = None, limit: int = 200
) -> dict:
    where, params = "", []
    if prefix:
        where = " WHERE signal_id LIKE ? ESCAPE '\\'"
        params.append(_like_escape(prefix) + "%")
    # bare columns next to MAX() come from the max row (SQLite guarantee)
    inner = (
        "SELECT signal_id, MAX(ts) AS last_ts, value, state, computed_at"
        f" FROM computed_signals{where} GROUP BY signal_id"
    )
    return _page(c, inner, params, ["signal_id"], cursor=cursor, limit=limit)


def signal_history(
    c: sqlite3.Connection,
    signal_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    cursor: str | None = None,
    limit: int = 500,
) -> dict:
    where, params = "", [signal_id]
    if start:
        where += " AND ts >= ?"
        params.append(start)
    if end:
        where += " AND ts <= ?"
        params.append(end)
    inner = (
        "SELECT ts, value, state, run_id, computed_at FROM computed_signals"
        f" WHERE signal_id = ?{where}"
    )
    return _page(c, inner, params, ["ts"], cursor=cursor, limit=limit)


def get_cot(c: sqlite3.Connection, contract: str, *, window: int = 52) -> list[dict]:
    """All cot_raw rows of the latest `window` report dates for a contract (ascending)."""
    dates = c.execute(
        "SELECT DISTINCT report_date FROM cot_raw WHERE contract_code = ?"
        " ORDER BY report_date DESC LIMIT ?",
        (contract, window),
    ).fetchall()
    if not dates:
        return []
    return _rows(
        c,
        "SELECT report_date, report_type, category, release_ts, long, short, spread,"
        " open_interest_all, pct_of_oi, change_long, change_short, traders_long,"
        " traders_short, conc_top4_long, conc_top4_short FROM cot_raw"
        " WHERE contract_code = ? AND report_date >= ?"
        " ORDER BY report_date, report_type, category",
        (contract, dates[-1][0]),
    )


def get_prices(
    c: sqlite3.Connection,
    symbol: str,
    *,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    cursor: str | None = None,
    limit: int = 500,
) -> dict:
    """interval '1d' = instrument_prices (daily); anything else = intraday_bars."""
    if interval == "1d":
        inner = (
            "SELECT ts, source, open, high, low, close, volume FROM instrument_prices"
            " WHERE symbol = ?"
        )
        params: list = [symbol]
        col = "ts"
    else:
        inner = (
            "SELECT bar_ts_utc AS ts, source, open, high, low, close, volume FROM intraday_bars"
            " WHERE symbol = ? AND interval = ?"
        )
        params = [symbol, interval]
        col = "bar_ts_utc"
    if start:
        inner += f" AND {col} >= ?"
        params.append(start)
    if end:
        inner += f" AND {col} <= ?"
        params.append(end)
    return _page(c, inner, params, ["ts", "source"], cursor=cursor, limit=limit)


_PLAYBOOK_COLS = (
    "scenario_uid, symbol, horizon, direction, scenario_id, title, trigger_condition,"
    " trigger_price, target_profit, invalidation_level, risk_reward_ratio, created_at_utc,"
    " session_id, state, triggered_at_utc, resolved_at_utc, entry_price, exit_price,"
    " mfe_points, mae_points, pnl_points, r_multiple, cfd_basis_offset, note"
)


def list_playbooks(
    c: sqlite3.Connection,
    *,
    state: str | None = None,
    symbol: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict:
    """STORED scenarios only — generation stays in the daemon."""
    where, params = "", []
    if state:
        where += " AND state = ?"
        params.append(state.upper())
    if symbol:
        where += " AND symbol = ?"
        params.append(symbol.upper())
    inner = f"SELECT {_PLAYBOOK_COLS} FROM playbook_scenarios WHERE 1=1{where}"
    return _page(
        c, inner, params, ["created_at_utc", "scenario_uid"], cursor=cursor, limit=limit, desc=True
    )


def playbook_detail(c: sqlite3.Connection, uid: str) -> dict | None:
    rows = _rows(
        c,
        f"SELECT {_PLAYBOOK_COLS}, payload_json FROM playbook_scenarios WHERE scenario_uid = ?",
        (uid,),
    )
    if not rows:
        return None
    out = rows[0]
    out["payload"] = _parse_json(out.pop("payload_json"))
    return out


_OUTBOX_SQL = (
    "SELECT id, brief_date, channel, status, attempts, last_error, sent_at, created_at,"
    " claimed_at FROM brief_deliveries"
)


def list_outbox(
    c: sqlite3.Connection, *, status: str | None = None, cursor: str | None = None, limit: int = 100
) -> dict:
    """The brief outbox (brief_deliveries); alerts retry on their own cadence."""
    if status:
        return _page(
            c,
            _OUTBOX_SQL + " WHERE status = ?",
            [status],
            ["id"],
            cursor=cursor,
            limit=limit,
            desc=True,
        )
    return _page(c, _OUTBOX_SQL, [], ["id"], cursor=cursor, limit=limit, desc=True)


def outbox_item(c: sqlite3.Connection, outbox_id: int) -> dict | None:
    rows = _rows(c, _OUTBOX_SQL + " WHERE id = ?", (outbox_id,))
    return rows[0] if rows else None


def list_calendar(
    c: sqlite3.Connection,
    days_forward: int = 7,
    days_backward: int = 3,
    *,
    cursor: str | None = None,
    limit: int = 500,
) -> dict:
    """Range on ts_utc itself (idx_events_ts usable): [start-day, end-day + 1)."""
    today = datetime.now(UTC).date()
    start = (today - timedelta(days=days_backward)).isoformat()
    end = (today + timedelta(days=days_forward + 1)).isoformat()
    inner = (
        "SELECT event_uid, ts_utc, country, name, importance, actual, consensus, previous,"
        " surprise_z FROM events WHERE ts_utc >= ? AND ts_utc < ?"
    )
    return _page(c, inner, [start, end], ["ts_utc", "event_uid"], cursor=cursor, limit=limit)


def list_news(
    c: sqlite3.Connection,
    *,
    source: str | None = None,
    symbol: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict:
    where, params = "", []
    if source:
        where += " AND source = ?"
        params.append(source.upper())
    if symbol:  # substring match with LIKE wildcards escaped
        where += " AND symbols_json LIKE ? ESCAPE '\\'"
        params.append(f"%{_like_escape(symbol)}%")
    inner = (
        "SELECT news_id, published_at_utc, source, title, url, summary, symbols_json,"
        f" cluster_id, relevance, novelty FROM market_news WHERE 1=1{where}"
    )
    page = _page(
        c, inner, params, ["published_at_utc", "news_id"], cursor=cursor, limit=limit, desc=True
    )
    for r in page["items"]:
        syms = _parse_json(r.pop("symbols_json"))
        r["symbols"] = syms if isinstance(syms, list) else []
        r["relevance"] = round(float(r["relevance"]), 2)
        r["novelty"] = round(float(r["novelty"]), 2)
    return page


def data_freshness(c: sqlite3.Connection) -> list[dict]:
    """Freshness SLA per ACTIVE series: newest ts vs the registry-freq lag."""
    today = datetime.now(UTC).date()
    rows = _rows(
        c,
        "SELECT r.series_id, r.freq, (SELECT MAX(o.ts) FROM raw_observations o"
        " WHERE o.series_id = r.series_id) AS last_ts FROM series_registry r"
        " WHERE r.active = 1 ORDER BY r.series_id",
    )
    return [{**r, **_freshness(r["last_ts"], r["freq"], today)} for r in rows]


def _job_out(r: dict) -> dict:
    r["params"] = _parse_json(r.pop("params_json")) or {}
    r["result"] = _parse_json(r.pop("result_json"))
    return r


def jobs_status(c: sqlite3.Connection, *, cursor: str | None = None, limit: int = 100) -> dict:
    page = _page(c, "SELECT * FROM jobs", [], ["id"], cursor=cursor, limit=limit, desc=True)
    page["items"] = [_job_out(r) for r in page["items"]]
    return page


def job_detail(c: sqlite3.Connection, job_id: int) -> dict | None:
    rows = _rows(c, "SELECT * FROM jobs WHERE id = ?", (job_id,))
    return _job_out(rows[0]) if rows else None


# ---------------------------------------------------------------------------
# Write layer (REST server). Each call is one BEGIN IMMEDIATE transaction
# that also appends an audit_log row; repeating an applied change is a no-op
# ({"changed": False}) and is not audited again.
# ---------------------------------------------------------------------------


@contextmanager
def _write_tx(c: sqlite3.Connection) -> Generator[None, None, None]:
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _audit(c: sqlite3.Connection, actor: str, action: str, target: str, detail: dict) -> None:
    c.execute(
        "INSERT INTO audit_log(ts, actor, action, target, detail_json) VALUES (?,?,?,?,?)",
        (_now(), actor, action, target, json.dumps(detail)),
    )


def cancel_playbook(
    c: sqlite3.Connection, uid: str, *, actor: str, note: str, exit_price: float | None = None
) -> dict:
    """PENDING_TRIGGER/ACTIVE -> CANCELLED_MANUAL. An ACTIVE scenario holds a
    position, so it needs the realized exit_price (pnl/R are computed)."""
    with _write_tx(c):
        rows = _rows(
            c,
            "SELECT state, direction, entry_price, invalidation_level FROM playbook_scenarios"
            " WHERE scenario_uid = ?",
            (uid,),
        )
        if not rows:
            raise ApiNotFound(f"playbook scenario '{uid}' not found")
        row, changed = rows[0], False
        if row["state"] != "CANCELLED_MANUAL":
            if row["state"] not in CANCELLABLE_STATES:
                raise ApiConflict(f"state {row['state']} cannot be cancelled")
            pnl = r_mult = 0.0
            if row["state"] == "ACTIVE":
                if exit_price is None:
                    raise ApiConflict("cancelling an ACTIVE scenario requires exit_price")
                entry = row["entry_price"]
                sign = {"LONG": 1.0, "SHORT": -1.0}.get(row["direction"], 0.0)
                if entry is not None:
                    pnl = sign * (exit_price - entry)
                    risk = abs(entry - row["invalidation_level"])
                    r_mult = pnl / risk if risk else 0.0
            else:
                exit_price = None  # never filled: nothing to exit
            c.execute(
                "UPDATE playbook_scenarios SET state = 'CANCELLED_MANUAL', resolved_at_utc = ?,"
                " note = ?, exit_price = ?, pnl_points = ?, r_multiple = ?"
                " WHERE scenario_uid = ?",
                (_now(), note, exit_price, round(pnl, 4), round(r_mult, 4), uid),
            )
            _audit(
                c,
                actor,
                "playbook.cancel",
                uid,
                {"from_state": row["state"], "note": note, "exit_price": exit_price},
            )
            changed = True
    return {"changed": changed, "scenario": playbook_detail(c, uid)}


def retry_outbox(c: sqlite3.Connection, outbox_id: int, *, actor: str) -> dict:
    """failed -> pending (attempt counter reset so the sender picks it up)."""
    with _write_tx(c):
        rows = _rows(c, "SELECT status FROM brief_deliveries WHERE id = ?", (outbox_id,))
        if not rows:
            raise ApiNotFound(f"outbox row {outbox_id} not found")
        status, changed = rows[0]["status"], False
        if status != "pending":
            if status != "failed":
                raise ApiConflict(f"only failed rows can be retried (status={status})")
            c.execute(
                "UPDATE brief_deliveries SET status = 'pending', attempts = 0,"
                " last_error = NULL, claimed_at = NULL WHERE id = ?",
                (outbox_id,),
            )
            _audit(c, actor, "outbox.retry", str(outbox_id), {"from_status": status})
            changed = True
    return {"changed": changed, "item": outbox_item(c, outbox_id)}


def set_series_active(c: sqlite3.Connection, series_id: str, active: bool, *, actor: str) -> dict:
    """Updates series_registry.active only and sets locked_by_ui so the next
    YAML sync (qa/backfill.sync_registry) keeps it. YAML stays the source of
    truth for every other column; the harvest selects series from the YAML."""
    with _write_tx(c):
        rows = _rows(
            c,
            "SELECT active, locked_by_ui FROM series_registry WHERE series_id = ?",
            (series_id,),
        )
        if not rows:
            raise ApiNotFound(f"series '{series_id}' not found")
        row, changed = rows[0], False
        if not (bool(row["active"]) == active and row["locked_by_ui"]):
            c.execute(
                "UPDATE series_registry SET active = ?, locked_by_ui = 1 WHERE series_id = ?",
                (int(active), series_id),
            )
            _audit(
                c,
                actor,
                "series.set_active",
                series_id,
                {"from": bool(row["active"]), "to": active},
            )
            changed = True
    return {"changed": changed, "series": series_detail(c, series_id)}


def enqueue_job(c: sqlite3.Connection, kind: str, *, actor: str) -> dict:
    """Queue an allowlisted job for the daemon (qa/jobs_runner). A queued or
    running job of the same kind is returned instead of a duplicate."""
    from .qa.jobs_runner import JOB_KINDS

    if kind not in JOB_KINDS:
        raise ApiBadRequest(f"job kind '{kind}' is not allowlisted")
    with _write_tx(c):
        existing = c.execute(
            "SELECT id FROM jobs WHERE kind = ? AND status IN ('queued', 'running')"
            " ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if existing:
            job_id, changed = existing[0], False
        else:
            job_id = c.execute(
                "INSERT INTO jobs(kind, params_json, status, requested_by, created_at)"
                " VALUES (?, '{}', 'queued', ?, ?)",
                (kind, actor, _now()),
            ).lastrowid
            _audit(c, actor, "job.enqueue", str(job_id), {"kind": kind})
            changed = True
    return {"changed": changed, "job": job_detail(c, job_id)}
