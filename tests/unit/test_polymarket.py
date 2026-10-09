"""Polymarket crowd probabilities: Gamma API -> computed_signals (local fake server)."""

from __future__ import annotations

import http.server
import json
import threading
from datetime import datetime

import pytest

from arkwatch import api, db
from arkwatch.daemon import SCHEDULE, WIB, _due_jobs
from arkwatch.fetchers import polymarket


def _m(slug, question, volume, outcomes='["Yes", "No"]', prices='["0.25", "0.75"]'):
    return {
        "slug": slug,
        "question": question,
        "volumeNum": volume,
        "outcomes": outcomes,
        "outcomePrices": prices,
        "endDate": "2026-12-31T00:00:00Z",
    }


MARKETS = [
    _m("fed-cut-dec", "Will the Fed cut interest rates in December?", 5_000_000),
    _m("fed-hike-dec", "Will the Fed hike rates in December?", 900_000),
    _m("fed-cut-jan", "Fed rate cut in January?", 800_000),
    _m("fed-cut-mar", "Fed rate cut in March?", 700_000),  # 4th fed: beyond top 3
    _m("fed-tiny", "Will the Fed cut rates at an emergency meeting?", 10_000),  # < min_volume
    _m("btc-150k", "Will Bitcoin reach $150k in 2026?", 2_000_000, prices='["0.1", "0.9"]'),
    _m("bad-json", "Will Bitcoin hit $1m?", 3_000_000, outcomes="not json"),
    _m("bad-len", "Will Bitcoin dip?", 3_000_000, prices='["0.5"]'),
    _m("bad-price", "Will Bitcoin pump?", 3_000_000, prices='["x", "y"]'),
    {"slug": "no-question", "volumeNum": 9e9},
    _m("celebrity", "Will a celebrity get married?", 9_000_000),  # no topic
    _m("Weird/Slug?", "Will gold close above $4000?", 600_000),
]


@pytest.fixture()
def gamma(monkeypatch):
    """Fake Gamma /markets; set state['status'] / state['body'] to change the reply."""
    state = {"status": 200, "body": json.dumps(MARKETS).encode(), "hits": 0, "path": ""}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"] += 1
            state["path"] = self.path
            body = state["body"]
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(polymarket, "URL", f"http://127.0.0.1:{srv.server_port}/markets")
    yield state
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "arkwatch.db", allow_init=True)
    yield c
    c.close()


def _signals(c):
    return dict(
        c.execute(
            "SELECT signal_id, value FROM computed_signals WHERE signal_id LIKE 'polymarket:%'"
        ).fetchall()
    )


def test_topic_matching_by_tokens():
    topics = polymarket.load_topics()
    assert polymarket.match_topic("Will the Fed cut interest rates?", topics) == "fed"
    assert polymarket.match_topic("S&P 500 above 7000 on Friday?", topics) == "sp500"
    assert polymarket.match_topic("US government shutdown by Nov 1?", topics) == "us_shutdown"
    assert polymarket.match_topic("Will a celebrity get married?", topics) is None
    # whole tokens only: 'golden' is not 'gold'
    assert polymarket.match_topic("Golden State wins the title?", topics) is None


def test_run_stores_top_markets_and_skips_malformed(gamma, conn):
    assert polymarket.run(conn) == 5
    assert _signals(conn) == {
        "polymarket:fed-cut-dec": 0.25,
        "polymarket:fed-hike-dec": 0.25,
        "polymarket:fed-cut-jan": 0.25,
        "polymarket:btc-150k": 0.1,
        "polymarket:weird-slug-": 0.25,
    }
    q = gamma["path"]
    assert "active=true" in q and "closed=false" in q and "order=volumeNum" in q
    inputs = json.loads(
        conn.execute(
            "SELECT inputs_json FROM computed_signals WHERE signal_id='polymarket:btc-150k'"
        ).fetchone()[0]
    )
    assert inputs["topic"] == "bitcoin"
    assert inputs["outcomes"] == ["Yes", "No"]
    assert inputs["volume"] == 2_000_000
    assert inputs["end_date"] == "2026-12-31T00:00:00Z"
    assert inputs["url"] == "https://polymarket.com/market/btc-150k"
    log = conn.execute("SELECT status, rows FROM fetch_log WHERE fetcher='polymarket'").fetchall()
    assert log == [("OK", 5)]


def test_rerun_is_an_idempotent_upsert(gamma, conn):
    polymarket.run(conn)
    polymarket.run(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id LIKE 'polymarket:%'"
    ).fetchone()[0]
    assert n == 5


def test_signal_listing_through_api(gamma, conn):
    polymarket.run(conn)
    page = api.list_signals(conn, prefix="polymarket:")
    assert {r["signal_id"] for r in page["items"]} == set(_signals(conn))


def test_cli_http_error_is_logged_redacted_and_exits_1(gamma, tmp_path, capsys):
    gamma["status"], gamma["body"] = 503, b"busy"
    path = tmp_path / "a.db"
    assert polymarket.main(["--db", str(path)]) == 1
    assert gamma["hits"] == 2  # one retry
    c = db.get_conn(path)
    status, err = c.execute("SELECT status, error FROM fetch_log").fetchone()
    c.close()
    assert status == "ERROR" and "503" in err


def test_cli_empty_body_and_empty_list_exit_0(gamma, tmp_path):
    path = tmp_path / "a.db"
    gamma["body"] = b"[]"
    assert polymarket.main(["--db", str(path)]) == 0
    gamma["body"] = json.dumps([_m("x", "Will a celebrity win?", 1e9)]).encode()
    assert polymarket.main(["--db", str(path)]) == 0
    gamma["body"] = b""
    assert polymarket.main(["--db", str(path)]) == 1  # unparseable body = ERROR
    c = db.get_conn(path)
    rows = c.execute("SELECT status FROM fetch_log ORDER BY id").fetchall()
    c.close()
    assert [r[0] for r in rows] == ["EMPTY", "EMPTY", "ERROR"]


def test_cli_dispatch(monkeypatch):
    import sys

    from arkwatch import __main__

    monkeypatch.setattr(polymarket, "main", lambda argv: 7)
    monkeypatch.setattr(sys, "argv", ["arkwatch", "polymarket"])
    assert __main__.main() == 7


def test_hourly_schedule_entry():
    assert any(
        day == "hourly" and cmd == "polymarket" and m == 20 for _h, m, day, cmd, _d in SCHEDULE
    )
    at = datetime(2026, 10, 9, 14, 25, tzinfo=WIB)
    keys = [k for cmd, _d, k in _due_jobs(at, {}) if cmd == "polymarket"]
    assert keys == ["1420-polymarket@2026-10-09"]  # only this hour, never a replay of earlier hours
    assert not [c for c, _d, _k in _due_jobs(at.replace(minute=19), {}) if c == "polymarket"]
    assert not [c for c, _d, _k in _due_jobs(at, {keys[0]: "1"}) if c == "polymarket"]
