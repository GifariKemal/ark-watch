"""book_risk.py - deterministic risk view of the open scenario book (no LLM, advisory only).

Open = PENDING_TRIGGER or ACTIVE. Reference price is the fill for ACTIVE scenarios and the
trigger for pending ones; risk distance = |ref - stop| / ref. Correlated clusters come from
config/risk_clusters.yaml. ark-watch never trades: flags and veto_hints are plain text for a
human to read.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime
from functools import cache
from typing import Any

from ..config import _load_yaml
from .asof import parse_as_of
from .playbook_tracker import _OPEN, _expiry
from .scorecard import asset_class

MAX_SAME_DIRECTION_ACTIVE = 3  # per cluster
MIN_STOP_PCT = 0.1  # % of entry
R_ASSUMPTION = (
    "each ACTIVE scenario risks exactly 1R (one unit at its stop); pending scenarios risk"
    " nothing until filled; no position sizing is modelled"
)


@cache
def _cluster_of() -> dict[str, str]:
    clusters = _load_yaml("risk_clusters.yaml")["clusters"]
    return {s: name for name, syms in clusters.items() for s in syms}


def compute_book_risk(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT scenario_uid, symbol, horizon, direction, scenario_id, state, trigger_price,"
        " entry_price, invalidation_level, target_profit, created_at_utc, session_id"
        f" FROM playbook_scenarios WHERE state IN ({','.join('?' * len(_OPEN))})"
        " ORDER BY created_at_utc, scenario_uid",
        _OPEN,
    ).fetchall()
    clusters = _cluster_of()
    book = []
    for uid, sym, hz, d, sid, state, trig, entry, stop, target, created, sess in rows:
        ref = entry if state == "ACTIVE" and entry is not None else trig
        risk = abs(ref - stop) if ref is not None else None
        expiry = _expiry(hz, sess)
        book.append(
            {
                "scenario_uid": uid,
                "symbol": sym,
                "scenario_type": sid,
                "horizon": hz,
                "direction": d,
                "state": state,
                "asset_class": asset_class(sym),
                "cluster": clusters.get(sym),
                "ref_price": ref,
                "stop": stop,
                "target": target,
                "risk_pct": round(100 * risk / ref, 4) if ref else None,
                "reward_to_risk": round(abs(target - ref) / risk, 2) if risk else None,
                "age_hours": round((now - parse_as_of(created)).total_seconds() / 3600, 2),
                "expired": expiry is not None and now >= expiry,
            }
        )

    flags: list[str] = []
    hints: list[str] = []
    by_sym: dict[str, set] = {}
    for s in book:
        by_sym.setdefault(s["symbol"], set()).add(s["direction"])
    for sym, dirs in sorted(by_sym.items()):
        if {"LONG", "SHORT"} <= dirs:
            flags.append(f"{sym}: LONG and SHORT scenarios are open at the same time (opposite)")
            hints.append(f"Pick one side on {sym}; the opposite scenarios hedge each other out.")

    active = [s for s in book if s["state"] == "ACTIVE"]
    crowd = Counter((s["cluster"], s["direction"]) for s in active if s["cluster"])
    for (cl, d), k in sorted(crowd.items()):
        if k > MAX_SAME_DIRECTION_ACTIVE and d in ("LONG", "SHORT"):
            flags.append(f"{cl}: {k} ACTIVE {d} scenarios in one correlated cluster")
            hints.append(
                f"Consider no new {d} exposure in {cl}: {k} correlated ACTIVE scenarios"
                f" already behave like one {k}R bet."
            )
    for s in active:
        if s["risk_pct"] is not None and s["risk_pct"] < MIN_STOP_PCT:
            flags.append(
                f"{s['scenario_uid']}: stop is {s['risk_pct']}% from entry (under {MIN_STOP_PCT}%)"
            )
            hints.append(f"{s['scenario_uid']}: a stop this tight is likely to be hit by noise.")
    for s in book:
        if s["expired"]:
            flags.append(f"{s['scenario_uid']}: still {s['state']} past its expiry")
            hints.append(
                f"{s['scenario_uid']}: treat as stale until the tracker resolves it; do not act on it."
            )

    net: dict[str, dict[str, int]] = {}
    for s in book:
        n = net.setdefault(s["asset_class"], {"long": 0, "short": 0, "net": 0})
        if s["direction"] in ("LONG", "SHORT"):
            n[s["direction"].lower()] += 1
            n["net"] = n["long"] - n["short"]
    cl_out: dict[str, dict[str, int]] = {}
    for s in book:
        if s["cluster"]:
            c = cl_out.setdefault(s["cluster"], {"open": 0, "active_long": 0, "active_short": 0})
            c["open"] += 1
            if s["state"] == "ACTIVE" and s["direction"] in ("LONG", "SHORT"):
                c[f"active_{s['direction'].lower()}"] += 1

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "advisory": "Advisory only; ark-watch never places trades.",
        "open_count": len(book),
        "by_state": dict(Counter(s["state"] for s in book)),
        "by_direction": dict(Counter(s["direction"] for s in book)),
        "by_asset_class": dict(Counter(s["asset_class"] for s in book)),
        "net_by_asset_class": net,
        "clusters": cl_out,
        "r_at_stake": len(active),
        "r_at_stake_assumption": R_ASSUMPTION,
        "scenarios": book,
        "flags": flags,
        "veto_hints": hints,
    }
