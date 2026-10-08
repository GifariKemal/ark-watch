"""FRED fetcher must ride out transient failures instead of failing the series."""

from __future__ import annotations

import http.server
import threading

from urllib3.util.retry import Retry

from arkwatch.fetchers import fred


def test_default_session_retries_transient_errors():
    adapter = fred._SESSION.get_adapter("https://api.stlouisfed.org")
    retry: Retry = adapter.max_retries
    assert retry.total >= 3
    assert 429 in retry.status_forcelist and 503 in retry.status_forcelist
    assert retry.backoff_factor > 0


def test_retry_recovers_after_503(monkeypatch):
    """Real HTTP server: 503 twice, then data -> fetch_observations returns the data."""
    hits = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits["n"] += 1
            if hits["n"] <= 2:
                self.send_response(503)
                self.end_headers()
                return
            body = b'{"observations":[{"date":"2026-10-07","value":"4.5","realtime_start":"2026-10-07"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FRED_API_KEY", "k")
    monkeypatch.setattr(fred, "BASE", f"http://127.0.0.1:{srv.server_port}/x")
    monkeypatch.setattr(fred, "THROTTLE_S", 0.0)
    s = fred.requests.Session()  # same retry policy, plain-http mount for the local server
    s.mount("http://", fred._SESSION.get_adapter("https://x"))
    try:
        out = fred.fetch_observations("DFF", session=s)
    finally:
        srv.shutdown()
        srv.server_close()
        s.close()
    assert hits["n"] == 3
    assert out == [{"ts": "2026-10-07", "value": 4.5, "realtime_start": "2026-10-07"}]
