"""backfill.py — populate the DB with full history (FRED + TGA 3-era).

Other sources are not backfilled here: EODHD has a short history window
(daily accumulation instead) and TREASURY is per-year.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

from .. import db
from ..config import load_registry
from ..fetchers import fred
from ..fetchers.fiscal import BASE as FISCAL_BASE
from ..fetchers.fiscal import ERAS

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

REQUIRED_FIELDS = (
    "series_id",
    "name",
    "block",
    "tier",
    "unit",
    "value_format",
    "freq",
    "primary_source",
)


def sync_registry(conn) -> int:
    """Sync upstream config into series_registry (FK target for raw_observations)."""
    rows = []
    for e in load_registry(active_only=False):
        rows.append(
            (
                e["series_id"],
                e["name"],
                e["block"],
                int(e.get("tier", 0)),
                e["unit"],
                e["value_format"],
                e["freq"],
                e.get("ts_convention"),
                e.get("release_schedule"),
                e.get("expected_start"),
                e.get("sanity_min"),
                e.get("sanity_max"),
                e["primary_source"],
                e.get("secondary_source"),
                e.get("tolerance"),
                int(e.get("active", 1)),
                e.get("calendar_family"),
            )
        )
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT OR REPLACE INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
        "ts_convention,release_schedule,expected_start,sanity_min,sanity_max,"
        "primary_source,secondary_source,tolerance,active,calendar_family)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute("COMMIT")
    return len(rows)


def backfill_fred(conn, entries, *, dry: bool = False) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        sid_native = (e.get("native_id") or e.get("primary_source") or e["series_id"]).split(
            ":", 1
        )[-1]
        start = str(e.get("expected_start") or "1900-01-01")[:10]
        obs = fred.fetch_observations(sid_native, start=start, sort="asc")
        rows = [
            (e["series_id"], o["ts"], o["value"], "FRED") for o in obs if o["value"] is not None
        ]
        if dry:
            out[e["series_id"]] = len(rows)
        else:
            out[e["series_id"]] = db.insert_observations(conn, rows)
            # ROUND-2: one-shot full-history loads were invisible to every
            # fetch_log-based health join (the 4 new 2026-09-17 series showed
            # ZERO fetch_log rows while holding 700-800 obs each)
            try:
                from .fetch_log import log_collection

                log_collection(
                    conn,
                    "backfill",
                    e["series_id"],
                    {"ts": rows[-1][1], "value": rows[-1][2]} if rows else None,
                    out[e["series_id"]],
                )
            except Exception:
                pass  # logging must never break the load
    return out


def backfill_cal(conn, *, dry: bool = False) -> dict[str, int]:
    """ROUND-4: the CAL: history load was an uncommitted one-off — commit the
    route: idempotent family walk + fetch_log rows (the round-2 visibility
    convention), so a missed month is always re-healable.

    ROUND-11: also loads the FOMC dot plot series (sep.py web-scrapes
    federalreserve.gov SEP pages — quarterly cadence, all vintages)."""
    from ..fetchers import caldist

    out: dict[str, int] = {}
    for suffix, fam in caldist.FAMILIES.items():
        sid = f"CAL:{suffix}"
        pts = caldist.family_rows(*fam)
        rows = [(sid, p["ts"], p["value"], "CAL") for p in pts]
        if dry:
            out[sid] = len(rows)
        else:
            out[sid] = db.insert_observations(conn, rows)
            try:
                from .fetch_log import log_collection

                log_collection(
                    conn,
                    "backfill",
                    sid,
                    {"ts": rows[-1][1], "value": rows[-1][2]} if rows else None,
                    out[sid],
                )
            except Exception:
                pass

    # FOMC dot plot (sep.py — web-scrape, quarterly cadence)
    try:
        from ..fetchers import sep

        sep.save_dot_series(conn)
    except Exception as ex:
        print(f"  ⚠ sep: {str(ex)[:90]}")

    return out


def _tga_rows(account_type: str) -> list[tuple]:
    rows, url, params = (
        [],
        FISCAL_BASE,
        {
            "filter": f"account_type:eq:{account_type}",
            "sort": "record_date",
            "page[size]": 10000,
        },
    )
    while url:
        j = requests.get(url, params=params, timeout=(10, 60)).json()
        for r in j.get("data", []):
            for k in ("close_today_bal", "open_today_bal"):
                v = r.get(k)
                if v not in (None, "", "null"):
                    rows.append(("FISCAL:TGA_DAILY", r["record_date"], float(v), "FISCAL"))
                    break
        nxt = (j.get("links") or {}).get("next")
        url, params = (nxt, None) if nxt else (None, None)
    return rows


def backfill_tga(conn, *, dry: bool = False) -> dict[str, int]:
    rows: list[tuple] = []
    for era, _s, _e in ERAS:
        rows.extend(_tga_rows(era))
    # Cross-era dedup (the PK + INSERT OR IGNORE also handles it)
    n = len(rows) if dry else db.insert_observations(conn, rows)
    return {"FISCAL:TGA_DAILY": n}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch backfill")
    p.add_argument("--block", help="filter by block (e.g. A)")
    p.add_argument("--limit", type=int)
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--dry", action="store_true", help="count without writing")
    p.add_argument(
        "--source",
        choices=["fred", "tga", "cal", "sep", "nyfedresearch", "frb", "geo"],
        default="fred",
    )
    args = p.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()

    reg = load_registry()
    if args.block:
        reg = [e for e in reg if e["block"] == args.block.upper()]
    if args.limit:
        reg = reg[: args.limit]

    if args.source == "tga":
        result = backfill_tga(None, dry=args.dry) if args.dry else _run_tga(args.db)
    elif args.source == "cal":
        conn = db.get_conn(args.db, allow_init=True)
        n_reg = sync_registry(conn)
        print(f"registry synced: {n_reg} entries")
        result = backfill_cal(conn, dry=args.dry)
        conn.close()
    elif args.source == "sep":
        # the daemon's Sunday dot-plot entry points here (commit b091fd0
        # referenced this choice before it existed — added 2026-09-19)
        from ..fetchers import sep

        conn = db.get_conn(args.db, allow_init=True)
        sync_registry(conn)
        if args.dry:
            rows = sep.series_rows()
            for suffix in sorted({r["series_suffix"] for r in rows}):
                pts = [r for r in rows if r["series_suffix"] == suffix]
                # max-by-ts, order-immune — series_rows walks vintages
                # NEWEST-FIRST, so pts[-1] is the OLDEST (round-4: the dry
                # print mislabeled every series years staler than reality)
                latest = max(pts, key=lambda r: r["ts"])
                print(f"  CAL:FOMC_DOT_{suffix:8s} {len(pts):3d} vintages · latest {latest}")
            result = {}
        else:
            n = sep.save_dot_series(conn)
            try:
                from .fetch_log import log_collection

                # idempotent re-run: 0 new rows is the HEALTHY weekly case,
                # not EMPTY (the fetch read thousands of observations)
                log_collection(conn, "backfill", "CAL:FOMC_DOT_ALL", None, n, status="OK")
            except Exception:
                pass
            result = {}
        conn.close()
    elif args.source == "frb":
        # Fed Board charge-off/delinquency (FRB: family) — quarterly SDMX,
        # re-released whole-history; the weekly re-run lands revisions
        from ..fetchers import fedsurvey

        conn = db.get_conn(args.db, allow_init=True)
        sync_registry(conn)
        if args.dry:
            print("  (dry run — full parse, no writes)")
            by_label: dict[str, list] = {}
            for lb, ts, v in fedsurvey._chgdel_rows():
                by_label.setdefault(lb, []).append((ts, v))
            for key, label in sorted(fedsurvey.FRB_CHGDEL_SERIES.items()):
                if label in by_label:
                    print(
                        f"  FRB:{key:10s} {len(by_label[label]):4d} obs · latest {max(by_label[label])}"
                    )
            result = {}
        else:
            n = fedsurvey.frb_save_history(conn, verbose=False)
            try:
                from .fetch_log import log_collection

                # 0 new rows = healthy idempotent revision-picker re-run
                log_collection(conn, "backfill", "FRB:CHGDEL_ALL", None, n, status="OK")
            except Exception:
                pass
            result = {}
        conn.close()
    elif args.source == "nyfedresearch":
        # NY Fed research expansion (2026-09-19): full-history ingest of the
        # 8 research datasets + revision pickup — HHDC/MCT/LW/GSCPI/HPW
        # rewrite whole histories, so the weekly re-run lands revisions as
        # vintage snapshots (never blocking, always idempotent)
        from ..fetchers import nyfedresearch

        conn = db.get_conn(args.db, allow_init=True)
        sync_registry(conn)
        if args.dry:
            print("  (dry run — full parse, no writes)")
            for fam in nyfedresearch.FAMILY_PARSERS:
                rows = nyfedresearch._family_rows(fam)
                for k, v in sorted(rows.items()):
                    if nyfedresearch.knows(k):
                        print(f"  NYFED:{k:26s} {len(v):4d} obs · latest {v[-1] if v else 'NONE'}")
            result = {}
        else:
            n = nyfedresearch.save_history(conn, verbose=False)
            try:
                from .fetch_log import log_collection

                # 0 new rows = healthy idempotent revision-picker re-run
                log_collection(conn, "backfill", "NYFEDRESEARCH_ALL", None, n, status="OK")
            except Exception:
                pass
            result = {
                f"NYFED:{k}": len(rows)
                for fam in nyfedresearch.FAMILY_PARSERS
                for k, rows in nyfedresearch._family_rows(fam).items()
                if nyfedresearch.knows(k)
            }
        conn.close()
    elif args.source == "geo":
        from ..fetchers import geo as geo_mod

        conn = db.get_conn(args.db, allow_init=True)
        sync_registry(conn)
        result = {}
        if args.dry:
            for key, fn in geo_mod.GEO_SERIES.items():
                rows = fn()
                print(f"  GEO:{key:12s} {len(rows):5d} obs · latest {rows[-1]}")
        else:
            for key, fn in geo_mod.GEO_SERIES.items():
                rows = fn()
                sid = f"GEO:{key}"
                result[sid] = db.insert_observations(
                    conn, [(sid, p["ts"], p["value"], "GEO") for p in rows]
                )
            conn.commit()
        conn.close()
    else:
        fred_targets = [e for e in reg if e["series_id"].startswith("FRED:")]
        if args.dry:
            result = backfill_fred(None, fred_targets, dry=True)
        else:
            conn = db.get_conn(args.db, allow_init=True)
            n_reg = sync_registry(conn)
            print(f"registry synced: {n_reg} entries")
            result = backfill_fred(conn, fred_targets)
            conn.close()

    total = sum(result.values())
    print(f"=== backfill ({args.source}) · new rows: {total} · series: {len(result)} ===")
    for sid, n in sorted(result.items()):
        print(f"  {sid:<26} {n:>7,}")
    return 0


def _run_tga(db_path: str) -> dict[str, int]:
    conn = db.get_conn(db_path, allow_init=True)
    sync_registry(conn)
    out = backfill_tga(conn)
    conn.close()
    return out


if __name__ == "__main__":
    sys.exit(main())
