"""Argus MCP news source: client protocol, mapping, market_news wiring, token hygiene."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arkwatch import db
from arkwatch.fetchers import argus

TOKEN = "tok-unit-test-secret-0123456789"


class FakeMCP(BaseHTTPRequestHandler):
    """Streamable-HTTP MCP stand-in. Class attrs steer behaviour per test."""

    calls: list = []
    sse = True
    fail_first = 0  # drop this many tools/call connections before answering
    delay = 0.0
    result: dict = {}

    def log_message(self, *a):  # silence stderr
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = type(self)
        cls.calls.append((body.get("method"), self.headers))
        if body.get("method") == "tools/call":
            if cls.fail_first:
                cls.fail_first -= 1
                self.close_connection = True
                self.connection.close()  # transport error, no HTTP response
                return
            time.sleep(cls.delay)
            payload = {"content": [{"type": "text", "text": json.dumps(cls.result)}]}
        elif body.get("method") == "initialize":
            payload = {"protocolVersion": "2025-06-18", "capabilities": {}}
        else:  # notifications/initialized
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        msg = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": payload})
        data = (f"event: message\ndata: {msg}\n\n" if cls.sse else msg).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if cls.sse else "application/json")
        self.send_header("mcp-session-id", "sess-1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def server(monkeypatch):
    FakeMCP.calls, FakeMCP.sse, FakeMCP.fail_first, FakeMCP.delay = [], True, 0, 0.0
    FakeMCP.result = {"query": "q", "items": [], "count": 0}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ARGUS_URL", f"http://127.0.0.1:{srv.server_port}/mcp")
    monkeypatch.setenv("ARGUS_TOKEN", TOKEN)
    yield FakeMCP
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("sse", [True, False])
def test_call_handshake_and_session_reuse(server, sse):
    server.sse = sse
    server.result = {"items": [{"title": "t"}]}
    client = argus.Client()
    assert client.call("news_sentiment_feed", {"query": "x"}) == {"items": [{"title": "t"}]}
    assert client.call("news_sentiment_feed", {"query": "y"})["items"]
    methods = [m for m, _ in server.calls]
    # one handshake per client (call batch), then calls reuse the session
    assert methods == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    headers = [h for _, h in server.calls]
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for h in headers)
    assert all("Python-urllib" not in h["User-Agent"] for h in headers)
    assert all(h.get("mcp-session-id") == "sess-1" for h in headers[1:])


def test_call_retries_once_on_transport_error(server):
    server.fail_first = 1
    assert argus.Client().call("news_sentiment_feed", {"query": "x"}) == server.result
    server.fail_first = 2
    with pytest.raises(OSError):
        argus.Client().call("news_sentiment_feed", {"query": "x"})


def test_call_timeout(server):
    server.delay = 1.0
    with pytest.raises(OSError):  # socket timeout (TimeoutError / URLError)
        argus.Client().call("news_sentiment_feed", {"query": "x"}, timeout=0.2)


def test_fetch_news_maps_dedupes_and_parses_tz(server):
    server.result = {
        "items": [
            {
                "title": " Gold up ",
                "url": "https://a.test/1",
                "snippet": "s",
                "published": "2026-09-21T20:22:00",
            },
            {"title": "dup", "url": "https://a.test/1", "published": "2026-09-21T20:22:00Z"},
            {
                "title": "Oil",
                "url": "https://b.test/2",
                "published": "2026-09-21T22:00:00+02:00",
                "score": 0.7,
            },
            {
                "title": "rfc",
                "url": "https://c.test/3",
                "published": "Mon, 21 Sep 2026 20:00:00 GMT",
            },
            {"title": "nodate", "url": "https://d.test/4", "published": "garbage"},
            {"title": "", "url": "https://e.test/5"},
            {"title": "no url"},
        ]
    }
    rows = argus.fetch_news(("q1", "q2"))
    assert [r["url"] for r in rows] == [
        f"https://{h}" for h in ("a.test/1", "b.test/2", "c.test/3", "d.test/4")
    ]
    gold, oil, rfc, nodate = rows
    assert gold["source"] == "ARGUS" and gold["title"] == "Gold up" and gold["summary"] == "s"
    assert gold["published"] == "2026-09-21T20:22:00+00:00"
    assert oil["published"] == "2026-09-21T20:00:00+00:00"
    assert rfc["published"] == "2026-09-21T20:00:00+00:00"
    assert nodate["published"].endswith("+00:00")
    assert oil["provider_payload"]["score"] == 0.7  # optional sentiment kept in payload only
    assert gold["id"] == argus.fetch_news(("q1",))[0]["id"]  # stable across runs


def test_fetch_news_one_failing_query_keeps_others(monkeypatch):
    def call(self, tool, args, timeout=25):
        if args["query"] == "bad":
            raise TimeoutError("slow")
        return {"items": [{"title": args["query"], "url": f"https://x.test/{args['query']}"}]}

    monkeypatch.setenv("ARGUS_TOKEN", TOKEN)
    monkeypatch.setattr(argus.Client, "call", call)
    assert [r["title"] for r in argus.fetch_news(("a", "bad", "b"))] == ["a", "b"]
    with pytest.raises(RuntimeError, match="all Argus queries failed"):
        argus.fetch_news(("bad",))


def test_fetch_news_respects_budget(monkeypatch):
    seen = []
    monkeypatch.setenv("ARGUS_TOKEN", TOKEN)
    monkeypatch.setattr(argus.Client, "call", lambda self, t, a, timeout=25: seen.append(a) or {})
    argus.fetch_news(("a", "b"), budget_s=0)
    assert seen == []


@pytest.fixture()
def news_env(monkeypatch):
    from arkwatch.qa import market_news as mn

    for k in ("EODHD_API_TOKEN", "CRYPTOPANIC_API_KEY", "ARGUS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mn, "_fmp", lambda: [])
    monkeypatch.setattr("arkwatch.fetchers.tree_news.fetch_tree_news", lambda: [])
    monkeypatch.setattr("arkwatch.fetchers.rss_news.fetch_all_rss_feeds", lambda: [])
    monkeypatch.setattr(mn, "_gdelt_updates", lambda: {})
    return mn


def _log(path):
    c = db.get_conn(path)
    rows = c.execute("SELECT target, status, error FROM fetch_log").fetchall()
    news = c.execute("SELECT source, title FROM market_news").fetchall()
    payloads = c.execute("SELECT payload_json FROM market_news_payloads").fetchall()
    c.close()
    return {t: (s, e) for t, s, e in rows}, news, payloads


def test_run_argus_skipped_when_unconfigured(tmp_path, news_env, capsys):
    news_env.run(str(tmp_path / "a.db"))
    log, _, _ = _log(tmp_path / "a.db")
    assert log["ARGUS"] == ("SKIPPED", "unconfigured: ARGUS_TOKEN")
    assert log["FMP"][0] == "OK"


def test_run_argus_ok(tmp_path, news_env, monkeypatch):
    monkeypatch.setenv("ARGUS_TOKEN", TOKEN)
    item = {
        "title": "Fed holds",
        "url": "https://n.test/1",
        "published": "2026-10-08T12:00:00",
        "sentiment": -0.2,
    }
    monkeypatch.setattr(argus.Client, "call", lambda self, t, a, timeout=25: {"items": [item]})
    news_env.run(str(tmp_path / "a.db"))
    log, news, payloads = _log(tmp_path / "a.db")
    assert log["ARGUS"][0] == "OK" and log["FMP"][0] == "OK"
    assert news == [("ARGUS", "Fed holds")]
    assert json.loads(payloads[0][0])["sentiment"] == -0.2


def test_run_argus_error_is_redacted(tmp_path, news_env, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_TOKEN", TOKEN)

    def boom(self, t, a, timeout=25):
        raise RuntimeError(f"401 echo Authorization: Bearer {TOKEN}")

    monkeypatch.setattr(argus.Client, "call", boom)
    news_env.run(str(tmp_path / "a.db"))
    log, _, _ = _log(tmp_path / "a.db")
    assert log["ARGUS"][0] == "ERROR" and log["FMP"][0] == "OK"
    out = capsys.readouterr()
    assert TOKEN not in json.dumps(log) and TOKEN not in out.out + out.err


def test_fetch_news_week_window_and_drops_degraded(monkeypatch):
    """since='day' returns junk (low_relevance) and ISO dates error out on the server; 'week'
    is the verified window, and any response Argus flags degraded is ignored."""
    seen: list[dict] = []

    class FakeClient:
        def call(self, tool, args, timeout):
            seen.append(args)
            if args["query"] == "bad":
                return {
                    "items": [{"title": "farmers market", "url": "https://x.test/1"}],
                    "degraded": True,
                }
            return {"items": [{"title": "ok", "url": "https://ok.test/1"}], "degraded": False}

    monkeypatch.setattr(argus, "Client", FakeClient)
    rows = argus.fetch_news(("good", "bad"))
    assert [a["since"] for a in seen] == ["week", "week"]
    assert [r["url"] for r in rows] == ["https://ok.test/1"]
