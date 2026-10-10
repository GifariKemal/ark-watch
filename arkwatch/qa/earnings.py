"""earnings.py — FMP earnings-calendar harvest + the heavy-week gauge.

Owner decision 2026-09-13 (data-first phase): this is a DATA deliverable —
the weekly heavyweight-share lands in computed_signals; there is no brief
line (delivery paused). Trading US100/US500 does not require watching
stocks one by one, but the indexes are top-heavy enough (top-10 ≈ 39% of
SPX / 54% of NDX per config/index_heavyweights.yaml) that mega-cap
earnings weeks are scheduled volatility for the whole index.

Weekly bucket = Monday..Sunday containing the announcement date. Shares:
share_spx = Σ weights of SPX heavyweights reporting that week (pp of
index); share_ndx likewise. A week with share_spx ≥ 15 is 'heavy' by
construction (a couple of megacaps reporting).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from ..config import PlanLimited
from .fetch_log import log_collection

_CFG = Path(__file__).resolve().parent.parent.parent / "config" / "index_heavyweights.yaml"


def _heavyweights() -> tuple[dict[str, float], dict[str, float]]:
    with open(_CFG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    spx = {r["symbol"]: float(r["weight"]) for r in cfg["spx"]}
    ndx = {r["symbol"]: float(r["weight"]) for r in cfg["ndx"]}
    return spx, ndx


def _week_start(d: date) -> date:
    """Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def harvest_earnings(conn, days: int = 42) -> int:
    """Land the next `days` of the earnings calendar (idempotent upsert;
    universe-wide — the heavyweights gauge reads from the table).

    days=42 (review ronde-2 P2): aligns with compute_earnings_weeks'
    5-week horizon + slack — at 21 days the outer gauge weeks silently
    had zero rows. Failure path logs fetch_log ERROR (the BYBIT:FLOWS
    class — it was print-only before)."""
    from ..fetchers.misc import fetch_earnings_calendar

    today = datetime.now(UTC).date()
    try:
        rows = fetch_earnings_calendar(
            today.isoformat(), (today + timedelta(days=days)).isoformat()
        )
    except PlanLimited as ex:  # free-plan quota/plan limit: SKIPPED, not a failure
        log_collection(conn, "f2", "FMP:EARNINGS", None, 0, err=str(ex)[:140], status="SKIPPED")
        return 0
    except Exception as ex:
        log_collection(conn, "f2", "FMP:EARNINGS", None, 0, err=str(ex)[:140])
        raise
    now = datetime.now(UTC).isoformat(timespec="seconds")
    n = 0
    for r in rows:
        cur = conn.execute(
            "INSERT INTO earnings_calendar(symbol, date, eps_estimated, eps_actual,"
            " revenue_estimated, revenue_actual, last_updated, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(symbol, date) DO UPDATE SET"
            " eps_estimated=excluded.eps_estimated, eps_actual=excluded.eps_actual,"
            " revenue_estimated=excluded.revenue_estimated,"
            " revenue_actual=excluded.revenue_actual,"
            " last_updated=excluded.last_updated, fetched_at=excluded.fetched_at",
            (
                r["symbol"],
                r["date"],
                r["eps_estimated"],
                r["eps_actual"],
                r["revenue_estimated"],
                r["revenue_actual"],
                r["last_updated"],
                now,
            ),
        )
        n += cur.rowcount if cur.rowcount > 0 else 0
    conn.commit()
    log_collection(conn, "f2", "FMP:EARNINGS", (rows[0] if rows else None), len(rows))
    return len(rows)


def compute_earnings_weeks(conn, weeks_ahead: int = 5) -> list[dict]:
    """Heavyweight reporting share per upcoming Monday-week, stored in
    computed_signals (signal_id earnings_week_spx / earnings_week_ndx;
    value = weight-share in PERCENT-POINTS of the index)."""
    spx_w, ndx_w = _heavyweights()

    # review ronde-2 (P2): normalize ticker forms ONCE — FMP serves
    # BRK-B (dash) while the config uses BRK.B (dot); without this the
    # Berkshire weight silently never counts
    def _norm(s: str) -> str:
        return s.upper().replace(".", "-")

    spx_w = {_norm(k): v for k, v in spx_w.items()}
    ndx_w = {_norm(k): v for k, v in ndx_w.items()}
    today = datetime.now(UTC).date()
    horizon = today + timedelta(days=7 * weeks_ahead)

    rows = conn.execute(
        "SELECT symbol, date FROM earnings_calendar WHERE date >= ? AND date <= ?",
        (today.isoformat(), horizon.isoformat()),
    ).fetchall()
    by_week: dict[date, dict[str, float]] = {}
    for sym, d in rows:
        wk = _week_start(date.fromisoformat(d))
        bucket = by_week.setdefault(wk, {"spx": 0.0, "ndx": 0.0, "n": 0})
        key = _norm(sym)
        if key in spx_w:
            bucket["spx"] += spx_w[key]
        if key in ndx_w:
            bucket["ndx"] += ndx_w[key]
        if key in spx_w or key in ndx_w:
            bucket["n"] += 1

    now = datetime.now(UTC).isoformat(timespec="seconds")
    run_id = now
    out = []
    for wk in sorted(by_week):
        b = by_week[wk]
        for sid, val in (
            ("earnings_week_spx", round(b["spx"], 1)),
            ("earnings_week_ndx", round(b["ndx"], 1)),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO computed_signals(signal_id, ts, run_id,"
                " computed_at, value, state, inputs_json) VALUES (?,?,?,?,?,?,?)",
                (sid, wk.isoformat(), run_id, now, val, "HEAVY" if val >= 15 else "QUIET", None),
            )
        out.append(
            {
                "week": wk.isoformat(),
                "spx_pp": round(b["spx"], 1),
                "ndx_pp": round(b["ndx"], 1),
                "n_heavy": b["n"],
            }
        )
    conn.commit()
    return out
