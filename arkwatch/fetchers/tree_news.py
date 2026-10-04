"""tree_news.py — Tree News real-time breaking financial wire fetcher.

Pulls curated breaking news from Bloomberg, Reuters, SEC, and crypto wires
via Tree News REST API and WebSocket interfaces.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime

API_URL = "https://news.treeofalpha.com/api/news"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) arkwatch/0.1"


def fetch_tree_news(limit: int = 50, timeout: int = 10) -> list[dict]:
    """Fetch recent curated breaking headlines from Tree News API."""
    url = f"{API_URL}?limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []

    if not isinstance(payload, list):
        return []

    out = []
    for item in payload:
        title = str(item.get("title") or "").strip()
        if not title:
            continue

        raw_time = item.get("time")
        if raw_time and isinstance(raw_time, (int, float)):
            pub_iso = datetime.fromtimestamp(raw_time / 1000, UTC).isoformat(timespec="seconds")
        else:
            pub_iso = datetime.now(UTC).isoformat(timespec="seconds")

        source_name = str(item.get("source") or item.get("sourceName") or "TREE_NEWS")
        symbols = item.get("symbols") or []
        if not isinstance(symbols, list):
            symbols = [str(symbols)]

        out.append(
            {
                "source": f"TREE_{source_name.upper()}",
                "title": title,
                "url": item.get("url") or "",
                "summary": str(item.get("body") or "")[:2000],
                "published": pub_iso,
                "symbols": symbols,
                "provider_payload": item,
            }
        )

    return out
