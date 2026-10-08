"""net.py — egress proxy for hosts that refuse the VPS datacenter IP.

VPS ground truth 2026-10-09: Yahoo 429s and CFTC 403s the datacenter IP (both
200 via the WARP SOCKS sidecar), while CME 403s and FRED times out THROUGH
WARP — so the proxy applies per host, never globally.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_HOSTS = "finance.yahoo.com,publicreporting.cftc.gov"


def proxies_for(url: str) -> dict | None:
    """requests-style proxies for `url`, or None (direct).

    ARKWATCH_PROXY (legacy ARKWATCH_YAHOO_PROXY), e.g. socks5h://warp:9091,
    applies only when the URL host equals or is a subdomain of an entry in
    ARKWATCH_PROXY_HOSTS (comma list, default DEFAULT_HOSTS)."""
    proxy = (os.environ.get("ARKWATCH_PROXY") or os.environ.get("ARKWATCH_YAHOO_PROXY", "")).strip()
    if not proxy:
        return None
    host = urlsplit(url).hostname or ""
    hosts = [h.strip() for h in os.environ.get("ARKWATCH_PROXY_HOSTS", DEFAULT_HOSTS).split(",")]
    if any(h and (host == h or host.endswith("." + h)) for h in hosts):
        return {"http": proxy, "https": proxy}
    return None
