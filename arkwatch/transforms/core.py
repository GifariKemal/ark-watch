"""core.py — base transforms: z-score, momentum, percentile, state.

Conventions: population sigma; 5y window scaled to the series frequency;
minimum observations 80% of the window; state flip at ±0.5; momentum-N =
N trading days (D series) or N months (M series).
"""

from __future__ import annotations

import math


def zscore(values: list[float], window: int = 1260, min_frac: float = 0.8) -> float | None:
    """Population z-score of the latest value against the trailing window."""
    if not values:
        return None
    recent = values[-window:]
    min_obs = int(window * min_frac)
    if len(recent) < min_obs:
        return None
    x = recent[-1]
    mean = sum(recent) / len(recent)
    var = sum((v - mean) ** 2 for v in recent) / len(recent)  # population
    std = math.sqrt(var) if var > 0 else 0
    # a constant series leaves float residue (3.63 x 1260 -> var 2e-31, z = -1.0): relative test
    if std <= 1e-12 * max(1.0, abs(mean)):
        return None
    return (x - mean) / std


def percentile_rank(values: list[float], window: int = 1260, min_frac: float = 0.8) -> float | None:
    """Percentile rank (0-100) of the latest value within the window.

    The min-observation guard is required: without it a nearly-empty series
    would still produce a confident-looking rank."""
    if not values:
        return None
    recent = values[-window:]
    if len(recent) < int(window * min_frac):
        return None
    x = recent[-1]
    below = sum(1 for v in recent if v <= x)
    return below / len(recent) * 100


def momentum(values: list[float], n: int = 20) -> float | None:
    """x[t] - x[t-n]. For D series n is trading days; for M series, months."""
    if len(values) <= n:
        return None
    return values[-1] - values[-1 - n]


def annualize_3m(m1: float, m2: float, m3: float) -> float:
    """3m-annualized rate from 3 monthly growth rates (decimals):
    ((1+m1)(1+m2)(1+m3))^4 - 1. Exponent 4 = 12/3.
    """
    return ((1 + m1) * (1 + m2) * (1 + m3)) ** 4 - 1


def state_from_z(z: float | None, thresholds: tuple[float, float] = (-0.5, 0.5)) -> str:
    """State from a z-score with ±0.5 thresholds."""
    if z is None:
        return "INSUFFICIENT"
    lo, hi = thresholds
    if z < lo:
        return "LOW"
    if z > hi:
        return "HIGH"
    return "NEUTRAL"


def state_direction(m: float | None, threshold: float = 0.0) -> str:
    """State from momentum direction: RISING/FALLING/FLAT."""
    if m is None:
        return "INSUFFICIENT"
    if m > threshold:
        return "RISING"
    if m < -threshold:
        return "FALLING"
    return "FLAT"
