"""ecb.py — ECB Data Portal: €STR daily fixing (and DFR as backup context).

€STR = Euro Short-Term Rate, the eurosystem's overnight benchmark (the
euro analog of SOFR). It is the underlying of the CME 3M €STR futures (ESR)
that ESTRWatch derives ECB hike/cut probabilities from — so the anchor of
that math must be the actual fixing, never the deposit rate minus an
assumed spread (±5bp anchor error = ±20pp probability error).

Series key in the registry: ECB:ESTR. The portal is free, no key; a
browser-like User-Agent is required (bare requests get 503 — verified
2026-09-10). The fixing for day T publishes ~08:00 CET on T+1, i.e. AFTER
the f2 job at 08:30 WIB, so the anchor lags ≤2 business days — absorbed by
the implementation-date convention in transforms/ecbwatch.py.
"""

from __future__ import annotations

import requests

PORTAL = "https://data-api.ecb.europa.eu/service/data"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/128.0 Safari/537.36 arkwatch/0.1 (personal research)"
    ),
    "Accept": "text/csv",
}

ESTR_KEY = "EST/B.EU000A2X2A25.WT"
DFR_KEY = "FM/B.U2.EUR.4F.KR.DFR.LEV"


class EcbError(RuntimeError):
    pass


def _csv_rows(key: str, last_n: int, start: str | None, first_n: int | None = None) -> list[dict]:
    params: dict[str, str | int] = {"format": "csvdata"}
    if start:
        params["startPeriod"] = start
        if first_n:
            # depth-gate path: only the FIRST observation, not full history
            # (review P2: a bare start= downloads ~550KB every verify run)
            params["firstNObservations"] = first_n
    else:
        params["lastNObservations"] = last_n
    r = requests.get(f"{PORTAL}/{key}", params=params, headers=UA, timeout=(10, 30))
    if r.status_code != 200:
        raise EcbError(f"ECB portal {key.split('/')[0]}: HTTP {r.status_code}")
    # csvdata: header row + one row per observation
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    if len(lines) < 2 or "TIME_PERIOD" not in lines[0]:
        raise EcbError("ECB portal: unexpected CSV shape")
    hdr = lines[0].split(",")
    i_ts, i_v = hdr.index("TIME_PERIOD"), hdr.index("OBS_VALUE")
    out = []
    for ln in lines[1:]:
        cells = ln.split(",")
        if len(cells) <= max(i_ts, i_v):
            continue
        try:
            out.append({"ts": cells[i_ts], "value": float(cells[i_v])})
        except ValueError:
            continue
    return out


def fetch_estr_fixings(last_n: int = 30, start: str | None = None) -> list[dict]:
    """€STR fixings, percent — [{ts: 'YYYY-MM-DD', value: 2.189}] ascending.

    `start` (ISO date) fetches everything from that date (backfill);
    otherwise the last `last_n` observations (incremental daily pull)."""
    rows = _csv_rows(ESTR_KEY, last_n, start)
    if not rows:
        raise EcbError("ECB €STR: no observations returned")
    return sorted(rows, key=lambda r: r["ts"])


def fetch_latest(series_id: str) -> dict:
    """Registry dispatcher interface (the TREASURY/nyfed pattern)."""
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if key != "ESTR":
        raise EcbError(f"ecb: unrouted series {series_id}")
    rows = fetch_estr_fixings(last_n=5)
    last = rows[-1]
    return {"ts": last["ts"], "value": last["value"]}


def fetch_first_ts(series_id: str) -> str:
    """Depth gate: €STR history starts 2019-10-02 (series launch)."""
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if key != "ESTR":
        raise EcbError(f"ecb: unrouted series {series_id}")
    rows = _csv_rows(ESTR_KEY, last_n=1, start="2019-10-01", first_n=1)
    return rows[0]["ts"] if rows else "2019-10-02"


def fetch_dfr(last_n: int = 5) -> list[dict]:
    """Deposit Facility Rate from the portal — backup when FRED:ECBDFR is
    stale (both carry the same series; kept for the anchor cross-check)."""
    return _csv_rows(DFR_KEY, last_n, None)
