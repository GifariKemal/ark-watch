"""surprise.py — σ surprise engine + ESI.

  z = (actual − consensus) / σ          — macro vs macro, NO price term
  σ  = sample stdev over rolling 5y surprises after >10×MAD outlier exclusion
  n<30 obs → permanently low_conf in indicator_stats (quarterly indicators etc.)
  ESI = Σ z·e^(−Δt/90d) / Σ e^(−Δt/90d) — exponential decay; empty days carry
       the previous value (not 0)
EOD suffices for every use case: z does not involve price; price reaction is
later measured on the daily bar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db
from .calendar import indicator_key, norm

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

# Engine parameters come from config (params_signals.yaml); the literals below
# are only the fallback if the file is unreadable. Values must stay identical
# to the shipped config — this is a definition move, not a recalibration.
try:
    from ..config import load_params_signals

    _PS = load_params_signals()
except Exception:
    _PS = {}
WINDOW_YEARS = int(_PS.get("surprise_window_years", 5))
MIN_OBS = int(
    _PS.get("surprise_min_obs", 30)
)  # below this, σ is flagged low_conf (quarterly indicators etc.)
WINSOR_SIGMA = float(_PS.get("surprise_winsor_sigma", 4.0))
ESI_TAU_DAYS = float(_PS.get("surprise_esi_tau_days", 90.0))


def backfill_keys(conn) -> int:
    """Fill/repair indicator_key for legacy or stranded rows.

    ROUND-9: the fill-only-NULL predicate left pre-alias rows stranded
    forever when an _ALIASES mapping landed after they were stored (live:
    TV's 'PPI YOY'/'PPI MOM' @09-10 — the alias exists, the rows never
    re-keyed, sigma stuck at n=1). The sweep now repairs ANY stored key
    that disagrees with the current indicator_key() computation."""
    rows = conn.execute("SELECT DISTINCT normalized_name FROM events").fetchall()
    n = 0
    fixed: list[str] = []
    conn.execute("BEGIN IMMEDIATE")
    for (nn,) in rows:
        want = indicator_key(nn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM events WHERE normalized_name=? AND"
            " (indicator_key IS NULL OR indicator_key<>?)",
            (nn, want),
        ).fetchone()[0]
        if cur:
            conn.execute(
                "UPDATE events SET indicator_key=? WHERE normalized_name=?"
                " AND (indicator_key IS NULL OR indicator_key<>?)",
                (want, nn, want),
            )
            n += cur
            fixed.append(f"{nn}→{want}")
    conn.execute("COMMIT")
    if fixed:
        print(f"  re-key: {n} rows across {len(fixed)} names ({'; '.join(fixed[:4])})")
    return n


def backfill_fmp(conn, years: int = 5, db_path: str | None = None) -> int:
    """Pull the FMP historical calendar per quarter → append-only (INSERT OR IGNORE).

    FMP /stable/economic-calendar supports past from/to ranges with
    actual+estimate filled; quarterly chunks for safety.
    """
    from ..fetchers import calendar as cal

    now = datetime.now(UTC)
    total = 0
    start = now - timedelta(days=365 * years)
    q = datetime(start.year, ((start.month - 1) // 3) * 3 + 1, 1, tzinfo=UTC)
    while q < now:
        q_end = min(q + timedelta(days=95), now)
        try:
            evs = cal.fetch_fmp(q.strftime("%Y-%m-%d"), q_end.strftime("%Y-%m-%d"))
        except Exception as ex:
            print(f"  ⚠ {q:%Y-%m}: {str(ex)[:90]}")
            q = q_end + timedelta(days=1)
            continue
        rows = []
        for e in evs:
            if e["actual"] is None or e["consensus"] is None:
                continue  # without a pair it is useless for σ
            nn = norm(e["name"])
            if not nn:
                continue
            # RONDE-4 P0 (D-027): the CANONICAL date-based uid — the old
            # full-timestamp uid here created a parallel row population the
            # calendar upsert could never reach (actuals froze)
            from .calendar import event_uid

            uid = event_uid(nn, e["ts_utc"])
            rows.append(
                (
                    uid,
                    e["ts_utc"],
                    e["ts_utc"],
                    "US",
                    e["name"],
                    nn,
                    e["importance"],
                    e["consensus"],
                    "FMP",
                    e["actual"],
                    "FMP",
                    e["previous"],
                    None,
                    0,
                    indicator_key(nn),
                )
            )
        added = 0
        if rows:
            conn.execute("BEGIN IMMEDIATE")
            # RONDE-5 P1 (D-028): INSERT OR IGNORE could never fill an existing
            # canonical row's NULL actual — the Aug-2026 NFP stayed frozen
            # because the daily pull window (now-3d) had already passed it.
            # The conditional upsert mirrors calendar.save's heal semantics:
            # fill-if-NULL, never overwrite a filled value.
            cur = conn.executemany(
                "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,"
                "normalized_name,importance,consensus,consensus_source,actual,actual_source,"
                "previous,surprise_z,is_curated,indicator_key)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(event_uid) DO UPDATE SET"
                " actual=excluded.actual, actual_source=excluded.actual_source,"
                " previous=COALESCE(events.previous, excluded.previous)"
                " WHERE events.actual IS NULL AND excluded.actual IS NOT NULL",
                rows,
            )
            conn.execute("COMMIT")
            added = cur.rowcount
            total += added
        print(f"  {q:%Y-%m}: +{len(rows)} pairs ({added} new/healed)")
        q = q_end + timedelta(days=1)
        time.sleep(0.4)  # polite rate limit
    return total


def compute_sigma(conn, as_of: str | None = None) -> dict:
    """Compute and append a point-in-time sigma snapshot.

    Returns indicator counts and the immutable snapshot ID.
    """
    now_dt = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    now_dt = now_dt.astimezone(UTC)
    now = now_dt.isoformat(timespec="microseconds")
    snapshot_id = f"{now}#{time.time_ns()}"
    cutoff = (now_dt - timedelta(days=365 * WINDOW_YEARS)).isoformat(timespec="microseconds")
    # One row per (key, date) — deterministic dedup across sources: a
    # NOT-EXISTS-by-rowid filter is not deterministic (the surviving row
    # depends on insertion order, so σ could change between runs without any
    # data change); GROUP BY + MAX is deterministic — duplicate rows of the
    # same event carry identical actual/consensus.
    rows = conn.execute(
        "SELECT indicator_key, substr(release_ts,1,10) d, MAX(actual), MAX(consensus) "
        "FROM events "
        "WHERE indicator_key IS NOT NULL AND actual IS NOT NULL AND consensus IS NOT NULL "
        "AND release_ts IS NOT NULL AND release_ts NOT IN ('', 'na') "
        "AND release_ts >= ? AND release_ts <= ? "
        "GROUP BY indicator_key, substr(release_ts,1,10) "
        "ORDER BY indicator_key, d",
        (cutoff, now),
    ).fetchall()

    by_key: dict[str, list[float]] = {}
    for key, _d, actual, cons in rows:
        # Quantize to source precision BEFORE differencing: calendar data is
        # rounded to 0.1, but two float representations of −0.1 leave
        # MAD ≈ 1e−16 (dust) → winsor bounds at ±4e−15 would clip every diff
        # to dust → σ ≈ 2e−15 → z in the trillions.
        by_key.setdefault(key, []).append(round(actual - cons, 10))

    n_lc = 0
    # ROUND-6: label names the REAL mechanism — >10×MAD unit-contaminants
    # are EXCLUDED from the population (round-5); the old 'winsor4MAD'
    # label described a clipping step that no longer exists
    _window_label = f"{WINDOW_YEARS}y-excl10MAD"
    snapshot_rows = []
    conn.execute("BEGIN IMMEDIATE")
    for key, diffs in by_key.items():
        n = len(diffs)
        # Winsor bounds come from MAD (median absolute deviation; 1.4826×MAD ≈ σ
        # for a normal distribution), NOT the sample σ: the sample σ is
        # already contaminated by outliers → ±4σ bounds would be too wide to
        # clip them (masking).
        srt = sorted(diffs)
        med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
        mad = sorted(abs(d - med) for d in diffs)
        mad = mad[n // 2] if n % 2 else (mad[n // 2 - 1] + mad[n // 2]) / 2
        sigma_robust = 1.4826 * mad
        # ROUND-3: EXCLUDE unit-contaminants (>10 MAD from the median) from
        # the population instead of winsor-clipping them — a clipped
        # contaminant still inflates sigma (live: NEW HOME SALES 1.69x from
        # %MoM-vs-level pairs) and damps every live z into ESI
        if sigma_robust > 0:
            kept = [d for d in diffs if abs(d - med) <= 10 * sigma_robust]
        else:
            kept = list(diffs)
        n_k = len(kept) or 1
        mean_c = sum(kept) / n_k
        sigma = math.sqrt(sum((d - mean_c) ** 2 for d in kept) / max(n_k - 1, 1))
        low_conf = 1 if n < MIN_OBS else 0
        n_lc += low_conf
        vintage = (key, sigma, n, _window_label, low_conf)
        snapshot_rows.append(vintage)
        conn.execute(
            "INSERT OR REPLACE INTO indicator_stats"
            "(indicator, as_of, sigma, n_obs, window, low_conf)"
            " VALUES (?,?,?,?,?,?)",
            (key, now_dt.date().isoformat(), sigma, n, _window_label, low_conf),
        )
    # ROUND-2: drop this as_of's ghost keys — families removed by re-keying/
    # stub-cleanup otherwise keep stale σ rows forever and the indicator
    # count overstates the living population (live: 12 ghosts vs 184 alive)
    conn.execute(
        "DELETE FROM indicator_stats WHERE as_of=? AND indicator NOT IN"
        " (SELECT DISTINCT indicator_key FROM events WHERE indicator_key IS NOT NULL)",
        (now_dt.date().isoformat(),),
    )
    payload = json.dumps(snapshot_rows, separators=(",", ":"), ensure_ascii=True)
    payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
    method = json.dumps(
        {
            "engine": "rolling-mad-exclusion-v1",
            "window_years": WINDOW_YEARS,
            "min_obs": MIN_OBS,
            "outlier_mad_multiple": 10,
            "mad_normal_consistency": 1.4826,
            "diff_rounding_decimals": 10,
            "sample_standard_deviation": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO indicator_sigma_snapshots"
        "(snapshot_id,as_of,calculated_at,n_indicators,method,payload_sha256)"
        " VALUES (?,?,?,?,?,?)",
        (snapshot_id, now_dt.date().isoformat(), now, len(snapshot_rows), method, payload_sha256),
    )
    conn.executemany(
        "INSERT INTO indicator_sigma_vintages"
        "(snapshot_id,indicator,sigma,n_obs,window,low_conf) VALUES (?,?,?,?,?,?)",
        [(snapshot_id, *row) for row in snapshot_rows],
    )
    conn.execute("COMMIT")
    return {"n_indicators": len(by_key), "low_conf": n_lc, "snapshot_id": snapshot_id}


def update_surprise_z(conn) -> int:
    """Recompute paired events and retain the exact sigma snapshot used.

    ROUND-5: recompute EVERY paired event each run (the vintage-locked
    `AND surprise_z IS NULL` fill meant sigma recalibrations never
    propagated — z stayed frozen at first-compute vintage while ESI
    'rebuilds' silently reused stale z). |z|>10 rows are quarantined to
    NULL (mixed units / corruption — distrusted numbers never reach the
    brief), and previously-filled rows that NOW exceed 10 are re-NULLed.
    """
    snapshot = conn.execute(
        "SELECT snapshot_id,calculated_at FROM indicator_sigma_snapshots "
        "ORDER BY calculated_at DESC,snapshot_id DESC LIMIT 1"
    ).fetchone()
    if snapshot is None:
        return 0
    sigma_by_key = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT indicator,sigma,n_obs,window,low_conf FROM indicator_sigma_vintages "
            "WHERE snapshot_id=?",
            (snapshot[0],),
        )
    }
    n = 0
    quarantined = 0
    conn.execute("BEGIN IMMEDIATE")
    for key, (sigma, n_obs, window, low_conf) in sigma_by_key.items():
        if not sigma or sigma <= 0:
            continue
        # ROUND-5: no `AND surprise_z IS NULL` — every paired event is
        # recomputed against the CURRENT sigma, so recalibrations
        # propagate; rows that now exceed the |z|>10 quarantine band are
        # re-NULLed in the same statement
        cur = conn.execute(
            "UPDATE events SET surprise_z = (actual - consensus) / ?, sigma_vintage=?, "
            "sigma_snapshot_id=?, sigma_n_obs=?, sigma_window=?, sigma_low_conf=? "
            "WHERE indicator_key=? "
            "AND actual IS NOT NULL AND consensus IS NOT NULL "
            "AND release_ts IS NOT NULL AND release_ts NOT IN ('','na') AND release_ts<=? "
            "AND ABS((actual - consensus) / ?) <= 10",
            (sigma, sigma, snapshot[0], n_obs, window, low_conf, key, snapshot[1], sigma),
        )
        n += cur.rowcount
        # ROUND-5: re-quarantine drift — previously-filled rows that the
        # CURRENT sigma now puts beyond |z|>10 go back to NULL
        conn.execute(
            "UPDATE events SET surprise_z=NULL,sigma_vintage=?,sigma_snapshot_id=?,"
            "sigma_n_obs=?,sigma_window=?,sigma_low_conf=? WHERE indicator_key=? "
            "AND surprise_z IS NOT NULL "
            "AND release_ts IS NOT NULL AND release_ts NOT IN ('','na') AND release_ts<=? "
            "AND ABS((actual - consensus) / ?) > 10",
            (sigma, snapshot[0], n_obs, window, low_conf, key, snapshot[1], sigma),
        )
        q = conn.execute(
            "SELECT COUNT(*) FROM events WHERE indicator_key=? "
            "AND surprise_z IS NULL AND actual IS NOT NULL "
            "AND consensus IS NOT NULL AND release_ts<=? "
            "AND ABS((actual - consensus) / ?) > 10",
            (key, snapshot[1], sigma),
        ).fetchone()[0]
        quarantined += q
    if quarantined:
        print(f"  quarantined: {quarantined} events with |z|>10 (mixed units/bad data)")
    conn.execute(
        "UPDATE events SET surprise_z=NULL,sigma_vintage=NULL,sigma_snapshot_id=NULL,"
        "sigma_n_obs=NULL,sigma_window=NULL,sigma_low_conf=NULL "
        "WHERE actual IS NOT NULL AND consensus IS NOT NULL "
        "AND (release_ts IS NULL OR release_ts IN ('','na') OR release_ts>? "
        "OR indicator_key IS NULL OR indicator_key NOT IN "
        "(SELECT indicator FROM indicator_sigma_vintages WHERE snapshot_id=?))",
        (snapshot[1], snapshot[0]),
    )
    conn.execute("COMMIT")
    return n


def _esi_inputs(conn, as_of: datetime, lookback_days: int):
    as_of = as_of.astimezone(UTC)
    snapshot = conn.execute(
        "SELECT snapshot_id,calculated_at,method,payload_sha256 FROM indicator_sigma_snapshots "
        "WHERE calculated_at<=? ORDER BY calculated_at DESC,snapshot_id DESC LIMIT 1",
        (as_of.isoformat(timespec="microseconds"),),
    ).fetchone()
    if snapshot is None:
        return None, None
    snapshot_id, calculated_at, method, payload_sha256 = snapshot
    sigma_by_key = {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in conn.execute(
            "SELECT indicator,sigma,n_obs,window,low_conf FROM indicator_sigma_vintages "
            "WHERE snapshot_id=?",
            (snapshot_id,),
        )
    }
    cutoff = (as_of - timedelta(days=lookback_days)).isoformat(timespec="microseconds")
    rows = conn.execute(
        "SELECT indicator_key,substr(release_ts,1,10) d,AVG(actual-consensus) "
        "FROM events WHERE indicator_key IS NOT NULL AND actual IS NOT NULL "
        "AND consensus IS NOT NULL AND release_ts IS NOT NULL AND release_ts NOT IN ('','na') "
        "AND release_ts>=? AND release_ts<=? "
        "GROUP BY indicator_key,substr(release_ts,1,10) ORDER BY d,indicator_key",
        (cutoff, as_of.isoformat(timespec="microseconds")),
    ).fetchall()
    daily = {}
    for key, day, surprise in rows:
        sigma_row = sigma_by_key.get(key)
        if sigma_row is None:
            continue
        sigma, _n_obs, _window, low_conf = sigma_row
        if low_conf or not sigma or sigma <= 0:
            continue
        z = surprise / sigma
        if abs(z) > 10:
            continue
        daily.setdefault((key, day), []).append(z)

    num = den = 0.0
    for (_key, day), zs in daily.items():
        z = sum(zs) / len(zs)
        z = min(max(z, -WINSOR_SIGMA), WINSOR_SIGMA)
        event_time = datetime.fromisoformat(day + "T12:00:00+00:00")
        age = (as_of - event_time).total_seconds() / 86400.0
        weight = math.exp(-age / ESI_TAU_DAYS)
        num += z * weight
        den += weight
    esi = num / den if den > 0 else None
    if not payload_sha256:
        vintage_rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT indicator,sigma,n_obs,window,low_conf FROM indicator_sigma_vintages "
                "WHERE snapshot_id=? ORDER BY indicator",
                (snapshot_id,),
            )
        ]
        payload_sha256 = hashlib.sha256(
            json.dumps(vintage_rows, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
    inputs = {
        "engine": "esi-v2",
        "sigma_snapshot_id": snapshot_id,
        "sigma_snapshot_calculated_at": calculated_at,
        "sigma_snapshot_method": method,
        "sigma_snapshot_sha256": payload_sha256,
        "tau_days": ESI_TAU_DAYS,
        "lookback_days": lookback_days,
        "z_clip": WINSOR_SIGMA,
    }
    return esi, inputs


def compute_esi(conn, as_of: datetime | None = None, lookback_days: int = 365) -> float | None:
    """ESI = Σ z·e^(−Δt/τ) / Σ e^(−Δt/τ), τ=90 days.

    z is clipped to ±4 entering the ESI — same philosophy as the σ winsorize:
    one corrupted row must not steer the index. Only low_conf=0 indicators
    contribute: a once-quarterly σ from n<30 is not reliable enough to drive
    the index.
    """
    now = as_of or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    value, _inputs = _esi_inputs(conn, now, lookback_days)
    return value


def store_esi(conn) -> float | None:
    """Daily ESI → computed_signals (audit trail + input to the flip trigger)."""
    now = datetime.now(UTC)
    esi, inputs = _esi_inputs(conn, now, 365)
    if esi is None:
        return None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR REPLACE INTO computed_signals"
        "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            "esi",
            now.date().isoformat(),
            now.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            round(esi, 4),
            "POSITIVE" if esi > 0 else "NEGATIVE",
            json.dumps(inputs, sort_keys=True, separators=(",", ":")),
        ),
    )
    conn.execute("COMMIT")
    return esi


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch surprise")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument(
        "--backfill",
        type=int,
        metavar="YEARS",
        help="pull N years of the FMP historical calendar (one-shot run)",
    )
    a = p.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv()  # needed when run directly as a module (not via -m arkwatch)
    except ImportError:
        pass
    conn = db.get_conn(a.db, allow_init=True)

    n_keys = backfill_keys(conn)
    print(f"indicator_key filled for {n_keys} names (legacy rows)")

    if a.backfill:
        print(f"=== FMP backfill {a.backfill} years ===")
        total = backfill_fmp(conn, years=a.backfill)
        print(f"total new rows: {total}")

    print("=== σ engine (rolling 5y, >10 MAD outlier exclusion) ===")
    r = compute_sigma(conn)
    print(f"  {r['n_indicators']} indicators · {r['low_conf']} low_conf (n<{MIN_OBS})")
    print(f"  snapshot: {r['snapshot_id']}")
    for row in conn.execute(
        "SELECT indicator, sigma, n_obs, low_conf FROM indicator_stats ORDER BY n_obs DESC LIMIT 8"
    ).fetchall():
        lc = " ⚠low_conf" if row[3] else ""
        print(f"  {row[0][:40]:42} σ={row[1]:9.2f} n={row[2]:3}{lc}")

    n_z = update_surprise_z(conn)
    print(f"=== surprise_z filled: {n_z} events ===")

    esi = store_esi(conn)
    if esi is not None:
        print(f"=== ESI = {esi:+.3f} ({'positive' if esi > 0 else 'negative'}) ===")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
