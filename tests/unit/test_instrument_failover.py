from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from arkwatch.qa import instruments
from arkwatch.transforms.synthetic import compute_xaugbp
from arkwatch.transforms.xccy import _get_spot


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE instrument_prices ("
        "symbol TEXT, ts TEXT, source TEXT, close REAL, "
        "PRIMARY KEY(symbol, ts, source))"
    )
    return conn


def test_eodhd_lookback_uses_from(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return []

    def fake_get(url, *, params, headers, timeout):
        seen.update(params)
        return Response()

    monkeypatch.setattr(instruments.requests, "get", fake_get)
    instruments.fetch_eodhd_daily("token", "BTC-USD.CC", days=7)
    assert "from" in seen
    assert "period" not in seen


def test_xccy_spot_falls_back_to_yahoo_on_same_date():
    conn = _conn()
    conn.execute("INSERT INTO instrument_prices VALUES ('EURUSD','2026-09-18','YAHOO',1.15)")
    assert _get_spot(conn, "2026-09-18") == 1.15


def test_xccy_spot_rejects_old_data():
    conn = _conn()
    conn.execute("INSERT INTO instrument_prices VALUES ('EURUSD','2026-09-10','EODHD',1.14)")
    assert _get_spot(conn, "2026-09-18") is None


def test_synthetic_prefers_eodhd_fx_then_yahoo():
    conn = _conn()
    today = datetime.now(UTC).date().isoformat()
    conn.executemany(
        "INSERT INTO instrument_prices VALUES (?,?,?,?)",
        [
            ("XAUUSD", today, "EODHD", 4300.0),
            ("GBPUSD", today, "YAHOO", 1.25),
        ],
    )
    assert compute_xaugbp(conn) == {"ts": today, "value": 3440.0}
