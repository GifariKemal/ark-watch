"""calendar.py — union calendar fetchers (FMP · TV · CME · EODHD) -> normalized events.

Verified response shapes:
  FMP   list{date "YYYY-MM-DD HH:MM:SS"(UTC), event, impact High/Med/Low, estimate, actual, previous}
  TV    result[]{title, date(ISO), importance 1/0/-1, forecast, actual, previous} — needs Origin/Referer
  CME   dates->events per date (curl_cffi; impact must be null) — {eventName,eventValues[{actual,previous,consensus}],date}
  EODHD list{type, date "YYYY-MM-DD HH:MM:SS", actual, previous, estimate} — no importance field
Uniform output: {ts_utc, name, importance, actual, consensus, previous, source}
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import requests

from ..config import PLAN_LIMIT_STATUSES, PlanLimited

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
}


def _num(v):
    """Parse numbers: thousands commas, '%', and K/M/B suffixes as multipliers.

    String expansion ("1.356M" -> "1.356000000" -> 1.356) silently corrupts
    values, so the suffix multiplies by 10^n instead.
    """
    if v in (None, "", "nan"):
        return None
    s = str(v).strip()
    mult = 1.0
    for suf, m in (("K", 1e3), ("M", 1e6), ("B", 1e9)):
        if s.upper().endswith(suf):
            s, mult = s[:-1], m
            break
    try:
        return float(s.replace(",", "").replace("%", "").strip()) * mult
    except ValueError:
        return None


def _iso(date_str: str) -> str:
    """Accept 'YYYY-MM-DD HH:MM:SS' / ISO-TZ input -> ISO-UTC (seconds).

    Explicit offsets must be CONVERTED, not discarded — overwriting tzinfo in
    place shifts times by 4-12 hours whenever a source sends non-UTC.
    """
    s = str(date_str).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        return ""


def fetch_fmp(from_d: str, to_d: str) -> list[dict]:
    key = os.environ.get("FMP_API_KEY", "")
    if not key:
        raise RuntimeError("FMP_API_KEY empty — set env (see API.md)")
    r = requests.get(
        "https://financialmodelingprep.com/stable/economic-calendar",
        params={"country": "US", "from": from_d, "to": to_d, "apikey": key},
        timeout=(10, 30),
    )
    if r.status_code in PLAN_LIMIT_STATUSES:
        raise PlanLimited(f"plan-limited: FMP economic-calendar (HTTP {r.status_code})")
    r.raise_for_status()
    out = []
    for x in r.json():
        ts = _iso(x.get("date", ""))
        if not ts:
            continue
        imp = str(x.get("impact") or "").lower()
        out.append(
            {
                "ts_utc": ts,
                "name": str(x.get("event") or "").strip(),
                "importance": {"high": "high", "medium": "medium", "low": "low"}.get(
                    imp, "unknown"
                ),
                "actual": _num(x.get("actual")),
                "consensus": _num(x.get("estimate")),
                "previous": _num(x.get("previous")),
                "source": "FMP",
            }
        )
    return out


def fetch_tv(from_d: str, to_d: str) -> list[dict]:
    r = requests.get(
        "https://economic-calendar.tradingview.com/events",
        params={
            "from": f"{from_d}T00:00:00.000Z",
            "to": f"{to_d}T23:59:59.000Z",
            "countries": "US",
        },
        headers={
            **UA,
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/",
        },
        timeout=(10, 30),
    )
    r.raise_for_status()
    out = []
    for x in r.json().get("result", []):
        ts = _iso(x.get("date", ""))
        if not ts:
            continue
        imp = x.get("importance", 0)
        out.append(
            {
                "ts_utc": ts,
                "name": str(x.get("title") or "").strip(),
                "importance": {1: "high", 0: "medium", -1: "low"}.get(imp, "unknown"),
                "actual": _num(x.get("actual")),
                "consensus": _num(x.get("forecast")),
                "previous": _num(x.get("previous")),
                "source": "TV",
            }
        )
    return out


def fetch_cme(from_d: str, to_d: str) -> list[dict]:
    from curl_cffi import requests as creq

    s = creq.Session(impersonate="chrome")
    r = s.post(
        "https://www.cmegroup.com/services/economic-release-dates",
        json={
            "date": from_d,
            "countries": ["US"],
            "impact": None,
            "daysLimit": (datetime.fromisoformat(to_d) - datetime.fromisoformat(from_d)).days + 1,
            "textSearch": None,
        },
        timeout=(10, 30),
    )
    r.raise_for_status()
    out = []
    for d in r.json().get("events", []):
        day = str(d.get("date", ""))[:10]
        if not day:
            continue
        ev = s.post(
            "https://www.cmegroup.com/services/economic-release-events",
            json={"date": day, "countries": ["US"], "impact": None, "textSearch": None, "size": 50},
            timeout=(10, 30),
        )
        ev.raise_for_status()
        for x in ev.json().get("events", []):
            ts = _iso(x.get("date", ""))
            if not ts:
                continue
            vals = {}
            for v in x.get("eventValues", []):
                for k in ("actual", "previous", "consensus"):
                    if v.get(k) is not None:
                        # Parse via _num directly: expanding "1.5M" to
                        # "1.5000000" would yield 1.5 instead of 1,500,000.
                        vals[k] = _num(v[k])
            imp = str(x.get("impact") or "").lower()
            out.append(
                {
                    "ts_utc": ts,
                    "name": str(x.get("eventName") or "").strip(),
                    "importance": {
                        "high": "high",
                        "market mover": "high",
                        "medium": "medium",
                        "low": "low",
                    }.get(imp, "unknown"),
                    "actual": vals.get("actual"),
                    "consensus": vals.get("consensus"),
                    "previous": vals.get("previous"),
                    "source": "CME",
                }
            )
    return out


def fetch_eodhd(from_d: str, to_d: str) -> list[dict]:
    tok = os.environ.get("EODHD_API_TOKEN", "")
    if not tok:
        return []  # optional provider unconfigured: no 401 per pull
    r = requests.get(
        "https://eodhd.com/api/economic-events",
        params={"from": from_d, "to": to_d, "country": "US", "api_token": tok, "fmt": "json"},
        headers=UA,
        timeout=(10, 30),
    )
    r.raise_for_status()
    out = []
    for x in r.json():
        ts = _iso(x.get("date", ""))
        if not ts:
            continue
        out.append(
            {
                "ts_utc": ts,
                "name": str(x.get("type") or x.get("event_name") or "").strip(),
                "importance": "unknown",
                "actual": _num(x.get("actual")),
                "consensus": _num(x.get("estimate")),
                "previous": _num(x.get("previous")),
                "source": "EODHD",
            }
        )
    return out
