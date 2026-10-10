"""treasury.py — official par yield curve via the per-year CSV.

Verified columns (live 2026-10-10): Date (MM/DD/YYYY), "1 Mo", "1.5 Month"
(1.5 MONTHS, not 1.5 years), "2/3/4/6 Mo", "1/2/3/5/7/10/20/30 Yr" — there
is no 4-year column. The server takes ~16-19 s per request whatever the
format, so each year is fetched once per process (all tenors share it).
Depth/backfill is done by looping years; v0 verification covers the latest
value only.
"""

from __future__ import annotations

import csv
import functools
from datetime import UTC, datetime

import requests

URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
)

# series_id (without prefix) -> CSV column
TENOR_MAP = {
    "PAR_1_5Y": "1.5 Month",  # 1.5 MONTHS despite the series_id name
    "PAR_7Y": "7 Yr",
    "PAR_20Y": "20 Yr",
}


class TreasuryError(RuntimeError):
    pass


@functools.lru_cache(maxsize=4)
def _entries(year: int) -> tuple[dict, ...]:
    r = requests.get(
        URL.format(year=year),
        params={
            "type": "daily_treasury_yield_curve",
            "field_tdr_date_value": str(year),
            "page": "",
            "_format": "csv",
        },
        timeout=(10, 30),
    )
    if r.status_code != 200:
        raise TreasuryError(f"treasury csv {year}: HTTP {r.status_code}")
    out = []
    for row in csv.DictReader(r.text.splitlines()):
        m, d, y = row.pop("Date").split("/")
        out.append({"ts": f"{y}-{m}-{d}"} | {k: float(v) for k, v in row.items() if v.strip()})
    return tuple(out)


def fetch_latest(series_id: str) -> dict:
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    field = TENOR_MAP.get(key)
    if field is None:
        raise TreasuryError(f"unknown tenor: {key}")
    year = datetime.now(UTC).year
    # early January: the new year's CSV is empty until the first print
    rows = [r for r in _entries(year) if field in r] or [
        r for r in _entries(year - 1) if field in r
    ]
    if not rows:
        raise TreasuryError(f"treasury: no rows with {field} in {year - 1}-{year}")
    r0 = max(rows, key=lambda r: r["ts"])
    return {"ts": r0["ts"], "value": r0[field]}
