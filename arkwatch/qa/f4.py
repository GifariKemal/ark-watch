"""f4.py — point-in-time replay + inflation episodes + threshold calibration.

backfill_first_prints: pull ALL FRED vintages (realtime_start=1990→now),
  keep the FIRST-PRINT value per release date → stored as rows with
  vintage_ts='first' (unique PK per series+ts). This is the data "known at the
  time" — an honest replay without look-ahead.
replay_regime: the monthly regime score is computed ONLY from first-print
  data already AVAILABLE at that point (release_ts <= month end) — as-of join.
Overlay NBER recession labels on the replay output.
Inflation episodes (min_months + peak_exit_ratio engine).
Calibrate the 2.5/3.0/3.5 thresholds from the per-state day distribution.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .. import db
from ..fetchers import fred

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
VINTAGE_TAG = "first"  # marker for first-print rows in raw_observations


# ---------------------------------------------------------------------------
# backfill first-prints
# ---------------------------------------------------------------------------


def fetch_first_prints(native: str, obs_start: str = "1990-01-01") -> list[dict]:
    """All FRED vintages → first-print value per date: [{ts, value, release}].

    FRED limits realtime requests two ways: (1) ≤1000 rows per page; (2) the
    realtime window may not span more than ~1000 vintage dates (a 36-year
    daily series has ~5,100 vintage dates → HTTP 400). Solution: slice the
    realtime window per year (≤260 vintage dates for dailies); the first row
    per ts (smallest realtime_start) is the first print.
    """
    from datetime import date

    today = datetime.now(UTC).date()
    y0 = int(obs_start[:4])
    rows: list[dict] = []
    for y in range(y0, today.year + 1):
        rs = max(f"{y}-01-01", obs_start)
        # Extend realtime_end to Jan 31 of the next year: December observations
        # are released in mid-January, so a window ending Jan 1 would drop them
        # entirely. The min-release aggregation below dedups observations seen
        # by both windows.
        re_ = f"{y + 1}-01-31" if y < today.year else today.isoformat()
        cursor = rs
        while cursor <= re_:
            try:
                batch = fred.fetch_observations(
                    native,
                    start=cursor,
                    realtime_start=rs,
                    realtime_end=re_,
                    sort="asc",
                    limit=1000,
                )
            except fred.FredError as ex:
                # A window before the series existed → 400 "does not exist in
                # ALFRED..."; skip that window (other years still process)
                if "does not" in str(ex):
                    break
                raise
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 1000:
                break
            new_ts = batch[-1]["ts"]
            if new_ts <= cursor:  # safety: a single date with >1000 vintages
                from datetime import timedelta

                cursor = (date.fromisoformat(new_ts) + timedelta(days=1)).isoformat()
            else:
                cursor = new_ts
    return fetch_first_prints_agg(rows)


def fetch_first_prints_agg(rows: list[dict]) -> list[dict]:
    """Aggregate across windows: the smallest realtime_start per ts is the first print."""
    first: dict[str, dict] = {}
    for o in rows:
        if o["value"] is None:
            continue
        cur = first.get(o["ts"])
        if cur is None or (o["realtime_start"] or "") < cur["release"]:
            first[o["ts"]] = {
                "ts": o["ts"],
                "value": o["value"],
                "release": o["realtime_start"] or o["ts"],
            }
    return sorted(first.values(), key=lambda r: r["ts"])


def save_first_prints(conn, series_id: str, prints: list[dict]) -> int:
    """Upsert first-prints — ALFRED truth repairs fetch-day stamps.

    ROUND-6: harvest's per-fetch 'first' INSERT (round-4) stamped
    release_ts = FETCH DAY (FRED only returns true realtime dates through a
    realtime-window query), and INSERT OR IGNORE made those wrong stamps
    permanent. These rows come from fetch_first_prints (ALFRED realtime
    queries — the TRUE publication dates), so they may overwrite any stored
    first-row whose release_ts equals its own fetched_at date (the stamp
    signature); a genuine backfill row always carries release < fetch-day
    and is never touched."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    payload = [
        (series_id, p["ts"], p["release"], p["value"], VINTAGE_TAG, "FRED", None, now)
        for p in prints
    ]
    if not payload:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.executemany(
        "INSERT INTO raw_observations"
        "(series_id,ts,release_ts,value,vintage_ts,source,precision_k,fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(series_id,ts,source,vintage_ts) DO UPDATE SET"
        "  release_ts=excluded.release_ts, value=excluded.value,"
        "  fetched_at=excluded.fetched_at"
        " WHERE raw_observations.release_ts = substr(raw_observations.fetched_at,1,10)"
        "   OR raw_observations.release_ts = 'na'",
        payload,
    )
    conn.execute("COMMIT")
    return cur.rowcount


def backfill_first_prints(conn, only: list[str] | None = None) -> dict[str, int]:
    """Backfill every FRED registry series → {native: n_new_rows}."""
    from ..config import load_registry

    reg = [e for e in load_registry(active_only=False) if e["series_id"].startswith("FRED:")]
    if only:
        reg = [e for e in reg if e["series_id"].split(":", 1)[1] in only]
    out: dict[str, int] = {}
    for e in reg:
        sid = e["series_id"]
        native = (e.get("native_id") or sid).split(":", 1)[-1]
        try:
            prints = fetch_first_prints(native)
            n = save_first_prints(conn, sid, prints)
            out[native] = n
            print(f"  {native:<14} first-prints={len(prints):5} new={n:5}")
        except Exception as ex:
            out[native] = -1
            print(f"  ✗ {native}: {str(ex)[:90]}")
    return out


# ---------------------------------------------------------------------------
# as-of values (point-in-time queries)
# ---------------------------------------------------------------------------


def asof_values(conn, series_id: str, as_of: str) -> list[float]:
    """First-print values AVAILABLE at `as_of` (YYYY-MM-DD).

    As-of join: for each ts, take the first-print row with release_ts <= as_of.
    """
    rows = conn.execute(
        "SELECT ts, value FROM raw_observations r "
        "WHERE series_id=? AND vintage_ts=? AND release_ts<=? "
        "AND release_ts=(SELECT MIN(release_ts) FROM raw_observations r2 "
        "               WHERE r2.series_id=r.series_id AND r2.ts=r.ts "
        "               AND r2.vintage_ts=?) "
        "ORDER BY ts",
        (series_id, VINTAGE_TAG, as_of, VINTAGE_TAG),
    ).fetchall()
    return [r[1] for r in rows if r[1] is not None]


class AsOfReader:
    """Point-in-time reader: only first-print data already available at as_of.

    Used by pillars.compute_pillars(reader=...) — the pillar logic is
    identical, only the data source changes (no duplication)."""

    def __init__(self, conn, as_of: str):
        self._conn = conn
        self._as_of = as_of
        self._cache: dict[str, list[float]] = {}

    def values(self, sid: str, limit: int = 1600) -> list[float]:
        if sid not in self._cache:
            self._cache[sid] = asof_values(self._conn, sid, self._as_of)
        return self._cache[sid][-limit:]

    def pairs(self, sid: str, limit: int = 1600) -> list[tuple[str, float]]:
        """[(ts, value)] first-print as-of — the episode path, which validates
        month gaps (a plain value series would hide them)."""
        rows = self._conn.execute(
            "SELECT ts, value FROM raw_observations r "
            "WHERE series_id=? AND vintage_ts=? AND release_ts<=? "
            "AND release_ts=(SELECT MIN(release_ts) FROM raw_observations r2 "
            "               WHERE r2.series_id=r.series_id AND r2.ts=r.ts "
            "               AND r2.vintage_ts=?) "
            "ORDER BY ts",
            (sid, VINTAGE_TAG, self._as_of, VINTAGE_TAG),
        ).fetchall()
        return [(r[0], r[1]) for r in rows if r[1] is not None][-limit:]

    def latest(self, sid: str):
        vals = self.values(sid, 1)
        return (self._as_of, vals[-1]) if vals else None


# ---------------------------------------------------------------------------
# monthly regime replay + NBER recession labels
# ---------------------------------------------------------------------------


def month_ends(start: str, end: str) -> list[str]:
    from datetime import date

    d = date.fromisoformat(f"{start[:7]}-01")
    e = date.fromisoformat(end)
    out = []
    while d <= e:
        nxt = (d.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1)
        out.append(min(nxt - __import__("datetime").timedelta(days=1), e).isoformat())
        d = nxt
    return out


def nber_recession_months(cycles: list[dict]) -> set[str]:
    """Set of 'YYYY-MM' months inside an NBER recession period."""
    from datetime import datetime

    out: set[str] = set()
    for c in cycles:
        try:
            peak = datetime.strptime(c["peak"], "%B %Y")
        except (TypeError, ValueError, KeyError):
            continue
        trough_s = c.get("trough")
        if not trough_s:
            continue
        try:
            trough = datetime.strptime(trough_s, "%B %Y")
        except ValueError:
            continue
        cur = peak
        while cur <= trough:
            out.add(cur.strftime("%Y-%m"))
            from datetime import timedelta

            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def run_replay(conn, start: str = "2005-01", verbose: bool = True) -> list[dict]:
    """Point-in-time regime score per month-end + NBER labels + inflation
    episodes. Returns [{month, score, label, recession, c_state,
    c_episode}] — the basis for threshold calibration and the honest backtest."""
    from ..fetchers.nber import fetch_recession_dates
    from ..signals.pillars import (
        REGIME_RISK_OFF,
        REGIME_RISK_ON,
        compute_pillars,
        compute_regime_score,
    )

    nber_na = False
    try:
        cycles = fetch_recession_dates()
    except Exception as ex:
        # A silent NBER failure would leave every recession label wrong with
        # no trace — warn and mark the state instead
        print(f"  ⚠ NBER fetch failed, recession labels all NA: {str(ex)[:80]}")
        cycles = []
        nber_na = True
    recess = nber_recession_months(cycles)

    from ..config import load_params_block_c

    pbc = load_params_block_c()
    min_months = int(pbc.get("min_months", 3))
    peak_window_m = int(pbc.get("peak_window_m", 24))
    peak_exit = float(pbc.get("peak_exit_ratio", 0.5))

    ends = month_ends(f"{start}-01", datetime.now(UTC).date().isoformat())
    records: list[dict] = []

    for as_of in ends:
        reader = AsOfReader(conn, as_of)
        pillars = compute_pillars(conn, reader=reader)
        score = compute_regime_score(pillars)
        label = (
            "RISK-ON"
            if score > REGIME_RISK_ON
            else ("RISK-OFF" if score < REGIME_RISK_OFF else "NEUTRAL")
        )
        # Pillar composition varies by era (2005-09 may have a single pillar),
        # making scores non-comparable — record the active pillar count per
        # month as metadata
        n_pillars = sum(1 for b in "ABCDEF" if pillars.get(b, {}).get("z") is not None)

        # Inflation episode from the as-of 3m-ann history (deterministic
        # from the series)
        cpi = reader.pairs("FRED:CPIAUCSL", 360)  # [(ts,v)] — gap-aware
        c_state, c_episode = episode_blok_c(
            cpi,
            min_months,
            peak_window_m,
            peak_exit,
            reaccel_low=float(pbc.get("reaccel_low_pct", 3.0)),
            confirm=float(pbc.get("confirm_pct", 5.0)),
        )
        nber_flag = "NA" if nber_na else str(int(as_of[:7] in recess))
        records.append(
            {
                "month": as_of[:7],
                "score": round(score, 3),
                "label": label,
                "n_pillars": n_pillars,
                "recession": (as_of[:7] in recess) if not nber_na else False,
                "c_state": c_state,
                "c_episode": c_episode,
            }
        )
        if verbose and len(records) % 24 == 0:
            print(f"  … {as_of[:7]} score={score:+.2f} {label} pillars={n_pillars}")

    # Persist a summary → computed_signals. ROUND-6 comment correction: the
    # computed_signals PK does NOT include run_id — INSERT OR REPLACE is
    # last-writer-wins per (signal_id, ts); each re-run REPLACES the prior
    # run's month rows (only the run_id label distinguishes eras; the last
    # full table state is the current replay by construction)
    conn.execute("BEGIN IMMEDIATE")
    for r in records:
        conn.execute(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES ('replay_regime', ?, 'f4-replay-v2', ?, ?, ?, NULL)",
            (
                r["month"],
                datetime.now(UTC).isoformat(timespec="seconds"),
                r["score"],
                f"{r['label']}|c={r['c_episode']}|nber={nber_flag}|pillars={r['n_pillars']}",
            ),
        )
    conn.execute("COMMIT")
    return records


# ---------------------------------------------------------------------------
# inflation episode engine
# ---------------------------------------------------------------------------


def episode_blok_c(
    cpi: list[tuple[str, float]] | list[float],
    min_months: int,
    peak_window_m: int,
    peak_exit: float,
    *,
    reaccel_low: float,
    confirm: float,
) -> tuple[str, str]:
    """Inflation state + episode status from the as-of CPI series.

    Accepts [(ts, value)] (recommended — month gaps are validated) or a plain
    [value] list (legacy, assumes consecutive months). Returns (c_state,
    episode). Exit rule: 3m-ann < peak_exit × max(TRAILING peak_window_m
    months of the series — not a per-episode peak), falling back to the full
    history when the series is shorter than the window. A per-episode peak
    makes exit nearly impossible after re-entry, and month gaps corrupt the
    3m ratio — gapped points are skipped, not interpolated.
    """
    # Normalize input → detect month gaps
    if cpi and isinstance(cpi[0], tuple):
        from datetime import date

        pts: list[tuple[int, float] | None] = []
        prev_m: date | None = None
        for ts, v in cast(list[tuple[str, float]], cpi):
            try:
                d = date.fromisoformat(str(ts)[:10])
            except ValueError:
                continue
            if prev_m is not None:
                gap = (d.year - prev_m.year) * 12 + (d.month - prev_m.month)
                if gap != 1:
                    prev_m = d
                    pts.append(None)  # break marker — 3m-ann is invalid across it
                    pts.append((0, v))
                    continue
            prev_m = d
            pts.append((0, v))
        # Simplify: on a break, only the last contiguous segment is used
        segs: list[list[float]] = []
        cur_seg: list[float] = []
        for p in pts:
            if p is None:
                if cur_seg:
                    segs.append(cur_seg)
                cur_seg = []
            else:
                cur_seg.append(p[1])
        if cur_seg:
            segs.append(cur_seg)
        # Use the LAST segment (ends at the newest data = point-in-time
        # semantics), not the longest (a pre-gap segment would be stale forever)
        cpi_vals = segs[-1] if segs else []
    else:
        cpi_vals = list(cast(list[float], cpi))

    if len(cpi_vals) < 4:
        return "INSUFFICIENT", "none"
    anns: list[float] = []
    for i in range(3, len(cpi_vals)):
        m1 = cpi_vals[i] / cpi_vals[i - 1] - 1
        m2 = cpi_vals[i - 1] / cpi_vals[i - 2] - 1
        m3 = cpi_vals[i - 2] / cpi_vals[i - 3] - 1
        anns.append(((1 + m1) * (1 + m2) * (1 + m3)) ** 4 - 1)

    cur = anns[-1] * 100
    # Use the same COOLING_PCT from params_block_c.yaml as pillars.py
    # (was hardcoded 1.0, risking divergence from the live brief)
    from ..config import load_params_block_c

    try:
        _pbc = load_params_block_c()
    except Exception:
        _pbc = {}
    cooling_pct = float(_pbc.get("cooling_pct", 1.0))
    if cur >= confirm:
        c_state = "REACCEL(high)"
    elif cur >= reaccel_low:
        c_state = "REACCEL(low)"
    elif cur < cooling_pct:
        c_state = "COOLING"
    else:
        c_state = "STABLE"

    # State machine: entry is re-evaluated after an exit in the same month
    # (fall-through); peak = trailing-window max of the series (not
    # per-episode)
    state = "none"
    dur = 0
    for i, a in enumerate(anns):
        a_pct = a * 100
        w_start = max(0, i + 1 - peak_window_m)
        peak = max(x * 100 for x in anns[w_start : i + 1])
        if state == "none":
            if a_pct >= reaccel_low:
                state = "high" if a_pct >= confirm else "low"
                dur = 1
            continue
        if state == "high":
            if a_pct < peak_exit * peak:  # exit rule (trailing window)
                state = "none"
                dur = 0
                # Fall-through: try entering again this month
                if a_pct >= reaccel_low:
                    state = "high" if a_pct >= confirm else "low"
                    dur = 1
            else:
                dur += 1
        elif state == "low":
            if a_pct < reaccel_low:  # simple exit
                state = "none"
                dur = 0
                if a_pct >= reaccel_low:  # unreachable — kept for shape consistency
                    pass
            else:
                dur += 1
                if a_pct >= confirm:
                    state = "high"
    official = dur >= min_months
    tag = (
        f"{state}({'official' if official else 'provisional'},{dur}m)"
        if state != "none"
        else "none"
    )
    return c_state, tag


# ---------------------------------------------------------------------------
# threshold calibration
# ---------------------------------------------------------------------------


def calibrate_thresholds(conn) -> list[dict]:
    """Compare 2.5/3.0/3.5: real episode count, median episode duration, months
    in a reaccel state. All three branches are pure reads (no side effects),
    and the statistics count actual episodes (transitions detected), not raw
    months."""
    import statistics

    from ..config import load_params_block_c

    pbc = load_params_block_c()
    results = []
    for thr in (2.5, 3.0, 3.5):
        episodes: list[str] = []
        for as_of in month_ends("2005-01-01", datetime.now(UTC).date().isoformat()):
            reader = AsOfReader(conn, as_of)
            cpi = reader.pairs("FRED:CPIAUCSL", 360)  # [(ts,v)] — gap-aware
            _cs, ep = episode_blok_c(
                cpi,
                int(pbc.get("min_months", 3)),
                int(pbc.get("peak_window_m", 24)),
                float(pbc.get("peak_exit_ratio", 0.5)),
                reaccel_low=thr,
                confirm=float(pbc.get("confirm_pct", 5.0)),
            )
            episodes.append(ep)
        # Detect episode transitions: the first month of each episode (dur == 1)
        epi_durs: list[int] = []
        cur = 0
        for ep in episodes:
            if ep == "none":
                if cur:
                    epi_durs.append(cur)
                cur = 0
                continue
            d = int(ep.split(",")[1].rstrip("m)"))
            if d == 1 and cur:  # a new episode started
                epi_durs.append(cur)
            cur = d
        if cur:
            epi_durs.append(cur)
        n_high_months = sum(1 for ep in episodes if ep.startswith("high"))
        results.append(
            {
                "threshold": thr,
                "months_reaccel_high": n_high_months,
                "episode_count": len(epi_durs),
                "median_episode_duration": statistics.median(epi_durs) if epi_durs else 0,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch f4")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    bp = sub.add_parser("backfill-first", help="pull first-prints for all FRED series")
    bp.add_argument("--only", nargs="*", help="restrict to native ids (e.g. CPIAUCSL ICSA)")
    sub.add_parser("replay", help="point-in-time regime per month")
    sub.add_parser("calibrate", help="thresholds 2.5/3/3.5")
    a = p.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    conn = db.get_conn(a.db, allow_init=True)

    if a.cmd == "backfill-first":
        print("=== FRED first-print backfill (all vintages → first release) ===")
        res = backfill_first_prints(conn, only=a.only)
        ok = sum(1 for v in res.values() if v >= 0)
        print(f"done: {ok}/{len(res)} series")
    elif a.cmd == "replay":
        print("=== point-in-time replay per month-end + NBER + episodes ===")
        recs = run_replay(conn)
        n_rec = sum(1 for r in recs if r["recession"])
        n_ro = sum(1 for r in recs if r["label"] == "RISK-OFF")
        print(f"months: {len(recs)} · NBER recessions: {n_rec} · RISK-OFF: {n_ro}")
        print("last 5 months:")
        for r in recs[-5:]:
            print(
                f"  {r['month']} score={r['score']:+.2f} {r['label']:<8} "
                f"c={r['c_state']:<13} ep={r['c_episode']}"
            )
    elif a.cmd == "calibrate":
        print("=== reaccel_low threshold calibration 2.5 / 3.0 / 3.5 ===")
        for row in calibrate_thresholds(conn):
            print(
                f"  thr={row['threshold']}: months-high={row['months_reaccel_high']:3} "
                f"episode={row['episode_count']:2} "
                f"median-episode-duration={row['median_episode_duration']}"
            )
    else:
        p.print_help()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
