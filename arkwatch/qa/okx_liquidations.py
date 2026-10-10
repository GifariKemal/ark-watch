"""Persistent collector for OKX public liquidation events."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import signal
import time
from datetime import UTC, datetime

import websocket

from .. import db as _db
from .fetch_log import log_collection
from .okx_market import (
    REST_GAP_WARNING,
    _instrument_snapshot,
    instrument_specs,
    normalize_contract_size,
    rest_trades,
)
from .okx_tradeflow import TradeFlowBuffer

URL = "wss://ws.okx.com:8443/ws/v5/public"
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
BOOK_SAMPLE_SECONDS = 5


def _events(message: dict) -> list[dict]:
    out = []
    for parent in message.get("data") or []:
        details = parent.get("details") or [parent]
        for row in details:
            merged = {**parent, **row}
            instrument = str(
                merged.get("instId") or parent.get("instFamily") or parent.get("uly") or ""
            )
            raw_ts = merged.get("ts") or merged.get("uTime") or merged.get("pTime")
            if not instrument or raw_ts is None:
                continue
            stamp = datetime.fromtimestamp(int(raw_ts) / 1000, UTC).isoformat(
                timespec="milliseconds"
            )
            price = float(merged.get("bkPx") or merged.get("px") or 0) or None
            size = float(merged.get("sz") or 0) or None
            notional = float(merged["notionalUsd"]) if merged.get("notionalUsd") else None
            payload = json.dumps(merged, sort_keys=True, separators=(",", ":"))
            uid = hashlib.sha256(f"OKX|{instrument}|{stamp}|{payload}".encode()).hexdigest()
            out.append(
                {
                    "uid": uid,
                    "ts": stamp,
                    "instrument": instrument,
                    "side": merged.get("posSide") or merged.get("side"),
                    "price": price,
                    "size": size,
                    "notional": notional,
                    "raw": payload,
                }
            )
    return out


def _store(conn, events: list[dict], specs: dict[str, dict] | None = None) -> int:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    specs = specs or {}
    values = []
    for event in events:
        asset_size, calculated_notional = (
            normalize_contract_size(
                event["size"], event["price"], specs.get(event["instrument"], {})
            )
            if event["size"]
            else (None, None)
        )
        values.append(
            (
                event["uid"],
                event["ts"],
                "OKX",
                event["instrument"],
                event["side"],
                event["price"],
                event["size"],
                event["notional"],
                event["raw"],
                now,
                asset_size,
                calculated_notional,
                "instrument_contract_value"
                if asset_size is not None or calculated_notional is not None
                else None,
            )
        )
    if not values:
        return 0
    return conn.executemany(
        "INSERT OR IGNORE INTO crypto_liquidations (event_uid,ts_utc,source,instrument,position_side,price,size,notional_usd,raw_json,fetched_at,size_asset,notional_usd_calculated,sizing_basis) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    ).rowcount


def _book(conn, message: dict, specs: dict[str, dict], last_saved: dict[str, float]) -> int:
    arg = message.get("arg") or {}
    instrument = str(arg.get("instId") or "")
    if not instrument or time.monotonic() - last_saved.get(instrument, 0) < BOOK_SAMPLE_SECONDS:
        return 0
    rows = message.get("data") or []
    if not rows:
        return 0
    row = rows[-1]
    bids = row.get("bids") or []
    asks = row.get("asks") or []
    if not bids or not asks:
        return 0
    bid_size = sum(float(level[1]) for level in bids[:5])
    ask_size = sum(float(level[1]) for level in asks[:5])
    bid_notional = sum(
        (
            normalize_contract_size(float(level[1]), float(level[0]), specs.get(instrument, {}))[1]
            or 0.0
        )
        for level in bids[:5]
    )
    ask_notional = sum(
        (
            normalize_contract_size(float(level[1]), float(level[0]), specs.get(instrument, {}))[1]
            or 0.0
        )
        for level in asks[:5]
    )
    total = bid_size + ask_size
    stamp = datetime.fromtimestamp(int(row["ts"]) / 1000, UTC).isoformat(timespec="milliseconds")
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
    cursor = conn.execute(
        "INSERT OR IGNORE INTO crypto_orderbook_snapshots "
        "(ts_utc,source,instrument,bid_size_top5,ask_size_top5,imbalance_top5,raw_json,fetched_at,bid_notional_usd_top5,ask_notional_usd_top5,imbalance_notional_usd_top5) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            stamp,
            "OKX_WS",
            instrument,
            bid_size,
            ask_size,
            (bid_size - ask_size) / total if total else None,
            payload,
            datetime.now(UTC).isoformat(timespec="seconds"),
            bid_notional or None,
            ask_notional or None,
            (bid_notional - ask_notional) / (bid_notional + ask_notional)
            if bid_notional + ask_notional
            else None,
        ),
    )
    last_saved[instrument] = time.monotonic()
    return cursor.rowcount


def _trades(message: dict, buffer: TradeFlowBuffer) -> int:
    arg = message.get("arg") or {}
    return buffer.add(message.get("data") or [], source="OKX_WS", instrument=arg.get("instId"))


def _store_market_message(
    conn,
    message: dict,
    specs: dict[str, dict],
    last_book_saved: dict[str, float],
    trade_buffer: TradeFlowBuffer,
) -> int:
    channel = (message.get("arg") or {}).get("channel")
    if channel == "trades":
        return _trades(message, trade_buffer)
    if channel == "books5":
        return _book(conn, message, specs, last_book_saved)
    return 0


def collect(db_path: str, seconds: int | None = None) -> int:
    deadline = time.monotonic() + seconds if seconds else None
    stored = 0
    conn = _db.get_conn(db_path, allow_init=True)
    ws = None
    trade_buffer = None
    try:
        try:
            _instrument_snapshot(conn, datetime.now(UTC))
        except Exception as ex:
            log_collection(conn, "okx_liquidations", "OKX:SWAP:instruments", None, 0, err=str(ex))
        specs = instrument_specs(conn)
        trade_buffer = TradeFlowBuffer(conn, specs)
        last_book_saved: dict[str, float] = {}
        ws = websocket.create_connection(URL, timeout=25)
        args = [{"channel": "liquidation-orders", "instType": "SWAP"}]
        args.extend(
            {"channel": channel, "instId": instrument}
            for channel in ("trades", "books5")
            for instrument in INSTRUMENTS
        )
        ws.send(json.dumps({"op": "subscribe", "args": args}))
        # close the disconnect gap: REST trades go in before any queued WS
        # message, the buffer highwater then drops the overlap
        for instrument in INSTRUMENTS:
            try:
                count, gap = rest_trades(instrument, trade_buffer)
                log_collection(
                    conn,
                    "okx_liquidations",
                    f"OKX:{instrument}:trades-rest-backfill",
                    None,
                    count,
                    err=REST_GAP_WARNING if gap else None,
                    status="DEGRADED" if gap else "OK",
                )
            except Exception as ex:
                log_collection(
                    conn,
                    "okx_liquidations",
                    f"OKX:{instrument}:trades-rest-backfill",
                    None,
                    0,
                    err=str(ex),
                )
        pinged = False
        while deadline is None or time.monotonic() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                # books5 pushes several times a second: a silent socket after a
                # ping is a dead TCP path the OS has not noticed yet
                if pinged:
                    raise ConnectionError("OKX WebSocket silent after ping") from None
                ws.send("ping")
                pinged = True
                continue
            pinged = False
            if raw == "pong":
                continue
            message = json.loads(raw)
            if message.get("event") == "error":
                raise RuntimeError(
                    f"OKX subscription error {message.get('code')}: {message.get('msg')}"
                )
            if message.get("event") == "subscribe":
                channel = (message.get("arg") or {}).get("channel", "unknown")
                log_collection(conn, "okx_liquidations", f"OKX:{channel}", message, 0, status="OK")
            channel = (message.get("arg") or {}).get("channel")
            if channel == "liquidation-orders":
                stored += _store(conn, _events(message), specs)
            else:
                stored += _store_market_message(conn, message, specs, last_book_saved, trade_buffer)
    finally:
        try:
            if trade_buffer is not None:
                trade_buffer.flush()
        finally:
            try:
                if ws is not None:
                    ws.close()
            finally:
                conn.close()
    return stored


def run_forever(db_path: str) -> None:
    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    delay = 2
    while True:
        started = time.monotonic()
        try:
            collect(db_path)
        except Exception as ex:
            # a session that streamed for a while was healthy: restart the backoff
            if time.monotonic() - started > 60:
                delay = 2
            wait = delay * random.uniform(0.5, 1.0)
            print(
                f"OKX liquidation reconnect in {wait:.1f}s: {type(ex).__name__}: {str(ex)[:160]}",
                flush=True,
            )
            time.sleep(wait)
            delay = min(delay * 2, 60)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arkwatch liquidations")
    parser.add_argument("--db", default="data/arkwatch.db")
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args(argv)
    if args.seconds:
        print(f"Memonitor likuidasi OKX selama {args.seconds} detik...")
        print({"stored": collect(args.db, args.seconds)})
    else:
        print("Mendengarkan stream WebSocket likuidasi real-time OKX (stream continuous)...")
        print(
            "Petunjuk: Gunakan 'arkwatch liquidations --seconds 10' untuk sampling, atau tekan Ctrl+C untuk berhenti.",
            flush=True,
        )
        try:
            run_forever(args.db)
        except KeyboardInterrupt:  # SIGTERM/Ctrl+C: collect() already flushed
            print("OKX liquidation collector stopped", flush=True)
    return 0
