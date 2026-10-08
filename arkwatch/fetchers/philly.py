"""philly.py — ADS index + SPF forecasts + Anxious Index (Philly Fed, curl_cffi).

Plain requests are rejected; curl_cffi impersonate='chrome' returns 200.

SPF (paket C, URLs pinned by the 29-agent research workflow 2026-09-04):
  medianGrowth.xlsx — quarterly % change forecasts (QoQ annualized): sheets
    RGDP/PGDP/CPI/..., columns YEAR, QUARTER, d<VAR>2..6 where the digit
    suffix is NOT the horizon: d<VAR>2 = horizon 0 (the survey-quarter
    nowcast), 3 = +1Q, ... 6 = +4Q (live-verified: RGDP2 @2026Q3 = 2.4624).
  anxious_index_chart.xlsx — 'Data' sheet, header at row 4 (index 3):
    Obs Year | Obs Quarter | Anxious Index | RECESS. The grid is PRE-FILLED
    to 2027Q4 — empty value cells are the future, not data; skip them.

Quarterly ts convention: the survey quarter maps to its START date
(2026Q3 → 2026-07-01), matching the registry quarter_start vocabulary.
"""

from __future__ import annotations

import io
from datetime import datetime

from curl_cffi import requests as cffi_requests
from openpyxl import load_workbook

ADS_URL = (
    "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/ads/"
    "ADS_Index_Most_Current_Vintage.xlsx"
)
SPF_GROWTH_URL = (
    "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/"
    "survey-of-professional-forecasters/historical-data/medianGrowth.xlsx"
)
ANXIOUS_URL = (
    "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/"
    "survey-of-professional-forecasters/anxious-index/anxious_index_chart.xlsx"
)

# sid suffix -> horizon column of medianGrowth.xlsx. Column headers are
# LOWERCASE 'd<var>N' (drgdp2, dpgdp2 — live-verified); d<VAR>2 = horizon 0
# (the survey-quarter nowcast). CPI and UNEMP are NOT in the growth workbook
# (13 sheets: NGDP PGDP CPROF EMP_* INDPROD HOUSING RGDP RCONSUM R*_INV
# RFEDGOV RSLGOV) — the inflation-forecast read here is PGDP (deflator);
# CPI nowcast coverage already lives at CLEVE:NOWCAST.
SPF_MAP = {
    "SPF_RGDP_NOW": "drgdp2",
    "SPF_PGDP_NOW": "dpgdp2",
}


class PhillyError(RuntimeError):
    pass


def _session() -> cffi_requests.Session:
    return cffi_requests.Session(impersonate="chrome")


def _download(url: str) -> bytes:
    s = _session()
    try:
        r = s.get(url, timeout=(10, 60))
        if r.status_code != 200 or r.content[:2] != b"PK":
            raise PhillyError(f"philly: HTTP {r.status_code} / not XLSX ({url[-60:]})")
        return r.content
    finally:
        s.close()


def fetch_latest(series_id: str = "PHILLY:ADS") -> dict:
    if series_id != "PHILLY:ADS":
        return _spf_latest(series_id)
    wb = load_workbook(io.BytesIO(_download(ADS_URL)), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    header_i = next(
        (
            i
            for i, row in enumerate(rows[:5])
            if row and any(isinstance(c, str) and "date" in c.lower() for c in row)
        ),
        None,
    )
    if header_i is None:
        raise PhillyError(f"philly ADS: 'date' header not found (sheet={wb.sheetnames[:3]})")
    header = [str(c).lower() if c else "" for c in rows[header_i]]
    di = next(i for i, c in enumerate(header) if "date" in c)
    # value column: the first numeric column AFTER the date (ADS index)
    last = None
    for row in rows[header_i + 1 :]:
        if row and row[di] is not None:
            for c in row[di + 1 :]:
                if isinstance(c, (int, float)):
                    last = (row[di], float(c))
                    break
    if last is None:
        raise PhillyError("philly ADS: no value rows")
    d, v = last
    if isinstance(d, datetime):
        d = d.date().isoformat()
    else:
        # AUDIT P1-4 (2026-09-13): the ADS XLSX types its date column as
        # TEXT 'YYYY:MM:DD' (colons) — passing it through stored
        # '2026:09:05' rows that break fromisoformat and sort AFTER real
        # ISO dates (':' > '-'). Normalize every text shape here.
        s = str(d).strip()
        for fmt in ("%Y:%m:%d", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                d = datetime.strptime(s, fmt).date().isoformat()
                break
            except ValueError:
                continue
        else:
            raise PhillyError(f"philly ADS: unparseable date cell {s!r}")
    return {"ts": str(d)[:10], "value": v}


# --- SPF (paket C) ----------------------------------------------------------------

_spf_cache: dict | None = None  # one download per workbook per process


def _growth_rows() -> list[tuple[str, dict[str, float]]]:
    """[(quarter_start_iso, {column: value})] from medianGrowth — memoized."""
    global _spf_cache
    if _spf_cache is not None and "growth" in _spf_cache:
        return _spf_cache["growth"]
    wb = load_workbook(io.BytesIO(_download(SPF_GROWTH_URL)), read_only=True, data_only=True)
    out: list[tuple[str, dict[str, float]]] = []
    for sheet in ("RGDP", "PGDP"):
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        header = [str(h).strip() if h is not None else "" for h in rows[0]]
        for row in rows[1:]:
            y, q = row[0], row[1]
            if not isinstance(y, (int, float)) or not isinstance(q, (int, float)):
                continue
            rec = {
                header[i]: float(row[i])
                for i in range(2, len(header))
                if header[i] and isinstance(row[i], (int, float))
            }
            if rec:
                out.append((_quarter_start(int(y), int(q)), rec))
    if not out:
        raise PhillyError("philly SPF growth workbook: no rows")
    _spf_cache = _spf_cache or {}
    _spf_cache["growth"] = out
    return out


def _quarter_start(year: int, quarter: int) -> str:
    if not 1 <= quarter <= 4:
        raise PhillyError(f"philly SPF: bad quarter {quarter}")
    return f"{year}-{(quarter - 1) * 3 + 1:02d}-01"


def _spf_latest(series_id: str) -> dict:
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if key == "ANXIOUS":
        return _anxious_latest()
    if key not in SPF_MAP:
        raise PhillyError(f"philly: unrouted series {series_id}")
    col = SPF_MAP[key]
    # max-date scan (never rows[-1] — the CBOE lesson)
    best: tuple[str, float] | None = None
    for ts, rec in _growth_rows():
        v = rec.get(col)
        if v is not None and (best is None or ts > best[0]):
            best = (ts, v)
    if best is None:
        raise PhillyError(f"philly SPF: column '{col}' has no values")
    return {"ts": best[0], "value": round(best[1], 4)}


def _anxious_latest() -> dict:
    wb = load_workbook(io.BytesIO(_download(ANXIOUS_URL)), read_only=True, data_only=True)
    ws = wb["Data"]
    rows = list(ws.iter_rows(values_only=True))
    # header at row index 3: Obs Year | Obs Quarter | Anxious Index | RECESS
    header_i = next(
        (
            i
            for i, r in enumerate(rows[:6])
            if r and any(str(c or "").strip() == "Obs Year" for c in r)
        ),
        None,
    )
    if header_i is None:
        raise PhillyError("philly anxious: 'Obs Year' header not found")
    best: tuple[str, float] | None = None
    for y, q, v, *_rest in rows[header_i + 1 :]:
        if (
            isinstance(y, (int, float))
            and isinstance(q, (int, float))
            and isinstance(v, (int, float))
            and (best is None or _quarter_start(int(y), int(q)) > best[0])
        ):
            best = (_quarter_start(int(y), int(q)), float(v))
    if best is None:
        raise PhillyError("philly anxious: no values")
    return {"ts": best[0], "value": round(best[1], 4)}
