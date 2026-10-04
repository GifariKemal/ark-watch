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

TIER1_SOURCES = frozenset(
    {
        "BLOOMBERG",
        "REUTERS",
        "SEC",
        "FEDERALRESERVE",
        "TREASURY",
        "WSJ",
        "CNBC",
        "FT",
        "FINANCIAL TIMES",
        "DOW JONES",
        "COINDESK",
        "THE BLOCK",
        "FOREXLIVE",
    }
)

MACRO_KEYWORDS = (
    "fed",
    "fomc",
    "powell",
    "rate",
    "yield",
    "inflation",
    "cpi",
    "ppi",
    "pce",
    "payroll",
    "jobs",
    "unemployment",
    "treasury",
    "debt",
    "deficit",
    "gdp",
    "oil",
    "crude",
    "brent",
    "wti",
    "opec",
    "energy",
    "petroleum",
    "gasoline",
    "gold",
    "silver",
    "copper",
    "xau",
    "xag",
    "metals",
    "tariff",
    "war",
    "sanction",
    "strike",
    "geopolit",
    "china",
    "iran",
    "israel",
    "russia",
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "crypto",
    "sec",
    "binance",
    "etf",
    "liquidation",
    "stock",
    "equity",
    "nasdaq",
    "sp500",
    "s&p",
    "dow",
    "rally",
    "selloff",
)

PROMOTIONAL_NOISE = (
    "airdrop",
    "giveaway",
    "promo",
    "how to buy",
    "how to get",
    "top 5 ways",
    "best crypto to buy",
    "meme coin",
    "presale",
    "whitelist",
    "bonus",
)


def _is_high_signal(source_upper: str, title: str, symbols: list[str]) -> bool:
    """Quality gate: filter out low-value noise and retain only macro/market moving wire items."""
    clean_title = title.lower().strip()

    # Reject empty or raw link-only headlines (e.g. 'https://edgecompute...')
    if not clean_title or clean_title.startswith("http://") or clean_title.startswith("https://"):
        return False

    # Reject spam / promotional keywords
    if any(p in clean_title for p in PROMOTIONAL_NOISE):
        return False

    # Always accept Tier-1 institutional wire agencies
    if any(tier1 in source_upper for tier1 in TIER1_SOURCES):
        return True

    # If symbols are explicitly tagged with assets we track
    if any(s.upper() in ("BTC", "ETH", "GOLD", "OIL", "SPY", "QQQ", "USD") for s in symbols):
        return True

    # Accept other sources (like Twitter / Blogs) ONLY if they discuss our macro keywords
    return any(k in clean_title for k in MACRO_KEYWORDS)


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
        if raw_time and isinstance(raw_time, int | float):
            pub_iso = datetime.fromtimestamp(raw_time / 1000, UTC).isoformat(timespec="seconds")
        else:
            pub_iso = datetime.now(UTC).isoformat(timespec="seconds")

        source_name = str(item.get("source") or item.get("sourceName") or "TREE_NEWS")
        source_upper = source_name.upper()

        symbols = item.get("symbols") or []
        if not isinstance(symbols, list):
            symbols = [str(symbols)]

        if not _is_high_signal(source_upper, title, symbols):
            continue

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
