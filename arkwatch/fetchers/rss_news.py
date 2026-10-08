"""rss_news.py — RSS news fetcher for official and breaking macro feeds.

Fetches structured RSS/XML feeds:
  - Federal Reserve official press releases
  - Yahoo Finance top market stories
  - CNBC breaking business headlines
"""

from __future__ import annotations

import email.utils
import urllib.request
from datetime import UTC, datetime

from defusedxml.ElementTree import fromstring

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
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()

    if not data:
        return []

    # defusedxml: third-party feeds must not get entity expansion / DTD tricks
    items = fromstring(data).findall(".//item")
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
    """Fetch all configured RSS feeds; one dead feed degrades only itself.

    Per-feed failures print a warning; if EVERY feed fails, raise so
    market_news.run records a fetch_log ERROR for RSS_FEEDS (it used to log a
    healthy-looking empty run)."""
    out, errors = [], []
    for name, url in FEEDS.items():
        try:
            out.extend(fetch_rss_feed(name, url))
        except Exception as ex:
            errors.append(f"{name}: {type(ex).__name__}: {str(ex)[:80]}")
            print(f"  ⚠ RSS {errors[-1]}")
    if errors and len(errors) == len(FEEDS):
        raise RuntimeError("all RSS feeds failed: " + " | ".join(errors)[:400])
    return out
