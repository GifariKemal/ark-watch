"""argus.py - optional news source via the owner's self-hosted Argus MCP server.

Streamable-HTTP JSON-RPC: initialize -> notifications/initialized -> tools/call.
Replies arrive as SSE `data:` lines or plain JSON. Config: ARGUS_TOKEN (required,
bearer, never logged) and ARGUS_URL (optional).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

DEFAULT_URL = "https://argus.gifariksuryo.xyz/mcp"
# Cloudflare 403s the default Python-urllib user agent
USER_AGENT = "arkwatch-argus/1.0"
TIMEOUT_S = 25
BUDGET_S = 120
QUERIES = (
    "Federal Reserve rate outlook",
    "US Treasury yields inflation",
    "gold XAUUSD",
    "crude oil WTI OPEC",
    "S&P 500 Nasdaq futures",
    "US dollar DXY",
    "Bitcoin ETF flows",
)


class Client:
    """One MCP session per client; reuse it for a batch of calls."""

    def __init__(self) -> None:
        self.url = os.environ.get("ARGUS_URL", "").strip() or DEFAULT_URL
        self._token = os.environ["ARGUS_TOKEN"].strip()
        self.sid: str | None = None
        self._ready = False
        self._id = 0

    def _post(self, body: dict, timeout: float) -> list[dict]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        }
        if self.sid:
            headers["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            self.sid = r.headers.get("mcp-session-id") or self.sid
            raw = r.read().decode("utf-8", "replace")
        msgs = [json.loads(ln[5:]) for ln in raw.splitlines() if ln.startswith("data:")]
        return msgs or ([json.loads(raw)] if raw.strip() else [])

    def _rpc(self, method: str, params: dict, timeout: float) -> list[dict]:
        self._id += 1
        return self._post(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, timeout
        )

    def call(self, tool: str, args: dict, timeout: float = TIMEOUT_S) -> dict:
        """tools/call -> the tool's JSON result. One retry (fresh session) on transport error."""
        for attempt in (0, 1):
            try:
                if not self._ready:
                    self.sid = None
                    self._rpc(
                        "initialize",
                        {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "arkwatch", "version": "1"},
                        },
                        timeout,
                    )
                    self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout)
                    self._ready = True
                msgs = self._rpc("tools/call", {"name": tool, "arguments": args}, timeout)
                break
            except (OSError, http.client.HTTPException):
                self._ready = False
                if attempt:
                    raise
        msg = msgs[-1] if msgs else {}
        res = msg.get("result")
        if not isinstance(res, dict) or res.get("isError"):
            raise RuntimeError(f"argus {tool}: {str(msg.get('error') or res)[:200]}")
        return json.loads("".join(c.get("text", "") for c in res.get("content", [])))


def _published(raw) -> str:
    """ISO-8601 UTC; naive = UTC; unparseable/missing = now (as the other news fetchers)."""
    dt = None
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                dt = None
    dt = dt or datetime.now(UTC)
    return dt.replace(tzinfo=dt.tzinfo or UTC).astimezone(UTC).isoformat(timespec="seconds")


def fetch_news(queries: tuple[str, ...] = QUERIES, budget_s: float = BUDGET_S) -> list[dict]:
    """Run queries sequentially within budget_s; a failing query only drops itself."""
    client = Client()
    deadline = time.monotonic() + budget_s
    out: dict[str, dict] = {}
    errors: list[str] = []
    ok = 0
    for query in queries:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            res = client.call(
                "news_sentiment_feed",
                # since='week' is the verified window ('day' returns low-relevance junk and an
                # ISO date makes the server error); results Argus flags degraded are ignored
                {"query": query, "sentiment": False, "since": "week"},
                timeout=min(TIMEOUT_S, left),
            )
        except Exception as ex:
            errors.append(f"{query}: {type(ex).__name__}: {ex}")
            continue
        ok += 1
        if res.get("degraded"):
            continue
        for item in res.get("items") or []:
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not url:
                continue
            uid = hashlib.sha256(url.encode()).hexdigest()[:20]
            out.setdefault(
                uid,
                {
                    "id": uid,
                    "source": "ARGUS",
                    "title": title,
                    "url": url,
                    "summary": str(item.get("snippet") or "")[:2000],
                    "published": _published(item.get("published")),
                    "symbols": [],
                    # raw item incl. any optional sentiment/score the server adds
                    "provider_payload": {**item, "query": query},
                },
            )
    if errors and not ok:
        raise RuntimeError(f"all Argus queries failed: {errors[0][:200]}")
    return list(out.values())
