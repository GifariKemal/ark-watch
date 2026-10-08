"""fred.py — FRED/ALFRED observations fetcher.

API conventions honored here:
  - key via env FRED_API_KEY (never hard-coded)
  - sort_order=asc&limit for history depth; sort_order=desc for latest values
  - NO custom User-Agent: FRED's Akamai edge tarpits custom/browser UAs from datacenter
    IPs (read timeout, verified from the VPS 2026-10-08); the default python-requests UA is fine
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://api.stlouisfed.org/fred/series/observations"
# FRED's official limit is 120 req/min; 0.55s spacing keeps batches safely below it.
THROTTLE_S = 0.55
_last_call = 0.0
# transient network/5xx/429 blips (container boot, DNS) must not blank a series until tomorrow
_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=4,
            read=1,  # a tarpitted read costs 30s; do not multiply it
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
    ),
)


class FredError(RuntimeError):
    pass


def _key() -> str:
    k = os.environ.get("FRED_API_KEY", "")
    if not k:
        raise FredError("FRED_API_KEY not set (env/.env)")
    return k


def fetch_observations(
    series_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    realtime_start: str | None = None,
    realtime_end: str | None = None,
    output_type: int = 1,
    sort: str = "desc",
    limit: int | None = None,
    session: requests.Session | None = None,
) -> list[dict]:
    """Returns list[{ts, value, realtime_start}] ordered per `sort` (value '.' = missing -> None)."""
    global _last_call
    params: dict[str, str | int] = {
        "series_id": series_id,
        "api_key": _key(),
        "file_type": "json",
        "sort_order": sort,
    }
    if start:
        params["observation_start"] = start
    if end:
        params["observation_end"] = end
    if realtime_start:
        params["realtime_start"] = realtime_start
    if realtime_end:
        params["realtime_end"] = realtime_end
    if limit:
        params["limit"] = limit
    if output_type != 1:
        params["output_type"] = output_type
    wait = THROTTLE_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()

    s = session or _SESSION
    resp = s.get(
        f"{BASE}?{urlencode(params)}",
        timeout=(10, 30),
    )
    if resp.status_code != 200:
        raise FredError(f"FRED {series_id}: HTTP {resp.status_code} — {resp.text[:200]}")
    data = resp.json()
    if data.get("error_message"):
        raise FredError(f"FRED {series_id}: {data['error_message']}")
    out = []
    for o in data.get("observations", []):
        v = o.get("value", ".")
        out.append(
            {
                "ts": o["date"],
                "value": None if v in (".", "") else float(v),
                "realtime_start": o.get("realtime_start"),
            }
        )
    return out
