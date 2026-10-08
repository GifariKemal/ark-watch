"""fiscal.py — Treasury fiscal signals (fiscaldata, migration v10).

Data basis (migration v10, harvested by the fd fiscaldata layer):
  - fd_auctions          — FULL per-CUSIP auction history 1979→ (~14k rows);
                           the calibration gift: every demand percentile below
                           is measured against ~50 years of same-term auctions
  - fd_debt_transactions — rolling ~14d Issues/Redemptions, Marketable AND
                           Nonmarketable (net issuance reads MARKETABLE only)
  - fd_interest_expense  — monthly interest expense, hierarchical rows
                           (month_amt / fytd_amt per category/group/type)
  - fd_avg_rates         — monthly avg interest rate, per-instrument rows AND
                           aggregate rows like 'Total Marketable'
  - raw_observations FISCAL:DEBT_TOTAL (daily, raw USD)

Units: SPLIT BY FAMILY (fetch-layer identity proof, migration v10 comment):
fd_debt_transactions.amount_today is $ MILLIONS (the DTS family — same as the
TGA series; ΣFYTD(issues−redemptions) matched Δdebt-to-penny to the dollar) →
M_TO_B there; fd_interest_expense and the FISCAL:DEBT_* series are raw USD →
PAR_TO_B (soma convention); the debt stock renders $T (RAW_TO_T). Bid-to-cover,
percentiles and rates are dimensionless.

Term matching is defensive on BOTH security_term and security_type: the pairs
that matter are Bill 4-Week + Note 2/5/10-Year + Bond 30-Year, and a bare term
match would pick up 10-Year TIPS / 2-Year FRN auctions whose bid-to-cover
distributions are a different animal. Normalization (case/whitespace/hyphen
insensitive, plus a compound-term prefix pass) tolerates live string variants;
unmatched shapes degrade silently.

Degradation: every public function no-ops ({}, None, 0) when its v10 table is
missing or empty — pre-v10 DBs skip the whole layer (_table_exists convention).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta

# The WEAK bucket shares the ALERT threshold from params_signals.yaml
# (fd_auction_weak_pct) — one config value so the brief label and the watcher
# alert can never disagree (the copper one-threshold precedent). The watcher
# still passes weak_pct explicitly into auction_demand_weak_alert (soma
# threshold_b convention — REQUIRED param); PCT_WEAK below is the same
# number, read at import for the display buckets.
try:
    from ..config import load_params_signals

    PCT_WEAK = float(load_params_signals().get("fd_auction_weak_pct", 10))
except Exception:
    PCT_WEAK = 10.0

# Unit conversions at the boundary (SPLIT BY FAMILY — see module docstring):
PAR_TO_B = 1e-9  # raw USD → $B (interest_expense, FISCAL:DEBT_* series)
M_TO_B = 1e-3  # $M → $B (fd_debt_transactions — the DTS family)
RAW_TO_T = 1e-12  # raw USD → $T (the debt-stock display)

# The (security_term, security_type) pairs that matter — NOMINAL only (a 10-Year
# TIPS or 2-Year FRN auction must not enter the nominal 10Y/2Y demand history).
# Dict order = display/inputs order; 4-Week first mirrors the bill calendar.
AUCTION_TERMS: tuple[tuple[str, str], ...] = (
    ("4-Week", "Bill"),
    ("2-Year", "Note"),
    ("5-Year", "Note"),
    ("10-Year", "Note"),
    ("30-Year", "Bond"),
)
# Headline term for the brief/store/alert: 10Y (the benchmark tenor), 5Y the
# named fallback. Other terms live in inputs_json / the terms dict only.
HEADLINE_TERMS = ("10-Year", "5-Year")
TERM_LABELS = {  # short brief labels ("10y b/c 2.31 p35" style)
    "4-Week": "4w",
    "2-Year": "2y",
    "5-Year": "5y",
    "10-Year": "10y",
    "30-Year": "30y",
}

# Percentile honesty floor (the dealer DEALER_Z_MIN_OBS convention): below
# ~2 years of same-term auctions a percentile mostly measures the window's own
# noise → honest None. Never bites on the full 1979+ history (hundreds of
# auctions per term); it protects young/partial tables only.
AUCTION_MIN_HIST = 26

# An auction older than this never fires the weak-demand alert. Auctions are
# frequent (4W bills weekly, 10Y ~monthly); 10d covers a holiday cycle while a
# dead harvest cannot re-alert forever.
AUCTION_ALERT_MAX_AGE_DAYS = 10

# computed_signals state buckets for the headline demand percentile. WEAK
# comes from config (above); SOFT/STRONG are display-only labels.
PCT_SOFT = 25.0  # below = SOFT
PCT_STRONG = 75.0  # at/above = STRONG; between = NORMAL

DEBT_TOTAL_SERIES = "FISCAL:DEBT_TOTAL"  # raw USD (registry, fd-build)

# Net issuance: trailing 7-calendar-day window ending at the latest record
# date (business days carry rows; weekends/holidays are simply absent).
NET_ISS_WINDOW_DAYS = 7
# The 'net iss 7d' segment reads as a CURRENT flow — an anchor older than 10d
# means the daily harvest broke, and the segment is marked stale (soma suffix
# convention) instead of passing an old window off as now.
NET_ISS_STALE_DAYS = 10

# Interest-expense fallback (no unambiguous TOTAL row): compare the month-sum
# against the same fiscal month one year back. Needs ≥13 distinct months of
# history and a prior-year record_date within ±YOY_MATCH_DAYS. ±15d tolerates
# month-end date drift (28-31d spacing) without ever matching a NEIGHBOR
# month — ±35d could pass an 11/13-month gap off as YoY.
FISCAL_MIN_MONTHS = 13
YOY_MATCH_DAYS = 15
_YEAR_DAYS = 365


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Single-table existence probe — v10 tables land in a separate migration
    (fd-build); every fiscal signal degrades on its own when absent."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row and row[0] == 1)


def _norm(s: str | None) -> str:
    """Defensive string key: lowercase, strip, drop spaces/hyphens/underscores
    ('10-Year' == '10 year' == '10YEAR')."""
    return re.sub(r"[\s\-_]+", "", (s or "").strip().lower())


_NORMED_TERMS = [(_norm(t), _norm(sec), t) for t, sec in AUCTION_TERMS]


def _match_term(term: str | None, sec_type: str | None) -> str | None:
    """Canonical AUCTION_TERMS entry for a live (term, type) pair, or None.

    Exact normalized match first; then a prefix pass for compound live shapes
    ('4-Week Bill' as a single term string). Prefixes are unambiguous within
    the set that matters ('2-Year' never prefixes '20-Year': '2year' vs
    '20year' diverge at the 2nd character).

    ROUND-5: benchmark REOPENINGS carry remaining-maturity shapes like
    '9-Year 11-Month' — canonicalized by rounding the tenor UP to the next
    integer year ('9-Year 11-Month' → 10-Year, '29-Year 3-Month' → 30-Year,
    '4-Year 10-Month' → 5-Year). Without this the auction-demand headline
    froze at the last original-issue date (live: stuck 08-12 while the same
    10-Year bucket auctioned 09-09 under the reopening label)."""
    import re as _re

    tn, sn = _norm(term), _norm(sec_type)
    if not tn:
        return None
    for nt, ns, canon in _NORMED_TERMS:
        if tn == nt and sn == ns:
            return canon
    # reopening remaining-maturity: '9-Year 11-Month' (normalized
    # '9year11month') → year 9 + 1 → 10-Year. Months round the tenor UP.
    m = _re.fullmatch(r"(\d+)year(?:\d+month)?", tn)
    if m and sn and "bill" not in sn:
        yrs = int(m.group(1)) + (1 if "month" in tn else 0)
        for nt, ns, canon in _NORMED_TERMS:
            if nt == f"{yrs}year" and ns == sn:
                return canon
    for nt, ns, canon in _NORMED_TERMS:
        if tn.startswith(nt) and sn == ns:
            return canon
    return None


def _pctile(values: list[float], cur: float) -> float:
    """Inclusive percentile: share of history AT OR BELOW cur (the watcher's
    HY-percentile convention — the current observation sits in its own
    population)."""
    return 100.0 * sum(1 for v in values if v <= cur) / len(values)


def _bucket(pct: float | None) -> str:
    """Headline state label from the demand percentile."""
    if pct is None:
        return "N/A"
    if pct < PCT_WEAK:
        return "WEAK"
    if pct < PCT_SOFT:
        return "SOFT"
    if pct >= PCT_STRONG:
        return "STRONG"
    return "NORMAL"


def _auction_history(conn: sqlite3.Connection) -> dict[str, list[tuple[str, float]]]:
    """term → [(auction_date, bid_to_cover)] ascending for the terms that
    matter, nominal types only. cusip breaks same-date ordering ties so the
    'latest' pick is deterministic."""
    rows = conn.execute(
        "SELECT security_term, security_type, auction_date, bid_to_cover "
        "FROM fd_auctions "
        "WHERE bid_to_cover IS NOT NULL AND security_term IS NOT NULL "
        "ORDER BY auction_date, cusip"
    ).fetchall()
    hist: dict[str, list[tuple[str, float]]] = {}
    for term, sec, ad, btc in rows:
        canon = _match_term(term, sec)
        if canon is not None:
            hist.setdefault(canon, []).append((ad, btc))
    return hist


def auction_demand(conn: sqlite3.Connection, min_hist: int | None = None) -> dict:
    """Latest-auction demand snapshot for the terms that matter.

    {as_of_date, headline_term, terms: {term: {auction_date, bid_to_cover,
    percentile, n_history}}}

    - percentile = inclusive share of ALL same-term NOMINAL auctions at or
      below the latest bid-to-cover (the 1979+ full history in fd_auctions is
      the calibration base); None under AUCTION_MIN_HIST obs (honest).
    - headline_term = 10-Year, else 5-Year, else None (other terms are context
      only — the brief/store/alert read the headline).

    Returns {} when fd_auctions is missing/empty (pre-v10 degrade).
    """
    if not _table_exists(conn, "fd_auctions"):
        return {}
    hist = _auction_history(conn)
    if not hist:
        return {}
    floor = AUCTION_MIN_HIST if min_hist is None else min_hist
    terms: dict[str, dict] = {}
    for term, _sec in AUCTION_TERMS:
        h = hist.get(term)
        if not h:
            continue
        auction_date, btc = h[-1]
        vals = [v for _d, v in h]
        terms[term] = {
            "auction_date": auction_date,
            "bid_to_cover": btc,
            "percentile": _pctile(vals, btc) if len(vals) >= floor else None,
            "n_history": len(vals),
        }
    if not terms:
        return {}
    return {
        "as_of_date": max(t["auction_date"] for t in terms.values()),
        "headline_term": next((t for t in HEADLINE_TERMS if t in terms), None),
        "terms": terms,
    }


def net_issuance(conn: sqlite3.Connection) -> dict:
    """Marketable net issuance over the trailing 7 calendar days ($B).

    Daily net = Σ Issues(Marketable) − Σ Redemptions(Marketable), raw USD in
    the table → $B at the boundary. Nonmarketable rows (savings bonds etc.)
    never touch market liquidity and are excluded. The window is anchored on
    MAX(record_date) (not today) so a dead harvest reports its own last
    window, and n_days exposes how many business days actually carry rows.

    Returns {} when fd_debt_transactions is missing/empty (pre-v10 degrade).
    """
    if not _table_exists(conn, "fd_debt_transactions"):
        return {}
    row = conn.execute("SELECT MAX(record_date) FROM fd_debt_transactions").fetchone()
    if not row or not row[0]:
        return {}
    anchor = row[0]
    start = (date.fromisoformat(anchor) - timedelta(days=NET_ISS_WINDOW_DAYS - 1)).isoformat()
    rows = conn.execute(
        "SELECT record_date, transaction_type, amount_today FROM fd_debt_transactions "
        "WHERE record_date >= ? AND lower(security_market) = 'marketable' "
        "AND amount_today IS NOT NULL",
        (start,),
    ).fetchall()
    daily: dict[str, float] = {}
    for rd, ttype, amt in rows:
        tn = _norm(ttype)
        if tn.startswith("issue"):
            daily[rd] = daily.get(rd, 0.0) + amt
        elif tn.startswith("redemption"):
            daily[rd] = daily.get(rd, 0.0) - amt
    return {
        "record_date": anchor,
        "window_start": start,
        "window_days": NET_ISS_WINDOW_DAYS,
        "n_days": len(daily),
        "net_7d_b": sum(daily.values()) * M_TO_B,
        "daily_b": {d: v * M_TO_B for d, v in sorted(daily.items())},
    }


def _fytd_row(rows: list[tuple]) -> tuple | None:
    """The PUBLIC-ISSUES category total of one record_date.

    Live shape (verified 2026-09-04): this dataset has NO aggregate 'Total'
    row — the headline is the SUM of fytd_amt over expense_catg_desc =
    'INTEREST EXPENSE ON PUBLIC ISSUES' (each component row's FYTD is its own
    running total, so the category sum IS the total). The GOVT ACCOUNT SERIES
    category is intragovernmental and is excluded from the public headline.
    The rows arg is (fytd_amt, catg, type, month_amt); returns
    (fytd_sum, month_sum) or None when the category is absent."""
    fytd = sum(
        r[0] or 0.0
        for r in rows
        if (r[1] or "").strip().upper() == "INTEREST EXPENSE ON PUBLIC ISSUES"
    )
    month = sum(
        r[3] or 0.0
        for r in rows
        if (r[1] or "").strip().upper() == "INTEREST EXPENSE ON PUBLIC ISSUES"
    )
    # FYTD is cumulative, so fytd ≥ month on valid data; a zero FYTD with a
    # nonzero month means a partial publish (NULL fytd legs) — ambiguous, not
    # a real zero: decline so the caller takes the honest month fallback.
    if fytd == 0.0:
        return None
    return (fytd, month)


def _month_sum(conn: sqlite3.Connection, rd: str) -> float | None:
    """Month-sum at one record_date: PUBLIC-ISSUES category when present (the
    GAS category is intragovernmental and must not inflate the public
    headline); when the category is absent entirely (defensive fallback shape)
    sum all rows — the YoY RATIO stays meaningful under a consistent shape."""
    pub = conn.execute(
        "SELECT SUM(month_amt) FROM fd_interest_expense "
        "WHERE record_date=? AND month_amt IS NOT NULL "
        "AND upper(trim(expense_catg_desc)) = 'INTEREST EXPENSE ON PUBLIC ISSUES'",
        (rd,),
    ).fetchone()
    if pub is not None and pub[0] is not None:
        return pub[0]
    row = conn.execute(
        "SELECT SUM(month_amt) FROM fd_interest_expense "
        "WHERE record_date=? AND month_amt IS NOT NULL",
        (rd,),
    ).fetchone()
    return None if row is None or row[0] is None else row[0]


def _year_ago_date(conn: sqlite3.Connection, latest: str) -> str | None:
    """record_date closest to one year before `latest` (±YOY_MATCH_DAYS) —
    monthly rows drift, an exact −365d lookup would miss."""
    target = date.fromisoformat(latest) - timedelta(days=_YEAR_DAYS)
    lo = (target - timedelta(days=YOY_MATCH_DAYS)).isoformat()
    hi = (target + timedelta(days=YOY_MATCH_DAYS)).isoformat()
    row = conn.execute(
        "SELECT record_date FROM fd_interest_expense WHERE record_date BETWEEN ? AND ? "
        "ORDER BY abs(julianday(record_date) - julianday(?)) LIMIT 1",
        (lo, hi, target.isoformat()),
    ).fetchone()
    return None if row is None else row[0]


def interest_burden(conn: sqlite3.Connection) -> dict:
    """Interest burden snapshot: FYTD expense ($B) + marketable avg rate (%).

    FYTD = the PUBLIC-ISSUES category sum (see _fytd_row — the live dataset
    has no aggregate row; verified shape 2026-09-04). The month path mirrors
    the same category filter; yoy needs ≥ FISCAL_MIN_MONTHS months of history
    else honest None. avg_rate_mkt = latest 'Total Marketable' row of
    fd_avg_rates (percent, as stored).

    Returns {} when neither table yields anything (pre-v10 degrade).
    """
    out: dict = {
        "record_date": None,
        "fytd_b": None,
        "month_b": None,  # fallback path: latest month-sum level ($B)
        "yoy_pct": None,  # fallback path: vs same fiscal month last year
        "avg_rate_mkt": None,
        "avg_rate_date": None,
    }
    if _table_exists(conn, "fd_interest_expense"):
        latest = conn.execute("SELECT MAX(record_date) FROM fd_interest_expense").fetchone()[0]
        if latest:
            out["record_date"] = latest
            rows = conn.execute(
                "SELECT fytd_amt, expense_catg_desc, expense_type_desc, month_amt "
                "FROM fd_interest_expense WHERE record_date=?",
                (latest,),
            ).fetchall()
            hit = _fytd_row(rows)
            if hit is not None:
                out["fytd_b"] = hit[0] * PAR_TO_B
            else:
                n_months = conn.execute(
                    "SELECT COUNT(DISTINCT record_date) FROM fd_interest_expense"
                ).fetchone()[0]
                prior = _year_ago_date(conn, latest) if n_months >= FISCAL_MIN_MONTHS else None
                cur_m = _month_sum(conn, latest)
                prv_m = None if prior is None else _month_sum(conn, prior)
                if cur_m is not None and prv_m:
                    out["month_b"] = cur_m * PAR_TO_B
                    out["yoy_pct"] = (cur_m / prv_m - 1.0) * 100.0
    if _table_exists(conn, "fd_avg_rates"):
        row = conn.execute(
            "SELECT record_date, avg_interest_rate FROM fd_avg_rates "
            "WHERE upper(trim(security_desc)) = 'TOTAL MARKETABLE' "
            "AND avg_interest_rate IS NOT NULL ORDER BY record_date DESC LIMIT 1"
        ).fetchone()
        if row is None:  # defensive: live capitalization/spacing variants
            row = conn.execute(
                "SELECT record_date, avg_interest_rate FROM fd_avg_rates "
                "WHERE upper(trim(security_desc)) LIKE 'TOTAL%MARKETABLE%' "
                "AND avg_interest_rate IS NOT NULL ORDER BY record_date DESC LIMIT 1"
            ).fetchone()
        if row is not None:
            out["avg_rate_date"], out["avg_rate_mkt"] = row[0], row[1]
    if out["record_date"] is None and out["avg_rate_mkt"] is None:
        return {}
    return out


def auction_demand_weak_alert(
    conn: sqlite3.Connection, weak_pct: float, max_age_days: int | None = None
) -> dict | None:
    """Headline (10Y, 5Y fallback) auction demand at/below the weak percentile.

    weak_pct is REQUIRED on purpose (soma threshold_b convention): the
    calibrated value lives in params_signals.yaml (fd_auction_weak_pct); a
    default here would silently resurrect the placeholder for any future
    caller that forgets to pass it. Percentile None (short history) → no fire;
    freshness-capped at AUCTION_ALERT_MAX_AGE_DAYS so a dead harvest cannot
    re-alert forever.
    """
    ad = auction_demand(conn)
    if not ad:
        return None
    term = ad.get("headline_term")
    if not term:
        return None
    d = ad["terms"][term]
    pct = d.get("percentile")
    if pct is None or pct > weak_pct:
        return None
    cap = AUCTION_ALERT_MAX_AGE_DAYS if max_age_days is None else max_age_days
    age = (datetime.now(UTC).date() - date.fromisoformat(d["auction_date"])).days
    if age > cap:
        return None
    return {
        "term": term,
        "auction_date": d["auction_date"],
        "bid_to_cover": d["bid_to_cover"],
        "percentile": pct,
        "n_history": d["n_history"],
        "weak_pct": weak_pct,
    }


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def _signed_b(v: float, dp: int = 0) -> str:
    """Signed $B: +58.4 → '+$58B' (soma _signed_b display convention)."""
    return f"{'+' if v >= 0 else '-'}${abs(v):.{dp}f}B"


def store_fiscal_signals(conn: sqlite3.Connection) -> int:
    """Persist the 3 fiscal signals to computed_signals (audit trail).

    Rows (ts = each source's latest date → natural dedup via INSERT OR
    REPLACE, store_soma_signals convention):
    - fd_auction_demand ts = HEADLINE TERM's auction date (not the max across
      terms — a weekly 4W bill would otherwise stamp a ts up to ~4w newer
      than the 10Y value it labels; backtest/explore join ts↔value)
      value = headline b/c (10Y, 5Y fallback)
      state = WEAK/SOFT/NORMAL/STRONG percentile bucket
    - fd_net_issuance   ts = latest record date    value = 7d net ($B)
                          state = ISSUING/REDEEMING/FLAT
    - fd_interest_burden ts = latest expense date  value = FYTD $B (month-sum
      level on the fallback path)                    state = FYTD/MTH_YOY/N/A

    inputs_json carries the full context (all terms' percentiles, the daily
    net-issuance window, the burden pieces) — the schema stores one value per
    signal_id, soma convention. Returns 0 when there is no fiscal data — a
    no-op, not an error.
    """
    ad = auction_demand(conn)
    ni = net_issuance(conn)
    ib = interest_burden(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[tuple] = []

    if ad and ad.get("headline_term"):
        term = ad["headline_term"]
        d = ad["terms"][term]
        rows.append(
            (
                "fd_auction_demand",
                d["auction_date"],  # the headline value's OWN observation date
                now,
                now,
                round(d["bid_to_cover"], 4),
                _bucket(d["percentile"]),
                json.dumps(
                    {
                        "unit": "bid_to_cover",
                        "headline_term": term,
                        "percentile": _r(d["percentile"], 1),
                        "terms": {
                            t: {
                                "auction_date": s["auction_date"],
                                "bid_to_cover": round(s["bid_to_cover"], 4),
                                "percentile": _r(s["percentile"], 1),
                                "n_history": s["n_history"],
                            }
                            for t, s in ad["terms"].items()
                        },
                    }
                ),
            )
        )
    if ni:
        v = ni["net_7d_b"]
        state = "ISSUING" if v > 0 else ("REDEEMING" if v < 0 else "FLAT")
        rows.append(
            (
                "fd_net_issuance",
                ni["record_date"],
                now,
                now,
                round(v, 3),
                state,
                json.dumps(
                    {
                        "unit": "$B",
                        "window_days": ni["window_days"],
                        "n_days": ni["n_days"],
                        "net_7d_b": round(v, 3),
                        "daily_b": {k: round(x, 3) for k, x in ni["daily_b"].items()},
                    }
                ),
            )
        )
    if ib and (ib["record_date"] is not None or ib["avg_rate_mkt"] is not None):
        value = ib["fytd_b"] if ib["fytd_b"] is not None else ib["month_b"]
        state = (
            "FYTD"
            if ib["fytd_b"] is not None
            else ("MTH_YOY" if ib["month_b"] is not None else "N/A")
        )
        ts = ib["record_date"] or ib["avg_rate_date"]
        rows.append(
            (
                "fd_interest_burden",
                ts,
                now,
                now,
                _r(value),
                state,
                json.dumps(
                    {
                        "unit": "$B",
                        "fytd_b": _r(ib["fytd_b"]),
                        "month_b": _r(ib["month_b"]),
                        "yoy_pct": _r(ib["yoy_pct"], 1),
                        "avg_rate_mkt": _r(ib["avg_rate_mkt"], 2),
                        "avg_rate_date": ib["avg_rate_date"],
                    }
                ),
            )
        )

    if not rows:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def fiscal_brief_line(conn: sqlite3.Connection) -> str | None:
    """Render the one-line fiscal segment (next to the DLR/Opt block).

    Fiscal: debt $37.4T · int FYTD $881B (avg mkt 3.3%) · 10y b/c 2.31 p80 · net iss 7d +$58B

    Segments degrade independently (debt / int / b/c / net iss); the int
    segment falls back to the month-sum YoY read ('int mth $90B +25%/yr') when
    the FYTD row is ambiguous. Returns None when NO segment has data (pre-v10
    DB or failed harvests — degradable).
    """
    segs: list[str] = []

    row = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? AND vintage_ts='realtime' "
        "AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (DEBT_TOTAL_SERIES,),
    ).fetchone()
    if row:
        seg = f"debt ${row[1] * RAW_TO_T:.1f}T"
        age = (datetime.now(UTC).date() - date.fromisoformat(str(row[0])[:10])).days
        if age > NET_ISS_STALE_DAYS:  # daily series >10d old = dead harvest
            seg += f" ⚠(stale {age}d)"
        segs.append(seg)

    ib = interest_burden(conn)
    int_seg = None
    if ib.get("fytd_b") is not None:
        int_seg = f"int FYTD ${ib['fytd_b']:.0f}B"
    elif ib.get("month_b") is not None:
        int_seg = f"int mth ${ib['month_b']:.0f}B {ib['yoy_pct']:+.0f}%/yr"
    rate_seg = None if ib.get("avg_rate_mkt") is None else f"avg mkt {ib['avg_rate_mkt']:.1f}%"
    if int_seg and rate_seg:
        segs.append(f"{int_seg} ({rate_seg})")
    elif int_seg:
        segs.append(int_seg)
    elif rate_seg:
        segs.append(f"({rate_seg})")

    ad = auction_demand(conn)
    if ad and ad.get("headline_term"):
        term = ad["headline_term"]
        d = ad["terms"][term]
        seg = f"{TERM_LABELS[term]} b/c {d['bid_to_cover']:.2f}"
        if d["percentile"] is not None:
            seg += f" p{d['percentile']:.0f}"
        segs.append(seg)

    ni = net_issuance(conn)
    if ni:
        seg = f"net iss 7d {_signed_b(ni['net_7d_b'])}"
        # a thinner-than-normal window inside the table (partial publish) must
        # not pass itself off as a full weekly flow — surface the day count
        if ni["n_days"] < 5:
            seg += f" ({ni['n_days']}d)"
        age = (datetime.now(UTC).date() - date.fromisoformat(ni["record_date"])).days
        if age > NET_ISS_STALE_DAYS:
            seg += f" ⚠(stale {age}d)"
        segs.append(seg)

    if not segs:
        return None
    return "Fiscal: " + " · ".join(segs)
