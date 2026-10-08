"""geo.py — free geopolitics & food indices: GPR (Caldara-Iacoviello),
GPRD daily, FAO Food Price Index, Harper Petersen HARPEX."""

from __future__ import annotations

import csv
import json

import requests

GPR_MONTHLY_URL = "https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls"
GPR_DAILY_URL = "https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls"
FAO_PAGE_URL = "https://www.fao.org/worldfoodsituation/FoodPricesIndex/en"
FAO_FALLBACK_URL = (
    "https://www.fao.org/media/docs/worldfoodsituationlibraries/"
    "wfs-library/food_price_indices_data.csv?sfvrsn=523ebd2a_84&download=true"
)


def _get_fao_csv_url() -> str:
    """Resolve the latest active FAO CSV URL from the landing page, with fallback."""
    import re

    try:
        r = requests.get(FAO_PAGE_URL, headers=UA, timeout=(10, 15))
        if r.status_code == 200:
            m = re.search(r'href="([^"]+food_price_indices_data\.csv[^"]*)"', r.text)
            if m:
                from urllib.parse import urljoin, urlparse

                url = urljoin(FAO_PAGE_URL, m.group(1).replace("&amp;", "&"))
                # scraped href: only follow it to fao.org over https
                u = urlparse(url)
                host = u.hostname or ""
                if u.scheme == "https" and (host == "fao.org" or host.endswith(".fao.org")):
                    return url
    except Exception:
        pass
    return FAO_FALLBACK_URL


HARPEX_URL = "https://www.harperpetersen.com/harpex"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0 Safari/537.36"}


class GeoError(RuntimeError):
    pass


def _get(url: str) -> bytes:
    r = requests.get(url, headers=UA, timeout=(10, 60))
    if r.status_code != 200 or not r.content:
        raise GeoError(f"geo: HTTP {r.status_code} — {url[-60:]}")
    return r.content


def _xls_rows(url: str):
    import xlrd

    wb = xlrd.open_workbook(file_contents=_get(url))
    ws = wb.sheet_by_index(0)
    return [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]


def _header_index(rows, must_have):
    for i, row in enumerate(rows[:10]):
        if any(str(c).strip() == must_have for c in row if c):
            return i
    raise GeoError(f"geo: header with '{must_have}' not found")


def _f(v):
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _month_start(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}-01"


def parse_gpr_rows(rows, column="GPR"):
    hi = _header_index(rows, column)
    header = [str(c).strip() for c in rows[hi]]
    ci = header.index(column)
    mi = header.index("month")
    out = []
    for row in rows[hi + 1 :]:
        v = _f(row[ci]) if ci < len(row) else None
        serial = _f(row[mi]) if mi < len(row) else None
        if v is None or serial is None:
            continue
        import xlrd

        d = xlrd.xldate_as_datetime(serial, 0)
        out.append({"ts": _month_start(d.year, d.month), "value": v})
    if not out:
        raise GeoError("geo: GPR no rows")
    return out


def parse_gprd_rows(rows, column="GPRD"):
    hi = _header_index(rows, column)
    header = [str(c).strip() for c in rows[hi]]
    ci = header.index(column)
    di = 0
    for j, h in enumerate(header):
        if h and h.lower() in ("day", "date"):
            di = j
            break
    out = []
    for row in rows[hi + 1 :]:
        v = _f(row[ci]) if ci < len(row) else None
        raw = _f(row[di]) if di < len(row) else None
        if v is None or raw is None or raw < 19000101:
            continue
        d = str(int(raw))
        out.append({"ts": f"{d[:4]}-{d[4:6]}-{d[6:]}", "value": v})
    if not out:
        raise GeoError("geo: GPRD no rows")
    return out


def parse_fao_csv(text: str, column: str = "Food Price Index"):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    hi = next(i for i, ln in enumerate(lines) if ln.lower().startswith("date"))
    rdr = csv.DictReader(lines[hi:])
    out = []
    for row in rdr:
        v = _f(row.get(column))
        d = (row.get("Date") or "").strip()
        if v is not None and len(d) >= 7:
            out.append({"ts": _month_start(int(d[:4]), int(d[5:7])), "value": v})
    if not out:
        raise GeoError(f"geo: FAO no rows for column {column!r}")
    return out


def parse_harpex(payload: dict):
    out = []
    for p in payload.get("harpex", []):
        v = _f(p.get("value"))
        d = str(p.get("date", ""))[:10]
        if v is not None and d:
            out.append({"ts": d, "value": v})
    if not out:
        raise GeoError("geo: HARPEX no rows")
    return out


def fetch_gpr() -> list[dict]:
    return parse_gpr_rows(_xls_rows(GPR_MONTHLY_URL))


def fetch_gprd() -> list[dict]:
    return parse_gprd_rows(_xls_rows(GPR_DAILY_URL))


def fetch_fao() -> list[dict]:
    return parse_fao_csv(_get(_get_fao_csv_url()).decode("utf-8", errors="replace"))


def fetch_fao_cereals() -> list[dict]:
    return parse_fao_csv(
        _get(_get_fao_csv_url()).decode("utf-8", errors="replace"), column="Cereals"
    )


def fetch_harpex() -> list[dict]:
    import html as _html

    page = _html.unescape(_get(HARPEX_URL).decode("utf-8", errors="replace"))
    start = 0
    blobs = []
    while True:
        i = page.find('{"harpex"', start)
        if i < 0:
            break
        depth = 0
        for j in range(i, len(page)):
            if page[j] == "{":
                depth += 1
            elif page[j] == "}":
                depth -= 1
                if depth == 0:
                    blobs.append(page[i : j + 1])
                    start = j + 1
                    break
        else:
            break
    if not blobs:
        raise GeoError("geo: HARPEX payload not found on page")
    return parse_harpex(json.loads(max(blobs, key=len)))


GEO_SERIES = {
    "GPR": fetch_gpr,
    "GPRD": fetch_gprd,
    "FAO_FFPI": fetch_fao,
    "FAO_CEREALS": fetch_fao_cereals,
    "HARPEX": fetch_harpex,
}


def fetch_latest(series_id: str) -> dict:
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    fn = GEO_SERIES.get(key)
    if fn is None:
        raise GeoError(f"geo: unrouted series {series_id}")
    rows = fn()
    return rows[-1]


def fetch_window(series_id: str, days: int = 10) -> list[dict]:
    from datetime import UTC, datetime, timedelta

    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    fn = GEO_SERIES.get(key)
    if fn is None:
        raise GeoError(f"geo: unrouted series {series_id}")
    floor = {"GPR": 130, "GPRD": 10, "FAO_FFPI": 130, "FAO_CEREALS": 130, "HARPEX": 10}[key]
    cutoff = (datetime.now(UTC).date() - timedelta(days=max(days, floor))).isoformat()
    return [p for p in fn() if p["ts"] >= cutoff]
