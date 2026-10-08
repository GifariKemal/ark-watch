"""caldist.py — calendar-derived series: events.actual → raw_observations.

No HTTP: the source is the events table itself. A CAL: series names an
indicator_key family (the post-alias canonical key); the fetcher maps each
release to its REFERENCE month and lands ONE level observation per month.
Twins of the same release dedup via MAX(actual) per (key, month) — the same
deterministic convention compute_sigma uses (duplicate rows carry identical
actuals after the sibling-heal).

Reference-month derivation (verified live 2026-09-17 on the ISM families):
the month token embedded in normalized_name ('... PMI AUG', released
2026-09-01 → 2026-08-01) wins; a token-less row falls back to the family's
release convention (ISM reports land on the 1st-3rd business day of month M
for reference month M−1). Token-vs-release cross-checks agreed on every
sampled row.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

# series_id suffix → (indicator_key family, release convention)
# convention "m_minus_1" = reference month is the month BEFORE release (ISM);
# anything else = the release month itself (FOMC projections: the SEP release
# IS the observation).
FAMILIES = {
    "ISM_MFG_PMI": ("ISM MANUFACTURING PMI", "m_minus_1", "max"),
    "ISM_SVC_PMI": ("ISM SERVICES PMI", "m_minus_1", "max"),
    "ISM_MFG_PRICES": ("ISM MANUFACTURING PRICES", "m_minus_1", "max"),
    "ISM_SVC_PRICES": ("ISM SERVICES PRICES", "m_minus_1", "max"),
    "ISM_MFG_EMPLOYMENT": ("ISM MANUFACTURING EMPLOYMENT", "m_minus_1", "max"),
    "ISM_MFG_NEW_ORDERS": ("ISM MANUFACTURING NEW ORDERS", "m_minus_1", "max"),
    "FOMC_LONGER": ("INTEREST RATE PROJECTION LONGER", "release_month", "max"),
    "EIA_CRUDE_STOCKS_CHG": ("EIA CRUDE OIL STOCKS CHANGE", "week_ending", "sum"),
    "EIA_GASOLINE_STOCKS_CHG": ("EIA GASOLINE STOCKS CHANGE", "week_ending", "sum"),
    "EIA_DISTILLATE_STOCKS_CHG": ("EIA DISTILLATE STOCKS CHANGE", "week_ending", "sum"),
    "BAKER_HUGHES_OIL_RIGS": ("BAKER HUGHES OIL RIG COUNT", "release_month", "last"),
    "CPI_MOM": ("INFLATION RATE MOM", "m_minus_1", "max"),
    "CORE_CPI_MOM": ("CORE INFLATION RATE MOM", "m_minus_1", "max"),
    "CORE_PCE_MOM": ("CORE PCE PRICE INDEX MOM", "m_minus_1", "max"),
    "RETAIL_SALES_YOY": ("RETAIL SALES YOY", "m_minus_1", "max"),
    "RETAIL_SALES_EX_AUTOS_MOM": ("RETAIL SALES EX AUTOS MOM", "m_minus_1", "max"),
    "RETAIL_SALES_EX_GAS_AUTOS_MOM": ("RETAIL SALES EX GAS AUTOS MOM", "m_minus_1", "max"),
    "EXISTING_HOME_SALES_MOM": ("EXISTING HOME SALES MOM", "m_minus_1", "max"),
    "CPI_LEVEL": ("CPI", "m_minus_1", "max"),
    "CPI_SA_LEVEL": ("CPI S A", "m_minus_1", "max"),
    "CB_CONSUMER_CONFIDENCE": ("CB CONSUMER CONFIDENCE", "release_month", "max"),
    "GOODS_TRADE_BALANCE": ("GOODS TRADE BALANCE", "m_minus_1", "max"),
}

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_TOKEN = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?:\s+\d{1,2})?\s*$")


class CalDistError(RuntimeError):
    pass


def _ref_month(name_norm: str, ts_utc: str, convention: str) -> tuple[int, int] | None:
    """(year, month) of the REFERENCE period for one release row."""
    y, mon = int(ts_utc[:4]), int(ts_utc[5:7])
    m = _TOKEN.search(name_norm)
    if m:
        mm = _MONTHS[m.group(1)]
        # the token may belong to the release year or the previous one
        # (a Jan release references Dec of the prior year)
        return (y, mm) if mm <= mon else (y - 1, mm)
    if convention == "m_minus_1":
        return (y - 1, 12) if mon == 1 else (y, mon - 1)
    return (y, mon)


def _release_events(rows, convention):
    """[(date, ref_month, value)] — one entry per release date.

    Twins (FMP token-carrying + TV token-less names, same date) collapse to
    one event; MAX arbitrates if a stale twin disagrees. Month attribution
    prefers any twin carrying a week-ending token, else the convention."""
    by_date: dict[str, dict] = {}
    for name_norm, d, actual in rows:
        m = _TOKEN.search(name_norm)
        cur = by_date.setdefault(d, {"v": None, "token": None})
        v = float(actual)
        if cur["v"] is None or v > cur["v"]:
            cur["v"] = v
        if m and cur["token"] is None:
            mm = _MONTHS[m.group(1)]
            y = int(d[:4])
            cur["token"] = (y, mm) if mm <= int(d[5:7]) else (y - 1, mm)
    out = []
    for d in sorted(by_date):
        e = by_date[d]
        rm = e["token"] or _ref_month("", d, convention)
        if rm is not None:
            out.append((d, rm, e["v"]))
    return out


def family_rows(
    indicator_key: str,
    convention: str = "m_minus_1",
    agg: str = "max",
    db_path: str | Path | None = None,
) -> list[dict]:
    """All months of a family: [{ts, value}] ascending.

    agg=max   — level series (ISM twins dedup, historical behavior)
    agg=sum   — weekly FLOWS collapsed to the true monthly total (EIA)
    agg=last  — latest release within the month (rig count)"""
    conn = sqlite3.connect(f"file:{Path(db_path or DEFAULT_DB)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT normalized_name, substr(ts_utc,1,10) d, actual FROM events"
            " WHERE indicator_key=? AND actual IS NOT NULL"
            " ORDER BY ts_utc",
            (indicator_key,),
        ).fetchall()
    finally:
        conn.close()
    if agg == "max":
        by_month: dict[tuple[int, int], float] = {}
        for name_norm, d, actual in rows:
            rm = _ref_month(name_norm, d, convention)
            if rm is None:
                continue
            cur = by_month.get(rm)
            if cur is None or float(actual) > cur:
                by_month[rm] = float(actual)
        return [{"ts": f"{y:04d}-{m:02d}-01", "value": v} for (y, m), v in sorted(by_month.items())]
    by_month_sum: dict[tuple[int, int], float] = {}
    by_month_last: dict[tuple[int, int], tuple[str, float]] = {}
    for d, rm, v in _release_events(rows, convention):
        by_month_sum[rm] = by_month_sum.get(rm, 0.0) + v
        cur = by_month_last.get(rm)
        if cur is None or d > cur[0]:
            by_month_last[rm] = (d, v)
    src = by_month_sum if agg == "sum" else {k: v for k, (_, v) in by_month_last.items()}
    return [
        {"ts": f"{y:04d}-{m:02d}-01", "value": round(v, 3)} for (y, m), v in sorted(src.items())
    ]


def fetch_latest(series_id: str) -> dict:
    fam = FAMILIES.get(series_id.split(":", 1)[1] if ":" in series_id else series_id)
    if fam is None:
        raise CalDistError(f"caldist: unrouted series {series_id}")
    key, convention, agg = fam
    rows = family_rows(key, convention, agg)
    if not rows:
        raise CalDistError(f"caldist: no actuals for family {key}")
    return rows[-1]


def fetch_window(series_id: str, days: int = 10) -> list[dict]:
    """Points inside the trailing window (the harvest's gap-heal contract).

    ROUND-4: the effective window is max(days, 62) — reference-month ts means
    the newest monthly point is 32-62d old, so a literal 10d trailing window
    could NEVER contain it and every run silently degraded to fetch_latest
    (a missed middle month would then be permanently unhealable).

    Release-month families (FOMC projections) are at best quarterly: SEPs
    space up to ~100d apart, so their floor is 130d (a 62d floor would badge
    EMPTY for weeks each mid-cycle — audit 2026-09-20)."""
    from datetime import UTC, datetime, timedelta

    fam = FAMILIES.get(series_id.split(":", 1)[1] if ":" in series_id else series_id)
    if fam is None:
        raise CalDistError(f"caldist: unrouted series {series_id}")
    days_eff = max(days, 62 if fam[1] == "m_minus_1" else 130)
    cutoff = (datetime.now(UTC).date() - timedelta(days=days_eff)).isoformat()
    return [p for p in family_rows(*fam) if p["ts"] >= cutoff]
