"""eodhd.py — EODHD fetchers (funding-stress, CMDI, CDS, UST curves, sentiments).

Verified pitfalls:
  - `code=`/`central_bank=` params are IGNORED by the server (the response
    contains all codes) -> filter client-side
  - key via env EODHD_API_TOKEN; the auth param MUST be `api_token=` (not api_key)
  - browser UA required (some plain clients are rejected)
  - CDS_US is annual Damodaran data in FRACTIONS (x10^4 = bps)
  - /ust/* returns {meta, data, links} with data = flat [{date, tenor, rate}]
    rows for ALL tenors (window params are advisory — filter client-side)
  - /api/sentiments is NEWS-based and SPARSE (a few points per month for
    btc-usd.cc), scale −1..+1 — every display carries its date
  - policy-rates RETIRED 2026-09-13 (vendor-api audit): the endpoint hosts
    FED+ECB only and the BoE branch never had rows; FRED:DFF + the ECB Data
    Portal cover both free, so the branch is deleted, not dormant
"""

from __future__ import annotations

import os

import requests

BASE = "https://eodhd.com/api"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
}


class EodhdError(RuntimeError):
    pass


def _token() -> str:
    t = os.environ.get("EODHD_API_TOKEN", "")
    if not t:
        raise EodhdError("EODHD_API_TOKEN not set (env/.env)")
    return t


def _get(path: str, params: dict | None = None) -> dict:
    p = {"api_token": _token(), "fmt": "json"}
    p.update(params or {})
    r = requests.get(f"{BASE}{path}", params=p, headers=UA, timeout=(10, 30))
    if r.status_code != 200:
        raise EodhdError(f"EODHD {path}: HTTP {r.status_code} — {r.text[:200]}")
    j = r.json()
    if not isinstance(j, dict) or "data" not in j:
        raise EodhdError(f"EODHD {path}: unrecognized response shape")
    return j["data"]


def fetch_ust_real_yields(tenor: str, days: int = 10) -> list[dict]:
    """Daily TIPS real yields (5Y/7Y/10Y/20Y/30Y) — the block-B secondary
    leg the registry has promised all along. [{ts, value}] ascending."""
    rows = [r for r in _get("/ust/real-yield-rates") if r.get("tenor") == tenor]
    if not rows:
        raise EodhdError(f"ust/real-yield: tenor {tenor} missing")
    rows.sort(key=lambda r: r["date"])
    return [{"ts": r["date"], "value": float(r["rate"])} for r in rows[-days:]]


def fetch_ust_nominal(tenor: str, days: int = 10) -> list[dict]:
    """Daily nominal UST curve (1M..30Y) — crossval leg for FRED:DGS*."""
    rows = [r for r in _get("/ust/yield-rates") if r.get("tenor") == tenor]
    if not rows:
        raise EodhdError(f"ust/yield: tenor {tenor} missing")
    rows.sort(key=lambda r: r["date"])
    return [{"ts": r["date"], "value": float(r["rate"])} for r in rows[-days:]]


def fetch_sentiments(tickers: str = "btc-usd.cc,eth-usd.cc") -> dict[str, list[dict]]:
    """News-based sentiment per ticker, scale −1..+1 (sparse). One call
    carries all tickers. {'BTC-USD.CC': [{ts, value, count}]} descending by
    the API's own ordering (latest first per ticker in practice; we sort)."""
    p = {"api_token": _token(), "fmt": "json", "s": tickers}
    r = requests.get(f"{BASE}/sentiments", params=p, headers=UA, timeout=(10, 30))
    if r.status_code != 200:
        raise EodhdError(f"sentiments: HTTP {r.status_code}")
    j = r.json()
    if not isinstance(j, dict):
        raise EodhdError("sentiments: unrecognized response shape")
    out = {}
    for tick, rows in j.items():
        pts = [
            {"ts": x["date"], "value": float(x["normalized"]), "count": x.get("count", 0)}
            for x in rows
            if isinstance(x, dict) and x.get("normalized") is not None
        ]
        pts.sort(key=lambda x: x["ts"])
        out[tick.upper()] = pts
    return out


CMDI_COLUMNS = {"CMDI": "market_cmdi", "CMDI_IG": "ig_cmdi", "CMDI_HY": "hy_cmdi"}

EOD_TICKERS = {"BDI": "BDIY.INDX"}


def _eod_rows(ticker: str, days: int) -> list[dict]:
    from datetime import UTC, datetime, timedelta

    to = datetime.now(UTC).date()
    fr = to - timedelta(days=days)
    r = requests.get(
        f"{BASE}/eod/{ticker}",
        params={
            "api_token": _token(),
            "fmt": "json",
            "from": fr.isoformat(),
            "to": to.isoformat(),
        },
        headers=UA,
        timeout=(10, 30),
    )
    if r.status_code != 200:
        raise EodhdError(f"EODHD eod/{ticker}: HTTP {r.status_code}")
    rows = r.json()
    if not isinstance(rows, list):
        raise EodhdError(f"EODHD eod/{ticker}: unrecognized shape")
    return [
        {"ts": x["date"][:10], "value": float(x["close"])}
        for x in rows
        if x.get("close") is not None
    ]


_cmdi_cache: list[dict] | None = None


def fetch_cmdi_all() -> list[dict]:
    """Full CMDI history, pagination-walked (REACTIVATION 2026-09-20).

    The endpoint pages at 20 rows by default (page[limit]/page[offset]) —
    the 2026-09-17 deactivation was exactly this missing walk: 1 obs stored
    vs 1,129 expected. Walks 100/page until a short page, ascending by date.
    Process-cached: one walk serves all three series."""
    global _cmdi_cache
    if _cmdi_cache is not None:
        return _cmdi_cache
    rows: list[dict] = []
    offset = 0
    while True:
        page = _get("/credit-risk/corporate/cmdi", {"page[limit]": 100, "page[offset]": offset})
        if not page:
            break
        rows.extend(page)
        if len(page) < 100:
            break
        offset += 100
        if offset > 5000:  # runaway backstop (full history is ~12 pages)
            raise EodhdError("cmdi: pagination exceeded 5,000 rows — refusing")
    if len(rows) < 500:
        raise EodhdError(f"cmdi: suspiciously short history ({len(rows)} rows)")
    rows.sort(key=lambda r: r["as_of_date"])
    _cmdi_cache = rows
    return rows


def _cmdi_window(series_id: str, days: int) -> list[dict]:
    """Full-history window for a weekly series with a multi-week release lag.

    AUDIT 2026-09-20: the original 8,000d rolling floor EXPIRED on
    2026-12-03 (2005-01-07 + 8,000d) — after that a cold-start rebuild
    would silently start at the cutoff with every gate green, a dated
    recurrence of the phantom class. The fix removes date math entirely:
    the pagination walk IS the window (whatever the vendor serves, we
    land), so total gap-heal holds by construction and no magic number
    can age out. `days` is accepted for the fetch_window contract and
    deliberately ignored."""
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    col = CMDI_COLUMNS.get(key)
    if col is None:
        raise EodhdError(f"cmdi: unrouted series {series_id}")
    return [
        {"ts": r["as_of_date"][:10], "value": float(r[col])}
        for r in fetch_cmdi_all()
        if r.get(col) is not None
    ]


def fetch_window(series_id: str, days: int = 12) -> list[dict]:
    """GAP-HEAL (audit P1-1): the funding-stress response already carries a
    ~19-day window per code — landing only the max row left shutdown-day
    holes permanent. Return every row for the code (PK dedup upstream).

    Unsupported series return [] (ronde-5): raising made every non-FS series
    log a daily WINDOW_FALLBACK error, burying real FS-window failures in
    expected noise — an empty list is the honest 'no window support' signal
    and the harvest falls back silently, as designed."""
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if key in EOD_TICKERS:
        return _eod_rows(EOD_TICKERS[key], max(days * 2, 120))
    if key in CMDI_COLUMNS:
        out = _cmdi_window(series_id, days)
        # dead-feed guard, lag-aware: OBSERVED lag is 2-4wk (2026-09-20:
        # newest print 30d old while the feed is alive) — 60d is double the
        # observed worst, so it only fires on a genuinely dead feed (the
        # raise lands as a fetch_log WINDOW_FALLBACK note; the owner-facing
        # early signal is the brief stale suffix at 45d, which fires first)
        from datetime import UTC, date, datetime

        if out:
            newest = date.fromisoformat(out[-1]["ts"])
            if (datetime.now(UTC).date() - newest).days > 60:
                raise EodhdError(f"cmdi window stale: newest {out[-1]['ts']}")
        return out
    if not key.startswith("FS_"):
        return []
    code = key[3:]
    rows = [r for r in _get("/spreads/funding-stress") if r.get("code") == code]
    rows.sort(key=lambda r: r["date"])
    out = [
        {"ts": r["date"], "value": float(r["value_bps"])}
        for r in rows[-int(days * 1.8) :]
        if r.get("value_bps") is not None
    ]
    # ronde-6 P2-8: server-shrink guard — if the newest point is >5 days
    # old the window is stale (a shrunk/truncated response heals nothing);
    # raise so the harvest records WINDOW_FALLBACK instead of a silent
    # partial heal
    from datetime import UTC, date, datetime

    if out:
        newest = date.fromisoformat(out[-1]["ts"])
        if (datetime.now(UTC).date() - newest).days > 5:
            raise EodhdError(f"funding-stress window stale: newest {out[-1]['ts']}")
    return out


def fetch_latest(series_id: str) -> dict:
    """Takes a series_id without prefix (e.g. FS_EFFR_SOFR). Returns {ts, value}."""
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id

    if key.startswith("FS_"):
        code = key[3:]
        rows = [r for r in _get("/spreads/funding-stress") if r.get("code") == code]
        if not rows:
            raise EodhdError(f"funding-stress: code {code} missing from response")
        r0 = max(rows, key=lambda r: r["date"])
        return {"ts": r0["date"], "value": float(r0["value_bps"])}

    if key in EOD_TICKERS:
        rows = _eod_rows(EOD_TICKERS[key], 30)
        if not rows:
            raise EodhdError(f"eod {key}: empty window")
        return rows[-1]

    if key in CMDI_COLUMNS:
        # default page carries the NEWEST rows (probe-verified) — page-1 max
        # is the latest print; the full walk lives in the window path
        rows = _get("/credit-risk/corporate/cmdi")
        r0 = max(rows, key=lambda r: r["as_of_date"][:10])
        return {"ts": r0["as_of_date"][:10], "value": float(r0[CMDI_COLUMNS[key]])}

    if key == "CDS_US":
        rows = [
            r
            for r in _get("/credit-risk/sovereign/cds-spreads", {"filter[country]": "USA"})
            if r.get("country_iso3") == "USA"
        ]
        if not rows:
            raise EodhdError("cds-spreads: no USA rows")
        r0 = max(rows, key=lambda r: r["as_of_date"][:10])
        return {"ts": r0["as_of_date"][:10], "value": float(r0["cds_spread"])}

    if key.startswith("USTR"):
        # USTR10 / USTR5 → TIPS real yield (block-B secondary)
        tenor = key[4:]
        if not tenor.endswith("Y"):
            tenor += "Y"
        rows = fetch_ust_real_yields(tenor, days=5)
        return rows[-1]

    if key.startswith("SENT_"):
        tick = {"BTC": "BTC-USD", "ETH": "ETH-USD"}.get(key[5:])
        if not tick:
            raise EodhdError(f"sentiments: unknown ticker {key}")
        data = fetch_sentiments()
        pts = next((v for k, v in data.items() if k.startswith(tick)), None)
        if not pts:
            raise EodhdError(f"sentiments: no points for {tick}")
        return {"ts": pts[-1]["ts"], "value": pts[-1]["value"]}

    raise EodhdError(f"unknown EODHD series: {key}")
