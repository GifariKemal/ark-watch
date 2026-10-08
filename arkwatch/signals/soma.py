"""soma.py — SOMA signals: maturity buckets, TIPS split, net liquidity, float scarcity.

Data basis (migration v7, harvested by fetchers/soma.py per PLAN-SOMA.md):
  - soma_holdings  — per-CUSIP weekly snapshots (as_of_date = Wednesday)
  - soma_summary   — weekly aggregate incl. rolling_off_7d/30d/90d
  - raw_observations FRED:WALCL (weekly Wednesday, $M) · FISCAL:TGA_DAILY
    (daily, $M) · FRED:RRPONTSYD (daily, $B)

v9 additions (PLAN-NYFED-FULL §2, degradable per-table when absent):
  - soma_agency_summary — weekly agency portfolio totals (MBS/CMBS/debts) on
    the SAME Wednesday grid → the Net Liq 'MBS/other' residual splits into an
    exact ΔMBS + a small 'other' (repo/float) residual
  - fed_operations — desk outright operations (tsy|ambs) → the ops explainer
    ties the weekly gross purchases to concrete auction results

Units: every money value RETURNED or RENDERED by this module is $B. The SOMA
tables store par in raw USD (PLAN-SOMA §3) → one conversion at the boundary
(PAR_TO_B); WALCL/TGA are $M → M_TO_B; RRPONTSYD is natively $B.

Portfolio-change semantics (verified to the dollar on 107/107 consecutive
week-pairs — 108 backfilled weeks, 2026-09-03): soma_summary.weekly_change is ΣchangeFromPriorWeek over
issues that SURVIVE the week — par maturing during the week silently leaves
the snapshot, so it never appears as a negative change. Identity:
Δtotal_par = weekly_change − prior week's rolling_off_7d. Portfolio and
liquidity math therefore uses Δtotal_par (consecutive snapshot totals);
weekly_change is exposed as gross purchases only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

# Single source for the maturity-bucket definition (edges + labels) — the
# same primitive the fetcher uses at harvest time.
from ..fetchers.soma import MATURITY_BUCKET_LABELS, maturity_bucket

try:
    from ..config import load_params_signals

    _PS = load_params_signals()
    # Conversions inside the guard: a non-numeric yaml value must not raise
    # at import time — the watcher imports this module inside a try, and an
    # import error would silently disable all three SOMA triggers.
    SOMA_WALCL_GAP_ALERT_B = float(_PS.get("soma_walcl_gap_alert_b", 80.0))
    SOMA_SPECIALS_NEW_PAR_B = float(_PS.get("soma_specials_new_par_b", 10.0))
    OPS_EXPLAIN_MIN_SHARE = float(_PS.get("ops_explain_min_share", 0.5))
except Exception:
    _PS = {}
    SOMA_WALCL_GAP_ALERT_B = 80.0
    SOMA_SPECIALS_NEW_PAR_B = 10.0
    OPS_EXPLAIN_MIN_SHARE = 0.5

# PLAN-SOMA §3: par_value/change_week/rolling_off_* are raw USD → $B scale.
PAR_TO_B = 1e-9
# FRED WALCL / fiscaldata TGA arrive in $M → $B.
M_TO_B = 1e-3

# A snapshot older than this never fires a SOMA alert. SOMA is weekly
# (Thursday release); 14 days covers US holidays without letting a broken
# harvest re-alert forever — the watcher cooldown is only 6h.
SOMA_ALERT_MAX_AGE_DAYS = 14

# Curve segments (Kurva line) below this weekly change ($B) render as
# '+$0B/wk' noise at 0-dp — only moves at or above display resolution are
# shown. Live 2026-08-26: TIPS change exactly 0 → the segment is noise.
SOMA_DISPLAY_FLOOR_B = 1.0

# The buy/matured detail on the headline renders only when both legs clear
# this ($B). Live shape clears it easily (buy +$42B · matured $38B); a quiet
# week must not print "(buy +$3B · matured $2B)".
GROSS_DETAIL_FLOOR_B = 10.0

# FR-34 anchors (block E). All three are read at the SOMA Wednesday dates so
# the whole liquidity line shares one weekly grid.
WALCL_SERIES = "FRED:WALCL"  # total Fed assets, $M
TGA_SERIES = "FISCAL:TGA_DAILY"  # Treasury General Account, $M
RRP_SERIES = "FRED:RRPONTSYD"  # ON-RRP take-up, $B

DAYS_PER_YEAR = 365.25  # matches fetchers/soma (WAM cross-check 8.26y)

# Float scarcity: the API shows the Fed's per-issue ownership capped at 70%
# (0.6999 fraction); 69.5 catches capped rows despite float dust. Issues at
# the cap have almost no free float left → repo-special candidates.
SPECIALS_CAP_PCT = 69.5
# A NEW cap-hitter = at the cap now with no capped row in ANY prior snapshot
# (and at least one prior snapshot existing — a fresh backfill's left edge
# must not flag every issue as NEW). 'Any prior' rather than a fixed 7d
# lookback survives the 6-day gaps in the NY Fed weekly calendar (Tuesday
# as-of when Wednesday is a holiday: Christmas, Juneteenth, Veterans Day).

# Alert thresholds (SOMA_WALCL_GAP_ALERT_B / SOMA_SPECIALS_NEW_PAR_B) are
# config-backed and parsed inside the import guard at the top of this module.


def _soma_tables_ready(conn: sqlite3.Connection) -> bool:
    """True when both v7 SOMA tables exist (pre-migration DBs skip the signals)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name IN ('soma_holdings','soma_summary')"
    ).fetchone()
    return bool(row and row[0] == 2)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Single-table existence probe — v9 tables land in a separate migration
    and may be absent on a pre-v9 DB; each v9 feature degrades on its own."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row and row[0] == 1)


def _snapshot_fresh(as_of: str) -> bool:
    age = (datetime.now(UTC).date() - date.fromisoformat(as_of)).days
    return age <= SOMA_ALERT_MAX_AGE_DAYS


def _obs_at_or_before(conn: sqlite3.Connection, sid: str, anchor: str) -> tuple[str, float] | None:
    """(ts, value) of the latest realtime row at or before `anchor` date.

    Anchoring on the SOMA Wednesday keeps weekly Δs on one grid and survives
    holidays (a fixed row-offset would drift across long weekends). The ts is
    returned so callers can refuse Δs whose two anchors resolved to the SAME
    row — that yields a fake Δ=0 (e.g. this week's WALCL release missing →
    both anchors land on last Wednesday's row).
    """
    if anchor is None:
        return None
    row = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? AND vintage_ts='realtime' "
        "AND ts<=? AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (sid, anchor),
    ).fetchone()
    return None if row is None else (row[0], row[1])


def _anchored_delta(
    conn: sqlite3.Connection, sid: str, cur_anchor: str, prev_anchor: str | None, scale: float
) -> tuple[float | None, float | None]:
    """(level, Δ) of a series between the two snapshot anchors, in `scale`
    units ($B). Δ is None unless BOTH anchors resolved and to DIFFERENT rows
    (same-row → no observation between the anchors → honest None, not 0)."""
    cur = _obs_at_or_before(conn, sid, cur_anchor)
    prev = _obs_at_or_before(conn, sid, prev_anchor)
    level = None if cur is None else cur[1]
    if cur is None or prev is None or cur[0] == prev[0]:
        return level, None
    return level, (cur[1] - prev[1]) * scale


def _bucket_stats(conn: sqlite3.Connection, as_of: str) -> dict[str, dict]:
    """Per-bucket {par, change, n_cusips} at one snapshot date (raw USD).

    change = Σ per-CUSIP changeFromPriorWeek inside the bucket — deliberate
    portfolio action (QT/redemptions/purchases), NOT the two-snapshot par
    diff, which would conflate QT with bonds merely aging across a bucket
    edge. NULL changes (pre-2013 history) count as 0.
    """
    stats = {b: {"par": 0.0, "change": 0.0, "n": 0} for b in MATURITY_BUCKET_LABELS}
    rows = conn.execute(
        "SELECT maturity_date, par_value, change_week FROM soma_holdings "
        "WHERE as_of_date=? AND maturity_date IS NOT NULL AND par_value IS NOT NULL",
        (as_of,),
    ).fetchall()
    as_of_d = date.fromisoformat(as_of)
    for mat, par, chg in rows:
        yrs = (date.fromisoformat(mat) - as_of_d).days / DAYS_PER_YEAR
        b = stats[maturity_bucket(yrs)]
        b["par"] += par
        b["change"] += chg or 0.0
        b["n"] += 1
    return stats


def _tips_from_summary(conn: sqlite3.Connection) -> dict:
    """TIPS par + weekly change ($B) from soma_summary — avoids depending on
    the exact security_type strings in soma_holdings."""
    rows = conn.execute(
        "SELECT tips FROM soma_summary WHERE tips IS NOT NULL ORDER BY as_of_date DESC LIMIT 2"
    ).fetchall()
    if not rows:
        return {"par": None, "change": None}
    change = (rows[0][0] - rows[1][0]) if len(rows) > 1 else None
    return {
        "par": rows[0][0] * PAR_TO_B,
        "change": None if change is None else change * PAR_TO_B,
    }


def soma_maturity_profile(conn: sqlite3.Connection) -> dict:
    """Current bucket distribution + weekly change per bucket.

    Per-bucket change_week = Σ per-CUSIP changeFromPriorWeek (deliberate
    portfolio change — see _bucket_stats); the bucket pars therefore do NOT
    diff to it. Returns {} when the SOMA tables are missing or empty
    (degradable).
    """
    if not _soma_tables_ready(conn):
        return {}
    row = conn.execute("SELECT MAX(as_of_date) FROM soma_holdings").fetchone()
    if not row or not row[0]:
        return {}
    cur_d = row[0]
    stats = _bucket_stats(conn, cur_d)
    buckets = [
        {
            "name": name,
            "par": s["par"] * PAR_TO_B,  # $B
            "change_week": s["change"] * PAR_TO_B,  # $B
            "n_cusips": s["n"],
        }
        for name, s in stats.items()
    ]
    # 'steepest' = the bucket QT is cutting deepest (most negative weekly
    # change). Named only when it is actually draining (min < 0): on a
    # flat/no-drain week min() would pick an arbitrary zero bucket
    drain = min(buckets, key=lambda b: b["change_week"]) if buckets else None
    steepest = drain["name"] if drain and drain["change_week"] < 0 else None
    return {
        "as_of_date": cur_d,
        "buckets": buckets,
        "steepest_bucket": steepest,
        "tips": _tips_from_summary(conn),
    }


def soma_roll_off_alert(conn: sqlite3.Connection, threshold_b: float) -> dict | None:
    """Check if 7-day roll-off exceeds threshold ($B). Returns alert dict or None.

    threshold_b is REQUIRED on purpose: the calibrated value lives in
    params_signals.yaml (soma_roll_off_7d_alert_b, $65B ≈ p98 of the 108-week
    backfill). A default here silently resurrected the discarded $20B spec
    value (fired 46% of weeks) for any future caller that forgets to pass it.

    Freshness-capped at SOMA_ALERT_MAX_AGE_DAYS: a stale Thursday snapshot
    must not re-alert on every cooldown expiry.
    """
    if not _soma_tables_ready(conn):
        return None
    row = conn.execute(
        "SELECT as_of_date, rolling_off_7d, rolling_off_30d FROM soma_summary "
        "WHERE rolling_off_7d IS NOT NULL ORDER BY as_of_date DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    as_of, r7, r30 = row
    if not _snapshot_fresh(as_of):
        return None
    rolling_7d_b = r7 * PAR_TO_B
    if rolling_7d_b <= threshold_b:
        return None
    return {
        "as_of_date": as_of,
        "rolling_off_7d_b": rolling_7d_b,
        "rolling_off_30d_b": None if r30 is None else r30 * PAR_TO_B,
        "threshold_b": threshold_b,
    }


def _agency_split(
    conn: sqlite3.Connection,
    as_of: str | None,
    prev_as_of: str | None,
    dwalcl_b: float | None,
    soma_change_b: float | None,
) -> tuple[float | None, float | None]:
    """Net Liq v2: (Δmbs_b, other_b) — the agency split of the old MBS/other
    residual (PLAN-NYFED-FULL §2.1), or (None, None) when unavailable.

    ΔMBS = Δ total agency portfolio (MBS+CMBS+agency debts) from consecutive
    soma_agency_summary rows. The agency desk harvests the SAME Wednesday
    as-of grid as the tsy desk (one SOMA job), so the split requires a row at
    BOTH soma anchors EXACTLY — a lagged/misaligned agency harvest degrades to
    the combined residual rather than differencing two mismatched windows.
    other = ΔWALCL − ΔSOMA − ΔMBS (repo/float/other-assets residual, usually
    small). Raw USD in the table → $B at the boundary.
    """
    if (
        dwalcl_b is None
        or soma_change_b is None
        or prev_as_of is None
        or as_of is None
        or not _table_exists(conn, "soma_agency_summary")
    ):
        return None, None
    rows = conn.execute(
        "SELECT as_of_date, total FROM soma_agency_summary "
        "WHERE as_of_date IN (?, ?) AND total IS NOT NULL",
        (as_of, prev_as_of),
    ).fetchall()
    totals = dict(rows)
    if as_of not in totals or prev_as_of not in totals:
        return None, None
    mbs_b = (totals[as_of] - totals[prev_as_of]) * PAR_TO_B
    return mbs_b, dwalcl_b - soma_change_b - mbs_b


def soma_ops_explainer(
    conn: sqlite3.Connection,
    prev_as_of: str | None,
    as_of: str | None,
    gross_b: float | None,
    min_share: float | None = None,
) -> dict | None:
    """Largest Results leg that explains the SOMA week's gross purchases.

    Sums fed_operations amounts (raw USD → $B) by (family, direction) over
    (prev_as_of, as_of] — effective date = settlement when known, else the
    operation date (the portfolio moves on settlement). A purchase leg
    'explains' the weekly gross when its share of |gross purchases| clears
    ops_explain_min_share (placeholder 0.5); only direction='P' legs can
    explain purchases (ambs SALES drain the agency portfolio, which is a
    different leg of the decomposition). Returns None when the table is
    missing/empty (pre-v9 degrade) or no leg clears the share.
    """
    share = OPS_EXPLAIN_MIN_SHARE if min_share is None else min_share
    if (
        prev_as_of is None
        or as_of is None
        or not gross_b
        or not _table_exists(conn, "fed_operations")
    ):
        return None
    denom = abs(gross_b)
    if denom <= 0:
        return None
    rows = conn.execute(
        "SELECT family, direction, COUNT(*), SUM(amount) FROM fed_operations "
        "WHERE status='Results' AND direction='P' AND family='tsy' "
        # tsy only: gross_b is the TREASURY weekly_change — an ambs purchase
        # adds to the AGENCY portfolio and must never be told as the source of
        # the tsy growth line
        "AND COALESCE(settlement_date, operation_date) > ? "
        "AND COALESCE(settlement_date, operation_date) <= ? "
        "GROUP BY family, direction",
        (prev_as_of, as_of),
    ).fetchall()
    best: dict | None = None
    for family, direction, n_ops, amt in rows:
        if amt is None:
            continue
        leg_b = amt * PAR_TO_B
        s = leg_b / denom
        if s >= share and (best is None or s > best["share"]):
            best = {
                "family": family,
                "direction": direction,
                "n_ops": n_ops,
                "amount_b": leg_b,
                "share": s,
            }
    return best


def soma_net_liquidity(conn: sqlite3.Connection) -> dict:
    """Weekly liquidity decomposition anchored on the SOMA Wednesday snapshot.

    FR-34 (FEATURES.md): net liquidity = WALCL − RRP − TGA, so
    Δnet = ΔWALCL − ΔRRP − ΔTGA. ΔWALCL is decomposed into ΔSOMA (Treasury
    portfolio, exact from consecutive snapshot totals) + MBS/other (residual
    — MBS runoff, repos, float); on a v9 DB the residual further splits into
    an exact ΔMBS (agency summary) + 'other' (repo/float leftover). Cash
    parked at the RRP facility or rebuilt into the TGA is outside the private
    system, so a FALLING RRP / TGA is a POSITIVE liquidity contribution.

    ΔSOMA = Δtotal_par (consecutive soma_summary rows) — NOT weekly_change,
    which is gross purchases of surviving issues only (matured par silently
    leaves the snapshot; see module docstring identity). All series Δs are
    None unless both anchors resolved to DIFFERENT observations (a missing
    Wednesday row must degrade to the SOMA-only fallback, never fabricate
    Δ=0). span_days = distance between the two SOMA snapshots — a missed
    harvest widens it past 7 and the brief labels the Δ accordingly.

    Returns {} when the SOMA tables are missing or empty (degradable).
    """
    if not _soma_tables_ready(conn):
        return {}
    rows = conn.execute(
        "SELECT as_of_date, total_par, weekly_change FROM soma_summary "
        "ORDER BY as_of_date DESC LIMIT 2"
    ).fetchall()
    if not rows:
        return {}
    as_of, total_par, gross = rows[0]
    prev_as_of = rows[1][0] if len(rows) > 1 else None
    span_days = (
        None
        if prev_as_of is None
        else (date.fromisoformat(as_of) - date.fromisoformat(prev_as_of)).days
    )

    soma_change_b = None
    if prev_as_of is not None and rows[0][1] is not None and rows[1][1] is not None:
        soma_change_b = (rows[0][1] - rows[1][1]) * PAR_TO_B
    gross_b = None if gross is None else gross * PAR_TO_B
    matured_b = None if (gross_b is None or soma_change_b is None) else gross_b - soma_change_b

    walcl_level, dwalcl_b = _anchored_delta(conn, WALCL_SERIES, as_of, prev_as_of, M_TO_B)
    mbs_other_b = None if (dwalcl_b is None or soma_change_b is None) else dwalcl_b - soma_change_b
    # Net Liq v2 (migration v9): split the residual into the exact agency Δ
    # and the small leftover — None/None pre-v9 or on a misaligned agency grid.
    mbs_b, other_b = _agency_split(conn, as_of, prev_as_of, dwalcl_b, soma_change_b)

    rrp_level, rrp_change_b = _anchored_delta(conn, RRP_SERIES, as_of, prev_as_of, 1.0)
    rrp_contribution_b = None if rrp_change_b is None else -rrp_change_b

    _, tga_change_b = _anchored_delta(conn, TGA_SERIES, as_of, prev_as_of, M_TO_B)
    tga_contribution_b = None if tga_change_b is None else -tga_change_b

    # Anchor on ΔWALCL when available (all Fed assets); otherwise the SOMA
    # leg alone (the brief shows only the components it actually has).
    anchor_b = dwalcl_b if dwalcl_b is not None else soma_change_b
    net = None
    if anchor_b is not None:
        net = anchor_b + (rrp_contribution_b or 0.0) + (tga_contribution_b or 0.0)
    return {
        "as_of_date": as_of,
        "prev_as_of_date": prev_as_of,  # the Δ window's left anchor
        "span_days": span_days,  # 7 = normal week; >9 = missed harvest gap
        "total_par_b": None if total_par is None else total_par * PAR_TO_B,
        "soma_change_b": soma_change_b,  # ΔSOMA Treasury portfolio (exact)
        "gross_change_b": gross_b,  # ΣchangeFromPriorWeek (surviving issues)
        "matured_b": matured_b,  # par that matured out of the snapshot
        "dwalcl_b": dwalcl_b,  # Δ total Fed assets
        "mbs_other_b": mbs_other_b,  # ΔWALCL − ΔSOMA (= mbs_b + other_b when split)
        "mbs_b": mbs_b,  # Δ agency portfolio total, exact (v9; None pre-v9)
        "other_b": other_b,  # ΔWALCL − ΔSOMA − ΔMBS residual (v9)
        "rrp_level_b": rrp_level,  # facility take-up level ($B)
        "rrp_change_b": rrp_change_b,
        "rrp_contribution_b": rrp_contribution_b,  # −ΔRRP (liquidity-sign)
        "tga_change_b": tga_change_b,
        "tga_contribution_b": tga_contribution_b,  # −ΔTGA (liquidity-sign)
        "net_change_b": net,  # FR-34 Δnet-liquidity ($B/wk)
        "state": None if net is None else ("DRAINING" if net < 0 else "INJECTING"),
    }


def soma_walcl_gap_alert(conn: sqlite3.Connection, threshold_b: float | None = None) -> dict | None:
    """BUILD-PLAN §6.3 integrity tripwire: |ΔWALCL − ΔSOMA| beyond threshold.

    Historical envelope from the 108-week backfill: gap ∈ [−75.2, +59.4] $B
    (median −0.7) — real MBS runoff and operations live inside ±$75B. A gap
    beyond that is more likely a broken harvest than a market event, so this
    fires as a data-integrity alarm (default $80B, 0 fires in 2 years).
    """
    thr = SOMA_WALCL_GAP_ALERT_B if threshold_b is None else threshold_b
    nl = soma_net_liquidity(conn)
    if not nl or nl.get("mbs_other_b") is None:
        return None
    if not _snapshot_fresh(nl["as_of_date"]):
        return None
    gap = nl["mbs_other_b"]
    if abs(gap) <= thr:
        return None
    return {
        "as_of_date": nl["as_of_date"],
        "gap_b": gap,
        "dwalcl_b": nl["dwalcl_b"],
        "dsoma_b": nl["soma_change_b"],
        "threshold_b": thr,
    }


def soma_float_scarcity(conn: sqlite3.Connection) -> dict:
    """Issues the Fed holds ≈ at its 70% ownership cap → scarce free float.

    When the Fed owns ~all free float of an issue, borrowers in the repo
    market must pay up for it (it trades 'special') and the issue can trade
    rich vs the curve. Returns {} when no pct_outstanding data exists.
    """
    if not _soma_tables_ready(conn):
        return {}
    row = conn.execute(
        "SELECT MAX(as_of_date) FROM soma_holdings WHERE pct_outstanding IS NOT NULL"
    ).fetchone()
    if not row or not row[0]:
        return {}
    cur_d = row[0]
    n, par = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(par_value),0) FROM soma_holdings "
        "WHERE as_of_date=? AND pct_outstanding >= ?",
        (cur_d, SPECIALS_CAP_PCT),
    ).fetchone()
    entrants = conn.execute(
        """
        SELECT h.cusip, h.security_type, h.maturity_date, h.par_value
        FROM soma_holdings h
        WHERE h.as_of_date=? AND h.pct_outstanding >= ?
          -- entrant = FIRST week at cap in our data (no capped row for this
          -- CUSIP in any earlier snapshot). Prior snapshots must exist at all
          -- — a fresh backfill's left edge would otherwise flag everything.
          AND EXISTS (SELECT 1 FROM soma_holdings w WHERE w.as_of_date < h.as_of_date)
          AND NOT EXISTS (
            SELECT 1 FROM soma_holdings w
            WHERE w.cusip=h.cusip AND w.as_of_date < h.as_of_date
              AND w.pct_outstanding >= ?)
        ORDER BY h.par_value DESC
        """,
        (cur_d, SPECIALS_CAP_PCT, SPECIALS_CAP_PCT),
    ).fetchall()
    return {
        "as_of_date": cur_d,
        "cap_pct": SPECIALS_CAP_PCT,
        "n_at_cap": n,
        "par_at_cap_b": None if par is None else par * PAR_TO_B,
        "new_entrants": [
            {
                "cusip": c,
                "security_type": s,
                "maturity_date": m,
                "par_b": p * PAR_TO_B if p is not None else None,
            }
            for c, s, m, p in entrants
        ],
    }


def soma_specials_new_alert(
    conn: sqlite3.Connection, min_par_b: float | None = None
) -> dict | None:
    """NEW cap-hitters with par ≥ min_par_b ($B) — repo-special candidates.

    Calibrated on the 108-week backfill: real entrant events are rare (one
    cluster in 104 live weeks — 2025-09-17, 2 issues / $57B) — so this alert
    is meaningful, unlike a plain count (66–93 issues sit >30% every week).
    """
    floor = SOMA_SPECIALS_NEW_PAR_B if min_par_b is None else min_par_b
    sc = soma_float_scarcity(conn)
    if not sc or not sc.get("new_entrants"):
        return None
    if not _snapshot_fresh(sc["as_of_date"]):
        return None
    big = [e for e in sc["new_entrants"] if (e["par_b"] or 0) >= floor]
    if not big:
        return None
    return {
        "as_of_date": sc["as_of_date"],
        "n": len(big),
        "par_b": sum(e["par_b"] for e in big),
        "entrants": big,
    }


def store_soma_signals(conn: sqlite3.Connection) -> int:
    """Compute all SOMA signals + persist to computed_signals (audit trail).

    Rows (ts = SOMA as_of_date → natural weekly dedup via INSERT OR REPLACE,
    same convention as store_cot_signals):
    - soma_buckets       value = total par ($B)      state = steepest bucket
    - soma_tips_split    value = TIPS par ($B)       state = RISING/FALLING/FLAT
    - soma_net_liquidity value = net change ($B/wk)  state = DRAINING/INJECTING
    - soma_specials      value = issues at 70% cap   state = OK/NEW_ENTRANT
    - soma_walcl_gap     value = ΔWALCL − ΔSOMA ($B) state = OK/DIVERGENT

    Returns 0 when there is no SOMA data — storage is a no-op, not an error.
    """
    profile = soma_maturity_profile(conn)
    liq = soma_net_liquidity(conn)
    scarcity = soma_float_scarcity(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[tuple] = []

    if profile:
        ts = profile["as_of_date"]
        total_b = sum(b["par"] for b in profile["buckets"])
        rows.append(
            (
                "soma_buckets",
                ts,
                now,
                now,
                round(total_b, 3),
                profile["steepest_bucket"] or "N/A",
                json.dumps(
                    {
                        "unit": "$B",
                        "buckets": [
                            {
                                "name": b["name"],
                                "par_b": round(b["par"], 3),
                                "change_week_b": (
                                    None if b["change_week"] is None else round(b["change_week"], 3)
                                ),
                                "n_cusips": b["n_cusips"],
                            }
                            for b in profile["buckets"]
                        ],
                    }
                ),
            )
        )
        tips = profile.get("tips") or {}
        if tips.get("par") is not None:
            chg = tips.get("change")
            state = (
                "N/A"
                if chg is None
                else ("RISING" if chg > 0 else "FALLING" if chg < 0 else "FLAT")
            )
            rows.append(
                (
                    "soma_tips_split",
                    ts,
                    now,
                    now,
                    round(tips["par"], 3),
                    state,
                    json.dumps(
                        {
                            "unit": "$B",
                            "change_week_b": None if chg is None else round(chg, 3),
                        }
                    ),
                )
            )
    if liq and liq.get("net_change_b") is not None:
        rows.append(
            (
                "soma_net_liquidity",
                liq["as_of_date"],
                now,
                now,
                round(liq["net_change_b"], 3),
                liq["state"],
                json.dumps(
                    {
                        "unit": "$B",
                        "soma_change_b": _r(liq.get("soma_change_b")),
                        "gross_change_b": _r(liq.get("gross_change_b")),
                        "matured_b": _r(liq.get("matured_b")),
                        "mbs_other_b": _r(liq.get("mbs_other_b")),
                        "mbs_b": _r(liq.get("mbs_b")),
                        "other_b": _r(liq.get("other_b")),
                        "rrp_contribution_b": _r(liq.get("rrp_contribution_b")),
                        "tga_contribution_b": _r(liq.get("tga_contribution_b")),
                        "convention": "Δnet = ΔWALCL − ΔRRP − ΔTGA (FR-34)",
                    }
                ),
            )
        )
    if scarcity and scarcity.get("n_at_cap"):
        rows.append(
            (
                "soma_specials",
                scarcity["as_of_date"],
                now,
                now,
                scarcity["n_at_cap"],
                "NEW_ENTRANT" if scarcity["new_entrants"] else "OK",
                json.dumps(
                    {
                        "unit": "count",
                        "cap_pct": scarcity["cap_pct"],
                        "par_at_cap_b": _r(scarcity.get("par_at_cap_b")),
                        "new_entrants": scarcity["new_entrants"],
                    }
                ),
            )
        )
    if liq and liq.get("mbs_other_b") is not None:
        gap = liq["mbs_other_b"]
        rows.append(
            (
                "soma_walcl_gap",
                liq["as_of_date"],
                now,
                now,
                round(gap, 3),
                "DIVERGENT" if abs(gap) > SOMA_WALCL_GAP_ALERT_B else "OK",
                json.dumps(
                    {
                        "unit": "$B",
                        "dwalcl_b": _r(liq.get("dwalcl_b")),
                        "dsoma_b": _r(liq.get("soma_change_b")),
                        "alert_b": SOMA_WALCL_GAP_ALERT_B,
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


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def _signed_b(v: float, dp: int = 0) -> str:
    """Signed $B: −18.4 → '-$18B' (matches the 'Fed BS $6.73T' display style).
    dp=1 is for the small 'other' residual (−0.2 → '-$0.2B') which would
    round to '-$0B' noise at the default resolution. Sub-half-display-
    resolution values snap to a POSITIVE display zero (float dust like
    −1e-16 must not choose the sign of '$0.0B')."""
    if abs(v) < 10**-dp / 2:
        v = 0.0
    return f"{'+' if v >= 0 else '-'}${abs(v):.{dp}f}B"


def soma_brief_line(conn: sqlite3.Connection) -> str | None:
    """Render the SOMA block for the Liquidity section (all $B).

    Fed SOMA: ΔSOMA +$4B/wk (buy +$42B · matured $38B · via 3 tsy-P ops) | roll 7d $61B, 30d $192B (as of 08-26)
      Kurva: 1-3y -$8B/wk (steepest) | TIPS -$2B/wk
      Net Liq: -$38B/wk (SOMA +$4B, MBS -$19B, other +$0.0B, RRP +$0B, TGA -$23B)
      Float: 20 issues Fed-capped ($506B par) · NEW 2 ($57B)

    (pre-v9 DB: no ops suffix, and the MBS/other legs render combined as
    'MBS/other -$19B' — the split needs soma_agency_summary on both anchors)

    ΔSOMA is the exact portfolio change (Δtotal_par); the buy/matured detail
    separates it into gross purchases and maturing par (weekly_change alone
    overstates change — matured par leaves the snapshot silently). Net Liq
    components are liquidity CONTRIBUTIONS (RRP/TGA = −Δ; a falling balance =
    cash returning = positive) and sum to the headline. Returns None when
    there is no soma_summary data (pre-v7 DB or failed Thursday harvest —
    degradable).
    """
    if not _soma_tables_ready(conn):
        return None
    summary = conn.execute(
        "SELECT as_of_date, rolling_off_7d, rolling_off_30d "
        "FROM soma_summary ORDER BY as_of_date DESC LIMIT 1"
    ).fetchone()
    if not summary:
        return None
    as_of, r7, r30 = summary
    liq = soma_net_liquidity(conn)

    # Rate label: a missed harvest widens the snapshot gap — a 2-week Δ must
    # not masquerade as a weekly rate.
    span = liq.get("span_days")
    per = "/wk" if span is None or span <= 9 else f"/{max(1, round(span / 7))}wk"

    d = liq.get("soma_change_b")
    seg = "n/a" if d is None else f"{_signed_b(d)}{per}"
    gross, matured = liq.get("gross_change_b"), liq.get("matured_b")
    # Ops explainer (v9): the concrete auction results behind this week's
    # gross purchases — appended inside the parenthetical when the buy/matured
    # detail renders, as its own parenthetical otherwise. Silent pre-v9.
    ops = soma_ops_explainer(conn, liq.get("prev_as_of_date"), as_of, gross)
    ops_txt = None
    if ops:
        unit = "op" if ops["n_ops"] == 1 else "ops"
        ops_txt = f"via {ops['n_ops']} {ops['family']}-{ops['direction']} {unit}"
    # The parenthetical is a ONE-interval decomposition (buy = the gross
    # between these two anchors) — it stays exact on holiday-week 6/8-day
    # spans; only a MISSED-harvest span (gap >9d, same rule as `per`) mixes
    # horizons and is suppressed.
    if (
        d is not None
        and gross is not None
        and matured is not None
        and (span is None or span <= 9)
        and abs(gross) >= GROSS_DETAIL_FLOOR_B
        and matured >= GROSS_DETAIL_FLOOR_B
    ):
        inner = f"buy {_signed_b(gross)} · matured ${matured:.0f}B"
        if ops_txt:
            inner += f" · {ops_txt}"
        seg += f" ({inner})"
    elif ops_txt:
        seg += f" ({ops_txt})"
    parts = [f"ΔSOMA {seg}"]
    if r7 is not None:
        roll = f"roll 7d ${r7 * PAR_TO_B:.0f}B"
        if r30 is not None:
            roll += f", 30d ${r30 * PAR_TO_B:.0f}B"
        parts.append(roll)
    header = f"Fed SOMA: {' | '.join(parts)} (as of {as_of[5:]})"
    age_days = (datetime.now(UTC).date() - date.fromisoformat(as_of)).days
    # Normal weekly cadence ages the snapshot to 8d by Thursday (Wednesday
    # data → Friday release → next Friday's harvest); 9d+ = a harvest went
    # missing. (A >7d threshold would false-flag every Thursday.)
    if age_days > 8:
        header += f" ⚠(stale {age_days}d)"
    lines = [header]

    profile = soma_maturity_profile(conn)
    seg_parts = []
    if profile.get("steepest_bucket"):
        steepest = next(b for b in profile["buckets"] if b["name"] == profile["steepest_bucket"])
        if abs(steepest["change_week"]) >= SOMA_DISPLAY_FLOOR_B:
            seg_parts.append(
                f"{steepest['name']} {_signed_b(steepest['change_week'])}/wk (steepest)"
            )
    tips = profile.get("tips") or {}
    if tips.get("change") is not None and abs(tips["change"]) >= SOMA_DISPLAY_FLOOR_B:
        seg_parts.append(f"TIPS {_signed_b(tips['change'])}/wk")
    if seg_parts:
        lines.append("  Kurva: " + " | ".join(seg_parts))

    if liq.get("net_change_b") is not None:
        comp = []
        if liq.get("soma_change_b") is not None:
            comp.append(f"SOMA {_signed_b(liq['soma_change_b'])}")
            if liq.get("mbs_b") is not None and liq.get("other_b") is not None:
                # Net Liq v2 (v9 agency summary): exact MBS leg + small 'other'
                # residual (1dp — the leftover is usually sub-$B)
                comp.append(f"MBS {_signed_b(liq['mbs_b'])}")
                comp.append(f"other {_signed_b(liq['other_b'], 1)}")
            elif liq.get("mbs_other_b") is not None:
                comp.append(f"MBS/other {_signed_b(liq['mbs_other_b'])}")
        elif liq.get("dwalcl_b") is not None:
            # single-snapshot DB (first harvest, no prior week yet): no ΔSOMA
            # to decompose — show the anchor as one piece so the displayed
            # components still sum to the headline
            comp.append(f"FedBS {_signed_b(liq['dwalcl_b'])}")
        if liq.get("rrp_contribution_b") is not None:
            comp.append(f"RRP {_signed_b(liq['rrp_contribution_b'])}")
        if liq.get("tga_contribution_b") is not None:
            comp.append(f"TGA {_signed_b(liq['tga_contribution_b'])}")
        lines.append(f"  Net Liq: {_signed_b(liq['net_change_b'])}{per} ({', '.join(comp)})")

    scarcity = soma_float_scarcity(conn)
    if scarcity.get("n_at_cap"):
        seg = f"  Float: {scarcity['n_at_cap']} issues Fed-capped (${scarcity['par_at_cap_b']:.0f}B par)"
        if scarcity.get("new_entrants"):
            n_par = sum(e["par_b"] or 0 for e in scarcity["new_entrants"])
            seg += f" · NEW {len(scarcity['new_entrants'])} (${n_par:.0f}B)"
        lines.append(seg)

    return "\n".join(lines)
