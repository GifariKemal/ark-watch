"""random_entry.py - random-entry control (null) for the playbook scorecard.

A setup type shows edge only if its expectancy beats what the same trade geometry earns from a
random entry: drift alone makes many volume-profile / AMT setups look positive. For each filled
scenario we draw K entry bars uniformly from the same symbol's 5m bars inside the scenario's own
opportunity window [created_at, session expiry), keep its direction and its stop / target
distances as a fraction of the fill, enter at the bar open and resolve with the tracker's own
`_simulate` / `_pnl` (partial at +1R, breakeven ratchet, VWAP early exit, stop-first ties, time
exit at the last bar close up to expiry). Not modelled: news-shock stops and FLIPPED exits (the
control has no catalyst feed and no opposite scenario).

Draw j of every trade forms null replicate j; the replicate's mean R is one null expectancy.
null_p = (1 + #(null expectancy >= observed)) / (K + 1); beats_random = null_p < 0.05.
Seeded per scenario_uid, so results do not depend on row order.
"""

from __future__ import annotations

import sqlite3
import zlib
from typing import Any

import numpy as np

from .playbook_tracker import _bars, _expiry, _pnl, _simulate

# Bar walks per scorecard computation: ~0.15 ms each on prod bars, ~0.8 s at the cap on 1 vCPU.
# ponytail: past 250 eligible trades K drops below K_MIN and the null is skipped; subsample
# trades per group (or vectorize _simulate) when the book gets there.
MAX_WALKS = 5000
K_MAX, K_MIN = 200, 20  # draws per trade; below K_MIN the smallest reachable p is > 0.05


def walk_r(direction: str, stop_frac: float, tgt_frac: float, bars: list[tuple], i: int) -> float:
    """R of a market entry at bars[i] open with the given stop / target distances (fractions)."""
    e = bars[i][1]
    sign = 1.0 if direction == "LONG" else -1.0
    stop, target = e - sign * stop_frac * e, e + sign * tgt_frac * e
    sim = _simulate(direction, e, stop, target, bars[i:])
    exit_p = sim["exit"] if sim["outcome"] else sim["last_close"]
    risk = abs(e - stop) or 1.0
    return round(_pnl(direction, e, exit_p, risk, bool(sim["partial_ts"])) / risk, 2)


def trade_draws(conn: sqlite3.Connection, trade: tuple, k: int, cache: dict) -> np.ndarray | None:
    """K random-entry R draws for one trade (uid, sym, direction, horizon, session, created,
    entry, stop, target); None when the window has no bars or the geometry is degenerate."""
    uid, sym, direction, horizon, session, created, entry, stop, target = trade
    expiry = _expiry(horizon, session)
    if expiry is None or not entry or not stop or not target or direction not in ("LONG", "SHORT"):
        return None
    end = expiry.isoformat(timespec="seconds")
    if (sym, created, end) not in cache:
        cache[sym, created, end] = _bars(conn, sym, created, end)
    bars = cache[sym, created, end]
    n_entry = sum(b[0] < end for b in bars)
    stop_frac, tgt_frac = abs(entry - stop) / entry, abs(target - entry) / entry
    if not n_entry or not stop_frac:
        return None
    rng = np.random.default_rng([0, zlib.crc32(uid.encode())])
    return np.array(
        [
            walk_r(direction, stop_frac, tgt_frac, bars, int(i))
            for i in rng.integers(n_entry, size=k)
        ]
    )


def null_summary(observed: list[float], draws: list[np.ndarray]) -> dict[str, Any]:
    """Compare the observed mean R of the covered trades with the null replicate means."""
    null = np.vstack(draws).mean(axis=0)
    obs = float(np.mean(observed))
    p = (1 + int(np.sum(null >= obs - 1e-9))) / (len(null) + 1)
    return {
        "null_n": len(draws),
        "null_draws": len(null),
        "null_mean_r": round(float(null.mean()), 4),
        "null_p": round(p, 4),
        "beats_random": p < 0.05,
    }


NO_NULL = {"null_n": 0, "null_draws": 0, "null_mean_r": None, "null_p": None, "beats_random": None}
