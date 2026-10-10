from datetime import UTC, datetime

import pytest
import websocket

from arkwatch import db
from arkwatch.qa import okx_liquidations, okx_market
from arkwatch.qa.okx_tradeflow import TradeFlowBuffer

INST = "BTC-USDT-SWAP"


def _trades(first: int, last: int) -> list[dict]:
    return [
        {
            "instId": INST,
            "tradeId": str(i),
            "px": "100",
            "sz": "1",
            "side": "buy",
            "ts": "1790000000000",
        }
        for i in range(last, first - 1, -1)
    ]


class _Buffer:
    def __init__(self, highwater):
        self.highwater = {INST: highwater} if highwater is not None else {}

    def add(self, rows, **_kw):
        return len(rows)


@pytest.mark.parametrize(
    ("highwater", "first", "gap"),
    [
        (None, 1001, False),  # nothing stored yet: no gap to cover
        (1000, 1001, False),  # contiguous with the highwater
        (1100, 1001, False),  # overlap: the cap reached back past the highwater
        (999, 1001, True),  # trade 1000 may be missing
    ],
)
def test_rest_gap_flag_only_when_trades_miss_the_highwater(monkeypatch, highwater, first, gap):
    monkeypatch.setattr(okx_market, "_get", lambda *_a: _trades(first, first + 499))
    assert okx_market.rest_trades(INST, _Buffer(highwater)) == (500, gap)


def test_trade_recovery_skipped_while_ws_batches_are_fresh(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "okx.db", allow_init=True)
    monkeypatch.setattr(okx_market, "rest_trades", lambda *_a: (7, False))
    assert okx_market._trade_recovery(conn, INST, None) == (7, False)
    conn.execute(
        "INSERT INTO crypto_trade_raw_batches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("b1", datetime.now(UTC).isoformat(), "OKX_WS", INST, 1, "", "", "1", "1", b"", "", ""),
    )
    assert okx_market._trade_recovery(conn, INST, None) is None
    conn.close()


class _SilentSocket:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        raise websocket.WebSocketTimeoutException("timed out")

    def close(self):
        pass


def test_collector_gives_up_on_a_socket_silent_after_ping(tmp_path, monkeypatch):
    sock = _SilentSocket()
    monkeypatch.setattr(okx_liquidations.websocket, "create_connection", lambda *_a, **_k: sock)
    monkeypatch.setattr(okx_liquidations, "_instrument_snapshot", lambda *_a: 0)
    backfilled = []
    monkeypatch.setattr(
        okx_liquidations, "rest_trades", lambda inst, buf: backfilled.append(inst) or (0, False)
    )
    with pytest.raises(ConnectionError):
        okx_liquidations.collect(str(tmp_path / "okx.db"))
    assert sock.sent[-1] == "ping" and sock.sent.count("ping") == 1
    assert backfilled == list(okx_liquidations.INSTRUMENTS)


def test_interrupted_flush_keeps_pending_for_the_shutdown_flush(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "okx.db", allow_init=True)
    buffer = TradeFlowBuffer(conn, {})
    buffer.add(_trades(1, 2), source="OKX_WS", instrument=INST)
    real_flow_row = buffer._flow_row

    def interrupted(*_a):
        raise KeyboardInterrupt

    monkeypatch.setattr(buffer, "_flow_row", interrupted)
    with pytest.raises(KeyboardInterrupt):
        buffer.flush()
    monkeypatch.setattr(buffer, "_flow_row", real_flow_row)
    assert buffer.flush() == 1
    assert conn.execute("SELECT COUNT(*) FROM crypto_trade_raw_batches").fetchone()[0] == 1
    conn.close()
