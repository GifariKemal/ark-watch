"""stats.py — small, pure inference helpers shared by the backtest harnesses.

Each function is deliberately thin over scipy/statsmodels/numpy so the
statistics are the library's, not hand-rolled.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence

import numpy as np
from scipy import stats as scipy_stats
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.proportion import proportion_confint


def wilson_ci(wins: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    lo, hi = proportion_confint(wins, n, alpha=alpha, method="wilson")
    return float(lo), float(hi)


def binom_pvalue_vs_base_rate(wins: int, n: int, base_rate: float) -> float:
    """One-sided P(X >= wins | n, base_rate) — is the hit-rate above the base rate?"""
    return float(scipy_stats.binomtest(wins, n, base_rate, alternative="greater").pvalue)


def fdr_by(
    pvals: Sequence[float], alpha: float = 0.05, method: str = "fdr_by"
) -> tuple[list[bool], list[float]]:
    """(rejected, q). Benjamini-Yekutieli by default (valid under any dependence
    between tests, which overlapping hypothesis cells have); "fdr_bh" optional."""
    rejected, q, _, _ = multipletests(list(pvals), alpha=alpha, method=method)
    return [bool(r) for r in rejected], [float(x) for x in q]


def _groups(values: Sequence[float], clusters: Sequence[Hashable]) -> list[np.ndarray]:
    by: dict[Hashable, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        by.setdefault(c, []).append(float(v))
    return [np.asarray(g) for g in by.values()]


def effective_n(values: Sequence[float], clusters: Sequence[Hashable]) -> float:
    """Kish n_eff = n / (1 + (m-1)*ICC), ICC from one-way ANOVA, ICC floored at 0
    (never inflates n) and n_eff floored at n_clusters."""
    groups = _groups(values, clusters)
    n, k = len(values), len(groups)
    if k <= 1 or k == n:
        return float(k)
    y = np.concatenate(groups)
    sizes = np.array([len(g) for g in groups])
    means = np.array([g.mean() for g in groups])
    msb = float((sizes * (means - y.mean()) ** 2).sum() / (k - 1))
    msw = float(sum(((g - g.mean()) ** 2).sum() for g in groups) / (n - k))
    n0 = (n - (sizes**2).sum() / n) / (k - 1)
    denom = msb + (n0 - 1) * msw
    icc = max(0.0, (msb - msw) / denom) if denom > 0 else 0.0
    return float(max(n / (1 + (n / k - 1) * icc), k))


def cluster_bootstrap_ci(
    values: Sequence[float],
    clusters: Sequence[Hashable],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile CI of the pooled mean, resampling whole clusters (sessions)."""
    groups = _groups(values, clusters)
    sums = np.array([g.sum() for g in groups])
    counts = np.array([len(g) for g in groups])
    idx = np.random.default_rng(seed).integers(0, len(groups), size=(n_boot, len(groups)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)
