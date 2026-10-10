"""Compressed OKX trade archive and one-minute taker-flow aggregates."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

from .okx_market import normalize_contract_size

BATCH_SECONDS = 10


def _trade_id(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _utc(ts: str | int) -> datetime:
    return datetime.fromtimestamp(int(ts) / 1000, UTC)


class TradeFlowBuffer:
    def __init__(self, conn: sqlite3.Connection, specs: dict[str, dict]):
        self.conn = conn
        self.specs = specs
        self.highwater: dict[str, int] = {}
        self.legacy: dict[str, int] = {}
        self.pending: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        self.active_batch = ""
        state = dict(conn.execute("SELECT instrument,last_trade_id FROM crypto_trade_flow_state"))
        missing = set()
        for instrument, raw_id in conn.execute(
            "SELECT instrument,MAX(CAST(trade_id AS INTEGER)) FROM crypto_trade_events "
            "WHERE instrument NOT IN (SELECT instrument FROM crypto_trade_flow_state) GROUP BY instrument"
        ):
            self.legacy[instrument] = _trade_id(raw_id)
            missing.add(instrument)
        self._backfill_legacy()
        for instrument, raw_id in state.items():
            self.highwater[instrument] = _trade_id(raw_id)
        for instrument in missing:
            self.highwater[instrument] = max(
                self.highwater.get(instrument, -1), self.legacy.get(instrument, -1)
            )

    def _backfill_legacy(self) -> None:
        candidates = [
            instrument
            for instrument, last_id in self.legacy.items()
            if self.conn.execute(
                "SELECT 1 FROM crypto_trade_flow_state WHERE instrument=?", (instrument,)
            ).fetchone()
            is None
        ]
        if not candidates:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for instrument in candidates:
                if self.conn.execute(
                    "SELECT 1 FROM crypto_trade_flow_state WHERE instrument=?", (instrument,)
                ).fetchone():
                    continue
                grouped: dict[str, list[dict]] = defaultdict(list)
                rows = self.conn.execute(
                    "SELECT trade_id,ts_utc,aggressor_side,price,size_contracts,size_asset,notional_usd "
                    "FROM crypto_trade_events WHERE instrument=? ORDER BY ts_utc",
                    (instrument,),
                )
                spec = self.specs.get(instrument, {})
                for trade_id, ts, side, price, size, asset, notional in rows:
                    if price is None or size is None:
                        continue
                    stamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if asset is None and notional is None:
                        asset, notional = normalize_contract_size(float(size), float(price), spec)
                    minute = stamp.replace(second=0, microsecond=0).isoformat(timespec="seconds")
                    grouped[minute].append(
                        {
                            "tradeId": str(trade_id),
                            "ts": str(int(stamp.timestamp() * 1000)),
                            "side": side,
                            "px": str(price),
                            "sz": str(size),
                            "_sizeAsset": asset,
                            "_notionalUsd": notional,
                        }
                    )
                for minute, trades in grouped.items():
                    self._flow_row(instrument, minute, trades)
                self.conn.execute(
                    "INSERT INTO crypto_trade_flow_state VALUES (?,?,?)",
                    (
                        instrument,
                        str(self.legacy[instrument]),
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    ),
                )
            self.conn.execute("COMMIT")
        except BaseException:  # SIGTERM arrives as KeyboardInterrupt
            self.conn.execute("ROLLBACK")
            raise

    def add(self, rows: list[dict], *, source: str, instrument: str | None = None) -> int:
        parsed = []
        for row in rows:
            inst = str(row.get("instId") or instrument or "")
            trade_id = str(row.get("tradeId") or "")
            try:
                trade_id_num = _trade_id(trade_id)
                _utc(row["ts"])
                price = float(row["px"])
                size = float(row["sz"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not inst or not trade_id or trade_id_num < 0 or size < 0 or price <= 0:
                continue
            parsed.append((trade_id_num, inst, trade_id, row))
        parsed.sort(key=lambda item: (item[1], item[0]))
        accepted = 0
        now = datetime.now(UTC).replace(microsecond=0)
        batch_time = now.replace(second=now.second - now.second % BATCH_SECONDS)
        if self.active_batch and batch_time.isoformat() != self.active_batch:
            self.flush()
        self.active_batch = batch_time.isoformat()
        for trade_id_num, inst, _trade_id_text, row in parsed:
            if trade_id_num <= self.highwater.get(inst, -1):
                continue
            self.highwater[inst] = trade_id_num
            enriched = dict(row)
            enriched.setdefault("instId", inst)
            size = float(row["sz"])
            price = float(row["px"])
            asset_size, notional = normalize_contract_size(size, price, self.specs.get(inst, {}))
            enriched["_source"] = source
            enriched["_sizeAsset"] = asset_size
            enriched["_notionalUsd"] = notional
            key = (inst, source, self.active_batch)
            self.pending[key].append(enriched)
            accepted += 1
        return accepted

    def flush(self) -> int:
        if not self.pending:
            return 0
        accepted_by_inst: dict[str, list[dict]] = defaultdict(list)
        for (instrument, _source, _bucket), rows in self.pending.items():
            accepted_by_inst[instrument].extend(rows)
        persisted_by_source: dict[tuple[str, str], list[dict]] = defaultdict(list)
        flow_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        inserted = 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            state = dict(
                self.conn.execute("SELECT instrument,last_trade_id FROM crypto_trade_flow_state")
            )
            for instrument, rows in accepted_by_inst.items():
                last = max(_trade_id(state.get(instrument, "-1")), self.legacy.get(instrument, -1))
                fresh = [row for row in rows if _trade_id(str(row["tradeId"])) > last]
                fresh.sort(key=lambda row: _trade_id(str(row["tradeId"])))
                by_id = {}
                for row in fresh:
                    trade_id = str(row["tradeId"])
                    key = (instrument, trade_id)
                    prior = by_id.get(key)
                    if prior is None or (
                        row.get("_source") == "OKX_WS" and prior.get("_source") != "OKX_WS"
                    ):
                        by_id[key] = row
                accepted = list(by_id.values())
                if not accepted:
                    continue
                for row in accepted:
                    source = str(row.get("_source") or "OKX_WS")
                    persisted_by_source[(instrument, source)].append(row)
                    minute = (
                        _utc(row["ts"])
                        .replace(second=0, microsecond=0)
                        .isoformat(timespec="seconds")
                    )
                    flow_groups[(instrument, minute)].append(row)
                highest = max(_trade_id(str(row["tradeId"])) for row in accepted)
                self.conn.execute(
                    "INSERT INTO crypto_trade_flow_state VALUES (?,?,?) "
                    "ON CONFLICT(instrument) DO UPDATE SET last_trade_id=excluded.last_trade_id,updated_at=excluded.updated_at",
                    (instrument, str(highest), datetime.now(UTC).isoformat(timespec="seconds")),
                )
            for (instrument, source, bucket), _rows in self.pending.items():
                rows = persisted_by_source.get((instrument, source), [])
                if not rows:
                    continue
                rows.sort(key=lambda row: _trade_id(str(row["tradeId"])))
                raw_rows = [
                    {key: value for key, value in row.items() if not key.startswith("_")}
                    for row in rows
                ]
                raw = json.dumps(
                    raw_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                compressed = gzip.compress(raw, compresslevel=6, mtime=0)
                times = [_utc(row["ts"]) for row in rows]
                trade_ids = [str(row["tradeId"]) for row in rows]
                batch_id = hashlib.sha256(
                    f"OKX|{source}|{instrument}|{bucket}|{trade_ids[0]}|{trade_ids[-1]}".encode()
                ).hexdigest()
                self.conn.execute(
                    "INSERT OR IGNORE INTO crypto_trade_raw_batches "
                    "(batch_id,batch_ts_utc,source,instrument,trade_count,first_trade_ts,last_trade_ts,first_trade_id,last_trade_id,payload_gzip,payload_sha256,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        bucket,
                        source,
                        instrument,
                        len(rows),
                        min(times).isoformat(timespec="milliseconds"),
                        max(times).isoformat(timespec="milliseconds"),
                        trade_ids[0],
                        trade_ids[-1],
                        compressed,
                        hashlib.sha256(raw).hexdigest(),
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    ),
                )
            for (instrument, minute), rows in flow_groups.items():
                inserted += self._flow_row(instrument, minute, rows)
            self.conn.execute("COMMIT")
        except BaseException:  # keep `pending` for the shutdown flush
            self.conn.execute("ROLLBACK")
            raise
        self.pending.clear()
        return inserted

    def _flow_row(self, instrument: str, minute: str, rows: list[dict]) -> int:
        trades = []
        for row in rows:
            side = str(row.get("side") or "").lower()
            if side not in ("buy", "sell"):
                continue
            trades.append(
                (
                    side,
                    float(row["sz"]),
                    row.get("_sizeAsset"),
                    row.get("_notionalUsd"),
                    _utc(row["ts"]),
                    str(row["tradeId"]),
                )
            )
        if not trades:
            return 0
        buys = [trade for trade in trades if trade[0] == "buy"]
        sells = [trade for trade in trades if trade[0] == "sell"]
        first, last = (
            min(trades, key=lambda trade: trade[4]),
            max(trades, key=lambda trade: trade[4]),
        )
        buy_asset = sum(float(trade[2]) for trade in buys if trade[2] is not None)
        sell_asset = sum(float(trade[2]) for trade in sells if trade[2] is not None)
        buy_usd = sum(float(trade[3]) for trade in buys if trade[3] is not None)
        sell_usd = sum(float(trade[3]) for trade in sells if trade[3] is not None)
        buy_norm_count = sum(trade[2] is not None or trade[3] is not None for trade in buys)
        sell_norm_count = sum(trade[2] is not None or trade[3] is not None for trade in sells)
        fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO crypto_trade_flow_1m "
            "(minute_utc,instrument,source,trade_count,buy_count,sell_count,buy_contracts,sell_contracts,buy_asset,sell_asset,buy_notional_usd,sell_notional_usd,buy_normalized_count,sell_normalized_count,first_trade_ts,last_trade_ts,first_trade_id,last_trade_id,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(minute_utc,instrument,source) DO UPDATE SET "
            "trade_count=trade_count+excluded.trade_count,buy_count=buy_count+excluded.buy_count,sell_count=sell_count+excluded.sell_count,"
            "buy_contracts=buy_contracts+excluded.buy_contracts,sell_contracts=sell_contracts+excluded.sell_contracts,"
            "buy_asset=CASE WHEN excluded.buy_normalized_count>0 THEN COALESCE(buy_asset,0)+COALESCE(excluded.buy_asset,0) ELSE buy_asset END,"
            "sell_asset=CASE WHEN excluded.sell_normalized_count>0 THEN COALESCE(sell_asset,0)+COALESCE(excluded.sell_asset,0) ELSE sell_asset END,"
            "buy_notional_usd=CASE WHEN excluded.buy_normalized_count>0 THEN COALESCE(buy_notional_usd,0)+COALESCE(excluded.buy_notional_usd,0) ELSE buy_notional_usd END,"
            "sell_notional_usd=CASE WHEN excluded.sell_normalized_count>0 THEN COALESCE(sell_notional_usd,0)+COALESCE(excluded.sell_notional_usd,0) ELSE sell_notional_usd END,"
            "buy_normalized_count=buy_normalized_count+excluded.buy_normalized_count,sell_normalized_count=sell_normalized_count+excluded.sell_normalized_count,"
            "first_trade_ts=MIN(first_trade_ts,excluded.first_trade_ts),last_trade_ts=MAX(last_trade_ts,excluded.last_trade_ts),"
            "first_trade_id=CASE WHEN CAST(excluded.first_trade_id AS INTEGER)<CAST(first_trade_id AS INTEGER) THEN excluded.first_trade_id ELSE first_trade_id END,"
            "last_trade_id=CASE WHEN CAST(excluded.last_trade_id AS INTEGER)>CAST(last_trade_id AS INTEGER) THEN excluded.last_trade_id ELSE last_trade_id END,fetched_at=excluded.fetched_at",
            (
                minute,
                instrument,
                "OKX",
                len(trades),
                len(buys),
                len(sells),
                sum(trade[1] for trade in buys),
                sum(trade[1] for trade in sells),
                buy_asset if buy_norm_count else None,
                sell_asset if sell_norm_count else None,
                buy_usd if buy_norm_count else None,
                sell_usd if sell_norm_count else None,
                buy_norm_count,
                sell_norm_count,
                first[4].isoformat(timespec="milliseconds"),
                last[4].isoformat(timespec="milliseconds"),
                first[5],
                last[5],
                fetched_at,
            ),
        )
        return 1
