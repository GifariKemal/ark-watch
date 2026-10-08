"""fiscal.py — fiscaldata Treasury APIs: daily TGA (three-era recipe) + the
2026-09-04 expansion (debt-to-the-penny registry series, auctions, public debt
transactions, interest expense, average interest rates).

Verified pitfalls:
- TGA (era 3): the endpoint is fiscal_service/v1/accounting/dts/
  operating_cash_balance (not 'od'/fiscal_account); the value is
  COALESCE(close_today_bal, open_today_bal) — era 3 puts the closing balance
  in open_today_bal while close contains the string 'null'; the unit is
  $ millions.
- All numerics arrive as STRINGS; missing = '' or 'null' → None, never 0.0.
- Pagination: page[number] ONLY — page[offset] is silently IGNORED (every
  request returns page 1 → infinite loop, live-verified 2026-09-04);
  links.next is a RELATIVE query string ('&page[number]=..') that requests
  rejects. page[size] max = 10000 (accepted; the 11,106-row auctions pull
  walks 2 pages).
- UNITS [RISET: live-verified 2026-09-04]: debt_to_penny + interest_expense
  are RAW USD; public_debt_transactions is $ MILLIONS (DTS convention) —
  proven by the FYTD identity Σ(issues−redemptions) = Δdebt_to_penny
  2025-09-30→2026-09-02 matching to the dollar (2,479,492 $M); auctions
  fields are price per $100 face / yield % / cover ratio / allocation %.
- public_debt_transactions carries a 5th dimension security_type_desc
  (Bills 'Regular Series' vs 'Cash Management Series'; savings 'Cash Issue
  Price' vs 'Interest Increment') that is NOT part of the locked v10 PK —
  parse_debt_tx_row folds it into security_type ('Bills (Cash Management
  Series)') so no API row is silently collapsed.
- auctions_query includes ANNOUNCEMENT rows (future auction_date, results
  numerics 'null') sharing (auction_date, cusip) with the results row that
  lands after the auction → the store must INSERT OR REPLACE (an OR IGNORE
  would freeze the announcement nulls forever; same lifecycle as nyfed
  fed_operations announcements→results).
"""

from __future__ import annotations

import math

import requests

BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"

ROOT = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
DEBT_PENNY = ROOT + "/v2/accounting/od/debt_to_penny"
AVG_RATES = ROOT + "/v2/accounting/od/avg_interest_rates"
INTEREST_EXPENSE = ROOT + "/v2/accounting/od/interest_expense"
DEBT_TRANSACTIONS = ROOT + "/v1/accounting/dts/public_debt_transactions"
AUCTIONS = ROOT + "/v1/accounting/od/auctions_query"

PAGE_SIZE_MAX = 10000  # [RISET: live-verified 2026-09-04 — page[size]=10000 accepted]
WINDOW_PAGE = 500  # [KEPUTUSAN: ukuran halaman utk window — 500 baris DTS ≈ 20
# record_dates (> jendela 14d), 500 baris interest-expense ≈ 13 bulan (> 6);
# _fetch_recent memastikan tanggal ter-newest lengkap lintas batas halaman]

# fields= keeps the auctions payload ~12 fields instead of ~100 (2 pages walk
# in ~10s vs minutes); record_date MUST be in the set — sorting by a field
# outside fields= returns HTTP 400 (live-verified 2026-09-04). The schema_fp
# fingerprints exactly this set.
AUCTION_FIELDS = (
    "record_date,auction_date,cusip,security_type,security_term,issue_date,maturity_date,"
    "price_per100,avg_med_yield,bid_to_cover_ratio,allocation_pctage,auction_format"
)

# debt_to_penny scalar series (registry model — no dedicated table)
DEBT_PENNY_FIELD = {
    "FISCAL:DEBT_PUBLIC": "debt_held_public_amt",
    "FISCAL:DEBT_INTRAGOV": "intragov_hold_amt",
    "FISCAL:DEBT_TOTAL": "tot_pub_debt_out_amt",
}

# account_type eras in chronological order
ERAS = [
    ("Federal Reserve Account", "2005-10-03", "2021-09-30"),
    ("Treasury General Account (TGA)", "2021-10-01", "2022-04-17"),
    ("Treasury General Account (TGA) Closing Balance", "2022-04-18", None),
]


class FiscalError(RuntimeError):
    pass


def _rows(account_type: str, sort: str, limit: int) -> list[dict]:
    params = {
        "filter": f"account_type:eq:{account_type}",
        "sort": sort,
        "page[size]": limit,
    }
    r = requests.get(BASE, params=params, timeout=(10, 30))
    if r.status_code != 200:
        raise FiscalError(f"fiscaldata: HTTP {r.status_code} — {r.text[:200]}")
    return r.json().get("data", [])


def _close(row: dict) -> float | None:
    for k in ("close_today_bal", "open_today_bal"):
        v = row.get(k)
        if v not in (None, "", "null"):
            return float(v)
    return None


def _num(v) -> float | None:
    """fiscaldata numerics are strings; '', 'null', None → None (never 0.0)."""
    if v in (None, "", "null"):
        return None
    return float(v)


def _txt(v) -> str | None:
    """Text fields can also carry the 'null' string (security_type_desc does)."""
    if v in (None, "", "null"):
        return None
    return str(v)


def page_plan(total: int, size: int) -> list[int]:
    """1-based page numbers covering `total` rows at `size` per page — the API
    paginates with page[number] only (page[offset] is ignored; links.next is
    relative and unusable)."""
    if total <= 0:
        return []
    return list(range(1, math.ceil(total / size) + 1))


def _get(path: str, params: dict, session=None) -> dict:
    sess = session or requests
    r = sess.get(path, params=params, timeout=(10, 60))
    if r.status_code != 200:
        raise FiscalError(f"fiscaldata: HTTP {r.status_code} — {r.text[:200]}")
    return r.json()


def _fetch_pages(
    path: str, params: dict, page_size: int = PAGE_SIZE_MAX, session=None
) -> list[dict]:
    """Walk ALL pages of an endpoint (full-history pulls; windowed pulls use
    _window). Stop on meta total-count or a short page."""
    rows: list[dict] = []
    number = 1
    while True:
        p = {**params, "page[size]": page_size, "page[number]": number}
        j = _get(path, p, session)
        data = j.get("data", [])
        rows += data
        total = (j.get("meta") or {}).get("total-count")
        # total-count is the FAST terminator; a missing/unreadable meta must
        # NOT short-circuit before the short-page backstop — that would
        # silently truncate a full-history pull at page 1 (11k+ auctions).
        if total is not None and len(rows) >= int(total):
            break
        if len(data) < page_size:
            break
        number += 1
    return rows


def _fetch_recent(
    path: str, params: dict, n_dates: int, size: int = WINDOW_PAGE, session=None
) -> list[dict]:
    """Newest-first pages until MORE than n_dates distinct record_dates are held
    or a short page ends the history. The extra older date matters because a
    page boundary can split a date's rows — with one older date in hand the
    newest n are provably complete (a full page alone is NOT truncation: 500
    DTS rows ≈ 20 record_dates > the 14-day window)."""

    rows: list[dict] = []
    number = 1
    while True:
        j = _get(path, {**params, "page[size]": size, "page[number]": number}, session)
        data = j.get("data", [])
        rows += data
        if len(data) < size or len({r["record_date"] for r in rows}) > n_dates:
            return rows
        number += 1


def latest_dates_rows(rows: list[dict], n_dates: int) -> list[dict]:
    """Keep rows whose record_date is among the newest n distinct dates."""
    keep = set(sorted({r["record_date"] for r in rows}, reverse=True)[:n_dates])
    return [r for r in rows if r["record_date"] in keep]


def fetch_auctions(session=None) -> list[dict]:
    """Full auction history 1979→now (11,106 rows live 2026-09-04), one row
    per cusip per auction, announcement rows included."""
    return _fetch_pages(
        AUCTIONS, {"fields": AUCTION_FIELDS, "sort": "auction_date,cusip"}, session=session
    )


def fetch_auctions_window(
    since_days: int = 120, session=None, size: int = WINDOW_PAGE
) -> list[dict]:
    """Daily increment. record_date = issue date and can be in the FUTURE
    (announcement rows) — the cutoff keeps future-dated rows in. Pages while
    the oldest row held is still inside the window (a page boundary must not
    silently end the window)."""
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC).date() - timedelta(days=since_days)).isoformat()
    rows: list[dict] = []
    number = 1
    while True:
        data = _get(
            AUCTIONS,
            {
                "fields": AUCTION_FIELDS,
                "sort": "-record_date",
                "page[size]": size,
                "page[number]": number,
            },
            session,
        ).get("data", [])
        rows += data
        if len(data) < size or (rows and (rows[-1].get("record_date") or "") < cutoff):
            break
        number += 1
    return [r for r in rows if (r.get("record_date") or "") >= cutoff]


def fetch_debt_transactions(dates: int = 14, session=None) -> list[dict]:
    """Rolling window of the newest n record_dates (DTS convention)."""
    rows = _fetch_recent(DEBT_TRANSACTIONS, {"sort": "-record_date"}, dates, session=session)
    return latest_dates_rows(rows, dates)


def fetch_interest_expense(months: int = 6, session=None) -> list[dict]:
    rows = _fetch_recent(INTEREST_EXPENSE, {"sort": "-record_date"}, months, session=session)
    return latest_dates_rows(rows, months)


def fetch_avg_rates(months: int = 6, session=None) -> list[dict]:
    rows = _fetch_recent(AVG_RATES, {"sort": "-record_date"}, months, session=session)
    return latest_dates_rows(rows, months)


def parse_auction_row(r: dict) -> dict | None:
    if not r.get("auction_date") or not r.get("cusip"):
        return None
    return {
        "auction_date": r["auction_date"],
        "cusip": r["cusip"],
        "security_type": _txt(r.get("security_type")),
        "security_term": _txt(r.get("security_term")),
        "issue_date": _txt(r.get("issue_date")),
        "maturity_date": _txt(r.get("maturity_date")),
        "price_per100": _num(r.get("price_per100")),
        "avg_med_yield": _num(r.get("avg_med_yield")),
        "bid_to_cover": _num(r.get("bid_to_cover_ratio")),
        "allocation_pct": _num(r.get("allocation_pctage")),
        "auction_format": _txt(r.get("auction_format")),
    }


def parse_debt_tx_row(r: dict) -> dict | None:
    if not (r.get("record_date") and r.get("transaction_type") and r.get("security_market")):
        return None
    st = _txt(r.get("security_type")) or ""
    desc = _txt(r.get("security_type_desc"))
    if desc:
        # 5th API dimension not in the locked v10 PK — folded, never dropped
        st = f"{st} ({desc})"
    return {
        "record_date": r["record_date"],
        "transaction_type": r["transaction_type"],
        "security_market": r["security_market"],
        "security_type": st,
        "amount_today": _num(r.get("transaction_today_amt")),
    }


def parse_interest_expense_row(r: dict) -> dict | None:
    if not (r.get("record_date") and r.get("expense_catg_desc")):
        return None
    # group/type are nullable PK legs — '' (not NULL) so SQLite's NULLs-are-
    # distinct rule cannot defeat the upsert dedup
    return {
        "record_date": r["record_date"],
        "expense_catg_desc": r["expense_catg_desc"],
        "expense_group_desc": _txt(r.get("expense_group_desc")) or "",
        "expense_type_desc": _txt(r.get("expense_type_desc")) or "",
        "month_amt": _num(r.get("month_expense_amt")),
        "fytd_amt": _num(r.get("fytd_expense_amt")),
    }


def parse_avg_rate_row(r: dict) -> dict | None:
    if not (r.get("record_date") and r.get("security_desc")):
        return None
    return {
        "record_date": r["record_date"],
        "security_desc": r["security_desc"],
        "security_type_desc": _txt(r.get("security_type_desc")),
        "avg_interest_rate": _num(r.get("avg_interest_rate_amt")),
    }


def fetch_window(series_id: str, days: int = 12, session=None) -> list[dict]:
    """GAP-HEAL (audit P1-1): land every observation in the window, not just
    the latest row — shutdown nights otherwise leave permanent one-day holes
    in the debt/TGA series (verified: fiscaldata still serves 2026-09-04)."""
    if series_id in DEBT_PENNY_FIELD:
        field = DEBT_PENNY_FIELD[series_id]
        rows = _get(
            DEBT_PENNY,
            {
                "fields": f"record_date,{field}",
                "sort": "-record_date",
                "page[size]": int(days * 1.8),
            },
            session,
        ).get("data", [])
        pts = [
            {"ts": r["record_date"], "value": _num(r.get(field))}
            for r in rows
            if _num(r.get(field)) is not None
        ]
        pts.sort(key=lambda p: p["ts"])
        return pts
    if series_id == "FISCAL:TGA_DAILY":
        rows = _rows(ERAS[-1][0], "-record_date", int(days * 1.8))
        pts = [{"ts": r["record_date"], "value": _close(r)} for r in rows if _close(r) is not None]
        pts.sort(key=lambda p: p["ts"])
        return pts
    raise FiscalError(f"fetch_window: unsupported series {series_id}")


def fetch_latest(series_id: str = "FISCAL:TGA_DAILY", session=None) -> dict:
    if series_id in DEBT_PENNY_FIELD:
        field = DEBT_PENNY_FIELD[series_id]
        rows = _get(
            DEBT_PENNY,
            {"fields": f"record_date,{field}", "sort": "-record_date", "page[size]": 5},
            session,
        ).get("data", [])
        for r in rows:
            v = _num(r.get(field))
            # pre-1997 rows carry 'null' splits — latest non-null wins
            if v is not None:
                return {"ts": r["record_date"], "value": v}
        raise FiscalError(f"{series_id}: no recent non-null rows")
    rows = _rows(ERAS[-1][0], "-record_date", 5)
    rows = [r for r in rows if _close(r) is not None]
    if not rows:
        raise FiscalError("TGA: no recent closing rows")
    r0 = max(rows, key=lambda r: r["record_date"])
    return {"ts": r0["record_date"], "value": _close(r0)}


def fetch_first_ts(series_id: str = "FISCAL:TGA_DAILY", session=None) -> str:
    """First observation of the full history (depth gate). debt_to_penny: the
    whole history is 8,385 rows < one max page; the split series return their
    first NON-null date (1997-09-30 live 2026-09-04), not the 1993 table start."""
    if series_id in DEBT_PENNY_FIELD:
        field = DEBT_PENNY_FIELD[series_id]
        rows = _get(
            DEBT_PENNY,
            {"fields": f"record_date,{field}", "sort": "record_date", "page[size]": PAGE_SIZE_MAX},
            session,
        ).get("data", [])
        for r in rows:
            if _num(r.get(field)) is not None:
                return r["record_date"]
        raise FiscalError(f"{series_id}: no non-null rows in history")
    rows = _rows(ERAS[0][0], "record_date", 1)
    if not rows:
        raise FiscalError("TGA: era-1 empty")
    return rows[0]["record_date"]
