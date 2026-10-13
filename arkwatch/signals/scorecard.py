"""scorecard.py - per scenario-type track record of the playbook, with evidence tiers.

Scoring is NET of a round-trip cost (config/params_playbook.yaml costs_bps by asset class,
converted to R with the stop distance); expectancy_gross_r and avg_cost_r show the paper figure
and what the cost took. The random-entry draws pay the same cost.

A trade is a resolved scenario that was filled (entry_price set) and closed by the tracker's
rules, the same definition as playbook_tracker.get_playbook_performance_metrics; it scores by its
realized r_multiple (win > 0). Manual cancels (CANCELLED_MANUAL, an operator-chosen exit) are
not rule-based outcomes and are left out. Win rate and its Wilson CI count wins among DECISIVE
trades (R != 0): the breakeven win rate they are compared with is |L| / (W + |L|), a share of
decisive trades, so scratches (0R) must not dilute the numerator's population. Scenario type = the generator's stable `scenario_id` (e.g.
SCENARIO_INTRADAY_SWEEP_SHORT); groups are type x direction x horizon x asset class.

Tiers describe tracked outcomes only. They are not a forecast and promise no future edge:
  unvalidated  n < 20
  supported    n >= 100, Wilson 95% lower bound of the win rate > breakeven win rate
               and session-clustered bootstrap 95% lower bound of expectancy > 0
  rejected     n >= 100, Wilson 95% upper bound of the win rate < breakeven win rate
  emerging     everything else (enough trades to look at, not enough to separate)

Random-entry control (random_entry.py): for groups with n >= 20 each trade is replayed from
random entry bars of the same symbol and window with the same direction and stop / target
geometry. null_mean_r, null_p and beats_random report whether the group's expectancy beats that
null. Within the walk budget each group replays a deterministic sample of at most NULL_SAMPLE
trades. 'supported' requires beats_random True, at least 10 sessions and an effective n of 20:
a group whose null could not be computed is capped at 'emerging' (it used to pass).
"""

from __future__ import annotations

import sqlite3
import zlib
from datetime import UTC, datetime
from functools import cache
from typing import Any

from ..config import _load_yaml
from ..qa.stats import (
    MIN_CLUSTERS,
    binom_pvalue_vs_base_rate,
    cluster_bootstrap_ci,
    effective_n,
    wilson_ci,
)
from .playbook_tracker import _OPEN
from .random_entry import K_MAX, K_MIN, MAX_WALKS, NO_NULL, null_summary, trade_draws

MIN_N, SUPPORT_N = 20, 100
NULL_SAMPLE = 50  # trades per group replayed for the random-entry null
DISCLAIMER = (
    "Tiers summarize tracked outcomes of past scenarios; they are not a forecast and do not"
    " promise future edge. Outcomes are paper fills on 5-minute bars at the level; the"
    " statistics are net of an assumed round-trip cost per asset class (params_playbook.yaml),"
    " not of actual broker fills."
    " ark-watch never trades."
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


@cache
def _costs_bps() -> dict[str, float]:
    return dict(_load_yaml("params_playbook.yaml")["costs_bps"])


def cost_r(bps: float, entry: float | None, stop: float | None) -> float:
    """Round-trip cost in R: bps of the entry over the stop distance (0 without geometry)."""
    if not entry or stop is None or entry == stop:
        return 0.0
    return round(bps / 1e4 * entry / abs(entry - stop), 4)


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


def group_stats(
    rs: list[float], sessions: list[str], resolved: list[str], null: dict | None = None
) -> dict[str, Any]:
    """Stats of one group of trades: realized R, their session ids and resolve timestamps, plus
    the random-entry null summary when it was computed."""
    null = null or NO_NULL
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    decisive = len(wins) + len(losses)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    be = -avg_loss / (avg_win - avg_loss) if avg_win is not None and avg_loss is not None else None
    n_eff_raw = effective_n(rs, sessions) if n else 0.0
    n_eff = round(n_eff_raw, 1)
    # trades inside one session move together: the Wilson interval uses design-effect-scaled
    # counts (x n_eff / n), not the raw n that overstated its precision (audit L7)
    scale = n_eff_raw / n if n else 1.0
    wr_ci = list(wilson_ci(len(wins) * scale, decisive * scale)) if decisive else None
    exp_ci = list(cluster_bootstrap_ci(rs, sessions, seed=0)) if n else None
    k = len(set(sessions))
    warning = None
    if n >= 2 and (n_eff < n / 2 or k < MIN_CLUSTERS):  # an ICC from <10 sessions is noise
        warning = (
            f"{n} trades come from {k} session(s) (effective n ~{n_eff}); outcomes within a"
            " session move together, so the rates are less certain than n suggests"
        )
    if n >= 2 and len(set(rs)) == 1:
        warning = (
            f"all {n} trades returned {rs[0]}R, so the bootstrap has no spread to measure and the"
            " expectancy interval is not a confidence statement"
        )
    tier = _tier(n, wr_ci, be, exp_ci)
    # 'supported' needs independent evidence: a beaten random-entry null and many sessions, not
    # one session's correlated trades
    if tier == "supported" and (
        null["beats_random"] is not True or k < MIN_CLUSTERS or n_eff < MIN_N
    ):
        tier = "emerging"
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "n_sessions": k,
        "effective_n": n_eff,
        "scratches": n - decisive,
        "win_rate": round(len(wins) / decisive, 4) if decisive else None,
        "win_rate_ci95": wr_ci,
        "expectancy_r": round(sum(rs) / n, 4) if n else None,
        "expectancy_ci95": exp_ci,
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "profit_factor": round(sum(wins) / -sum(losses), 4) if losses else None,
        "breakeven_win_rate": round(be, 4) if be is not None else None,
        "p_value_vs_breakeven": binom_pvalue_vs_base_rate(len(wins), decisive, be)
        if be is not None
        else None,
        "tier": tier,
        "sample_warning": warning,
        "last_updated": max(resolved) if resolved else None,
        "calibration": None,
        "calibration_reason": NO_CALIBRATION,
    } | null


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
        "SELECT scenario_id, direction, horizon, symbol, session_id, r_multiple, resolved_at_utc,"
        " scenario_uid, created_at_utc, entry_price, invalidation_level, target_profit"
        f" FROM playbook_scenarios WHERE state NOT IN ({','.join('?' * len(_OPEN))})"
        " AND state <> 'CANCELLED_MANUAL' AND entry_price IS NOT NULL"
        f" AND r_multiple IS NOT NULL{where}",
        (*_OPEN, *params),
    ).fetchall()
    costs = _costs_bps()
    groups: dict[tuple, list[tuple]] = {}
    for sid, d, h, sym, sess, r, res, uid, created, entry, stop, target in rows:
        cls = _classes().get(sym.upper(), "other")
        if asset_class and cls != asset_class.strip().lower():
            continue
        geo = (uid, sym, d, h, sess, created, entry, stop, target)
        cost = cost_r(costs.get(cls, costs.get("other", 0.0)), entry, stop)
        # scored NET of the round-trip cost; the gross paper R rides along for display
        groups.setdefault((sid, d, h, cls), []).append((r - cost, sess, res, geo, cost, r))

    # random-entry null only where a tier can be earned, within the MAX_WALKS budget: each group
    # replays a deterministic sample (by uid hash) so the null survives a growing book
    big = [t for t in groups.values() if len(t) >= MIN_N]
    per_group = min(NULL_SAMPLE, MAX_WALKS // (K_MIN * len(big))) if big else 0
    covered = sum(min(len(t), per_group) for t in big)
    n_draws = min(K_MAX, MAX_WALKS // covered) if covered else 0
    bar_cache: dict = {}

    def null(trades: list[tuple]) -> dict | None:
        if len(trades) < MIN_N or n_draws < K_MIN or not per_group:
            return None
        sample = sorted(trades, key=lambda t: zlib.crc32(t[3][0].encode()))[:per_group]
        # the random entries pay the same cost as the trade they replay: net vs net
        pairs = [(t[0], trade_draws(conn, t[3], n_draws, bar_cache), t[4]) for t in sample]
        pairs = [(r, d - c) for r, d, c in pairs if d is not None]
        return null_summary([r for r, _ in pairs], [d for _, d in pairs]) if pairs else None

    def stats(trades: list[tuple], with_null: bool = False) -> dict[str, Any]:
        rs, sess, res = ([t[i] for t in trades] for i in range(3))
        out = group_stats(rs, sess, [x for x in res if x], null(trades) if with_null else None)
        n = len(trades)
        out["expectancy_gross_r"] = round(sum(t[5] for t in trades) / n, 4) if n else None
        out["avg_cost_r"] = round(sum(t[4] for t in trades) / n, 4) if n else None
        return out

    out = [
        {"scenario_type": k[0], "direction": k[1], "horizon": k[2], "asset_class": k[3]}
        | stats(t, with_null=True)
        for k, t in sorted(groups.items())
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "disclaimer": DISCLAIMER,
        "groups": out,
        "overall": stats([t for ts in groups.values() for t in ts]),
    }
