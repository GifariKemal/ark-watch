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
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
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


def test_cli_http_error_is_logged_redacted_and_exits_0(gamma, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(polymarket.time, "sleep", lambda _s: None)  # skip the 1-2 s retry backoff
    gamma["status"], gamma["body"] = 503, b"busy"
    path = tmp_path / "a.db"
    assert polymarket.main(["--db", str(path)]) == 0  # extra source: logged, never pages the phone
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
    assert polymarket.main(["--db", str(path)]) == 0  # unparseable body = ERROR row, still no page
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


def test_pagination_walks_pages_until_volume_drops_below_minimum(monkeypatch):
    """The Gamma API silently caps `limit` at 100 rows: macro markets (Fed, recession) sit on
    later pages. Pages are walked by offset until a short page or volume under the minimum."""
    calls: list[str] = []

    def page(offset):
        # 250 rows, descending volume; rows from index 120 on are below the 50k minimum
        rows = []
        for i in range(offset, min(offset + 100, 250)):
            vol = 900_000 - i * 5_000 if i < 120 else 1_000
            rows.append(
                _m(
                    f"m{i}",
                    "Will the Fed cut rates in 2026?" if i == 105 else f"Will thing {i}?",
                    vol,
                )
            )
        return rows

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            off = int(self.path.split("offset=")[1].split("&")[0]) if "offset=" in self.path else 0
            body = json.dumps(page(off)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    monkeypatch.setattr(polymarket, "URL", f"http://127.0.0.1:{srv.server_port}/markets")
    monkeypatch.setattr(polymarket.time, "sleep", lambda _s: None)
    try:
        rows = polymarket.fetch_markets()
    finally:
        srv.shutdown()
        srv.server_close()
    assert any("m105" in (r.get("slug") or "") for r in rows)  # a row only on page 2 is reached
    assert len(calls) == 2  # page 2 ends below the minimum volume, so page 3 is never requested
    assert all("limit=100" in c for c in calls)


# --- retry policy: transient only, with backoff ----------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "hits", "waits"),
    [
        (429, {"Retry-After": "3"}, 2, [3.0]),
        (429, {"Retry-After": "120"}, 2, [polymarket.RETRY_AFTER_CAP_S]),
        (503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 2, ["jitter"]),
        (502, {}, 2, ["jitter"]),
        (404, {}, 1, []),  # a 4xx will not fix itself: no retry, no wait
        (403, {"Retry-After": "3"}, 1, []),
    ],
)
def test_get_page_retries_only_transient_status_with_backoff(
    monkeypatch, status, headers, hits, waits
):
    seen = {"hits": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["hits"] += 1
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    slept: list[float] = []
    monkeypatch.setattr(polymarket.time, "sleep", slept.append)
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        with pytest.raises(polymarket.PolymarketError, match=f"HTTP {status}"):
            polymarket._get_page(f"http://127.0.0.1:{srv.server_port}/markets", 0)
    finally:
        srv.shutdown()
        srv.server_close()
    assert seen["hits"] == hits
    assert len(slept) == len(waits)
    for got, want in zip(slept, waits, strict=True):
        assert 1.0 <= got <= 2.0 if want == "jitter" else got == want


def test_get_page_retries_a_transport_error_then_succeeds(monkeypatch):
    calls = []

    class R:
        status_code = 200

        @staticmethod
        def json():
            return [{"slug": "ok"}]

    def get(*_a, **_kw):
        calls.append(1)
        if len(calls) == 1:
            raise polymarket.requests.ConnectionError("reset")
        return R()

    slept: list[float] = []
    monkeypatch.setattr(polymarket.requests, "get", get)
    monkeypatch.setattr(polymarket.time, "sleep", slept.append)
    assert polymarket._get_page(None, 0) == [{"slug": "ok"}]
    assert len(calls) == 2 and len(slept) == 1 and 1.0 <= slept[0] <= 2.0
