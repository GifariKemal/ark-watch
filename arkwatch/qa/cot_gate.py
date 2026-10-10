"""cot_gate.py — weekly exact-match gate: our CFTC Socrata parse vs FMP's
independent parse of the same filings (vendor-api audit #2).

Block-G numbers reach the brief ('z+1.8 crowded long — do not chase') from a
homegrown parse of CFTC's 194-column disagg dataset. A silent mis-parse
(column mixup, category drift, week shift) would look authoritative with no
detector. FMP v4 commitment_of_traders_report is an independent parse of
the SAME filings — integer positions, so agreement must be EXACT.

Category bridge (FMP = legacy classification, ours = disaggregated):
  - open_interest_all  ↔ open_interest_all   (direct, exact)
  - nonrept_*_all      ↔ our 'nonrep'        (direct, exact)
  Σrept identities are INVALID across the two reports (legacy reportables =
  noncomm+comm+spread; disagg reportables = mm+prod+swap+other — different
  groupings of the same traders, verified live 2026-09-13: every Σrept
  "mismatch" was my wrong identity, while OI + nonrep matched exactly on
  every contract). OI + nonrep are the only cross-report integers — and a
  column-mixup in either WOULD break them.
Run Saturdays in f2 after the COT harvest; one 31MB dump call/week.
"""

from __future__ import annotations

import os

import requests

from ..config import PLAN_LIMIT_STATUSES, PlanLimited
from .fetch_log import log_collection

# our contract_code -> FMP v4 symbol (fallback when cftc code differs in
# kind, e.g. the synthetic '13874+' S&P handle). NOT COVERED by the FMP
# dump (verified 2026-09-13, skipped by design): 146021 Ether (FMP carries
# BT bitcoin only) and 085691 legacy copper (supplementary legacy dataset).
_CODE_TO_SYM = {
    "088691": "GC",
    "084691": "SI",
    "085692": "HG",
    "076651": "PL",
    "099741": "E6",
    "097741": "J6",
    "096742": "B6",
    "232741": "A6",
    "098662": "DX",
    "133741": "BT",
    "13874+": "ES",
    "209742": "NQ",
}

# our disagg reportable categories that must sum to FMP tot_rept
_REPORTABLE = ("mm", "prod", "swap", "other")


def fetch_fmp_cot_dump() -> list[dict]:
    """Full FMP v4 COT report (~31MB, ~2y weekly history, all contracts)."""
    key = os.environ.get("FMP_API_KEY", "")
    if not key:
        raise RuntimeError("FMP_API_KEY not set")
    r = requests.get(
        "https://financialmodelingprep.com/v4/commitment_of_traders_report",
        params={"apikey": key},
        timeout=(30, 180),
    )
    if r.status_code in PLAN_LIMIT_STATUSES:
        raise PlanLimited(f"plan-limited: FMP COT (HTTP {r.status_code})")
    if r.status_code != 200:
        raise RuntimeError(f"FMP COT: HTTP {r.status_code}")
    j = r.json()
    if not isinstance(j, list) or not j:
        raise RuntimeError("FMP COT: empty response")
    return j


def run_cot_gate(conn) -> tuple[int, int]:
    """Compare our latest cot_raw report_date vs the FMP dump. Returns
    (checked, mismatches); every mismatch prints loudly and the fetch_log
    row goes ERROR (a mismatch means one of the two parsers is wrong —
    exactly what this gate exists to catch)."""
    dump = fetch_fmp_cot_dump()
    # index the dump by (cftc_code, date) and (symbol, date)
    by_code: dict[str, dict[str, dict]] = {}
    by_sym: dict[str, dict[str, dict]] = {}
    for row in dump:
        code = str(row.get("cftc_contract_market_code", "")).strip()
        sym = str(row.get("symbol", "")).strip().upper()
        d = str(row.get("date", ""))[:10]
        if not d:
            continue
        if code:
            by_code.setdefault(code, {})[d] = row
            if code.isdigit():
                by_code.setdefault(code.lstrip("0") or "0", {})[d] = row
        if sym:
            by_sym.setdefault(sym, {})[d] = row

    latest = conn.execute(
        "SELECT contract_code, MAX(report_date) FROM cot_raw GROUP BY contract_code"
    ).fetchall()
    checked = mismatches = 0
    for code, report_date in latest:
        sym = _CODE_TO_SYM.get(code)
        # match preference: exact cftc code → stripped code → our symbol map
        # (the synthetic '13874+' S&P handle only resolves via symbol)
        rows_by_date = None
        for lookup in (code, code.rstrip("+").lstrip("0") or code):
            if lookup in by_code:
                rows_by_date = by_code[lookup]
                break
        if rows_by_date is None and sym and sym in by_sym:
            rows_by_date = by_sym[sym]
        if rows_by_date is None:
            print(f"  cot-gate: {code} ({sym}) not in FMP dump — skipped")
            continue
        fmp = rows_by_date.get(report_date)
        if fmp is None:
            fmp_date = max(rows_by_date)
            print(f"  ⚠ cot-gate {code} ({sym}): FMP last {fmp_date} ≠ our {report_date}")
            continue

        ours = {
            cat: (lng, sht)
            for cat, lng, sht in conn.execute(
                "SELECT category, long, short FROM cot_raw WHERE contract_code=?"
                " AND report_date=? AND report_type='disagg'",
                (code, report_date),
            ).fetchall()
        }
        oi_ours = conn.execute(
            "SELECT open_interest_all FROM cot_raw WHERE contract_code=?"
            " AND report_date=? AND report_type='disagg' LIMIT 1",
            (code, report_date),
        ).fetchone()
        if not ours and (oi_ours is None or oi_ours[0] is None):
            # ROUND-2 fix: TFF-only contracts (the 9 financials) have no
            # disagg rows — the gate silently skipped them. OI comparison via
            # futures-only TFF rows; nonrep stays disagg-only (legacy-vs-TFF
            # reportable groupings differ, Σrept identities are invalid).
            if code.endswith("+"):
                # ROUND-10: the '+' suffix marks a SYNTHETIC consolidated
                # handle (13874+ = S&P500 E-mini + full-size combined). FMP
                # symbols map to single contracts (ES = E-mini only), so the
                # OI fallback compares apples to oranges (live: consolidated
                # 2,483,362 vs FMP ES 2,446,519 — the 36,843 gap IS the big
                # contract's OI, not a parse error). Skip the fallback only;
                # the disagg path above (when rows exist) is unaffected.
                print(
                    f"  cot-gate: {code} consolidated handle — TFF OI fallback skipped (FMP maps single contracts)"
                )
                continue
            oi_ours = conn.execute(
                "SELECT open_interest_all FROM cot_raw WHERE contract_code=?"
                " AND report_date=? AND report_type='tff' LIMIT 1",
                (code, report_date),
            ).fetchone()

        def _chk(label: str, ours_v, fmp_v, code=code):
            nonlocal mismatches
            if ours_v is None or fmp_v is None:
                return
            if abs(int(ours_v) - int(fmp_v)) > 0:
                print(f"  ✗ cot-gate {code} {label}: ours {ours_v} vs FMP {fmp_v}")
                mismatches += 1

        if oi_ours and oi_ours[0] is not None:
            checked += 1
            _chk("OI", oi_ours[0], fmp.get("open_interest_all"))
        if "nonrep" in ours:
            checked += 2
            _chk("nonrep L", ours["nonrep"][0], fmp.get("nonrept_positions_long_all"))
            _chk("nonrep S", ours["nonrep"][1], fmp.get("nonrept_positions_short_all"))

    err = None
    if mismatches:
        err = f"{mismatches} field mismatches across {len(latest)} contracts"
    log_collection(conn, "f2", "FMP:COT-GATE", None, checked, err=err)
    print(
        f"  cot-gate: {checked} checks, {mismatches} mismatches @{latest[0][1] if latest else '?'}"
    )
    return checked, mismatches
