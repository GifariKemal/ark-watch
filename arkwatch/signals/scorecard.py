"""scorecard.py - per scenario-type track record of the playbook, with evidence tiers.

A trade is a resolved scenario that was filled (entry_price set), the same definition as
playbook_tracker.get_playbook_performance_metrics; it scores by its realized r_multiple
(win > 0). Scenario type = the generator's stable `scenario_id` (e.g.
SCENARIO_INTRADAY_SWEEP_SHORT); groups are type x direction x horizon x asset class.

Tiers describe tracked outcomes only. They are not a forecast and promise no future edge:
  unvalidated  n < 20
  supported    n >= 100, Wilson 95% lower bound of the win rate > breakeven win rate
               and session-clustered bootstrap 95% lower bound of expectancy > 0
  rejected     n >= 100, Wilson 95% upper bound of the win rate < breakeven win rate
  emerging     everything else (enough trades to look at, not enough to separate)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from functools import cache
from typing import Any

from ..config import _load_yaml
from ..qa.stats import binom_pvalue_vs_base_rate, cluster_bootstrap_ci, effective_n, wilson_ci
from .playbook_tracker import _OPEN

MIN_N, SUPPORT_N = 20, 100
DISCLAIMER = (
    "Tiers summarize tracked outcomes of past scenarios; they are not a forecast and do not"
    " promise future edge. ark-watch never trades."
)
NO_CALIBRATION = (
    "scenarios carry no claimed win probability (empirical_support rates describe the setup,"
    " not the trade outcome), so there is nothing to calibrate"
)


@cache
def _classes() -> dict[str, str]:
    return {
        i["symbol"]: i.get("class", "other") for i in _load_yaml("instruments.yaml")["instruments"]
    }


def asset_class(symbol: str) -> str:
    return _classes().get(symbol.strip().upper(), "other")


def _tier(n: int, wr_ci, be: float | None, exp_ci) -> str:
    if n < MIN_N:
        return "unvalidated"
    if n >= SUPPORT_N and be is not None:
        if wr_ci[0] > be and exp_ci[0] > 0:
            return "supported"
        if wr_ci[1] < be:
            return "rejected"
    return "emerging"


def group_stats(rs: list[float], sessions: list[str], resolved: list[str]) -> dict[str, Any]:
    """Stats of one group of trades: realized R, their session ids and resolve timestamps."""
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    be = -avg_loss / (avg_win - avg_loss) if wins and losses else None
    wr_ci = list(wilson_ci(len(wins), n)) if n else None
    exp_ci = list(cluster_bootstrap_ci(rs, sessions, seed=0)) if n else None
    n_eff = round(effective_n(rs, sessions), 1) if n else 0.0
    k = len(set(sessions))
    warning = None
    if n >= 2 and n_eff < n / 2:
        warning = (
            f"{n} trades come from {k} session(s) (effective n ~{n_eff}); outcomes within a"
            " session move together, so the rates are less certain than n suggests"
        )
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "n_sessions": k,
        "effective_n": n_eff,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "win_rate_ci95": wr_ci,
        "expectancy_r": round(sum(rs) / n, 4) if n else None,
        "expectancy_ci95": exp_ci,
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "profit_factor": round(sum(wins) / -sum(losses), 4) if losses else None,
        "breakeven_win_rate": round(be, 4) if be is not None else None,
        "p_value_vs_breakeven": binom_pvalue_vs_base_rate(len(wins), n, be)
        if be is not None
        else None,
        "tier": _tier(n, wr_ci, be, exp_ci),
        "sample_warning": warning,
        "last_updated": max(resolved) if resolved else None,
        "calibration": None,
        "calibration_reason": NO_CALIBRATION,
    }


def compute_scorecard(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    horizon: str | None = None,
    direction: str | None = None,
    asset_class: str | None = None,
) -> dict[str, Any]:
    where, params = "", []
    for col, val in (("symbol", symbol), ("horizon", horizon), ("direction", direction)):
        if val:
            where += f" AND {col} = ?"  # col is an internal literal
            params.append(val.strip().upper())
    rows = conn.execute(
        "SELECT scenario_id, direction, horizon, symbol, session_id, r_multiple, resolved_at_utc"
        f" FROM playbook_scenarios WHERE state NOT IN ({','.join('?' * len(_OPEN))})"
        f" AND entry_price IS NOT NULL{where}",
        (*_OPEN, *params),
    ).fetchall()
    groups: dict[tuple, list[tuple]] = {}
    for sid, d, h, sym, sess, r, res in rows:
        cls = _classes().get(sym.upper(), "other")
        if asset_class and cls != asset_class.strip().lower():
            continue
        groups.setdefault((sid, d, h, cls), []).append((r or 0.0, sess, res))

    def stats(trades: list[tuple]) -> dict[str, Any]:
        rs, sess, res = (list(x) for x in zip(*trades, strict=True)) if trades else ([], [], [])
        return group_stats(rs, sess, [x for x in res if x])

    out = [
        {"scenario_type": k[0], "direction": k[1], "horizon": k[2], "asset_class": k[3]} | stats(t)
        for k, t in sorted(groups.items())
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "disclaimer": DISCLAIMER,
        "groups": out,
        "overall": stats([t for ts in groups.values() for t in ts]),
    }
