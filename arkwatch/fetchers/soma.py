"""soma.py — NY Fed Markets API: SOMA Treasury holdings per CUSIP.

Weekly granularity: Wednesday as-of date, released Thursday COB ET, so the
harvest job runs Friday ~06:00 WIB. History back to 2003-07-09 (1,208 weeks).

Live-verified endpoints (2026-09-03; OpenAPI: markets.newyorkfed.org/static/docs/markets-api.yml):
  /api/soma/asofdates/latest.json         -> {"soma": {"asOfDates": ["2026-08-26"]}}
  /api/soma/asofdates/list.json           -> all as-of dates, DESC (for backfill)
  /api/soma/tsy/get/asof/{YYYY-MM-DD}.json -> {"soma": {"holdings": [ {...} ]}}
  /api/soma/summary.json                  -> official weekly totals (calibration cross-check)
  /api/soma/tsy/wam/all/asof/{date}.json  -> official weighted-average maturity (8.26y @ 2026-08-26)

Verified pitfalls:
  - The path drafted in PLAN-SOMA.md (/soma/holds/list/{date}.json) does NOT exist (HTTP 400);
    the real one is /soma/tsy/get/asof/{date}.json and the rows live under soma.holdings.
  - Every numeric field arrives as a STRING; "" means missing (e.g. changeFromPriorWeek
    is "" in older history) and must become None, never 0.0.
  - percentOutstanding is a FRACTION (0.6999 = the 70% Fed ownership cap); stored as
    percent (x100) to match the pct_outstanding column name and avoid 100x display bugs.
  - securityType values are plural: Bills | NotesBonds | FRNs | TIPS.
  - There is no "latest" alias for tsy/get/asof — chain asofdates/latest first.
  - changeFromPriorWeek only covers issues that SURVIVE the week: par maturing
    during the week silently leaves the snapshot, so ΣchangeFromPriorWeek is
    GROSS purchases, not the portfolio change. Identity (verified to the
    dollar on 107/107 consecutive week-pairs, 108 weeks, 2026-09-03):
    Δtotal_par = ΣchangeFromPriorWeek − prior week's rolling_off_7d.
    Consumers wanting portfolio Δ must diff consecutive total_par
    (see signals/soma.soma_net_liquidity).
"""

from __future__ import annotations

from datetime import date, timedelta

from curl_cffi import requests as creq

BASE = "https://markets.newyorkfed.org/api/soma"
REQUEST_TIMEOUT = (10, 60)  # (connect, read): 431-row JSON payloads are ~150KB
PCT_OUTSTANDING_SCALE = 100.0  # API fraction (0.6999) -> percent (69.99)
DAYS_PER_YEAR = (
    365.25  # [RISET: convention for years-to-maturity; WAM cross-check 8.26y 2026-09-03]
)
# Bucket edges in years; a holding with y < 1 lands in "0-1y", y < 3 in "1-3y", etc.
MATURITY_BUCKET_EDGES_Y: tuple[float, ...] = (1.0, 3.0, 5.0, 7.0, 10.0)
MATURITY_BUCKET_LABELS = ("0-1y", "1-3y", "3-5y", "5-7y", "7-10y", "10y+")
# Roll-off horizons (days) mirrored by the soma_summary columns.
ROLLOFF_WINDOWS_D = (7, 30, 90)
# API securityType -> soma_summary column. Raw values are stored unmolested in
# soma_holdings.security_type; this map is the single translation point.
SECURITY_TYPE_COLUMNS = {
    "Bills": "bills",
    "NotesBonds": "notes_bonds",
    "FRNs": "frn",
    "TIPS": "tips",
}
NOMINAL_TYPES = ("Bills", "NotesBonds", "FRNs")  # everything except TIPS


class SomaError(RuntimeError):
    pass


def _f(v) -> float | None:
    """API strings -> float; '' or missing -> None (missing must not become 0.0)."""
    if v is None or v == "":
        return None
    return float(v)


def _get_json(path: str, session: creq.Session | None = None) -> dict:
    s = session or creq.Session(impersonate="chrome")
    r = s.get(f"{BASE}{path}", timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise SomaError(f"soma {path}: HTTP {r.status_code}")
    return r.json()


def fetch_latest_asof(session: creq.Session | None = None) -> str:
    """Most recent weekly as-of date (Wednesday)."""
    j = _get_json("/asofdates/latest.json", session)
    dates = j.get("soma", {}).get("asOfDates", [])
    if not dates:
        raise SomaError("asofdates/latest: empty")
    return dates[0]


def fetch_asof_dates(session: creq.Session | None = None) -> list[str]:
    """All weekly as-of dates, newest first (2003-07-09 -> present)."""
    j = _get_json("/asofdates/list.json", session)
    return j.get("soma", {}).get("asOfDates", [])


def fetch_soma_holdings(
    as_of: str | None = None, session: creq.Session | None = None
) -> list[dict]:
    """Latest or historical per-CUSIP Treasury holdings.

    Returns [{as_of_date, cusip, security_type, maturity_date, par_value,
              pct_outstanding, change_week}] with par_value/change_week in raw
    USD and pct_outstanding in percent (0-100).
    """
    own_session = session is None
    s = session or creq.Session(impersonate="chrome")
    try:
        if as_of is None:
            as_of = fetch_latest_asof(s)
        j = _get_json(f"/tsy/get/asof/{as_of}.json", s)
    finally:
        if own_session:
            s.close()
    rows = j.get("soma", {}).get("holdings", [])
    if not rows:
        raise SomaError(f"soma holdings {as_of}: empty")
    out = []
    for r in rows:
        pct = _f(r.get("percentOutstanding"))
        out.append(
            {
                "as_of_date": r.get("asOfDate", as_of),
                "cusip": r["cusip"],
                "security_type": r.get("securityType"),
                "maturity_date": r.get("maturityDate") or None,
                "par_value": _f(r.get("parValue")),
                "pct_outstanding": pct * PCT_OUTSTANDING_SCALE if pct is not None else None,
                "change_week": _f(r.get("changeFromPriorWeek")),
            }
        )
    return out


def _years_to_maturity(maturity_date: str, as_of_date: str) -> float | None:
    if not maturity_date:
        return None
    delta = date.fromisoformat(maturity_date) - date.fromisoformat(as_of_date)
    return delta.days / DAYS_PER_YEAR


def maturity_bucket(years: float) -> str:
    """Map years-to-maturity to a bucket label (half-open intervals: [0,1), [1,3), ...)."""
    for edge, label in zip(MATURITY_BUCKET_EDGES_Y, MATURITY_BUCKET_LABELS, strict=False):
        if years < edge:
            return label
    return MATURITY_BUCKET_LABELS[-1]


def compute_maturity_buckets(holdings: list[dict], as_of_date: str) -> dict:
    """Bucket by years-to-maturity: 0-1y, 1-3y, 3-5y, 5-7y, 7-10y, 10y+.

    Pure function of (holdings, as_of_date) — call for two dates and diff the
    results to get bucket migration. Rows without a maturity_date are skipped.
    Returns {bucket: {par, change_week, n_cusips}} (change_week sums treat
    missing values as 0 so a partial-history week does not read as a drop).
    """
    out = {b: {"par": 0.0, "change_week": 0.0, "n_cusips": 0} for b in MATURITY_BUCKET_LABELS}
    for h in holdings:
        y = _years_to_maturity(h.get("maturity_date") or "", as_of_date)
        if y is None or h.get("par_value") is None:
            continue
        b = out[maturity_bucket(y)]
        b["par"] += h["par_value"]
        b["change_week"] += h.get("change_week") or 0.0
        b["n_cusips"] += 1
    return out


def compute_tips_nominal_split(holdings: list[dict]) -> dict:
    """TIPS vs nominal (Bills+NotesBonds+FRNs) separation.

    Returns {tips: {par, change}, nominal: {par, change}} in raw USD.
    """
    out = {
        "tips": {"par": 0.0, "change": 0.0},
        "nominal": {"par": 0.0, "change": 0.0},
    }
    for h in holdings:
        key = "tips" if h.get("security_type") == "TIPS" else "nominal"
        if h.get("par_value") is not None:
            out[key]["par"] += h["par_value"]
        out[key]["change"] += h.get("change_week") or 0.0
    return out


def rolling_off(holdings: list[dict], as_of_date: str, days: int) -> float:
    """Par value maturing within `days` of as_of_date (inclusive), raw USD."""
    horizon = (date.fromisoformat(as_of_date) + timedelta(days=days)).isoformat()
    return sum(
        h["par_value"]
        for h in holdings
        if h.get("par_value") is not None
        and h.get("maturity_date")
        and h["maturity_date"] <= horizon
    )


def compute_soma_summary(holdings: list[dict], as_of_date: str) -> dict:
    """Aggregate per-CUSIP rows -> one soma_summary row (all money in raw USD).

    total_par is Treasury-only (the tsy endpoint holds Bills+NotesBonds+FRNs+TIPS;
    agency/MBS live in separate endpoints). Verified against the official
    /api/soma/summary.json to the dollar on 2026-09-03.
    """
    out: dict = {"as_of_date": as_of_date}
    by_col = {col: 0.0 for col in SECURITY_TYPE_COLUMNS.values()}
    total = 0.0
    weekly_change = 0.0
    w_sum = 0.0
    y_w_sum = 0.0
    for h in holdings:
        par = h.get("par_value")
        if par is not None:
            total += par
            col = SECURITY_TYPE_COLUMNS.get(h.get("security_type") or "")
            if col is not None:
                by_col[col] += par
            y = _years_to_maturity(h.get("maturity_date") or "", as_of_date)
            if y is not None:
                w_sum += par
                y_w_sum += par * y
        weekly_change += h.get("change_week") or 0.0
    out.update(by_col)
    out["total_par"] = total
    out["weekly_change"] = weekly_change
    for days in ROLLOFF_WINDOWS_D:
        out[f"rolling_off_{days}d"] = rolling_off(holdings, as_of_date, days)
    out["n_cusips"] = len(holdings)
    out["avg_maturity_years"] = y_w_sum / w_sum if w_sum > 0 else None
    return out
