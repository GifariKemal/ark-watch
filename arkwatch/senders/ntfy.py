"""ntfy.py - Channel adapter for ntfy (https://ntfy.sh style) phone push.

No account needed: NTFY_URL is the full topic URL (https://ntfy.sh/<random-topic>).
The topic name IS the credential on a public server (anyone holding it can read
and post), so it is env-only and never printed. NTFY_TOKEN (optional) is sent as
a Bearer token for self-hosted / access-controlled topics.
"""

from __future__ import annotations

import base64
import os
import re
from urllib.parse import urlsplit

import requests

MAX_BYTES = 3500  # ntfy caps a message at 4096 bytes; headroom for safety
CLICK_URL = "https://zonelab.gifariksuryo.xyz/macro#alerts"


def _header(value: str) -> str:
    """HTTP headers are latin-1: ntfy decodes RFC 2047 for anything else (emoji)."""
    if value.isascii():
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


def _truncate(text: str, limit: int = MAX_BYTES) -> bytes:
    raw = text.encode()
    if len(raw) <= limit:
        return raw
    ell = "…".encode()
    return raw[: limit - len(ell)].decode(errors="ignore").encode() + ell


def _post(url: str, data: bytes, headers: dict) -> requests.Response:
    """One retry on transport error; the exception never carries the URL."""
    for _ in range(2):
        try:
            return requests.post(url, data=data, headers=headers, timeout=10)
        except requests.RequestException as ex:
            err = type(ex).__name__
    raise RuntimeError(f"ntfy: {err}")  # outside except: no chained URL-bearing cause


class NtfyChannel:
    name = "ntfy"

    def format_brief(self, markdown: str, brief_date: str) -> str:
        """Short push form of the ~4.5k-char brief: title line, first 6 lines, link."""
        m = re.search(r"REGIME\s*:\s*(.+?)\s*\(score\s*([+-]?[\d.]+)\)", markdown)
        label = f"{m.group(1)} {m.group(2)}" if m else "n/a"
        lines = [ln for ln in markdown.splitlines() if ln.strip()][:6]
        return "\n".join([f"Morning brief {brief_date}: {label}", *lines, _click_url("brief")])

    def send_text(self, text: str) -> str | None:
        """POST one message; return the ntfy message id (None on an HTTP error)."""
        url = os.environ.get("NTFY_URL", "").strip()
        if not url:
            raise RuntimeError("NTFY_URL not set")
        first = text.split("\n", 1)[0]
        brief = first.startswith("Morning brief ")
        from ..qa.watcher import URGENT

        urgent = any(t.upper() in first for t in URGENT)
        headers = {
            "Title": _header(first[:80]),
            "Priority": "4" if urgent else "3",
            "Tags": "newspaper" if brief else "rotating_light" if urgent else "warning",
            "Click": _click_url("brief" if brief else None),
        }
        if token := os.environ.get("NTFY_TOKEN", "").strip():
            headers["Authorization"] = f"Bearer {token}"
        r = _post(url, _truncate(text), headers)
        if r.status_code != 200:
            print(f"  ⚠ ntfy: HTTP {r.status_code}")
            return None
        return r.json().get("id")


def _click_url(fragment: str | None = None) -> str:
    url = os.environ.get("NTFY_CLICK_URL", "").strip() or CLICK_URL
    return urlsplit(url)._replace(fragment=fragment).geturl() if fragment else url
