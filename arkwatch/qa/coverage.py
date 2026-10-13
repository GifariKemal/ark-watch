"""coverage.py — registry completeness lint (required fields, fetch routes, anchors).

COT contracts, flows, events, and instrument prices live outside the series
registry and are not part of this check.
"""

from __future__ import annotations

import argparse
import sys

from ..config import load_anchors, load_cot_contracts, load_curated_calendar, load_registry
from ..db import default_db_path
from ..qa.verify_sources import ROUTES

REQUIRED = ("unit", "value_format", "freq", "primary_source")
MIN_ACTUALS = 24  # calendar-gap gate (see the gap section below)
# ROUND-4: the gate must use SUM(importance='high')>0 — MAX() over TEXT is a
# LEXICAL compare ('unknown' > 'high'), so any stray low/medium/unknown row
# silently suppressed the whole family; the R3 '0 gaps' was false-clean.


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch coverage")
    p.parse_args(argv)

    reg_all = load_registry(active_only=False)
    reg = [e for e in reg_all if e.get("active", 1)]
    errors: list[str] = []

    print("=== coverage: registry ↔ sources of truth ===")
    blocks: dict[str, int] = {}
    for e in reg:
        blocks[e["block"]] = blocks.get(e["block"], 0) + 1
    print("active series per block:", " · ".join(f"{b}={n}" for b, n in sorted(blocks.items())))
    inactive = [e["series_id"] for e in reg_all if not e.get("active", 1)]
    print(f"documented inactive: {len(inactive)} → {', '.join(inactive) or '-'}")

    # Required-field lint
    for e in reg:
        missing = [f for f in REQUIRED if not e.get(f)]
        if missing:
            errors.append(f"{e['series_id']}: required fields empty {missing}")

    # Fetcher routes (an active prefix without a route is acceptable when its
    # backfill is phased)
    unrouted = sorted(
        {
            e["series_id"].split(":")[0] + ":"
            for e in reg
            if not any(e["series_id"].startswith(p) for p in ROUTES)
        }
    )
    print(
        "active prefixes without a verify route v0:",
        ", ".join(unrouted) or "-",
        "(considered OK when backfill is phased)",
    )

    # Anchors: each anchor must reference an active series OR a known snapshot
    # table. (The anchors file uses IDs WITHOUT a prefix — normalize both
    # ways, as verify does.)
    KNOWN_NON_REGISTRY = ("CVOL:",)
    # Legitimate calibration anchors that are intentionally NOT registry
    # entries (used for identity/secondary checks):
    CALIBRATION_ONLY = {
        "T10Y2Y",  # Two-way slope identity
        "WTREGEN",  # Weekly TGA secondary check
    }
    anchors = load_anchors()
    reg_ids = {e["series_id"] for e in reg}
    reg_ids |= {sid.split(":", 1)[1] for sid in reg_ids if ":" in sid}  # prefix-less variant
    orphan = sorted(
        {
            a["series_id"]
            for a in anchors
            if a["series_id"] not in reg_ids
            and ("FRED:" + a["series_id"]) not in reg_ids
            and a["series_id"] not in CALIBRATION_ONLY
            and not any(a["series_id"].startswith(p) for p in KNOWN_NON_REGISTRY)
        }
    )
    if orphan:
        errors.append(f"orphan anchors (not in the active registry): {orphan}")
    print(
        f"anchors: {len(anchors)} rows · orphan: {len(orphan)} "
        f"(+{len(CALIBRATION_ONLY)} legitimate calibration: {sorted(CALIBRATION_ONLY)})"
    )

    cot = load_cot_contracts()
    print(f"cot_contracts: {len(cot)} contracts")
    cal = load_curated_calendar()
    fomc = len(cal.get("fomc_2026") or []) + len(cal.get("fomc_2027") or [])
    print(
        f"curated_calendar: {fomc} FOMC meetings (2026+2027) · "
        f"{len(cal.get('release_times') or {})} standard release times"
    )

    # Calendar-family gaps (round-3 queue, 2026-09-17): the HOUST/PERMIT
    # class — families carrying real actuals while NO series covers them —
    # was invisible until an accidental question surfaced it. Gate:
    # >=MIN_ACTUALS actuals AND a high-importance release, so ~120 minor
    # families (EIA stocks, regional surveys, twin MoM/YoY splits) do not
    # bury the real gaps on day one.

    from .. import db as _db

    db_path = str(default_db_path())
    _conn = _db.get_conn(db_path)
    gaps = _conn.execute(
        """
        SELECT e.indicator_key, COUNT(DISTINCT substr(e.ts_utc,1,10)),
               SUM(e.actual IS NOT NULL)
        FROM events e
        WHERE e.indicator_key IS NOT NULL
        GROUP BY e.indicator_key
        HAVING SUM(e.actual IS NOT NULL) >= ?
           AND COUNT(DISTINCT substr(e.ts_utc,1,10)) >= ?
           AND SUM(e.importance = 'high') > 0
           AND NOT EXISTS (SELECT 1 FROM series_registry r
                           WHERE r.calendar_family = e.indicator_key AND r.active = 1)
        ORDER BY 3 DESC
        """,
        (MIN_ACTUALS, MIN_ACTUALS),
    ).fetchall()
    _conn.close()
    print(
        f"calendar families: {len(gaps)} GAP(s) ≥{MIN_ACTUALS} actuals without a"
        " covering series (register the series or a CAL: derived series"
        " with calendar_family=<key> to close)"
    )
    for k, releases, n_actual in gaps:
        print(f"  ✗ {k:44s} releases={releases:<4} actuals={n_actual}")
    # ROUND-5: the gap list is a TRIAGE REPORT, not a lint error — the 27
    # known families include by-design members (policy events, NSA twins)
    # that will never be closed by a series, so exit-1 here would page
    # 'job failed' every Sunday forever (and mask same-night failures via
    # the shared key). Visibility stays via this printout + the daemon's
    # ✓ stdout tail; exit-1 remains reserved for genuine lint breakage.

    print(f"=== lint: {len(errors)} errors ===")
    for e in errors:
        print("  ✗", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
