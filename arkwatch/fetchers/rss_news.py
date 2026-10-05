"""rss_news.py — RSS news fetcher for official and breaking macro feeds.

Fetches structured RSS/XML feeds:
  - Federal Reserve official press releases
  - Yahoo Finance top market stories
  - CNBC breaking business headlines
"""

from __future__ import annotations

import email.utils
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

FEEDS = {
    "FED": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ECB": "https://www.ecb.europa.eu/rss/press.html",
    "BOE": "https://www.bankofengland.co.uk/rss/news",
    "TREASURY": "https://home.treasury.gov/rss.xml",
    "SEC": "https://www.sec.gov/news/pressreleases.rss",
    "FOREXLIVE": "https://www.forexlive.com/feed/news",
    "OILPRICE": "https://oilprice.com/rss/main",
    "COINTELEGRAPH": "https://cointelegraph.com/rss",
    "MARKETWATCH": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "YAHOO": "https://finance.yahoo.com/news/rssindex",
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) arkwatch/0.1"


def _parse_pub_date(pub_text: str | None) -> str:
    """Parse various RSS pubDate formats (RFC 2822, ISO) into UTC ISO-8601 string."""
    if not pub_text:
        return datetime.now(UTC).isoformat(timespec="seconds")

    clean = pub_text.strip()
    try:
        dt = email.utils.parsedate_to_datetime(clean)
        return dt.astimezone(UTC).isoformat(timespec="seconds")
    except Exception:
        pass

    try:
        dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        return dt.astimezone(UTC).isoformat(timespec="seconds")
    except Exception:
        pass

    return datetime.now(UTC).isoformat(timespec="seconds")


def _tag_symbols(source_name: str) -> list[str]:
    src = source_name.upper()
    if src == "FED":
        return ["$FED"]
    if src == "ECB":
        return ["$EUR", "$ECB"]
    if src == "BOE":
        return ["$GBP", "$BOE"]
    if src == "TREASURY":
        return ["$USD", "$TREASURY"]
    if src == "SEC":
        return ["$SEC"]
    if src == "OILPRICE":
        return ["CL1", "BZ1"]
    if src == "FOREXLIVE":
        return ["$DXY", "$MACRO"]
    if src == "COINTELEGRAPH":
        return ["$BTC", "$ETH"]
    return []


def fetch_rss_feed(source_name: str, url: str, timeout: int = 10) -> list[dict]:
    """Fetch and parse an RSS feed, returning standardized news dictionaries."""
    data = None
    try:
        from curl_cffi import requests as creq

        s = creq.Session(impersonate="chrome")
        r = s.get(url, timeout=timeout)
        if r.status_code == 200:
            data = r.content
    except Exception:
        pass

    if data is None:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read()
        except Exception:
            return []

    if not data:
        return []

    try:
        root = ET.fromstring(data)
    except Exception:
        return []

    items = root.findall(".//item")
    out = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        pub = _parse_pub_date(it.findtext("pubDate"))

        if not title:
            continue

        out.append(
            {
                "source": f"RSS_{source_name.upper()}",
                "title": title,
                "url": link,
                "summary": desc[:2000],
                "published": pub,
                "symbols": _tag_symbols(source_name),
                "provider_payload": {
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "pubDate": pub,
                },
            }
        )
    return out


def fetch_all_rss_feeds() -> list[dict]:
    """Fetch all configured RSS news feeds (FED, YAHOO, CNBC)."""
    out = []
    for name, url in FEEDS.items():
        try:
            items = fetch_rss_feed(name, url)
            out.extend(items)
        except Exception:
            pass
    return out
