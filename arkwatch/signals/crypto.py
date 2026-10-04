"""crypto.py — crypto derivatives analytics: liquidation flows, imbalances, and cascade detection.

Processes real-time WebSocket forced liquidations (crypto_liquidations) into
structured macro-swing indicators for BTC/ETH.

Key Indicators:
  - Liquidation Imbalance Ratio: (Long_Liq - Short_Liq) / Total_Liq [-1.0 .. +1.0]
  - Liquidation Cascade / Capitulation: Volume spike (>2.5σ) flagging exhaustion bottoms / tops.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta


def _liquidations_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_liquidations'"
    ).fetchone()
    return bool(row)


def liquidation_summary(
    conn: sqlite3.Connection,
    instrument: str | None = None,
    *,
    window_hours: int = 24,
    as_of: datetime | str | None = None,
) -> dict | None:
    """Aggregate forced liquidations over a trailing rolling window (default 24h).

    Returns:
      {
        'instrument': str,
        'window_hours': int,
        'as_of': 'YYYY-MM-DDTHH:MM:SSZ',
        'long_notional_usd': float,
        'short_notional_usd': float,
        'total_notional_usd': float,
        'long_count': int,
        'short_count': int,
        'imbalance_ratio': float,  # -1.0 (100% short squeezed) .. +1.0 (100% long flushed)
        'state': 'LONG_FLUSH' | 'SHORT_SQUEEZE' | 'BALANCED' | 'QUIET'
      }
    """
    if not _liquidations_ready(conn):
        return None

    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of)
    else:
        target_dt = as_of

    since = (target_dt - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    until = target_dt.isoformat(timespec="seconds")

    query = (
        "SELECT LOWER(position_side), "
        "       COALESCE(notional_usd_calculated, notional_usd, price * size, 0.0) "
        "FROM crypto_liquidations "
        "WHERE ts_utc >= ? AND ts_utc <= ? "
    )
    params: list = [since, until]
    if instrument:
        query += "AND instrument = ? "
        params.append(instrument)

    rows = conn.execute(query, params).fetchall()

    long_vol = 0.0
    short_vol = 0.0
    long_cnt = 0
    short_cnt = 0

    for side, notional in rows:
        n = float(notional or 0.0)
        if "long" in str(side):
            long_vol += n
            long_cnt += 1
        elif "short" in str(side):
            short_vol += n
            short_cnt += 1

    total_vol = long_vol + short_vol
    if total_vol > 0:
        imbalance = (long_vol - short_vol) / total_vol
    else:
        imbalance = 0.0

    if total_vol < 10_000:
        state = "QUIET"
    elif imbalance > 0.40:
        state = "LONG_FLUSH"
    elif imbalance < -0.40:
        state = "SHORT_SQUEEZE"
    else:
        state = "BALANCED"

    return {
        "instrument": instrument or "ALL",
        "window_hours": window_hours,
        "as_of": until,
        "long_notional_usd": round(long_vol, 2),
        "short_notional_usd": round(short_vol, 2),
        "total_notional_usd": round(total_vol, 2),
        "long_count": long_cnt,
        "short_count": short_cnt,
        "imbalance_ratio": round(imbalance, 4),
        "state": state,
    }


def liquidation_cascade_detector(
    conn: sqlite3.Connection,
    instrument: str = "BTC-USDT-SWAP",
    *,
    window_hours: int = 1,
    lookback_days: int = 30,
    z_threshold: float = 2.5,
    as_of: datetime | str | None = None,
) -> dict | None:
    """Detect anomalous liquidation spikes (cascades) relative to 30-day baseline.

    A high-volume long flush (>2.5σ) often marks seller capitulation / swing bottom.
    A high-volume short squeeze (>2.5σ) often marks buyer exhaustion / swing top.
    """
    current = liquidation_summary(
        conn, instrument=instrument, window_hours=window_hours, as_of=as_of
    )
    if not current:
        return None

    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of)
    else:
        target_dt = as_of

    since = (target_dt - timedelta(days=lookback_days)).isoformat(timespec="seconds")
    until = target_dt.isoformat(timespec="seconds")

    # Sample baseline hourly volumes
    hourly_rows = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:00:00', ts_utc) as hour_slot, "
        "       SUM(COALESCE(notional_usd_calculated, notional_usd, price * size, 0.0)) "
        "FROM crypto_liquidations "
        "WHERE instrument = ? AND ts_utc >= ? AND ts_utc <= ? "
        "GROUP BY hour_slot",
        (instrument, since, until),
    ).fetchall()

    volumes = [float(r[1]) for r in hourly_rows if r[1] is not None]

    if len(volumes) < 24:
        # Insufficient history for statistical z-score
        return {
            "instrument": instrument,
            "as_of": until,
            "current_volume_usd": current["total_notional_usd"],
            "z_score": None,
            "signal": "INSUFFICIENT_HISTORY",
            "summary": current,
        }

    mean_v = sum(volumes) / len(volumes)
    variance = sum((x - mean_v) ** 2 for x in volumes) / len(volumes)
    std_v = variance**0.5

    current_v = current["total_notional_usd"]
    z = (current_v - mean_v) / std_v if std_v > 0 else 0.0

    if z >= z_threshold:
        if current["state"] == "LONG_FLUSH":
            signal = "LIQUIDATION_CAPITULATION"  # oversold exhaustion setup
        elif current["state"] == "SHORT_SQUEEZE":
            signal = "SHORT_EXHAUSTION"  # overbought exhaustion setup
        else:
            signal = "HIGH_VOLUME_FLUSH"
    else:
        signal = "NORMAL"

    return {
        "instrument": instrument,
        "as_of": until,
        "current_volume_usd": current_v,
        "baseline_mean_usd": round(mean_v, 2),
        "z_score": round(z, 2),
        "signal": signal,
        "summary": current,
    }


def compute_cvd(
    conn: sqlite3.Connection,
    instrument: str = "BTC-USDT-SWAP",
    *,
    window_hours: int = 24,
    as_of: datetime | str | None = None,
) -> dict | None:
    """Compute Cumulative Volume Delta (CVD) from 1-minute taker trade flow."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_trade_flow_1m'"
    ).fetchone()
    if not row:
        return None

    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of)
    else:
        target_dt = as_of

    since = (target_dt - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    until = target_dt.isoformat(timespec="seconds")

    query = """
        SELECT minute_utc,
               COALESCE(buy_notional_usd, 0.0),
               COALESCE(sell_notional_usd, 0.0)
        FROM crypto_trade_flow_1m
        WHERE instrument = ? AND minute_utc >= ? AND minute_utc <= ?
        ORDER BY minute_utc ASC
    """
    rows = conn.execute(query, (instrument, since, until)).fetchall()
    if not rows:
        return None

    total_buy = 0.0
    total_sell = 0.0
    running_cvd = 0.0
    series = []

    for minute, buy, sell in rows:
        b = float(buy)
        s = float(sell)
        delta = b - s
        running_cvd += delta
        total_buy += b
        total_sell += s
        series.append(
            {
                "minute": minute,
                "delta_usd": round(delta, 2),
                "cvd_usd": round(running_cvd, 2),
            }
        )

    total_vol = total_buy + total_sell
    buy_ratio = total_buy / total_vol if total_vol > 0 else 0.5
    net_delta = total_buy - total_sell

    if buy_ratio > 0.55:
        state = "AGGRESSIVE_BUYING"
    elif buy_ratio < 0.45:
        state = "AGGRESSIVE_SELLING"
    else:
        state = "BALANCED"

    return {
        "instrument": instrument,
        "window_hours": window_hours,
        "as_of": until,
        "total_buy_usd": round(total_buy, 2),
        "total_sell_usd": round(total_sell, 2),
        "total_volume_usd": round(total_vol, 2),
        "net_delta_usd": round(net_delta, 2),
        "buy_ratio": round(buy_ratio, 4),
        "state": state,
        "points_count": len(series),
    }


def store_crypto_signals(conn: sqlite3.Connection) -> int:
    """Compute and persist crypto liquidation metrics to computed_signals."""
    if not _liquidations_ready(conn):
        return 0

    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")
    ts_date = now.date().isoformat()

    rows: list[tuple] = []
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        # 1. 24h summary
        sum_24h = liquidation_summary(conn, instrument=inst, window_hours=24, as_of=now)
        if sum_24h:
            sig_name = f"crypto_liq_24h_{inst[:3].lower()}"
            rows.append(
                (
                    sig_name,
                    ts_date,
                    "crypto",
                    now_iso,
                    sum_24h["total_notional_usd"],
                    sum_24h["state"],
                    json.dumps(sum_24h),
                )
            )

        # 2. 1h cascade detector
        cascade = liquidation_cascade_detector(conn, instrument=inst, window_hours=1, as_of=now)
        if cascade:
            sig_name = f"crypto_liq_cascade_{inst[:3].lower()}"
            rows.append(
                (
                    sig_name,
                    ts_date,
                    "crypto",
                    now_iso,
                    cascade["z_score"] if cascade["z_score"] is not None else 0.0,
                    cascade["signal"],
                    json.dumps(cascade),
                )
            )

        # 3. 24h CVD
        cvd = compute_cvd(conn, instrument=inst, window_hours=24, as_of=now)
        if cvd:
            sig_name = f"crypto_cvd_24h_{inst[:3].lower()}"
            rows.append(
                (
                    sig_name,
                    ts_date,
                    "crypto",
                    now_iso,
                    cvd["net_delta_usd"],
                    cvd["state"],
                    json.dumps(cvd),
                )
            )

    if not rows:
        return 0

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
