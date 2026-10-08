"""horizon_backtest.py — timing-cell edge test over REAL resolved playbook trades.

Outcomes come only from playbook_scenarios rows that were triggered and
resolved (HIT_TARGET_WIN / HIT_STOP_LOSS, entry_price + r_multiple present).
Each hypothesis is a cell: scenario_id (or "*" = any) x one timing dimension
value (QT 6h quarter, 90m sub-quarter, weekday, week-of-month) read off the
trigger time in New York, or the scenario as a whole ("ALL").

Per tested cell: n, n_eff (sessions as clusters), win rate + Wilson 95% CI,
expectancy R + session-cluster bootstrap CI, one-sided binomial p-value vs the
EMPIRICAL base rate (pooled win rate of every resolved trade in the sample)
and a Benjamini-Yekutieli q (BH optional). A cell with fewer than
min_observations trades is reported as insufficient_data, with no p-value.

ponytail: no re-simulation over intraday_bars — only trades the tracker
actually resolved count; add a bar re-simulator when live history is too thin.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import stats

NY_TZ = ZoneInfo("America/New_York")
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "qt_quarter": ("Q1_ASIA", "Q2_LONDON", "Q3_NY_AM", "Q4_NY_PM"),
    "sub_quarter_90m": ("0", "1", "2", "3"),
    "weekday": WEEKDAYS[:5],
    "month_week": ("W1", "W2", "W3", "W4"),
}


def _features(session_id: str, triggered_at_utc: str) -> dict[str, str]:
    """Timing features of one trade. QT day starts 18:00 ET (6h quarters)."""
    t = datetime.fromisoformat(triggered_at_utc)
    ny = (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(NY_TZ)
    minutes = ((ny.hour - 18) % 24) * 60 + ny.minute
    try:
        day = date.fromisoformat(session_id[:10])  # CME trading date
    except ValueError:
        day = ny.date()
    return {
        "qt_quarter": DIMENSIONS["qt_quarter"][minutes // 360],
        "sub_quarter_90m": str(minutes % 360 // 90),
        "weekday": WEEKDAYS[day.weekday()],
        "month_week": f"W{min(4, (day.day - 1) // 7 + 1)}",
    }


def generate_hypotheses(scenario_ids: list[str]) -> list[dict[str, Any]]:
    """Every (scenario | "*") x (ALL | dimension=value) cell, minus "*|ALL"
    (the pooled sample is the base rate itself, not a hypothesis)."""
    out = []
    for sc in [*sorted(scenario_ids), "*"]:
        if sc != "*":
            out.append({"id": f"{sc}|ALL", "scenario_id": sc, "dimension": None, "value": None})
        for dim, values in DIMENSIONS.items():
            for v in values:
                out.append(
                    {"id": f"{sc}|{dim}={v}", "scenario_id": sc, "dimension": dim, "value": v}
                )
    return out


def _resolved_trades(conn: sqlite3.Connection, symbols: list[str] | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT symbol, scenario_id, session_id, triggered_at_utc, state, r_multiple"
        " FROM playbook_scenarios WHERE state IN ('HIT_TARGET_WIN', 'HIT_STOP_LOSS')"
        " AND entry_price IS NOT NULL AND r_multiple IS NOT NULL"
        " AND triggered_at_utc IS NOT NULL"
    ).fetchall()
    wanted = {s.upper() for s in symbols} if symbols else None
    return [
        {
            "scenario_id": sc,
            "session": f"{sym}|{sess}",
            "win": state == "HIT_TARGET_WIN",
            "r": float(r),
            **_features(sess, trig),
        }
        for sym, sc, sess, trig, state, r in rows
        if wanted is None or sym.upper() in wanted
    ]


def _evidence(q: float, exp_ci_lo: float, alpha: float) -> str:
    hits = (q < alpha) + (exp_ci_lo > 0)
    return ("no_edge", "mixed", "supported")[hits]


def run_horizon_backtest(
    conn: sqlite3.Connection,
    symbols: list[str] | None = None,
    *,
    min_observations: int = 30,
    alpha: float = 0.05,
    fdr_method: str = "fdr_by",
    n_boot: int = 2000,
    seed: int = 0,
) -> dict[str, Any]:
    """Evaluate every timing cell on real resolved trades; see module docstring."""
    trades = _resolved_trades(conn, symbols)
    base_rate = sum(t["win"] for t in trades) / len(trades) if trades else None
    grid = generate_hypotheses(sorted({t["scenario_id"] for t in trades})) if trades else []

    results: list[dict[str, Any]] = []
    for h in grid:
        cell = [
            t
            for t in trades
            if h["scenario_id"] in ("*", t["scenario_id"])
            and (h["dimension"] is None or t[h["dimension"]] == h["value"])
        ]
        rec = {**h, "n": len(cell)}
        results.append(rec)
        if len(cell) < min_observations:
            rec["status"] = rec["evidence"] = "insufficient_data"
            continue
        wins = sum(t["win"] for t in cell)
        sessions = [t["session"] for t in cell]
        rs = [t["r"] for t in cell]
        n_eff = stats.effective_n([float(t["win"]) for t in cell], sessions)
        # the binomial test uses the session-deflated sample, not raw n
        k_eff = round(wins * n_eff / len(cell))
        rec.update(
            status="tested",
            n_sessions=len(set(sessions)),
            n_eff=round(n_eff, 1),
            wins=wins,
            win_rate=round(wins / len(cell), 4),
            win_rate_ci95=stats.wilson_ci(wins, len(cell), alpha),
            expectancy_r=round(sum(rs) / len(rs), 4),
            expectancy_r_ci95=stats.cluster_bootstrap_ci(
                rs, sessions, n_boot=n_boot, alpha=alpha, seed=seed
            ),
            base_rate=round(base_rate, 4),
            p_raw=stats.binom_pvalue_vs_base_rate(k_eff, round(n_eff), base_rate),
        )

    tested = [r for r in results if r["status"] == "tested"]
    if tested:
        _, qs = stats.fdr_by([r["p_raw"] for r in tested], alpha, fdr_method)
        for r, q in zip(tested, qs, strict=True):
            r["q"] = q
            r["evidence"] = _evidence(q, r["expectancy_r_ci95"][0], alpha)

    return {
        "n_trades": len(trades),
        "base_rate": round(base_rate, 4) if base_rate is not None else None,
        "total_hypotheses": len(grid),
        "tested": len(tested),
        "insufficient_data": len(grid) - len(tested),
        "supported": sum(r["evidence"] == "supported" for r in tested),
        "fdr_method": fdr_method,
        "alpha": alpha,
        "min_observations": min_observations,
        "results": results,
    }
