"""harvest.py — daily job: increment every routed series in the registry.

Runs 06:00 WIB: FRED fetches the last 10 observations (catches light
revisions + weekends), non-FRED fetches the latest value. All writes go
to fetch_log (status, rows, duration). Idempotent (PK + INSERT OR IGNORE).
One failing series does not fail the job — it is logged and the loop continues.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db
from ..config import PROVIDER_ENV, PlanLimited, load_registry, missing_env
from ..fetchers.caldist import NoDataYet
from ..qa.verify_sources import ROUTES

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

# Any FRED series can be revised in place (GDPNOW ±8×/quarter; ICSA/CPI/PAYEMS
# revised monthly; WALCL seasonally adjusted): a dedup-only skip would freeze
# the first print forever → realtime holds the source's LATEST value (FRED
# semantics), and the previous value is preserved as a vintage row whenever it
# changes. Implemented in the harvest loop below.


_SECRET_RE = None


def _redact(text: str) -> str:
    """ROUND-4 (security): fetch exceptions echo the failing URL — keys ride
    query params (api_key=…&api_token=…). One 4xx away from a key persisting
    into fetch_log; scrub centrally at both writers."""
    global _SECRET_RE
    if _SECRET_RE is None:
        import re as _re

        _SECRET_RE = _re.compile(
            r"((?:api_key|api_token|apikey|token|key)=)[^&\s]+"
            r"|(/bot)\d+:[\w-]+"  # Telegram bot token in an echoed URL path
            r"|(/api/webhooks/)[^\s'\"]+"  # Discord webhook id/token
            r"|(bearer\s+)[^\s'\"]+"  # Authorization: Bearer <token> echo
            r"|((?:set-)?cookie[\"']?\s*[:=]\s*)[^\n]+",  # Cookie header/env echo
            _re.IGNORECASE,
        )
    return _SECRET_RE.sub(lambda m: next(g for g in m.groups() if g) + "REDACTED", text)


def _window_or_latest(
    mod, sid_full: str, prefix: str
) -> tuple[list[tuple], dict | None, str | None]:
    """GAP-HEAL (audit P1-1, 2026-09-13): fetchers exposing fetch_window land
    EVERY observation in the window — a shutdown night that missed a
    publication day heals on the next run (the latest-only contract froze
    those holes permanently; e.g. the 2026-09-04 one-day gap, 26 series).
    Unsupported series fall back to fetch_latest.

    SOURCE PINNING (audit round-2, 2026-09-20): a module may declare
    SOURCE='CAL' to keep ONE storage label when its ROUTES prefix differs
    from its historical source — prefix.rstrip(':') on the longer
    CAL:FOMC_DOT route stamped source='CAL:FOMC_DOT' while sep's backfill
    writes 'CAL', silently storing every vintage twice (source is a PK leg)."""
    fw = getattr(mod, "fetch_window", None)
    pts = None
    window_err = None
    if callable(fw):
        try:
            pts = fw(sid_full, days=10)
        except Exception as ex:
            # review ronde-2 (P2): the fallback itself is correct, but a
            # PERSISTENTLY dead window path would silently re-freeze holes
            # (the P1-1 failure mode returning invisibly) — surface it in
            # the caller's fetch_log error field
            pts = None
            window_err = f"WINDOW_FALLBACK {str(ex)[:60]}"
    src = getattr(mod, "SOURCE", None) or prefix.rstrip(":")
    if pts:
        rows = [(sid_full, p["ts"], p["value"], src) for p in pts if p.get("value") is not None]
        first_obj = rows and {"ts": rows[-1][1], "value": rows[-1][2]} or None
        return rows, first_obj, None
    cur = mod.fetch_latest(sid_full)
    rows = [(sid_full, cur["ts"], cur["value"], src)]
    return rows, cur, window_err


def harvest(db_path: str = str(DEFAULT_DB), *, block: str | None = None) -> tuple[int, int, int]:
    from dotenv import load_dotenv

    load_dotenv()
    import hashlib
    import time as _t

    from ..qa.backfill import sync_registry

    conn = db.get_conn(db_path, allow_init=True)
    sync_registry(conn)

    reg = [e for e in load_registry() if any(e["series_id"].startswith(p) for p in ROUTES)]
    if block:
        reg = [e for e in reg if e["block"] == block.upper()]

    def _schema_fp(obj: dict | None) -> str | None:
        """Response-shape fingerprint: sha1 of the first parsed object's sorted
        keys. A silent upstream schema change could make the parser miss
        fields, so the drift check below raises an alarm."""
        if not isinstance(obj, dict) or not obj:
            return None
        return hashlib.sha1("|".join(sorted(obj.keys())).encode()).hexdigest()[:16]

    ok = fail = rows_new = 0
    now = datetime.now(UTC).isoformat(timespec="seconds")
    skipped: dict[str, str] = {}  # prefix -> reason: ONE fetch_log row per run
    for e in reg:
        sid_full = e["series_id"]
        prefix = next(p for p in ROUTES if sid_full.startswith(p))
        reason = missing_env(*PROVIDER_ENV.get(prefix, ()))
        if reason:
            skipped[prefix] = reason
            continue
        mod = ROUTES[prefix]
        t0 = _t.monotonic()
        status, n, err = "OK", 0, None
        fp = None
        try:
            first_obj = None
            if prefix == "FRED:":
                ref = (e.get("native_id") or e.get("primary_source") or sid_full).split(":", 1)[-1]
                # ROUND-4: freq-aware depth — the flat 10-obs window never
                # caught monthly/annual/benchmark revisions (a monthly series
                # revises months back; weekly revises ~12 weeks)
                _depth = {"D": 10, "W": 60, "M": 36, "Q": 16, "A": 5}.get(
                    (e.get("freq") or "D").upper(), 10
                )
                obs = mod.fetch_observations(ref, sort="desc", limit=_depth)
                if obs:
                    first_obj = obs[0]
                # release_ts = the FRED realtime_start (the date this value
                # became effective) → point-in-time replay works for new data
                # (historical backfill stays 'na')
                rows = [
                    (sid_full, o["ts"], o["value"], "FRED", o.get("realtime_start"))
                    for o in obs
                    if o["value"] is not None
                ]
                # ROUND-6 FIX of the ROUND-4 fix: the plain fetch carries
                # realtime_start = FETCH DAY for every obs (FRED only stamps
                # true publication dates when a realtime window is passed) —
                # so the per-fetch 'first' INSERT stamped fetch-dates onto
                # old observations (561 factually-wrong release_ts rows on
                # prod; PK first-writer-wins made them stick). Gate by the
                # series' publication lag: only obs plausibly FIRST SEEN in
                # this fetch may stamp (a deeper catch-up window cannot
                # create new first-prints); f4 backfill-first remains the
                # authority for true historical release dates.
                _lag_days = {"D": 3, "W": 10, "M": 45, "Q": 130, "A": 400}.get(
                    (e.get("freq") or "D").upper(), 3
                )
                _cutoff = (datetime.now(UTC) - timedelta(days=_lag_days)).date().isoformat()
                conn.executemany(
                    "INSERT OR IGNORE INTO raw_observations"
                    "(series_id,ts,release_ts,value,vintage_ts,source,fetched_at)"
                    " VALUES (?,?,?,?, 'first', 'FRED', ?)",
                    [
                        (sid_full, o["ts"], "na", o["value"], now)
                        for o in obs
                        if o["value"] is not None and o["ts"] >= _cutoff
                    ],
                )
            else:
                rows, first_obj, window_err = _window_or_latest(mod, sid_full, prefix)
                if window_err:
                    err = window_err  # visible in fetch_log (review ronde-2 P2)
            fp = _schema_fp(first_obj)
            # Anti-drift alarm: compare with the last fingerprint for this series
            if fp:
                prev = conn.execute(
                    "SELECT schema_fp FROM fetch_log WHERE target=? "
                    "AND schema_fp IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (sid_full,),
                ).fetchone()
                if prev and prev[0] != fp:
                    err = f"SCHEMA_DRIFT {prev[0]}→{fp}"
                    print(f"  ⚠ {sid_full}: RESPONSE SCHEMA CHANGED ({err})")
            n = db.insert_observations(conn, rows)
            # FRED + the NY Fed research families + FRB charge-off + CAL event
            # aggregates are in-place revisable (FRED revises months back;
            # HHDC/MCT/LW/GSCPI/HPW rewrite whole histories; CHGDEL re-releases
            # quarterly; CAL sum/last families ACCRETE intra-month — the audit
            # P0 2026-09-21: without the revision path the month's first
            # PARTIAL print froze forever): realtime holds the LATEST value;
            # the revision history lives in vintage rows.
            if prefix in ("FRED:", "NYFED:", "FRB:", "CAL:"):
                n += db.apply_realtime_revisions(conn, rows)
            ok += 1
            rows_new += n
        except NoDataYet:
            status, err = "EMPTY", "no data yet"
        except PlanLimited as ex:  # HTTP 402: outside the plan, not a failure
            status, err = "SKIPPED", str(ex)
        except Exception as ex:
            status, err = "ERROR", _redact(str(ex))[:150]
            fail += 1
        # quota_used = 1 call for PAID sources (FMP/EODHD), 0 for free
        # institutional ones; approximates the daily call count
        quota = 1 if prefix in ("FMP:", "EODHD:") else 0
        # ROUND-2: a fetch that returns NOTHING is EMPTY, not OK. Note the
        # predicate is the FETCHED row count, not inserted rows — an
        # idempotent re-run legitimately inserts 0 new rows (healthy).
        if status == "OK" and not rows:
            status = "EMPTY"
        conn.execute(
            "INSERT INTO fetch_log(ts,fetcher,target,status,error,duration_ms,rows,"
            "schema_fp,quota_used) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                now,
                prefix.rstrip(":").lower(),
                sid_full,
                status,
                err,
                int((_t.monotonic() - t0) * 1000),
                n,
                fp,
                quota,
            ),
        )
        if status == "ERROR":
            print(f"  ✗ {sid_full}: {err}")
    for prefix, reason in skipped.items():
        conn.execute(
            "INSERT INTO fetch_log(ts,fetcher,target,status,error,rows,quota_used)"
            " VALUES (?,?,?,'SKIPPED',?,0,0)",
            (now, prefix.rstrip(":").lower(), prefix, reason),
        )
        print(f"  · {prefix}* skipped ({reason})")
    conn.close()
    return ok, fail, rows_new


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch harvest")
    p.add_argument("--block")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    ok, fail, rows = harvest(a.db, block=a.block)
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"=== harvest {ts}: {ok} OK · {fail} ERROR · {rows:,} new rows ===")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
