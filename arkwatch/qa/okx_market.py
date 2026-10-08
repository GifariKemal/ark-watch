"""Public OKX derivative snapshots and bounded REST recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .. import db as _db
from .fetch_log import log_collection

if TYPE_CHECKING:
    from .okx_tradeflow import TradeFlowBuffer

BASE_URL = "https://www.okx.com/api/v5"
SWAPS = {"BTC-USDT-SWAP": "BTC-USDT", "ETH-USDT-SWAP": "ETH-USDT"}
SESSION = requests.Session()
SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            connect=3,
            read=2,
            status=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
    ),
)


def _get(path: str, params: dict) -> list[dict]:
    response = SESSION.get(f"{BASE_URL}/{path}", params=params, timeout=(5, 20))
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX {path}: {payload.get('code')} {payload.get('msg', '')}")
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError(f"OKX {path}: empty response")
    return rows


def _stamp(raw: str | int) -> str:
    return datetime.fromtimestamp(int(raw) / 1000, UTC).isoformat(timespec="milliseconds")


def _store_metric(
    conn, instrument: str, metric: str, value, raw: dict, stamp: str | None = None
) -> None:
    if value in (None, ""):
        return
    conn.execute(
        "INSERT OR IGNORE INTO crypto_derivatives VALUES (?,?,?,?,?,?)",
        (
            stamp or _stamp(raw["ts"]),
            "OKX",
            instrument,
            metric,
            float(value),
            json.dumps(raw, sort_keys=True, separators=(",", ":")),
        ),
    )


def _instrument_snapshot(conn, now: datetime) -> int:
    latest = conn.execute(
        "SELECT MAX(observed_at_utc) FROM crypto_instruments WHERE source='OKX'"
    ).fetchone()[0]
    if latest:
        last = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        if now - last < timedelta(hours=24):
            return 0
    rows = _get("public/instruments", {"instType": "SWAP"})
    selected = [row for row in rows if row.get("state") == "live"]
    live_ids = {row.get("instId") for row in selected}
    if not set(SWAPS).issubset(live_ids):
        raise RuntimeError("OKX instrument metadata missing BTC or ETH swap")
    stamp = now.isoformat(timespec="seconds")
    values = [
        (row["instId"], "OKX", stamp, json.dumps(row, sort_keys=True, separators=(",", ":")))
        for row in selected
    ]
    conn.executemany("INSERT OR IGNORE INTO crypto_instruments VALUES (?,?,?,?)", values)
    return len(values)


def instrument_specs(conn) -> dict[str, dict]:
    specs = {}
    for instrument, raw in conn.execute(
        "SELECT instrument,raw_json FROM crypto_instruments WHERE source='OKX' "
        "AND observed_at_utc=(SELECT MAX(i2.observed_at_utc) FROM crypto_instruments i2 "
        "WHERE i2.instrument=crypto_instruments.instrument AND i2.source='OKX')"
    ):
        try:
            specs[instrument] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
    return specs


def normalize_contract_size(
    size: float, price: float | None, spec: dict
) -> tuple[float | None, float | None]:
    try:
        contracts = float(size)
        contract_value = float(spec["ctVal"]) * float(spec.get("ctMult") or 1)
    except (KeyError, TypeError, ValueError):
        return None, None
    currency = str(spec.get("ctValCcy") or "").upper()
    if currency in {
        str(spec.get("baseCcy") or "").upper(),
        str(spec.get("instId") or "").split("-")[0].upper(),
    }:
        asset_size = contracts * contract_value
        return asset_size, asset_size * price if price and price > 0 else None
    if currency in {"USD", "USDT", "USDC"}:
        notional = contracts * contract_value
        return None, notional
    return None, None


def _snapshot(
    conn, instrument: str, path: str, params: dict, metric_names: tuple[tuple[str, str], ...]
) -> int:
    rows = _get(path, params)
    saved = 0
    for row in rows:
        raw_ts = row.get("ts")
        stamp = _stamp(raw_ts or row.get("fundingTime") or row.get("nextFundingTime"))
        if raw_ts:
            age = datetime.now(UTC) - datetime.fromtimestamp(int(raw_ts) / 1000, UTC)
            if age > timedelta(minutes=10) or age < -timedelta(minutes=1):
                raise RuntimeError(
                    f"OKX {path} returned a stale or future snapshot for {instrument}"
                )
        for field, metric in metric_names:
            before = conn.total_changes
            _store_metric(conn, instrument, metric, row.get(field), row, stamp)
            saved += int(conn.total_changes > before)
    return saved


def _book_snapshot(conn, instrument: str) -> int:
    row = _get("market/books", {"instId": instrument, "sz": "5"})[0]
    bids = row.get("bids") or []
    asks = row.get("asks") or []
    if not bids or not asks:
        raise RuntimeError(f"OKX order book incomplete for {instrument}")
    bid_size = sum(float(level[1]) for level in bids[:5])
    ask_size = sum(float(level[1]) for level in asks[:5])
    denom = bid_size + ask_size
    spec = instrument_specs(conn).get(instrument, {})
    bid_notional = sum(
        (normalize_contract_size(float(level[1]), float(level[0]), spec)[1] or 0.0)
        for level in bids[:5]
    )
    ask_notional = sum(
        (normalize_contract_size(float(level[1]), float(level[0]), spec)[1] or 0.0)
        for level in asks[:5]
    )
    ts = _stamp(row["ts"])
    now = datetime.now(UTC).isoformat(timespec="seconds")
    raw = json.dumps(row, sort_keys=True, separators=(",", ":"))
    cursor = conn.execute(
        "INSERT OR IGNORE INTO crypto_orderbook_snapshots "
        "(ts_utc,source,instrument,bid_size_top5,ask_size_top5,imbalance_top5,raw_json,fetched_at,bid_notional_usd_top5,ask_notional_usd_top5,imbalance_notional_usd_top5) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts,
            "OKX",
            instrument,
            bid_size,
            ask_size,
            (bid_size - ask_size) / denom if denom else None,
            raw,
            now,
            bid_notional or None,
            ask_notional or None,
            (bid_notional - ask_notional) / (bid_notional + ask_notional)
            if bid_notional + ask_notional
            else None,
        ),
    )
    return cursor.rowcount


def _trade_recovery(conn, instrument: str, buffer: TradeFlowBuffer) -> tuple[int, bool] | None:
    latest = conn.execute(
        "SELECT MAX(batch_ts_utc) FROM crypto_trade_raw_batches WHERE instrument=? AND source='OKX_WS'",
        (instrument,),
    ).fetchone()[0]
    if latest and datetime.now(UTC) - datetime.fromisoformat(latest) <= timedelta(seconds=60):
        return None
    rows = _get("market/trades", {"instId": instrument, "limit": "500"})
    return buffer.add(rows, source="OKX_REST", instrument=instrument), len(rows) >= 500


def collect(db_path: str) -> dict[str, int]:
    from .okx_tradeflow import TradeFlowBuffer

    conn = _db.get_conn(db_path, allow_init=True)
    out = {}
    try:
        try:
            out["instruments"] = _instrument_snapshot(conn, datetime.now(UTC))
            log_collection(
                conn,
                "okx_market",
                "OKX:SWAP:instruments",
                None,
                out["instruments"],
                status="OK" if out["instruments"] == 0 else None,
            )
        except Exception as ex:
            out["instruments"] = -1
            log_collection(conn, "okx_market", "OKX:SWAP:instruments", None, 0, err=str(ex))
        endpoints = (
            (
                "open-interest",
                "public/open-interest",
                "instType",
                (
                    ("oi", "open_interest_contracts"),
                    ("oiCcy", "open_interest_asset"),
                    ("oiUsd", "open_interest_usd"),
                ),
            ),
            (
                "funding-rate",
                "public/funding-rate",
                "instId",
                (("fundingRate", "funding_rate"), ("nextFundingRate", "next_funding_rate")),
            ),
            ("mark-price", "public/mark-price", "instType", (("markPx", "mark_price"),)),
            ("index-price", "market/index-tickers", "instId", (("idxPx", "index_price"),)),
        )
        trade_buffer = TradeFlowBuffer(conn, instrument_specs(conn))
        for instrument, index_id in SWAPS.items():
            for name, path, mode, fields in endpoints:
                try:
                    params = (
                        {"instType": "SWAP", "instId": instrument}
                        if mode == "instType"
                        else {"instId": instrument if name == "funding-rate" else index_id}
                    )
                    if name == "open-interest":
                        params = {"instType": "SWAP", "instId": instrument}
                    count = _snapshot(conn, instrument, path, params, fields)
                    out[f"{instrument}:{name}"] = count
                    log_collection(conn, "okx_market", f"OKX:{instrument}:{name}", None, count)
                except Exception as ex:
                    out[f"{instrument}:{name}"] = -1
                    log_collection(
                        conn, "okx_market", f"OKX:{instrument}:{name}", None, 0, err=str(ex)
                    )
            for name, fetch in (
                ("orderbook-rest", _book_snapshot),
                ("trades-rest-recovery", _trade_recovery),
            ):
                try:
                    count = (
                        fetch(conn, instrument, trade_buffer)
                        if name == "trades-rest-recovery"
                        else fetch(conn, instrument)
                    )
                    if count is None:
                        out[f"{instrument}:{name}"] = 0
                        log_collection(
                            conn, "okx_market", f"OKX:{instrument}:{name}", None, 0, status="OK"
                        )
                        continue
                    degraded = isinstance(count, tuple)
                    if degraded:
                        count, capped = count
                        degraded = bool(capped)
                    out[f"{instrument}:{name}"] = count
                    warning = (
                        "REST returned its 500-trade cap; gap coverage is uncertain"
                        if degraded
                        else None
                    )
                    log_collection(
                        conn,
                        "okx_market",
                        f"OKX:{instrument}:{name}",
                        None,
                        count,
                        err=warning,
                        status="DEGRADED" if degraded else None,
                    )
                except Exception as ex:
                    out[f"{instrument}:{name}"] = -1
                    log_collection(
                        conn, "okx_market", f"OKX:{instrument}:{name}", None, 0, err=str(ex)
                    )
        trade_buffer.flush()
        return out
    finally:
        conn.close()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="arkwatch okx-market")
    parser.add_argument("--db", default="data/arkwatch.db")
    result = collect(parser.parse_args(argv).db)
    print(result)
    return 1 if all(value < 0 for value in result.values()) else 0
