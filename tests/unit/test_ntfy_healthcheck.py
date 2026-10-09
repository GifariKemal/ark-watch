"""ntfy push channel, dead-man ping (HEALTHCHECK_PING_URL), send gate, Yahoo RSS via proxy."""

from __future__ import annotations

import base64
import json
import logging
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arkwatch import daemon, db
from arkwatch.senders import ntfy, outbox
from arkwatch.senders.base import active_channels

TOKEN = "tk_unit-secret-0123456789"
TOPIC = "arkwatch-secret-topic-9f2c"


class Fake(BaseHTTPRequestHandler):
    calls: list = []
    fail_first = 0

    def log_message(self, *a):
        pass

    def _handle(self):
        cls = type(self)
        n = int(self.headers.get("Content-Length") or 0)
        cls.calls.append((self.command, self.path, dict(self.headers), self.rfile.read(n)))
        if cls.fail_first:
            cls.fail_first -= 1
            self.close_connection = True
            self.connection.close()  # transport error, no HTTP response
            return
        data = json.dumps({"id": "msg123", "event": "message"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _handle


@pytest.fixture()
def server():
    Fake.calls, Fake.fail_first = [], 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def no_channels(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL", "NTFY_URL"):
        monkeypatch.delenv(k, raising=False)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rfc2047(value: str) -> str:
    if value.startswith("=?UTF-8?B?"):
        return base64.b64decode(value[10:-2]).decode()
    return value


# --- ntfy channel -------------------------------------------------------------


def test_ntfy_headers_priority_click_token(server, monkeypatch):
    monkeypatch.setenv("NTFY_URL", f"{server}/{TOPIC}")
    monkeypatch.setenv("NTFY_TOKEN", TOKEN)
    monkeypatch.delenv("NTFY_CLICK_URL", raising=False)
    ch = ntfy.NtfyChannel()
    assert ch.send_text("⚠ WIB 07:00 · COPPER\ncopper broke\nctx") == "msg123"
    method, path, h, body = Fake.calls[-1]
    assert (method, path) == ("POST", f"/{TOPIC}")
    assert _rfc2047(h["Title"]) == "⚠ WIB 07:00 · COPPER"
    assert h["Priority"] == "3" and h["Tags"]
    assert h["Click"] == "https://zonelab.gifariksuryo.xyz/macro#alerts"
    assert h["Authorization"] == f"Bearer {TOKEN}"
    assert body.decode().startswith("⚠ WIB 07:00")

    monkeypatch.setenv("NTFY_CLICK_URL", "https://example.test/x#alerts")
    monkeypatch.delenv("NTFY_TOKEN")
    ch.send_text("⚠ WIB 07:00 · NET_LIQ_REVERSAL\n" + "t" * 200)
    h = Fake.calls[-1][2]
    assert h["Priority"] == "4" and h["Click"] == "https://example.test/x#alerts"
    assert "Authorization" not in h
    assert len(_rfc2047(h["Title"])) <= 80


def test_ntfy_body_truncated_to_3500_bytes(server, monkeypatch):
    monkeypatch.setenv("NTFY_URL", f"{server}/{TOPIC}")
    ntfy.NtfyChannel().send_text("title\n" + "é" * 5000)
    body = Fake.calls[-1][3]
    assert len(body) <= 3500 and body.decode().endswith("…")


def test_ntfy_retries_once_on_transport_error(server, monkeypatch):
    monkeypatch.setenv("NTFY_URL", f"{server}/{TOPIC}")
    Fake.fail_first = 1
    assert ntfy.NtfyChannel().send_text("hello") == "msg123"
    assert len(Fake.calls) == 2


def test_ntfy_failure_never_leaks_url_or_token(monkeypatch, capsys):
    url = f"http://127.0.0.1:{_closed_port()}/{TOPIC}"
    monkeypatch.setenv("NTFY_URL", url)
    monkeypatch.setenv("NTFY_TOKEN", TOKEN)
    with pytest.raises(RuntimeError) as ei:
        ntfy.NtfyChannel().send_text("hello")
    text = str(ei.value) + repr(ei.value.__cause__) + capsys.readouterr().out
    assert TOPIC not in text and TOKEN not in text
    from arkwatch.qa.harvest import _redact

    assert TOPIC not in _redact(f"failed for url: https://ntfy.sh/{TOPIC}")


def test_ntfy_http_error_returns_none_without_url(server, monkeypatch, capsys):
    monkeypatch.setenv("NTFY_URL", f"{server}/{TOPIC}")
    monkeypatch.setattr(ntfy, "_post", lambda *a, **k: type("R", (), {"status_code": 403})())
    assert ntfy.NtfyChannel().send_text("x") is None
    assert TOPIC not in capsys.readouterr().out


def test_ntfy_registered_and_unset_is_skipped(tmp_path, monkeypatch, no_channels):
    assert "ntfy" not in active_channels()
    path = tmp_path / "a.db"
    c = db.get_conn(path, allow_init=True)
    c.execute(
        "INSERT INTO brief_deliveries(brief_date,channel,status,created_at)"
        " VALUES ('2026-10-09','ntfy','pending','x')"
    )
    c.close()
    monkeypatch.setattr(outbox, "send_pending_alerts", lambda *a, **k: {})
    res = outbox.send_pending(str(path))
    c = db.get_conn(path)
    assert c.execute("SELECT status, last_error FROM brief_deliveries").fetchone() == (
        "skipped",
        "unconfigured: NTFY_URL",
    )
    c.close()
    assert res["failed"] == 0
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/x")
    assert set(active_channels()) == {"ntfy"}


BRIEF = (
    "=== US MACRO BRIEF — Fri, 09 Oct 2026 ===\n"
    "WIB 07:00 · data as of 08-Oct ET\n"
    "REGIME : RISK-ON (score +1.4)\n"
    "QUADRANT: Goldilocks\n\n"
    "Dollar  : smile left\n"
    "Quality: ok 90/95 healthy\n"
    "Overnight changes:\n"
    "line 8\n" + "x\n" * 2000
)


def test_ntfy_brief_short_form_via_outbox(server, tmp_path, monkeypatch):
    monkeypatch.setenv("NTFY_URL", f"{server}/{TOPIC}")
    monkeypatch.delenv("NTFY_CLICK_URL", raising=False)
    short = ntfy.NtfyChannel().format_brief(BRIEF, "2026-10-09")
    title, *rest = short.split("\n")
    assert title == "Morning brief 2026-10-09: RISK-ON +1.4"
    assert rest[5] == "Quality: ok 90/95 healthy" and "Overnight changes:" not in short
    assert short.endswith("https://zonelab.gifariksuryo.xyz/macro#brief")

    path = tmp_path / "a.db"
    c = db.get_conn(path, allow_init=True)
    c.execute(
        "INSERT INTO brief_log(date, markdown, regime_score, generated_at)"
        " VALUES ('2026-10-09', ?, 1.4, 'x')",
        (BRIEF,),
    )
    c.execute(
        "INSERT INTO brief_deliveries(brief_date,channel,status,created_at)"
        " VALUES ('2026-10-09','ntfy','pending','x')"
    )
    c.commit()
    c.close()
    monkeypatch.setattr(outbox, "active_channels", lambda: {"ntfy": ntfy.NtfyChannel()})
    monkeypatch.setattr(outbox, "send_pending_alerts", lambda *a, **k: {})
    assert outbox.send_pending(str(path))["sent"] == 1
    h, body = Fake.calls[-1][2], Fake.calls[-1][3].decode()
    assert _rfc2047(h["Title"]) == "Morning brief 2026-10-09: RISK-ON +1.4"
    assert "line 8" not in body and h["Click"].endswith("#brief")


# --- daemon: send gate ----------------------------------------------------------


def test_send_paused_without_channel_and_resumed_with_one(monkeypatch, no_channels):
    spawned = []
    monkeypatch.setattr(daemon, "_spawn", lambda argv, t: spawned.append(argv) or (0, "ok", ""))
    monkeypatch.setattr(daemon, "_heartbeat", lambda: None)
    assert daemon._paused_jobs() == {"send"}
    assert daemon._run_job("send", "d") is True and spawned == []
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/x")
    assert daemon._paused_jobs() == set()
    assert daemon._run_job("send", "d") is True and spawned[-1][-1] == "send"


def test_brief_outbox_rows_include_ntfy(tmp_path):
    from arkwatch.signals import brief

    path = tmp_path / "a.db"
    db.get_conn(path, allow_init=True).close()
    brief.save_brief(str(path), "md", 0.1)
    c = sqlite3.connect(path)
    chans = {r[0] for r in c.execute("SELECT channel FROM brief_deliveries")}
    c.close()
    assert chans == {"telegram", "ntfy"}


# --- daemon: dead-man ping ------------------------------------------------------


def test_ping_get_and_fail_suffix(server, monkeypatch, caplog):
    url = f"{server}/ping/{TOPIC}"
    monkeypatch.setenv("HEALTHCHECK_PING_URL", url)
    daemon._ping().join(5)
    daemon._ping("/fail").join(5)
    assert [(m, p) for m, p, *_ in Fake.calls] == [
        ("GET", f"/ping/{TOPIC}"),
        ("GET", f"/ping/{TOPIC}/fail"),
    ]
    assert TOPIC not in caplog.text


def test_ping_unset_is_noop(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_PING_URL", raising=False)
    assert daemon._ping() is None


def test_ping_unreachable_never_raises_slows_or_logs_url(monkeypatch, caplog):
    url = f"http://127.0.0.1:{_closed_port()}/{TOPIC}"
    monkeypatch.setenv("HEALTHCHECK_PING_URL", url)
    caplog.set_level(logging.DEBUG)
    t0 = time.monotonic()
    th = daemon._ping()
    assert time.monotonic() - t0 < 0.5  # fire-and-forget: the loop never waits
    th.join(10)
    assert TOPIC not in caplog.text and "127.0.0.1" not in caplog.text
    assert "ping failed" in caplog.text


def test_ping_rate_limited_to_5_minutes_and_only_when_healthy(monkeypatch):
    sent = []
    monkeypatch.setattr(daemon, "_ping", lambda suffix="": sent.append(suffix))
    monkeypatch.setattr(daemon, "healthcheck", lambda: 0)
    last = daemon._maybe_ping(float("-inf"), now=1000.0)
    last = daemon._maybe_ping(last, now=1000.0 + 299)
    assert sent == [""] and last == 1000.0
    last = daemon._maybe_ping(last, now=1000.0 + 300)
    assert sent == ["", ""]
    monkeypatch.setattr(daemon, "healthcheck", lambda: 1)
    daemon._maybe_ping(last, now=5000.0)
    assert sent == ["", ""]


def test_failed_job_pings_fail(monkeypatch):
    sent = []
    monkeypatch.setattr(daemon, "_ping", lambda suffix="": sent.append(suffix))
    monkeypatch.setattr(daemon, "DB_PATH", "/nonexistent/dir/x.db")
    daemon._alert_job_failed("harvest", "boom")
    assert sent == ["/fail"]


# --- RSS: Yahoo through the proxy -------------------------------------------------


def test_rss_fallback_proxies_yahoo_only(monkeypatch):
    import curl_cffi.requests as creq
    import requests

    from arkwatch.fetchers import rss_news

    monkeypatch.setenv("ARKWATCH_PROXY", "socks5h://warp:9091")

    class Session429:
        def __init__(self, **kw):
            pass

        def get(self, url, **kw):
            return type("R", (), {"status_code": 429})()

    class Ok:
        status_code = 200
        content = b"<rss><channel><item><title>t</title></item></channel></rss>"

        def raise_for_status(self):
            pass

    seen = {}
    monkeypatch.setattr(creq, "Session", Session429)
    monkeypatch.setattr(requests, "get", lambda url, **kw: seen.setdefault(url, kw) and Ok())
    assert rss_news.fetch_rss_feed("YAHOO", rss_news.FEEDS["YAHOO"])[0]["title"] == "t"
    assert seen[rss_news.FEEDS["YAHOO"]]["proxies"] == {
        "http": "socks5h://warp:9091",
        "https": "socks5h://warp:9091",
    }

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return Ok.content

    monkeypatch.setattr(rss_news.urllib.request, "urlopen", lambda *a, **k: Resp())
    rss_news.fetch_rss_feed("FED", rss_news.FEEDS["FED"])
    assert rss_news.FEEDS["FED"] not in seen  # direct urllib, never the proxy


def test_rss_proxied_http_error_raises(monkeypatch):
    import curl_cffi.requests as creq
    import requests

    from arkwatch.fetchers import rss_news

    monkeypatch.setenv("ARKWATCH_PROXY", "socks5h://warp:9091")
    monkeypatch.setattr(creq, "Session", lambda **kw: (_ for _ in ()).throw(OSError("x")))

    class R429:
        status_code = 429

        def raise_for_status(self):
            raise requests.HTTPError("429")

    monkeypatch.setattr(requests, "get", lambda url, **kw: R429())
    with pytest.raises(requests.HTTPError):
        rss_news.fetch_rss_feed("YAHOO", rss_news.FEEDS["YAHOO"])
