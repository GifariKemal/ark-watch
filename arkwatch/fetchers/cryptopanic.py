"""cryptopanic.py — CryptoPanic developer API fetcher with community sentiment votes.

Pulls curated news posts with trader sentiment votes (positive, negative, important)
when CRYPTOPANIC_API_KEY is configured in env.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import UTC, datetime

API_URL = "https://cryptopanic.com/api/v1/posts/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) arkwatch/0.1"


def fetch_cryptopanic_posts(limit: int = 50, timeout: int = 10) -> list[dict]:
    """Fetch recent curated news from CryptoPanic API with community sentiment votes."""
    key = os.environ.get("CRYPTOPANIC_API_KEY", "")
    if not key:
        return []

    url = f"{API_URL}?auth_token={key}&public=true&filter=rising"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []

    results = payload.get("results") or []
    out = []
    for post in results:
        title = str(post.get("title") or "").strip()
        if not title:
            continue

        raw_pub = post.get("published_at")
        pub_iso = (
            str(raw_pub)[:19] + "+00:00"
            if raw_pub
            else datetime.now(UTC).isoformat(timespec="seconds")
        )

        currencies = [c.get("code") for c in post.get("currencies", []) if c.get("code")]
        votes = post.get("votes") or {}

        out.append(
            {
                "source": "CRYPTOPANIC",
                "title": title,
                "url": post.get("url") or "",
                "summary": f"Votes: +{votes.get('positive', 0)} / -{votes.get('negative', 0)} | Important: {votes.get('important', 0)}",
                "published": pub_iso,
                "symbols": currencies,
                "provider_payload": post,
            }
        )
    return out
