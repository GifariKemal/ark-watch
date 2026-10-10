"""calendar.py (qa) — union of 4 sources + dedup + curation → events table.

Rules: TIME precedence = curated > TV > CME > FMP > EODHD; VALUE precedence
(consensus/actual) = FMP > EODHD > CME > TV. 00:00 is date-only (never treat
it as a real time). Dedup key = (normalized_name, UTC date). Curation: FOMC
(from config, exact ET time, DST-aware via zoneinfo) + standard BLS/BEA/ISM/
UMich release times for 00:00 entries matching a keyword.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import db
from ..config import load_curated_calendar
from ..fetchers import calendar as cal

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
ET = ZoneInfo("America/New_York")

TIME_PRECEDENCE = {"CURATED": 0, "TV": 1, "CME": 2, "FMP": 3, "EODHD": 4}
VALUE_PRECEDENCE = {"FMP": 0, "EODHD": 1, "CME": 2, "TV": 3}

# keyword → standard ET release time (from config release_times; hard-mapped for v0 matching)
STD_KEYWORDS = [
    (
        r"\bCPI\b|\bCONSUMER PRICE\b|\bPPI\b|\bPRODUCER PRICE\b|NONFARM|INITIAL JOBLESS|CONTINUING JOBLESS|RETAIL SALES|DURABLE GOODS|GDP\b|PERSONAL (INCOME|SPENDING)|CORE PCE|PCE PRICE",
        (8, 30),
    ),
    (r"\bISM\b|MICHIGAN|CONSUMER (SENTIMENT|CONFIDENCE)|PMI\b", (10, 0)),
]


def norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", name.upper()).strip()


# --- indicator family keys for the σ surprise engine -------------------------
# Source names carry temporal suffixes ("AUG", "AUG 28", "FINAL", "PRELIMINARY")
# plus a "US " prefix; without stripping, every release becomes a "new
# indicator" and σ never accumulates. NOT stripped: substance qualifiers
# ("1 YEAR" vs "5 YEAR", "MM" vs "YY", "PRIVATE") — those are genuinely
# different indicators.
_STRIP_TAIL = re.compile(
    r"(\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\s+\d{1,2})?(\s+\d{4})?)+$"
    r"|\s+(FINAL|PRELIMINARY|PRELIM|PREL|ADVANCE|SECOND|THIRD)(\s+ESTIMATE)?$"
    r"|\s+Q[1-4](\s+\d{4})?$"
    r"|\s+\d{4}$"
)

# Cross-source aliases for the same (post-strip) indicator; looked up AFTER
# the "US " prefix is stripped — include both forms where needed.
# 2026-09-17 anomaly audit: FMP RENAMED families (CPI→Inflation Rate,
# ISM Non-Manufacturing→ISM Services, Markit→S&P Global) strand the old
# names' rows forever (different indicator_key → the fill-if-NULL heal can
# never reach them) and double-count one release inside ESI under two keys.
_ALIASES = {
    "CONSUMER SENTIMENT": "MICHIGAN CONSUMER SENTIMENT",
    "US CONSUMER SENTIMENT": "MICHIGAN CONSUMER SENTIMENT",
    "US CHICAGO PMI": "CHICAGO PMI",
    "JOBLESS CLAIMS": "INITIAL JOBLESS CLAIMS",
    "US JOBLESS CLAIMS": "INITIAL JOBLESS CLAIMS",
    # FMP renames — canonical = the NEW names (they are the ones still
    # receiving actuals; spelling = normalized forms live-verified in DB)
    "CPI YOY": "INFLATION RATE YOY",
    "CPI MOM": "INFLATION RATE MOM",
    "CORE CPI YOY": "CORE INFLATION RATE YOY",
    "CORE CPI MOM": "CORE INFLATION RATE MOM",
    "ISM NON MANUFACTURING PMI": "ISM SERVICES PMI",
    "ISM NON MANUFACTURING PRICES": "ISM SERVICES PRICES",
    "ISM NON MANUFACTURING EMPLOYMENT": "ISM SERVICES EMPLOYMENT",
    "ISM NON MANUFACTURING NEW ORDERS": "ISM SERVICES NEW ORDERS",
    "ISM NON MANUFACTURING BUSINESS ACTIVITY": "ISM SERVICES BUSINESS ACTIVITY",
    "MARKIT SERVICES PMI": "S P GLOBAL SERVICES PMI",
    "BUDGET BALANCE": "MONTHLY BUDGET STATEMENT",
    # ROUND-2: headline-name twins never merged into their value-carrying
    # families — orphan keys (n=1, sigma=0) never receive surprise_z while
    # the same release moves markets (units verified: CME 'US RETAIL SALES'
    # 09-16 actual 1.2 == RETAIL SALES MOM; diffusion index == diffusion PMI)
    "RETAIL SALES": "RETAIL SALES MOM",
    "ISM MANUFACTURING INDEX": "ISM MANUFACTURING PMI",
    # ROUND-4: TV's stray 2026-09-10 PPI spellings (1-row orphans; the TV
    # actual 0.4 == the FMP twin's — same release under a split key)
    "PPI MOM": "PRODUCER PRICE INDEX MOM",
    "PPI YOY": "PRODUCER PRICE INDEX YOY",
}


def indicator_key(name_norm: str) -> str:
    """normalized_name → stable indicator family key across releases."""
    k = name_norm.strip()
    while True:
        k2 = _STRIP_TAIL.sub("", k).strip()
        if k2 == k:
            break
        k = k2
    if k.startswith("US "):
        k = k[3:]
    k = _ALIASES.get(k, k)
    return k or name_norm


def _std_time_et(name_norm: str):
    for pat, hm in STD_KEYWORDS:
        if re.search(pat, name_norm):
            return hm
    return None


def pull(from_d: str | None = None, to_d: str | None = None) -> tuple[list[dict], dict]:
    now = datetime.now(UTC)
    # trailing window 10d (2026-09-17 audit: the old 3d heal window could not
    # survive a daemon outage — a 63h PC outage + T+1/T+2 vendor actual lag
    # stranded 113 (key,date) pairs with no actual from any source)
    from_d = from_d or (now - timedelta(days=10)).strftime("%Y-%m-%d")
    to_d = to_d or (now + timedelta(days=14)).strftime("%Y-%m-%d")
    fetched: dict[str, list[dict]] = {}
    for src, fn in (
        ("FMP", cal.fetch_fmp),
        ("TV", cal.fetch_tv),
        ("CME", cal.fetch_cme),
        ("EODHD", cal.fetch_eodhd),
    ):
        try:
            fetched[src] = fn(from_d, to_d)
        except Exception as ex:
            from .harvest import _redact  # HTTPError text carries the URL incl. apikey=

            print(f"  ⚠ {src} failed: {_redact(str(ex))[:110]}")
            fetched[src] = []
    counts = {k: len(v) for k, v in fetched.items()}

    # Merge with precedence, tracking provenance per field: actual/consensus can
    # be won by a source other than the time winner, so a single "source"
    # column would misreport where each value came from
    merged: dict[tuple[str, str], dict] = {}
    for src in ("EODHD", "FMP", "CME", "TV"):  # low → high precedence for time
        for e in fetched.get(src, []):
            n = norm(e["name"])
            if not n:
                continue
            key = (n, e["ts_utc"][:10])
            cur = merged.get(key)
            if cur is None:
                merged[key] = {
                    **e,
                    "normalized_name": n,
                    "actual_src": e["source"],
                    "consensus_src": e["source"],
                }
                continue
            # Time: the higher-precedence source wins
            if TIME_PRECEDENCE[e["source"]] < TIME_PRECEDENCE[cur["source"]]:
                cur["ts_utc"] = e["ts_utc"]
            # Values: take the filled value from the best-precedence source
            for f, src_key in (
                ("actual", "actual_src"),
                ("consensus", "consensus_src"),
                ("previous", None),
            ):
                if (
                    cur[f] is None
                    and e[f] is not None
                    or (
                        cur[f] is not None
                        and e[f] is not None
                        and VALUE_PRECEDENCE[e["source"]] < VALUE_PRECEDENCE[cur["source"]]
                    )
                ):
                    cur[f] = e[f]
                    if src_key:
                        cur[src_key] = e["source"]
            if cur["importance"] == "unknown" and e["importance"] != "unknown":
                cur["importance"] = e["importance"]
    events = list(merged.values())

    # Curation 1: exact FOMC (day 2 of the meeting, 14:00 ET)
    kurasi = load_curated_calendar()
    fomc_dates: dict[str, dict] = {}
    f26 = kurasi.get("fomc_2026") or []
    f27 = kurasi.get("fomc_2027") or {}
    f27 = f27.get("meetings", []) if isinstance(f27, dict) else f27
    for m in list(f26) + list(f27):
        # Seed field actually used: decision_day (day 2, 14:00 ET statement) + dates
        dd = str(m.get("decision_day") or "")[:10]
        if dd:
            fomc_dates[dd] = m
    for e in events:
        d = e["ts_utc"][:10]
        if d in fomc_dates and re.search(
            r"FOMC|FEDERAL FUNDS|INTEREST RATE DECISION", e["normalized_name"]
        ):
            hh, mm = 14, 0
            dt_et = datetime.fromisoformat(d).replace(hour=hh, minute=mm, tzinfo=ET)
            e["ts_utc"] = dt_et.astimezone(UTC).isoformat(timespec="seconds")
            e["is_curated"] = 1
    # Curation 2: standard release times for date-only (00:00) entries
    for e in events:
        if e["ts_utc"][11:16] == "00:00" and not e.get("is_curated"):
            hm = _std_time_et(e["normalized_name"])
            if hm:
                dt_et = datetime.fromisoformat(e["ts_utc"][:10]).replace(
                    hour=hm[0], minute=hm[1], tzinfo=ET
                )
                e["ts_utc"] = dt_et.astimezone(UTC).isoformat(timespec="seconds")
                e["is_curated"] = 1
    return events, counts


def event_uid(normalized_name: str, ts_utc: str) -> str:
    """Canonical event uid — DATE-based: sha1(normalized_name|DATE|US)[:16].

    RONDE-4 P0 (D-027): two uid schemes lived simultaneously (this date-based
    one in calendar.save, and a FULL-TIMESTAMP one in qa/surprise.py) — 92.5%
    of stored rows carried uids the calendar upsert could never match, so
    actuals silently never filled for them. ONE canonical helper for ALL
    writers; the uid uses only the DATE because the winning TIME can change
    between pulls (dedup key is name+date)."""
    return hashlib.sha1(f"{normalized_name}|{ts_utc[:10]}|US".encode()).hexdigest()[:16]


# Sub-component event families whose vendor feed is DEAD: no consensus AND no
# actual since 2023-08 (live-verified 2026-09-17: today's rows arrive fully
# empty — cons=None actual=None). The HEADLINE ("PHILADELPHIA FED
# MANUFACTURING INDEX") is healthy and stays; the sub-components live as real
# FRED series instead (GACDFSA/PPCDFSA…066MSFRBPHI, block D, since 1968).
DEAD_SUBCOMPONENT_PREFIXES = (
    "PHILLY FED BUSINESS CONDITIONS",
    "PHILLY FED CAPEX INDEX",
    "PHILLY FED EMPLOYMENT",
    "PHILLY FED NEW ORDERS",
    "PHILLY FED PRICES PAID",
)

# CME calendar stub families that NEVER carry consensus or actuals
# (live-verified 2026-09-17 against every CME-sourced row in events:
# act=0 AND cons=0 for each family below, while their data-carrying twins
# live under different names — e.g. EIA crude actuals arrive via FMP's
# 'EIA CRUDE OIL STOCKS CHANGE'). Blocking kills the radar phantoms
# ('US EIA PETROLEUM STATUS REPORT' weekly high-importance rows promising
# outcomes that never land) and the never-healable empty-row churn.
# Families with INTERMITTENT actuals (US BAKER HUGHES RIG COUNT, US JOBLESS
# CLAIMS, US MBA MORTGAGE APPLICATIONS, US CONSUMER SENTIMENT, …) are
# deliberately absent — the valueless-importance demote below handles them.
DEAD_CME_PREFIXES = (
    "US MARKET REFLECTIONS",
    "US TREASURY BUYBACK ANNOUNCEMENT",
    "US TREASURY STATEMENT",
    "US API WEEKLY OIL STOCKS",
    "US CROP PROGRESS",
    "US CROP PRODUCTION",
    "US EIA NATURAL GAS REPORT",
    "US EIA PETROLEUM STATUS REPORT",
    "US EXPORT INSPECTIONS",
    "US EXPORT SALES",
    "US FED BALANCE SHEET",
    "US EMPLOYMENT SITUATION",
    "US CONSUMER CREDIT",
    "US COTTON SYSTEM",
    "US BANK RESERVE SETTLEMENT",
    "US FATS AMP OILS",
    "US GRAIN CRUSHINGS",
    "US USDA SUPPLY DEMAND",
    "US USDA",
    "US JACKSON HOLE SYMPOSIUM",
    # treasury auctions/announcements (bill/note/bond, all tenors)
    "US 2 WEEK BILL",
    "US 3 MONTH BILL",
    "US 4 MONTH BILL",
    "US 4 WEEK BILL",
    "US 6 MONTH BILL",
    "US 6 WEEK BILL",
    "US 8 WEEK BILL",
    "US 8 WEEK",
    "US 13 WEEK",
    "US 17 WEEK",
    "US 26 WEEK",
    "US 52 WEEK",
    "US 2 YEAR NOTE",
    "US 3 YEAR NOTE",
    "US 5 YEAR NOTE",
    "US 7 YEAR NOTE",
    "US 10 YEAR NOTE",
    "US 20 YEAR BOND",
    "US 30 YEAR BOND",
)
DEAD_CME_SUFFIXES = (" SPEAKS", " SPEECH")


def _dead_stub(normalized_name: str) -> bool:
    """Never-carry-data event rows (dead vendor families)."""
    return normalized_name.startswith(DEAD_CME_PREFIXES) or normalized_name.endswith(
        DEAD_CME_SUFFIXES
    )


def save(db_path: str, events: list[dict]) -> int:
    conn = db.get_conn(db_path, allow_init=True)
    rows = []
    for e in events:
        if any(e["normalized_name"].startswith(p) for p in DEAD_SUBCOMPONENT_PREFIXES):
            continue  # dead family — never carries numbers, pure ingest noise
        if _dead_stub(e["normalized_name"]):
            continue  # CME stub — schedule marker only; its data twin (if
            # any) arrives under a different name from a value-carrying source
        # hour-plausibility gate (2026-09-17 audit: 195 historical US rows at
        # 05:30Z/07:00Z = 01:30/03:00 ET — no US institution publishes then;
        # FMP tz-slips). 00:00 is date-only by convention and bypasses. A
        # failed row degrades to date-only + importance low instead of being
        # dropped (the date may still be right; the time is what's fake).
        _h = e["ts_utc"][11:13]
        if _h and _h != "00" and int(_h) < 8:
            e = {**e, "ts_utc": f"{e['ts_utc'][:10]}T00:00:00"}
            if e["importance"] in ("high", "medium"):
                e = {**e, "importance": "low"}
        # valueless-demote (2026-09-17 audit): a US-prefixed row with NEITHER
        # consensus NOR actual is a CME/TV schedule stub — its data twin (if
        # any) lives under another name from a value-carrying source. While
        # empty it must not sit on the radar as high/medium promising an
        # outcome this row can never deliver (live: 'US EIA PETROLEUM STATUS
        # REPORT' weekly phantoms). Families that DO carry values keep their
        # importance on the very save that delivers the numbers — the demote
        # only applies while the row itself is empty.
        if (
            e["consensus"] is None
            and e["actual"] is None
            and e["normalized_name"].startswith("US ")
            and e["importance"] in ("high", "medium")
        ):
            e = {**e, "importance": "low"}

        uid = event_uid(e["normalized_name"], e["ts_utc"])
        rows.append(
            (
                uid,
                e["ts_utc"],
                e["ts_utc"],
                "US",
                e["name"],
                e["normalized_name"],
                e["importance"],
                e["consensus"],
                e.get("consensus_src", e["source"]),
                e["actual"],
                e.get("actual_src", e["source"]),
                e["previous"],
                None,
                int(e.get("is_curated", 0)),
                indicator_key(e["normalized_name"]),
            )
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        # The consensus may be updated pre-release, while the actual is
        # append-only after release (fill-if-NULL). A plain INSERT OR IGNORE
        # would freeze pre-release rows — the first insert carries no actual,
        # and later fetches (post-release, with actuals) would be ignored,
        # starving surprise_z/ESI of fresh data. Two explicit statements,
        # because SQLite upsert cannot express a doubly-conditional DO UPDATE.
        cur = conn.executemany(
            "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
            "importance,consensus,consensus_source,actual,actual_source,previous,surprise_z,is_curated,"
            "indicator_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(event_uid) DO UPDATE SET "
            "  actual=excluded.actual, actual_source=excluded.actual_source, "
            "  ts_utc=excluded.ts_utc, release_ts=excluded.release_ts, "
            "  importance=CASE WHEN excluded.importance<>'low' THEN excluded.importance"
            "                   ELSE events.importance END "
            " WHERE events.actual IS NULL AND excluded.actual IS NOT NULL",
            rows,
        )
        conn.executemany(
            "UPDATE events SET consensus=?, consensus_source=?, "
            "  importance=CASE WHEN ?<>'low' AND ? IS NOT NULL THEN ? ELSE importance END "
            "WHERE event_uid=? AND actual IS NULL "
            "AND (? IS NOT NULL AND events.consensus IS NOT ?)",
            # plain qmarks: numbered ?N with a sequence is deprecated (error in Python 3.14)
            [(r[7], r[8], r[6], r[6], r[6], r[0], r[7], r[7]) for r in rows],
        )
        # Sibling heal (2026-09-17 audit): a TV twin of a release whose FMP
        # row carries the actual stays actual-NULL forever when the twin's
        # own source never refills it (live: the 09-04 NFP TV rows stranded
        # while the FMP twin had actual=162). When an incoming row carries
        # an actual, fill same indicator_key + same-date siblings that are
        # still NULL — idempotent, one statement.
        # ROUND-2 scale guard: one key family can hold genuinely different
        # units (a CME %MoM twin vs the FMP level family) — a heal across a
        # >10x scale gap poisons sigma/ESI (live: one EXISTING HOME row).
        # Only heal within a plausible 0.1x..10x band of the target's own
        # consensus (a consensus-less target falls back to the family gate).
        conn.executemany(
            "UPDATE events SET actual=?, actual_source=? "
            "WHERE indicator_key=? AND substr(ts_utc,1,10)=substr(?,1,10) "
            "AND actual IS NULL AND event_uid<>? "
            "AND (consensus IS NULL OR (ABS(?) >= 0.1*ABS(consensus)"
            "     AND ABS(?) <= 10*ABS(consensus)))",
            [(r[9], r[10], r[14], r[1], r[0], r[9], r[9]) for r in rows if r[9] is not None],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
    return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch calendar")
    p.add_argument("--from", dest="from_d")
    p.add_argument("--to", dest="to_d")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    events, counts = pull(a.from_d, a.to_d)
    n_new = save(a.db, events)
    # ROUND-7: per-source fetch_log rows — a dead TV/CME-calendar endpoint
    # was invisible (the job always exits 0, failures only printed). A dead
    # source now lands as EMPTY in the same health surface everything else
    # uses. (FMP carries the values, so EMPTY ≠ data loss — visibility only.)
    try:
        import sqlite3 as _sq

        from .fetch_log import log_collection as _lc_cal

        _conn = _sq.connect(a.db, isolation_level=None)
        for src, n in counts.items():
            _lc_cal(_conn, "calendar", f"CAL:{src}", None, n)
        _conn.close()
    except Exception:
        pass  # observability must never fail the calendar job
    hi = sum(1 for e in events if e["importance"] == "high")
    print(
        f"=== calendar union-4: fetch={counts} · unique={len(events)} (high={hi}) · new rows={n_new} ==="
    )
    for e in sorted([x for x in events if x["importance"] == "high"], key=lambda x: x["ts_utc"])[
        :10
    ]:
        mark = " ★curated" if e.get("is_curated") else ""
        cons = f" cons={e['consensus']}" if e["consensus"] is not None else ""
        print(f"  {e['ts_utc']}  {e['name'][:44]:<44}{cons}{mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
