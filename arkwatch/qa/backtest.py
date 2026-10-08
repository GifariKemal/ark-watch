"""backtest.py — regime → per-instrument next-day return hit-rate.

Each price day D is labeled with the regime (VIX / HY OAS / 20-obs DFII10
momentum) KNOWN at the close of D, then scored on the close(D) → close(next
trading day) return.

Point-in-time rule (`lag_days`, default 1): FRED daily values for date d are
published the following business day, so the regime value of date d is only
used for decisions on or after d + lag_days calendar days — i.e. for returns
starting at close(d+1). lag_days=0 reproduces the old same-day labeling,
which looks ahead. Caveat: raw_observations holds the latest (revised)
realtime vintage, not the first-release value; HY OAS / VIX / DFII revisions
are rare and small but this is not a strict as-first-published backtest.

The default thresholds are fixed a priori (not fitted). walk_forward() instead
picks quantile thresholds on an expanding training window and scores only the
following block (out-of-sample).
"""

from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from .. import db
from . import stats

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

INSTRUMENTS = {
    "XAUUSD": ("XAUUSD", "EODHD"),
    "BTC": ("BTCUSD", "EODHD"),
    "US500": ("US500", "YAHOO"),
    "DXY": ("DXY", "YAHOO"),
}
# (vix_stress, hy_stress, dfii_mom_falling, dfii_mom_rising)
DEFAULT_THRESHOLDS = (25.0, 5.0, -0.05, 0.05)
MAX_STALE_DAYS = 7  # regime older than this at decision time -> day skipped

Obs = tuple[str, str, float, float, float, float]  # (date, inst, ret%, vix, hy, dfii_mom)


def _prices(conn: sqlite3.Connection, symbol: str, source: str) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT ts, close FROM instrument_prices WHERE symbol=? AND source=? "
        "AND close IS NOT NULL ORDER BY ts",
        (symbol, source),
    ).fetchall()
    return [(r[0][:10], r[1]) for r in rows]


def _series_daily(conn: sqlite3.Connection, sid: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? "
        "AND vintage_ts='realtime' ORDER BY ts",
        (sid,),
    ).fetchall()
    return {r[0][:10]: r[1] for r in rows if r[1] is not None}


def _regime_label(
    vix: float, hy: float, dfii_m: float, th: tuple[float, ...] = DEFAULT_THRESHOLDS
) -> str:
    """Simple classification based on VIX + HY + RY momentum."""
    if vix > th[0]:
        return "STRESS"
    if hy > th[1]:
        return "CREDIT_STRESS"
    if dfii_m < th[2]:
        return "RY_FALLING"
    if dfii_m > th[3]:
        return "RY_RISING"
    return "NEUTRAL"


def _observations(conn: sqlite3.Connection, lag_days: int = 1) -> list[Obs]:
    """Point-in-time (decision_date, instrument, next-day return %, regime inputs)."""
    vix = _series_daily(conn, "FRED:VIXCLS")
    hy = _series_daily(conn, "FRED:BAMLH0A0HYM2")
    dfii = _series_daily(conn, "FRED:DFII10")
    dfii_dates = sorted(dfii)
    dfii_mom = {
        dfii_dates[i]: dfii[dfii_dates[i]] - dfii[dfii_dates[i - 20]]
        for i in range(20, len(dfii_dates))
    }
    regime_dates = sorted(set(vix) & set(hy) & set(dfii_mom))

    obs: list[Obs] = []
    for inst, (sym, src) in INSTRUMENTS.items():
        px = _prices(conn, sym, src)
        for (d, cur), (_, nxt) in zip(px, px[1:], strict=False):
            if not cur:
                continue
            known = date.fromisoformat(d) - timedelta(days=lag_days)
            i = bisect.bisect_right(regime_dates, known.isoformat()) - 1
            if i < 0 or (known - date.fromisoformat(regime_dates[i])).days > MAX_STALE_DAYS:
                continue
            r = regime_dates[i]
            obs.append((d, inst, (nxt - cur) / cur * 100, vix[r], hy[r], dfii_mom[r]))
    return obs


def _aggregate(labeled: list[tuple[str, str, float]]) -> dict[str, dict[str, dict]]:
    """[(regime, inst, ret%)] → {regime: {inst: {n, wins, total_ret, win_rate, ...}}}."""
    results: dict[str, dict[str, dict]] = {}
    for regime, inst, ret in labeled:
        r = results.setdefault(regime, {}).setdefault(inst, {"n": 0, "wins": 0, "total_ret": 0.0})
        r["n"] += 1
        r["total_ret"] += ret
        r["wins"] += ret > 0
    for per_inst in results.values():
        for r in per_inst.values():
            r["win_rate"] = r["wins"] / r["n"] * 100
            r["win_rate_ci95"] = tuple(x * 100 for x in stats.wilson_ci(r["wins"], r["n"]))
            r["avg_return"] = r["total_ret"] / r["n"]
    return results


def run_backtest(conn: sqlite3.Connection, *, lag_days: int = 1) -> dict[str, dict[str, dict]]:
    """Return {regime: {instrument: {n, wins, win_rate, win_rate_ci95, avg_return, total_ret}}}."""
    return _aggregate(
        [
            (_regime_label(v, h, m), inst, ret)
            for _, inst, ret, v, h, m in _observations(conn, lag_days)
        ]
    )


def walk_forward(obs: list[Obs], n_blocks: int = 4) -> dict:
    """Expanding-window walk-forward: dates split into n_blocks+1 contiguous
    blocks; for fold k, thresholds = quantiles (VIX q80, HY q80, DFII mom
    q20/q80) of blocks < k only, applied to block k. Only out-of-sample
    observations are aggregated."""
    dates = sorted({o[0] for o in obs})
    edges = [round(i * len(dates) / (n_blocks + 1)) for i in range(n_blocks + 2)]
    labeled, folds = [], []
    for k in range(1, n_blocks + 1):
        train_end, test_start, test_end = (
            dates[edges[k] - 1],
            dates[edges[k]],
            dates[edges[k + 1] - 1],
        )
        train = [o for o in obs if o[0] <= train_end]
        th = (
            float(np.quantile([o[3] for o in train], 0.8)),
            float(np.quantile([o[4] for o in train], 0.8)),
            float(np.quantile([o[5] for o in train], 0.2)),
            float(np.quantile([o[5] for o in train], 0.8)),
        )
        folds.append(
            {"train": (dates[0], train_end), "test": (test_start, test_end), "thresholds": th}
        )
        labeled += [
            (_regime_label(o[3], o[4], o[5], th), o[1], o[2])
            for o in obs
            if test_start <= o[0] <= test_end
        ]
    return {"folds": folds, "results": _aggregate(labeled)}


def _fdr_guardrail(
    results: dict, *, method: str = "fdr_by", alpha: float = 0.05, min_n: int = 10
) -> list[dict]:
    """Multiple-testing guardrail. Hossfeld & Röthig 2016 showed COT "predictive"
    findings collapse after correction; one test per regime×instrument cell.

    Null = the instrument's own base rate of up-days across the whole sample
    (not 0.5: gold/BTC/US500 drift up). Benjamini-Yekutieli by default (cells
    overlap in time → dependent tests); method="fdr_bh" optional.
    """
    up: dict[str, list[int]] = {}
    for per_inst in results.values():
        for inst, r in per_inst.items():
            acc = up.setdefault(inst, [0, 0])
            acc[0] += r["wins"]
            acc[1] += r["n"]

    cells = [
        (f"{regime}/{inst}", r, up[inst][0] / up[inst][1])
        for regime, per_inst in results.items()
        for inst, r in per_inst.items()
        if r["n"] >= min_n
    ]
    if not cells:
        return []
    pvals = [stats.binom_pvalue_vs_base_rate(r["wins"], r["n"], base) for _, r, base in cells]
    rejected, q = stats.fdr_by(pvals, alpha, method)
    return [
        {
            "signal": label,
            "base_rate": round(base, 4),
            "win_rate_ci95": stats.wilson_ci(r["wins"], r["n"]),
            "p_raw": round(p, 4),
            "p_fdr": round(qv, 4),
            "significant_after_fdr": sig,
        }
        for (label, r, base), p, qv, sig in zip(cells, pvals, q, rejected, strict=True)
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch backtest")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--lag-days", type=int, default=1)
    p.add_argument("--walk-forward", type=int, default=0, metavar="BLOCKS")
    p.add_argument("--fdr", choices=("fdr_by", "fdr_bh"), default="fdr_by")
    a = p.parse_args(argv)
    conn = db.get_conn(a.db)
    try:
        if a.walk_forward:
            results = walk_forward(_observations(conn, a.lag_days), a.walk_forward)["results"]
        else:
            results = run_backtest(conn, lag_days=a.lag_days)
    finally:
        conn.close()

    mode = (
        f"walk-forward {a.walk_forward} blocks (OOS only)" if a.walk_forward else "fixed thresholds"
    )
    print(f"=== BACKTEST: Regime → Instrument Returns ({mode}, lag={a.lag_days}d) ===\n")
    for regime in sorted(results.keys()):
        print(f"  {regime}:")
        for inst in sorted(results[regime].keys()):
            r = results[regime][inst]
            if r["n"] < 5:
                continue
            lo, hi = r["win_rate_ci95"]
            print(
                f"    {inst:<8} n={r['n']:>4}  win={r['win_rate']:.0f}% [{lo:.0f}-{hi:.0f}]  "
                f"avg={r['avg_return']:+.2f}%/day  total={r['total_ret']:+.1f}%"
            )

    fdr = _fdr_guardrail(results, method=a.fdr)
    if fdr:
        print(f"\n=== FDR GUARDRAIL ({a.fdr} α=0.05, null = instrument up-day base rate) ===")
        n_sig = sum(1 for f in fdr if f["significant_after_fdr"])
        print(f"  {n_sig}/{len(fdr)} signals PASS after multiple-testing correction:\n")
        for f in fdr:
            flag = "✅" if f["significant_after_fdr"] else "❌"
            print(
                f"  {flag} {f['signal']:<28} base={f['base_rate']:.2f} "
                f"p={f['p_raw']:.4f} → q={f['p_fdr']:.4f}"
            )
        if n_sig == 0:
            print("\n  ⚠ NO signals pass FDR — all findings are likely noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
