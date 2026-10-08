"""misc.py — Atlanta Fed mortgage rate, FMP holidays + recession probability, small fetchers."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import requests

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
}


def fetch_latest(series_id: str) -> dict:
    """ROUTES dispatcher (06:00 harvest): {ts, value} for the misc-owned series."""
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if key == "RECESSION_PROB":
        return fetch_recession_prob()
    raise RuntimeError(f"misc: unrouted series {series_id}")


def fetch_recession_prob(lookback_days: int = 200) -> dict:
    """Smoothed US recession probability (FMP economic-indicators, monthly).

    Live-verified 2026-09-04: {date: 2026-07-01, value: 0.76} — value is the
    probability on a 0-100 scale, one observation per month (publication lags
    the reference month). Window ≥180d so at least ~6 monthly points can
    return; we store only the newest.
    """
    key = os.environ.get("FMP_API_KEY", "")
    if not key:
        raise RuntimeError("FMP_API_KEY not set (env/.env)")
    to = datetime.now(UTC).date()
    fr = to - timedelta(days=lookback_days)
    r = requests.get(
        "https://financialmodelingprep.com/stable/economic-indicators",
        params={
            "name": "smoothedUSRecessionProbabilities",
            "from": fr.isoformat(),
            "to": to.isoformat(),
            "apikey": key,
        },
        timeout=(10, 30),
    )
    if r.status_code != 200:
        raise RuntimeError(f"FMP recession-prob: HTTP {r.status_code}")
    rows = [x for x in r.json() if x.get("date") and x.get("value") is not None]
    if not rows:
        raise RuntimeError("FMP recession-prob: empty response")
    newest = max(rows, key=lambda x: x["date"][:10])
    return {"ts": newest["date"][:10], "value": float(newest["value"])}


FMP_EARNINGS_PAGE_CAP = 4000  # round-3 P1: page 0 truncates newest-first at
# exactly 4000 rows in season — the NEAREST (actionable) weeks never land


def fetch_earnings_calendar(from_d: str, to_d: str, page: int = 0) -> list[dict]:
    """FMP earnings calendar window, ALL pages walked (round-3 P1: a single
    page-0 GET at the 42d harvest window returns exactly the 4000-row cap in
    NEWEST-FIRST order — the near weeks silently never landed; page 1 carried
    the missing 1230 rows). [{symbol, date, eps_estimated, eps_actual,
    revenue_estimated, revenue_actual, last_updated}]."""
    key = os.environ.get("FMP_API_KEY", "")
    if not key:
        raise RuntimeError("FMP_API_KEY not set")
    out: list[dict] = []
    seen: set[tuple] = set()
    while True:
        r = requests.get(
            "https://financialmodelingprep.com/stable/earnings-calendar",
            params={"from": from_d, "to": to_d, "page": page, "apikey": key},
            timeout=(10, 60),
        )
        if r.status_code != 200:
            raise RuntimeError(f"FMP earnings-calendar: HTTP {r.status_code}")
        rows = r.json()
        if not isinstance(rows, list):
            raise RuntimeError("FMP earnings-calendar: unrecognized shape")
        for x in rows:
            if not (x.get("symbol") and x.get("date")):
                continue
            k = (x["symbol"], str(x["date"])[:10])
            if k in seen:
                continue
            seen.add(k)
            out.append(
                {
                    "symbol": x.get("symbol", ""),
                    "date": str(x.get("date", ""))[:10],
                    "eps_estimated": x.get("epsEstimated"),
                    "eps_actual": x.get("epsActual"),
                    "revenue_estimated": x.get("revenueEstimated"),
                    "revenue_actual": x.get("revenueActual"),
                    "last_updated": str(x.get("lastUpdated", ""))[:10],
                }
            )
        if len(rows) < FMP_EARNINGS_PAGE_CAP:
            return out
        page += 1  # full page → the window has more pages (cap truncation)
        if page > 10:  # ronde-5: bound the walk — a server that ignores the
            return out  # page param (always-full pages) must not loop forever


# fetch_fmp_holidays DELETED 2026-09-13 (vendor-api audit #5): zero callers
# since it landed — the CME harvest walk-back already treats empty days as
# soft-holidays, making the pre-check dead weight. holidays-by-exchange
# remains documented in the vendor docs if a scheduler-holiday feature
# (NICE_TO_HAVE #8) is ever built.


def fetch_mpt_mortgage() -> dict | None:
    """Atlanta Fed Mortgage Analytics Tool (30Y fixed mortgage rate).

    NOTE: The Atlanta Fed CQER page was retired in 2021. The canonical 30Y
    fixed mortgage rate is tracked via FRED:MORTGAGE30US (Freddie Mac PMMS).
    """
    return None
