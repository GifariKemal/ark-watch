"""cleve.py — Cleveland Fed: inflation nowcast + inflation-expectations model.

Nowcast (FusionCharts JSON, no key): a list of objects per target month; the
LAST element is the current month. subcaption is "YYYY-M";
categories[].category[].label is "MM/DD" without a year; dataset[] =
{seriesname, data:[{value: str|''}]}. The CPI nowcast value is the last
non-empty value of the dataset whose seriesname starts with 'CPI Inflation'.

Expectations (audit 2026-09-04, paket B): the workbook at EXPECTATIONS_URL is
an XLSX served under a .csv name — sheets 'Expected Inflation' (Model Output
Date + 1y..30y columns) and 'Real Interest Rate' (1-month/1-year/10-year).
Monthly Model Output Dates (~537 rows, early-1980s →). Values are decimals
(0.0239 = 2.39%) — converted to pct at the parse boundary. One download
serves all four sids (module-level memo per process).
"""

from __future__ import annotations

import io
from datetime import date, datetime

import requests
from openpyxl import load_workbook

URL = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json"
EXPECTATIONS_URL = (
    "https://www.clevelandfed.org/-/media/files/webcharts/inflationexpectations/"
    "inflation-expectations.csv"
)
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
}


class CleveError(RuntimeError):
    pass


def fetch_latest(series_id: str = "CLEVE:NOWCAST") -> dict:
    bare = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if bare in RECPROB_KEYS:
        return _recprob_latest(bare)
    if series_id != "CLEVE:NOWCAST":
        return _expectations_latest(series_id)
    r = requests.get(URL, headers=UA, timeout=(10, 30))
    if r.status_code != 200:
        raise CleveError(f"cleveland: HTTP {r.status_code}")
    months = r.json()
    if not isinstance(months, list) or not months:
        raise CleveError("cleveland: empty response")
    cur = months[-1]
    chart = cur["chart"]
    # ts = model-run date (chart._comment "YYYY-MM-DD HH:MM") — more robust
    # than category labels, which can contain vline entries (e.g. "CPI Jul")
    ts = str(chart.get("_comment", ""))[:10]
    if len(ts) != 10:
        raise CleveError("cleveland: chart._comment (model-run date) unreadable")
    series = next(
        (d for d in cur.get("dataset", []) if d.get("seriesname", "").startswith("CPI Inflation")),
        None,
    )
    if series is None:
        raise CleveError("cleveland: dataset 'CPI Inflation' missing")
    vals = [d["value"] for d in series["data"] if d.get("value") not in ("", None)]
    if not vals:
        raise CleveError("cleveland: no non-empty nowcast values")
    return {"ts": ts, "value": float(vals[-1])}


# sid suffix -> (sheet, column-header fragment). Column headers carry leading
# spaces (' 1 year Expected Inflation'); matching is substring-based.
EXPECTATION_MAP = {
    "EXPINF_1Y": ("Expected Inflation", "1 year"),
    "EXPINF_10Y": ("Expected Inflation", "10 year"),
    "REALRATE_1Y": ("Real Interest Rate", "Real Rate 1-year"),
    "REALRATE_10Y": ("Real Interest Rate", "Real Rate 10-year"),
}

# The 'Ten-year Expected Chart' sheet is PERCENT-NATIVE (2.49 = 2.49%, unlike
# the other sheets' 0.0249 decimals) and carries the model's own risk-premia
# decomposition (live 2026-08: IRP 0.4436, Real Risk Premium 1.3380).
PREMIA_MAP = {
    "IRP_10Y_MODEL": "Inflation Risk Premium",
    "RRP_10Y_MODEL": "Real Risk Premium",
}
_TEN_YEAR_SHEET = "Ten-year Expected Chart"

_expectations_cache: dict | None = None  # one workbook per process


def _expectations_workbook() -> dict:
    """{sheet: (header_list, [(model_date_iso, row_values)])} — one download.

    The header lives in the sheet's own header row (a missing cell in the
    LATEST month must not make the column unfindable — the rows[-1]-keys
    lookup had exactly that hole); date-sortedness is NOT assumed (callers
    scan max/min — the CBOE rows[-1] lesson).
    """
    global _expectations_cache
    if _expectations_cache is not None:
        return _expectations_cache
    r = requests.get(EXPECTATIONS_URL, headers=UA, timeout=(10, 30))
    if r.status_code != 200 or r.content[:2] != b"PK":
        raise CleveError(f"cleveland expectations: HTTP {r.status_code} / not XLSX")
    wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    out: dict[str, tuple] = {}
    # The premia sheet is auxiliary — a workbook without it (schema change,
    # test fixtures) still serves the four base series; _premia_latest raises
    # its own clear error when the sheet is genuinely absent.
    sheets = ["Expected Inflation", "Real Interest Rate"]
    if _TEN_YEAR_SHEET in wb.sheetnames:
        sheets.append(_TEN_YEAR_SHEET)
    for sheet in sheets:
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        header = [str(h).strip() if h is not None else "" for h in rows[0]]
        series_rows = []
        for row in rows[1:]:
            if isinstance(row[0], datetime):
                series_rows.append((row[0].date().isoformat(), row))
        if not series_rows:
            raise CleveError(f"cleveland expectations: sheet '{sheet}' empty")
        out[sheet] = (header, series_rows)
    wb.close()
    _expectations_cache = out
    return out


def _expectations_key(series_id: str) -> str:
    """Tolerant key resolution: accepts 'CLEVE:EXPINF_1Y', 'EXPINF_1Y', and
    descriptive strings CONTAINING a known key (the verify_sources
    primary_source-derived ref can be mangled)."""
    for k in EXPECTATION_MAP:
        if k in series_id:
            return k
    raise CleveError(f"cleveland: unrouted series {series_id}")


def _premia_latest(series_id: str) -> dict:
    """The Ten-year Expected Chart legs — PERCENT-NATIVE (no ×100)."""
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if key not in PREMIA_MAP:
        raise CleveError(f"cleveland: unrouted series {series_id}")
    wb = _expectations_workbook()
    if _TEN_YEAR_SHEET not in wb:
        # KeyError would surface raw from the dict access — convert so the
        # harvest loop's per-series fetch_log ERROR carries a readable cause
        raise CleveError(f"cleveland premia: sheet '{_TEN_YEAR_SHEET}' absent")
    header, rows = wb[_TEN_YEAR_SHEET]
    ci = _column_index(header, PREMIA_MAP[key])
    best: tuple[str, float] | None = None
    for ts, row in rows:
        v = row[ci] if ci < len(row) else None
        if isinstance(v, (int, float)) and (best is None or ts > best[0]):
            best = (ts, float(v))
    if best is None:
        raise CleveError(f"cleveland premia: no numeric values for '{PREMIA_MAP[key]}'")
    return {"ts": best[0], "value": round(best[1], 4)}


def _column_index(header: list[str], frag: str) -> int:
    """Header-based column pick via stripped STARTSWITH — substring containment
    would match '1 year' inside '11 year' (the sheet carries 1y..30y columns);
    prefix matching keeps the digit boundary exact. Pinned by tests."""
    hits = [i for i, h in enumerate(header) if h.lower().startswith(frag.lower())]
    if len(hits) != 1:
        raise CleveError(
            f"cleveland expectations: column '{frag}' ambiguous/missing ({len(hits)} hits)"
        )
    return hits[0]


def _expectations_latest(series_id: str) -> dict:
    if any(k in series_id for k in PREMIA_MAP):
        return _premia_latest(series_id)
    key = _expectations_key(series_id)
    sheet, frag = EXPECTATION_MAP[key]
    header, rows = _expectations_workbook()[sheet]
    ci = _column_index(header, frag)
    # scan for the MAXIMUM date — the workbook is date-ascending today, but
    # sortedness is an assumption, not a contract (CBOE rows[-1] lesson)
    best: tuple[str, float] | None = None
    for ts, row in rows:
        v = row[ci] if ci < len(row) else None
        if isinstance(v, (int, float)) and (best is None or ts > best[0]):
            best = (ts, float(v))
    if best is None:
        raise CleveError(f"cleveland expectations: no numeric values for '{frag}'")
    return {"ts": best[0], "value": round(best[1] * 100.0, 4)}  # decimal -> pct


def fetch_first_ts(series_id: str) -> str:
    """Depth-gate helper (verify_sources Gate 3): earliest ts per family."""
    bare = series_id.split(":", 1)[1] if ":" in series_id else series_id
    if bare in RECPROB_KEYS:
        return _recprob_first_ts(bare)
    if bare in PREMIA_MAP:
        if _TEN_YEAR_SHEET not in _expectations_workbook():
            raise CleveError(f"cleveland: premia sheet '{_TEN_YEAR_SHEET}' absent")
        _header, rows = _expectations_workbook()[_TEN_YEAR_SHEET]
        return min(ts for ts, _row in rows)
    key = _expectations_key(series_id)
    sheet = EXPECTATION_MAP[key][0]
    _header, rows = _expectations_workbook()[sheet]
    return min(ts for ts, _row in rows)


# --- Yield-curve recession probability (paket C; URL pinned by the research
# workflow 2026-09-04). Two legs in one CSV: recession_probability (the model
# estimate, filled through the latest month) and recession_probability_forecast
# (extends +12 months). Dates are M/D/YYYY; the file carries full history
# (1960→) — the estimate leg is the series, the forecast leg is stored as its
# own forward-looking series at the same model-month ts.
RECPROB_URL = (
    "https://www.clevelandfed.org/-/media/files/webcharts/yieldcurve/"
    "chart2_recession_probability_w_forecast.csv"
)
RECPROB_KEYS = ("RECPROB", "RECPROB_F12")
_recprob_cache: list[tuple[str, float | None, float | None]] | None = None


def _recprob_rows() -> list[tuple[str, float | None, float | None]]:
    """[(iso_date, est, forecast)] — memoized single download, M/D/YYYY→ISO."""
    global _recprob_cache
    if _recprob_cache is not None:
        return _recprob_cache
    r = requests.get(RECPROB_URL, headers=UA, timeout=(10, 30))
    if r.status_code != 200 or not r.text.lstrip().startswith("date"):
        raise CleveError(f"cleveland recprob: HTTP {r.status_code} / not CSV")
    out: list[tuple[str, float | None, float | None]] = []
    for ln in r.text.strip().splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 3:
            continue
        try:
            m, d, y = parts[0].split("/")
            ts = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            continue
        est = float(parts[1]) if parts[1] not in ("", "null") else None
        fc = float(parts[2]) if parts[2] not in ("", "null") else None
        if est is not None or fc is not None:
            out.append((ts, est, fc))
    if not out:
        raise CleveError("cleveland recprob: empty")
    _recprob_cache = out
    return out


def _plus_months(ts: str, n: int) -> str:
    """Calendar-month arithmetic on a month-start ISO ts (exact grid landing)."""
    d = date.fromisoformat(ts)
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return f"{y:04d}-{m:02d}-01"


def _f12_pairs() -> list[tuple[str, float]]:
    """[(model_month, +12m forecast made in that month)] — vintage pairs.

    Target computed by CALENDAR month arithmetic (always lands on the
    month-start grid; the old timedelta(days=365) drifted across Februaries).
    A ±5d fallback covers any residual publication-date quirk — NOT ±31d,
    which would borrow a NEIGHBORING month's forecast (a +11m value silently
    stored as +12m). Months whose true target row has no forecast are simply
    skipped (the CSV only carries the recent vintage window).
    """
    rows = _recprob_rows()
    fc_by_ts = {ts: fc for ts, _est, fc in rows if fc is not None}
    pairs = []
    for ts, est, _fc in rows:
        if est is None:
            continue
        target = _plus_months(ts, 12)
        cand = fc_by_ts.get(target)
        if cand is None:
            near = [
                t
                for t in fc_by_ts
                if 0 < abs((date.fromisoformat(t) - date.fromisoformat(target)).days) <= 5
            ]
            if near:
                cand = fc_by_ts[
                    min(
                        near,
                        key=lambda t: abs(
                            (date.fromisoformat(t) - date.fromisoformat(target)).days
                        ),
                    )
                ]
        if cand is not None:
            pairs.append((ts, cand))
    return pairs


def _recprob_first_ts(key: str) -> str:
    if key == "RECPROB":
        first = min((ts for ts, est, _fc in _recprob_rows() if est is not None), default=None)
        if first is None:
            raise CleveError("cleveland recprob: est leg empty")
        return first
    pairs = _f12_pairs()
    if not pairs:
        raise CleveError("cleveland recprob: F12 pairing empty")
    return min(ts for ts, _ in pairs)


def _recprob_latest(key: str) -> dict:
    rows = _recprob_rows()
    if key == "RECPROB":
        best: tuple[str, float] | None = None
        for ts, est, _fc in rows:
            if est is not None and (best is None or ts > best[0]):
                best = (ts, est)
        if best is None:
            raise CleveError("cleveland recprob: est leg empty")
        return {"ts": best[0], "value": round(best[1], 4)}
    pairs = _f12_pairs()
    if not pairs:
        raise CleveError("cleveland recprob: F12 pairing empty")
    ts, v = max(pairs)
    return {"ts": ts, "value": round(v, 4)}
