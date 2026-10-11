"""kalshi fetcher: price choice, nearest event, storage shape, partial failure."""

import json

from arkwatch import db
from arkwatch.fetchers import kalshi


def _m(ticker, sub, bid, ask, last, vol, close="2026-10-28T17:59:00Z", status="active"):
    return {
        "ticker": ticker,
        "yes_sub_title": sub,
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "last_price_dollars": last,
        "volume_fp": vol,
        "close_time": close,
        "status": status,
    }


def test_probability_uses_a_tight_mid_else_the_last_trade():
    assert kalshi.probability(_m("t", "s", "0.83", "0.85", "0.80", "1")) == 0.84
    assert kalshi.probability(_m("t", "s", "0.10", "0.60", "0.42", "1")) == 0.42  # wide quote
    assert kalshi.probability(_m("t", "s", None, None, "x", "1")) is None


def test_nearest_event_and_most_traded_markets():
    dec = {
        "series_ticker": "KXFEDDECISION",
        "title": "Fed decision in Dec 2026?",
        "markets": [_m("D-H0", "Hold", "0.60", "0.62", "0.6", "900", close="2026-12-09T19:00:00Z")],
    }
    octo = {
        "series_ticker": "KXFEDDECISION",
        "title": "Fed decision in Oct 2026?",
        "markets": [
            _m("O-H0", "Hold", "0.83", "0.85", "0.84", "500"),
            _m("O-C25", "Cut 25bps", "0.14", "0.16", "0.15", "800"),
            _m("O-H25", "Hike 25bps", "0.00", "0.01", "0.01", "50", status="closed"),
        ],
    }
    rows = kalshi.nearest_markets([dec, octo], keep=5)
    assert [(r["ticker"], r["p"]) for r in rows] == [("O-C25", 0.15), ("O-H0", 0.84)]
    assert rows[0]["question"] == "Fed decision in Oct 2026?: Cut 25bps"


def test_run_stores_odds_rows_and_marks_a_partial_outage_degraded(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "k.db", allow_init=True)
    ev = {"series_ticker": "KXRECSSNBER", "title": "Recession this year?",
          "markets": [_m("KXRECSSNBER-26", "Yes", "0.05", "0.07", "0.06", "1200")]}  # fmt: skip

    def fake(series):
        if series == "KXCPIYOY":
            raise kalshi.KalshiError("kalshi KXCPIYOY: HTTP 503")
        return [ev] if series == "KXRECSSNBER" else []

    monkeypatch.setattr(kalshi, "fetch_series", fake)
    assert kalshi.run(conn) == 1
    sid, value, topic, inputs = conn.execute(
        "SELECT signal_id, value, state, inputs_json FROM computed_signals"
    ).fetchone()
    assert (sid, round(value, 4), topic) == ("kalshi:kxrecssnber-26", 0.06, "recession")
    d = json.loads(inputs)
    assert (d["venue"], d["volume_unit"], d["url"]) == (
        "kalshi",
        "contracts",
        "https://kalshi.com/markets/kxrecssnber",
    )
    status, err = conn.execute(
        "SELECT status, error FROM fetch_log WHERE fetcher='kalshi'"
    ).fetchone()
    assert status == "DEGRADED" and "KXCPIYOY: HTTP 503" in err
