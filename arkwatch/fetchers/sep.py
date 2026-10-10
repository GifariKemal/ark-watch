"""sep.py — FOMC Summary of Economic Projections (dot plot) from federalreserve.gov.

The SEP publishes 4x/year (Mar/Jun/Sep/Dec meetings) at:
  /monetarypolicy/fomcprojtabl{YYYYMMDD}.htm
The page's Federal Funds Rate table carries the Actual row (history), the
upper/lower 70% confidence interval bounds, and the Median row — the dot
plot's median path for the current + 3 forward years.

Parse strategy (live-verified 2026-09-19 across 4 vintages): strip tags in
the Federal Funds Rate section, then locate the Median row's numbers. The
row layout shifts by projection-horizon (a Sep-2026 page projects
2026-2029, a Dec-2025 page 2025-2028) — years are read from the table
header, not assumed.

AUDIT round-2 correction (live-probed 2026-09-20): the CURRENT table format
carries NO longer-run column — the Sep-2026 header reads 2021..2029 with the
median ending 4.1/4.1/3.9/3.6 for 2026-29, and the legacy 'longer = last
numeric column' inference is inert (the post-Median window bleeds into the
Lower-End row, whose leading dash blocks it — see the inline note). The
longer-run median exists only in the calendar events family (INTEREST RATE
PROJECTION LONGER) — a different data model; no CAL:FOMC_DOT_LONGER series
is produced or promised until that wiring exists.

Series produced (quarterly, ts = the SEP release date):
  CAL:FOMC_DOT_<YYYY>   — median federal funds rate for calendar year YYYY
One series per REGISTERED target year, keyed by release date, so the FULL
revision history is preserved (the trading signal is the REVISION between
SEPs, not the level). New horizon years enter automatically (2029 began at
the Sep-2026 SEP).
"""

from __future__ import annotations

import functools
import html as _html
import re
from datetime import UTC, datetime

import requests

BASE = "https://www.federalreserve.gov/monetarypolicy/fomcprojtabl"
CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# All known SEP pages (refreshed by fetch_window/fetch_latest via the calendar)
_cache: dict[str, dict] = {}


class SepError(RuntimeError):
    pass


# storage source label (audit round-2): the backfill has always written
# source='CAL'; the daily harvest route 'CAL:FOMC_DOT' must stamp the SAME
# label or every vintage lands twice (source is a PK leg)
SOURCE = "CAL"


def _fetch_page(date_iso: str) -> str:
    r = requests.get(f"{BASE}{date_iso.replace('-', '')}.htm", timeout=(10, 60))
    if r.status_code != 200:
        raise SepError(f"SEP {date_iso}: HTTP {r.status_code}")
    return r.text


def _parse_fed_funds_median(text: str) -> dict[str, float]:
    """Parse the Federal Funds Rate → Median row → {year: value}.

    The table structure (live-verified across all 22 vintages 2021-2026):
      header: 2021|2022|...|2029   (actual years + projection years — NO
                                    Longer-run column in this table; that
                                    median lives in the events family only)
      rows:   Actual|0.1|4.4|...|-|-|-|-|     (numbers + '-' placeholders)
              Upper CI|-|-|...|4.6|5.8|...
              Median|-|-|...|4.1|4.1|3.9|3.6|
    The '-' placeholders MUST be counted for column alignment — a
    numbers-only regex silently shifts the projection years onto the
    actual-history columns (the round-11 parser's first cut did exactly
    that). Cells are split on '|', each mapped to float or None.
    """
    i = text.find("Federal Funds Rate")
    if i < 0:
        raise SepError("SEP: 'Federal Funds Rate' section not found")
    section = text[i : i + 5000]
    clean = re.sub(r"<[^>]+>", "|", section)
    clean = _html.unescape(clean)
    clean = re.sub(r"\s+", " ", clean)

    # header years — search a WIDER zone for the year row (the header
    # precedes 'Federal Funds Rate' in the HTML; our section starts AT it)
    ai = clean.find("Actual")
    head_zone = clean[:ai] if ai > 0 else clean[:400]
    years = re.findall(r"(20\d{2})", head_zone)

    # Median row: the FIRST 'Median' after the Fed Funds header (the
    # section may contain other tables below — rfind overshoots)
    mi = clean.find("Median", ai if ai > 0 else 0)
    if mi < 0:
        raise SepError("SEP: Median row not found in Fed Funds section")
    after = clean[mi + len("Median") : mi + len("Median") + 400]
    # cells: numeric-or-dash tokens in column order
    cells = re.findall(r"(-?\d+\.\d)|(-)", after)
    vals: list[float | None] = []
    for num, _dash in cells:
        vals.append(float(num) if num else None)

    # ROUND-3 CORRECTION (the round-1 note here asserted the opposite —
    # live-probed twice): the Sep-2026 header reads 2021..2029 (9 year
    # columns, 2029 PRESENT) and the Median row is 9 cells: 5 dashes
    # (history) + 4.1/4.1/3.9/3.6 = the 2026/2027/2028/2029 medians. There
    # is NO longer-run column in this table format — the longer-run median
    # lives only in the calendar events family. The legacy longer-inference
    # below is inert-but-retained: the 400-char window after 'Median'
    # bleeds into the Lower-End row, and vals[len(years)] lands on that
    # row's leading DASH (None), so 'longer' is never emitted on the live
    # format. If a future SEP ever carries a genuine longer column, that
    # dash alignment breaks FIRST — treat any 'longer' emission as a
    # parse-drift signal, verify against the page before trusting it.
    out: dict[str, float] = {}
    for yr, v in zip(years, vals[: len(years)], strict=False):
        if v is not None:
            out[yr] = v
    # legacy longer-run inference — see the note above (inert on live format)
    if len(vals) > len(years) and vals[len(years)] is not None:
        out["longer"] = vals[len(years)]
    return out


def parse_sep(date_iso: str) -> dict[str, float]:
    """One SEP vintage → {year: median_fed_funds_pct, 'longer': value}."""
    if date_iso in _cache:
        return _cache[date_iso]
    result = _parse_fed_funds_median(_fetch_page(date_iso))
    _cache[date_iso] = result
    return result


@functools.lru_cache(maxsize=1)
def sep_dates() -> list[str]:
    """All SEP release dates from the FOMC calendar page (newest first).

    Cached per process: verify/harvest route every CAL:FOMC_DOT_* series
    here, and the calendar only changes 4x/year (jobs run as subprocesses)."""
    r = requests.get(CALENDAR_URL, timeout=(10, 60))
    if r.status_code != 200:
        raise SepError(f"FOMC calendar: HTTP {r.status_code}")
    dates = sorted(set(re.findall(r"fomcprojtabl(\d{8})\.htm", r.text)), reverse=True)
    if not dates:
        raise SepError("FOMC calendar: no SEP links found")
    return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]


def series_rows() -> list[dict]:
    """All SEPs × all projection years → [{ts, series_suffix, value}].

    series_suffix: the target year ('2026', '2027', ...) or 'LONGER'.
    ts = the SEP release date (the vintage — revisions live in the date key).
    """
    return [r for d in sep_dates() for r in _vintage_rows(d)]


def _vintage_rows(d: str) -> list[dict]:
    try:
        parsed = parse_sep(d)
    except Exception as ex:
        print(f"  ⚠ SEP {d}: {str(ex)[:70]}")
        return []
    return [
        {"ts": d, "series_suffix": "LONGER" if k == "longer" else k, "value": v}
        for k, v in parsed.items()
    ]


# CAL: routing interface (fetch_latest / fetch_window)
def _dot_rows(series_id: str) -> list[dict]:
    """series_rows filtered to the REQUESTED projection year — the bare
    version mixed all years into every series (CAL:FOMC_DOT_2026's latest
    could have been the 2028 median; audit 2026-09-20)."""
    want = _want(series_id)
    rows = series_rows()
    if want is not None:
        rows = [r for r in rows if r["series_suffix"] == want]
    if not rows:
        raise SepError(f"sep: no rows for {series_id}")
    return rows


def _want(series_id: str) -> str | None:
    if not series_id.startswith("CAL:FOMC_DOT"):
        raise SepError(f"sep: unrouted {series_id}")
    return series_id.rsplit("_", 1)[-1] if "_" in series_id[4:] else None


def fetch_latest(series_id: str) -> dict:
    # sep_dates() is sorted NEWEST-FIRST (live-caught once: [-1] of the full
    # walk was the 2023-09 SEP), so the first vintage carrying the year IS the
    # latest — parse only up to it instead of all ~22 pages (p95 was 8 s).
    want = _want(series_id)
    for d in sep_dates():
        for r in _vintage_rows(d):
            if want is None or r["series_suffix"] == want:
                return {"ts": d, "value": r["value"]}
    raise SepError(f"sep: no rows for {series_id}")


def fetch_window(series_id: str, days: int = 10) -> list[dict]:
    from datetime import timedelta

    cutoff = (datetime.now(UTC).date() - timedelta(days=max(days, 2000))).isoformat()
    return [{"ts": r["ts"], "value": r["value"]} for r in _dot_rows(series_id) if r["ts"] >= cutoff]


def save_dot_series(conn) -> int:
    """Write all rows to raw_observations as CAL:FOMC_DOT_{year} series.

    Only years registered in series_registry are written (the FK would
    reject unregistered ids; older vintages project years like 2022-2025
    that are no longer trading-relevant — they live in the SEP page but
    not in our series model)."""
    from .. import db as _db

    registered = {
        r[0]
        for r in conn.execute(
            "SELECT series_id FROM series_registry WHERE series_id LIKE 'CAL:FOMC_DOT_%'"
        ).fetchall()
    }
    if not registered:
        print("  ⚠ dot plot: no CAL:FOMC_DOT_* series registered — run registry sync first")
        return 0
    rows = series_rows()
    payload = []
    for r in rows:
        sid = f"CAL:FOMC_DOT_{r['series_suffix']}"
        if sid in registered:
            payload.append((sid, r["ts"], r["value"], "CAL"))
    if not payload:
        return 0
    n = _db.insert_observations(conn, payload)
    print(
        f"  dot plot: {n} obs across {len(set(p[0] for p in payload))} target-years"
        f" ({len(set(p[1] for p in payload))} vintages)"
    )
    return n
