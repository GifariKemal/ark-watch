"""calibrate.py — automated quarterly calibration of golden anchors (D-010).

Audits golden anchors against age (>90d) and database drift (abs(live - expected) > tol).
Generates fresh anchor candidates from stable database observations.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ..config import load_anchors, load_registry

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"


@dataclass
class AnchorAudit:
    series_id: str
    anchor_date: str
    expected: float
    tolerance: float
    age_days: int
    db_value: float | None
    drift: float | None
    status: str  # OK, EXPIRED, DRIFTED, UNSEEN
    note: str = ""


def audit_anchors(
    conn: sqlite3.Connection,
    anchors: list[dict] | None = None,
    *,
    max_age_days: int = 90,
    today: date | None = None,
) -> list[AnchorAudit]:
    """Audit all golden anchors for staleness (>max_age_days) and data drift."""
    anc = anchors if anchors is not None else load_anchors()
    now_date = today or datetime.now(UTC).date()
    records: list[AnchorAudit] = []

    for a in anc:
        raw_sid = str(a["series_id"])
        sid = f"FRED:{raw_sid}" if ":" not in raw_sid else raw_sid
        anchor_date_str = str(a["anchor_date"])
        try:
            a_date = date.fromisoformat(anchor_date_str[:10])
            age = (now_date - a_date).days
        except ValueError:
            age = 9999

        exp = float(a["expected"])
        tol = float(a["tolerance"])

        # Fetch observation at anchor date
        row = conn.execute(
            "SELECT value FROM raw_observations "
            "WHERE (series_id=? OR series_id=?) AND ts LIKE ? AND vintage_ts='realtime' "
            "AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (sid, raw_sid, f"{anchor_date_str[:10]}%"),
        ).fetchone()

        db_val = float(row[0]) if row else None
        drift = abs(db_val - exp) if db_val is not None else None

        if db_val is None:
            status = "UNSEEN"
            note = f"no observation for {anchor_date_str} in DB"
        elif drift is not None and drift > tol:
            status = "DRIFTED"
            note = f"value shifted {exp} -> {db_val} (diff {drift:.4f} > tol {tol})"
        elif age > max_age_days:
            status = "EXPIRED"
            note = f"anchor is {age}d old (threshold {max_age_days}d)"
        else:
            status = "OK"
            note = "verified within tolerance"

        records.append(
            AnchorAudit(
                series_id=raw_sid,
                anchor_date=anchor_date_str,
                expected=exp,
                tolerance=tol,
                age_days=age,
                db_value=db_val,
                drift=drift,
                status=status,
                note=note,
            )
        )

    return records


def propose_fresh_anchor(
    conn: sqlite3.Connection,
    series_entry: dict,
    *,
    settle_days: int = 14,
    today: date | None = None,
) -> dict | None:
    """Propose a candidate fresh anchor from stable database observations (>=settle_days old)."""
    now_date = today or datetime.now(UTC).date()
    cutoff = (now_date - timedelta(days=settle_days)).isoformat()
    sid = series_entry["series_id"]

    row = conn.execute(
        "SELECT ts, value FROM raw_observations "
        "WHERE series_id=? AND ts<=? AND vintage_ts='realtime' AND value IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1",
        (sid, cutoff),
    ).fetchone()

    if not row:
        return None

    obs_ts, val = str(row[0])[:10], float(row[1])
    unit = str(series_entry.get("unit") or "").lower()
    val_fmt = str(series_entry.get("value_format") or "")

    # Default tolerance heuristics based on unit
    if "pct" in unit or "percent" in unit or val_fmt == "pct":
        tol = 0.02
    elif "diffusion" in unit or "index" in unit:
        tol = 0.1
    elif abs(val) > 1000:
        tol = 1.0
    else:
        tol = 0.05

    clean_sid = sid[5:] if sid.startswith("FRED:") else sid

    return {
        "series_id": clean_sid,
        "anchor_date": obs_ts,
        "expected": val,
        "tolerance": tol,
        "provenance": f"quarterly auto-calibrated {now_date.isoformat()}, {sid} @ {obs_ts}",
    }


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from .. import db

    p = argparse.ArgumentParser(prog="arkwatch calibrate")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--max-age", type=int, default=90, help="Max anchor age in days (default: 90)")
    p.add_argument(
        "--propose", action="store_true", help="Propose fresh anchors for expired/drifted rows"
    )
    p.add_argument("--json", action="store_true", help="Output JSON format")
    a = p.parse_args(argv)

    conn = db.get_conn(a.db, allow_init=True)
    audits = audit_anchors(conn, max_age_days=a.max_age)

    ok_n = sum(1 for r in audits if r.status == "OK")
    exp_n = sum(1 for r in audits if r.status == "EXPIRED")
    drf_n = sum(1 for r in audits if r.status == "DRIFTED")
    uns_n = sum(1 for r in audits if r.status == "UNSEEN")

    if a.json:
        out = {
            "summary": {
                "total": len(audits),
                "ok": ok_n,
                "expired": exp_n,
                "drifted": drf_n,
                "unseen": uns_n,
            },
            "audits": [asdict(r) for r in audits],
        }
        if a.propose:
            reg = {e["series_id"]: e for e in load_registry()}
            candidates = []
            for r in audits:
                if r.status in ("EXPIRED", "DRIFTED"):
                    full_sid = f"FRED:{r.series_id}" if ":" not in r.series_id else r.series_id
                    if full_sid in reg:
                        cand = propose_fresh_anchor(conn, reg[full_sid])
                        if cand:
                            candidates.append(cand)
            out["candidates"] = candidates
        print(json.dumps(out, indent=2))
        conn.close()
        return 0

    print("=== Golden Anchors Quarterly Calibration Audit ===")
    print(
        f"Total: {len(audits)} anchors | OK: {ok_n} | Expired (> {a.max_age}d): {exp_n} | Drifted: {drf_n} | Unseen: {uns_n}\n"
    )

    if exp_n > 0 or drf_n > 0:
        print(
            f"{'SERIES ID':<24} {'ANCHOR DATE':<12} {'EXPECTED':>10} {'DB VAL':>10} {'STATUS':<10} {'NOTE'}"
        )
        print("-" * 100)
        for r in audits:
            if r.status in ("EXPIRED", "DRIFTED"):
                db_s = f"{r.db_value:.4f}" if r.db_value is not None else "N/A"
                print(
                    f"{r.series_id:<24} {r.anchor_date:<12} {r.expected:>10.4f} {db_s:>10} {r.status:<10} {r.note}"
                )

    if a.propose:
        reg = {e["series_id"]: e for e in load_registry()}
        print("\n=== Proposed Candidate Anchors ===")
        for r in audits:
            if r.status in ("EXPIRED", "DRIFTED"):
                full_sid = f"FRED:{r.series_id}" if ":" not in r.series_id else r.series_id
                if full_sid in reg:
                    cand = propose_fresh_anchor(conn, reg[full_sid])
                    if cand:
                        print(f"  - series_id: {cand['series_id']}")
                        print(f'    anchor_date: "{cand["anchor_date"]}"')
                        print(f"    expected: {cand['expected']}")
                        print(f"    tolerance: {cand['tolerance']}")
                        print(f'    provenance: "{cand["provenance"]}"')

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
