"""news.py — news flow velocity and breaking catalyst spike detector.

Monitors news publication frequency and macro relevance in market_news to detect
breaking catalyst events before their full impact is reflected in EOD prices.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

KEY_TOPICS = {
    "OIL": ["oil", "crude", "wti", "brent", "opec", "energy", "petroleum"],
    "GOLD": ["gold", "bullion", "xau", "precious metals", "silver"],
    "FED": ["fed", "fomc", "powell", "rate cut", "rate hike", "inflation", "cpi"],
    "CRYPTO": ["bitcoin", "btc", "ethereum", "eth", "crypto", "sec"],
    "EQUITY": ["stocks", "sp500", "nasdaq", "earnings", "tech", "nvidia"],
}


def _news_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_news'"
    ).fetchone()
    return bool(row)


def news_velocity(
    conn: sqlite3.Connection,
    topic: str | None = None,
    *,
    window_hours: int = 2,
    baseline_days: int = 7,
    as_of: datetime | str | None = None,
) -> dict | None:
    """Compute current news publication velocity vs rolling baseline.

    Returns:
      {
        'topic': str,
        'window_hours': int,
        'recent_article_count': int,
        'recent_weighted_count': float,
        'baseline_hourly_rate': float,
        'velocity_ratio': float,
        'state': 'NEWS_SPIKE' | 'ELEVATED' | 'NORMAL' | 'QUIET',
        'top_headlines': list[str]
      }
    """
    if not _news_ready(conn):
        return None

    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of)
    else:
        target_dt = as_of

    since_window = (target_dt - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    since_baseline = (target_dt - timedelta(days=baseline_days)).isoformat(timespec="seconds")
    until = target_dt.isoformat(timespec="seconds")

    topic_key = topic.upper() if topic else "ALL"
    keywords = KEY_TOPICS.get(topic_key, [])

    if keywords:
        like_clause = " OR ".join("LOWER(title) LIKE ?" for _ in keywords)
        params_recent = [f"%{k}%" for k in keywords] + [since_window, until]
        params_base = [f"%{k}%" for k in keywords] + [since_baseline, until]
        recent_query = (
            f"SELECT title, source, relevance, novelty FROM market_news "
            f"WHERE ({like_clause}) AND published_at_utc >= ? AND published_at_utc <= ? "
            f"ORDER BY published_at_utc DESC"
        )
        base_query = (
            f"SELECT COUNT(*) FROM market_news "
            f"WHERE ({like_clause}) AND published_at_utc >= ? AND published_at_utc <= ?"
        )
    else:
        recent_query = (
            "SELECT title, source, relevance, novelty FROM market_news "
            "WHERE published_at_utc >= ? AND published_at_utc <= ? "
            "ORDER BY published_at_utc DESC"
        )
        base_query = (
            "SELECT COUNT(*) FROM market_news WHERE published_at_utc >= ? AND published_at_utc <= ?"
        )
        params_recent = [since_window, until]
        params_base = [since_baseline, until]

    recent_rows = conn.execute(recent_query, params_recent).fetchall()
    base_count = conn.execute(base_query, params_base).fetchone()[0]

    recent_n = len(recent_rows)
    # Weight count by macro relevance
    weighted_n = sum(float(r[2]) for r in recent_rows) if recent_rows else 0.0

    baseline_hours = baseline_days * 24.0
    baseline_hourly = base_count / baseline_hours if baseline_hours > 0 else 0.1
    current_hourly = recent_n / float(window_hours) if window_hours > 0 else 0.0

    ratio = current_hourly / baseline_hourly if baseline_hourly > 0 else 1.0

    if recent_n < 2:
        state = "QUIET"
    elif ratio >= 3.0 and recent_n >= 4:
        state = "NEWS_SPIKE"
    elif ratio >= 1.8 and recent_n >= 3:
        state = "ELEVATED"
    else:
        state = "NORMAL"

    top_headlines = [f"[{r[1]}] {r[0]}" for r in recent_rows[:5]]

    return {
        "topic": topic_key,
        "window_hours": window_hours,
        "as_of": until,
        "recent_article_count": recent_n,
        "recent_weighted_count": round(weighted_n, 2),
        "baseline_hourly_rate": round(baseline_hourly, 2),
        "current_hourly_rate": round(current_hourly, 2),
        "velocity_ratio": round(ratio, 2),
        "state": state,
        "top_headlines": top_headlines,
    }


def all_news_velocity(conn: sqlite3.Connection) -> dict[str, dict]:
    """Compute news velocity across all primary asset topics and overall market."""
    out = {}
    overall = news_velocity(conn, None)
    if overall:
        out["ALL"] = overall
    for topic in KEY_TOPICS:
        res = news_velocity(conn, topic)
        if res:
            out[topic] = res
    return out


def store_news_signals(conn: sqlite3.Connection) -> int:
    """Persist news velocity spike states to computed_signals."""
    data = all_news_velocity(conn)
    if not data:
        return 0

    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")
    ts_date = now.date().isoformat()

    rows: list[tuple] = []
    for topic, m in data.items():
        sig_id = f"news_velocity_{topic.lower()}"
        rows.append(
            (
                sig_id,
                ts_date,
                "news_velocity",
                now_iso,
                m["velocity_ratio"],
                m["state"],
                json.dumps(m),
            )
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return len(rows)
