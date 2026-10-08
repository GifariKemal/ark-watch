"""explore.py — top-down drill-down CLI (blocks → series → signal)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ..config import load_registry
from ..db import get_conn
from ..transforms.core import zscore

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
_TRACE_WINDOWS = {"D": 1260, "W": 260, "M": 60, "Q": 20, "A": 5}


def explore_blocks():
    reg = load_registry(active_only=True)
    by_block: dict[str, list] = {}
    for e in reg:
        by_block.setdefault(e["block"], []).append(e)
    print("=== BLOCKS ===")
    for blk in sorted(by_block):
        entries = by_block[blk]
        print(f"  {blk}: {len(entries)} series")
    print(f"  total: {len(reg)} active series")


def explore_block(conn: sqlite3.Connection, block: str):
    reg = load_registry(active_only=True)
    entries = [e for e in reg if e["block"] == block.upper()]
    if not entries:
        print(f"Block {block} not found or empty")
        return
    print(f"=== BLOCK {block.upper()} — {len(entries)} series ===")
    print(f"{'series_id':<28} {'value':>12} {'ts':<12} {'fresh':>6} {'source'}")
    print("-" * 80)
    for e in entries:
        sid = e["series_id"]
        row = conn.execute(
            "SELECT ts, value FROM raw_observations WHERE series_id=? "
            "AND vintage_ts='realtime' ORDER BY ts DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if row:
            # Render 0.0 correctly (a truthiness check would print N/A);
            # freshness is relative (last 7 days), not a fixed date
            val = f"{row[1]:,.2f}" if row[1] is not None else "N/A"
            ts = str(row[0])[:10]
            try:
                from datetime import date
                from datetime import datetime as _dt

                age = (date.today() - _dt.fromisoformat(ts).date()).days
                fresh = "✓" if age <= 7 else "⚠"
            except ValueError:
                fresh = "⚠"
        else:
            val, ts, fresh = "-", "-", "✗"
        src = e.get("primary_source", "")[:30]
        print(f"  {sid:<28} {val:>12} {ts:<12} {fresh:>6} {src}")


def explore_series(conn: sqlite3.Connection, sid: str):
    print(f"=== SERIES: {sid} ===")
    # Profile
    reg = load_registry(active_only=False)
    entry = next((e for e in reg if e["series_id"] == sid), None)
    if entry is None:
        # ROUND-4: an unregistered id printed a bare header and exited 0 —
        # indistinguishable from an empty-but-registered series
        print("  ✗ NOT IN REGISTRY — never registered, or the id is misspelled/")
        print("    inactive. No observations are being collected for it.")
        near = [e["series_id"] for e in reg if sid.upper() in e["series_id"].upper()][:5]
        if near:
            print(f"  similar: {', '.join(near)}")
        return 1
    if entry:
        print(f"  name      : {entry.get('name')}")
        print(f"  block     : {entry.get('block')}")
        print(f"  tier      : {entry.get('tier')}")
        print(f"  unit      : {entry.get('unit')} ({entry.get('value_format')})")
        print(f"  freq      : {entry.get('freq')}")
        print(f"  primary   : {entry.get('primary_source')}")
        print(f"  secondary : {entry.get('secondary_source')}")
        print(f"  sanity    : [{entry.get('sanity_min')}, {entry.get('sanity_max')}]")
    # Last 10 observations
    rows = conn.execute(
        "SELECT ts, value, source, vintage_ts FROM raw_observations "
        "WHERE series_id=? ORDER BY ts DESC LIMIT 10",
        (sid,),
    ).fetchall()
    if rows:
        print("\n  last 10 observations:")
        for r in rows:
            vintage = f" (vintage {r[3][:10]})" if r[3] != "realtime" else ""
            print(f"    {r[0]}  {r[1]:>12,.4f}  via {r[2]}{vintage}")
    # fetch_log stores the fetcher in `fetcher` and the series_id in `target`
    logs = conn.execute(
        "SELECT ts, status, rows, error FROM fetch_log WHERE target = ? ORDER BY ts DESC LIMIT 3",
        (sid,),
    ).fetchall()
    if logs:
        print("\n  last fetch_log entries:")
        for log in logs:
            print(f"    {log[0]}  {log[1]}  rows={log[2]}  {log[3] or ''}")
    return 0


# documented alias → stored signal_id (ROUND-4: docs say REGIME_SCORE /
# INFL_STATE but the stored ids are lowercase/compound — a case/alias miss
# printed the REGIME fallback while looking like the requested signal)
_SIGNAL_ALIASES = {
    "REGIME_SCORE": "regime_score",
    "INFL_STATE": "pillar_c",
    "ESI": "esi",
}


def explore_signal(conn: sqlite3.Connection, signal_id: str, trace: bool = False):
    resolved = _SIGNAL_ALIASES.get(signal_id.upper(), signal_id)
    if resolved != signal_id:
        print(f"  (alias: {signal_id} → {resolved})")
    signal_id = resolved
    print(f"=== SIGNAL: {signal_id} ===")
    rows = conn.execute(
        "SELECT ts, value, state, inputs_json FROM computed_signals "
        "WHERE signal_id=? ORDER BY ts DESC LIMIT 3",
        (signal_id,),
    ).fetchall()
    if not rows:
        # case-insensitive last resort
        ci = conn.execute(
            "SELECT DISTINCT signal_id FROM computed_signals"
            " WHERE signal_id=? COLLATE NOCASE LIMIT 1",
            (signal_id,),
        ).fetchone()
        if ci and ci[0] != signal_id:
            print(f"  (case: {signal_id} → {ci[0]})")
            return explore_signal(conn, ci[0], trace)
        print(f"  (no computed_signals for {signal_id})")
        # Fallback: compute fresh — id-aware: only the regime/pillar family
        # makes sense here; anything else has no fresh-compute path
        from ..signals.compute import compute_pillars, compute_regime_score

        pillars = compute_pillars(conn)
        score = compute_regime_score(pillars)
        print(f"  regime_score (fresh): {score:+.3f}")
        for blk in "ABCDEF":
            p = pillars.get(blk, {})
            print(f"    {blk} {p.get('label', ''):<14} z={p.get('z', 'N/A')}")
        return None
    for r in rows:
        print(f"  {r[0]}  value={r[1]}  state={r[2]}")
        if r[3]:
            print(f"    inputs: {r[3][:200]}")
    # --trace = trace signal inputs → current series values (top-down)
    if trace and rows:
        import json as _json

        latest_inputs = rows[0][3]
        try:
            inputs = _json.loads(latest_inputs) if latest_inputs else {}
        except (ValueError, TypeError):
            inputs = {}
        series_ids = list(dict.fromkeys(_input_series_ids(inputs)))
        if not series_ids and signal_id in {
            "regime_score",
            *(f"pillar_{b.lower()}" for b in "ABCDEF"),
        }:
            from ..signals.pillars import compute_pillars

            pillars = compute_pillars(conn)
            blocks = "ABCDEF" if signal_id == "regime_score" else signal_id[-1].upper()
            series_ids = list(
                dict.fromkeys(
                    sid for block in blocks for sid in pillars.get(block, {}).get("parts", [])
                )
            )
        if series_ids:
            entries = {e["series_id"]: e for e in load_registry(active_only=False)}
            print("\n  trace inputs -> latest value, change, and 5y z-score:")
            print(
                "  z5y(level) is descriptive raw-level context, not necessarily the signal's transform."
            )
            for sid in series_ids[:8]:
                traced = _trace_series(conn, sid, entries.get(sid, {}))
                if traced is None:
                    print(f"    {sid:<28} no realtime observations")
                    continue
                current, previous, source, z, n_obs, window, ts, previous_ts = traced
                prev_date = previous_ts[:10] if previous_ts else "N/A"
                delta = "N/A" if previous is None else f"{current - previous:+,.4f} vs {prev_date}"
                if window is None:
                    z_text = "N/A (irregular frequency)"
                elif z is None:
                    minimum = int(window * 0.8)
                    reason = (
                        f"{n_obs}/{window}; needs {minimum}" if n_obs < minimum else "zero variance"
                    )
                    z_text = f"N/A ({reason})"
                else:
                    z_text = f"{z:+.2f} ({n_obs}/{window})"
                unit = entries.get(sid, {}).get("unit") or ""
                print(
                    f"    {sid:<28} {current:>12,.4f} {unit:<10} delta={delta:<10} "
                    f"z5y(level)={z_text:<28} via {source} ({ts[:10]})"
                )
            if len(series_ids) > 8:
                print(f"    ... {len(series_ids) - 8} additional input series omitted")
        else:
            print("\n  trace unavailable: stored signal has no raw-series references")
    return None


def _input_series_ids(value) -> list[str]:
    if isinstance(value, str):
        return [value] if ":" in value else []
    if isinstance(value, dict):
        return [sid for item in value.values() for sid in _input_series_ids(item)]
    if isinstance(value, list):
        return [sid for item in value for sid in _input_series_ids(item)]
    return []


def _trace_series(conn, sid: str, entry: dict):
    """Return latest-source observations without mixing providers or vintages."""
    source = conn.execute(
        "SELECT source FROM raw_observations WHERE series_id=? AND vintage_ts='realtime' "
        "GROUP BY source ORDER BY MAX(ts) DESC, source=? DESC LIMIT 1",
        (sid, entry.get("primary_source", "")),
    ).fetchone()
    if source is None:
        return None
    window = _TRACE_WINDOWS.get(str(entry.get("freq", "")))
    limit = max(window or 2, 2) + 1
    rows = conn.execute(
        "SELECT ts,value FROM raw_observations WHERE series_id=? AND source=? "
        "AND vintage_ts='realtime' ORDER BY ts DESC LIMIT ?",
        (sid, source[0], limit),
    ).fetchall()
    rows.reverse()
    current = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    values = [float(row[1]) for row in rows]
    z = zscore(values, window=window) if window is not None else None
    z_obs = min(len(values), window) if window is not None else len(values)
    return (
        current[1],
        previous[1] if previous else None,
        source[0],
        z,
        z_obs,
        window,
        current[0],
        previous[0] if previous else None,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch explore")
    p.add_argument("target", help="blocks | block <X> | series <id> | signal <id>")
    p.add_argument("arg", nargs="?", help="additional argument (block/series/signal id)")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument(
        "--trace", action="store_true", help="trace latest input values, changes, and 5y z-scores"
    )
    a = p.parse_args(argv)

    conn = get_conn(a.db, read_only=True)

    if a.target == "blocks":
        explore_blocks()
    elif a.target == "block" and a.arg:
        explore_block(conn, a.arg)
    elif a.target == "series" and a.arg:
        explore_series(conn, a.arg)
    elif a.target == "signal" and a.arg:
        explore_signal(conn, a.arg, trace=a.trace)
    else:
        p.print_help()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
