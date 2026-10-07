"""backtest.py — regime → per-instrument return hit-rate.

Classify each day's regime (from pillar z-scores) → next-day return of
XAUUSD/BTC/US500 → aggregate per regime. No look-ahead: returns are computed
from the day-H vs day-H+1 closing prices.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

INSTRUMENTS = {
    "XAUUSD": ("XAUUSD", "EODHD"),
    "BTC": ("BTCUSD", "EODHD"),
    "US500": ("US500", "YAHOO"),
    "DXY": ("DXY", "YAHOO"),
}


def _prices(conn: sqlite3.Connection, symbol: str, source: str) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT ts, close FROM instrument_prices WHERE symbol=? AND source=? "
        "AND close IS NOT NULL ORDER BY ts",
        (symbol, source),
    ).fetchall()
    return [(r[0], r[0] and r[1]) for r in rows]


def _series_daily(conn: sqlite3.Connection, sid: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT ts, value FROM raw_observations WHERE series_id=? "
        "AND vintage_ts='realtime' ORDER BY ts",
        (sid,),
    ).fetchall()
    return {r[0][:10]: r[1] for r in rows if r[1] is not None}


def _regime_label(vix: float, hy: float, dfii_m: float) -> str:
    """Simple classification based on VIX + HY + RY momentum."""
    if vix > 25:
        return "STRESS"
    if hy > 5.0:
        return "CREDIT_STRESS"
    if dfii_m < -0.05:
        return "RY_FALLING"
    if dfii_m > 0.05:
        return "RY_RISING"
    return "NEUTRAL"


def run_backtest(conn: sqlite3.Connection) -> dict[str, dict[str, dict]]:
    """Return {regime: {instrument: {n, win_rate, avg_return, total_return}}}."""
    # Regime input series
    vix = _series_daily(conn, "FRED:VIXCLS")
    hy = _series_daily(conn, "FRED:BAMLH0A0HYM2")
    dfii = _series_daily(conn, "FRED:DFII10")

    # 20-day DFII momentum
    dfii_dates = sorted(dfii.keys())
    dfii_mom: dict[str, float] = {}
    for i in range(20, len(dfii_dates)):
        d = dfii_dates[i]
        dfii_mom[d] = dfii[d] - dfii[dfii_dates[i - 20]]

    # Instrument prices
    prices: dict[str, list[tuple[str, float]]] = {}
    for name, (sym, src) in INSTRUMENTS.items():
        prices[name] = _prices(conn, sym, src)

    # Convert to dicts for fast lookup
    pdict: dict[str, dict[str, float]] = {}
    for name, plist in prices.items():
        pdict[name] = {ts[:10]: v for ts, v in plist}

    # Classify each day → next-day return; only use dates where VIX + HY +
    # DFII momentum are all available
    common_dates = set(vix.keys()) & set(hy.keys()) & set(dfii_mom.keys())
    common_dates = sorted(common_dates)

    results: dict[str, dict[str, dict]] = {}
    for d in common_dates:
        regime = _regime_label(vix[d], hy[d], dfii_mom[d])
        if regime not in results:
            results[regime] = {}

        # Find prices on day d and the nearest following trading day
        for inst_name in INSTRUMENTS:
            pmap = pdict.get(inst_name, {})
            if d not in pmap:
                continue
            cur_price = pmap[d]
            # Next trading day that has a price
            next_dates = [x for x in sorted(pmap.keys()) if x > d]
            if not next_dates:
                continue
            next_d = next_dates[0]
            next_price = pmap[next_d]

            if cur_price == 0:
                continue
            ret = (next_price - cur_price) / cur_price * 100

            if inst_name not in results[regime]:
                results[regime][inst_name] = {"n": 0, "wins": 0, "total_ret": 0.0}
            r = results[regime][inst_name]
            r["n"] += 1
            r["total_ret"] += ret
            if ret > 0:
                r["wins"] += 1

    # Compute aggregates
    for regime in results:
        for inst in results[regime]:
            r = results[regime][inst]
            r["win_rate"] = r["wins"] / r["n"] * 100 if r["n"] > 0 else 0
            r["avg_return"] = r["total_ret"] / r["n"] if r["n"] > 0 else 0

    return results


def _fdr_guardrail(results: dict) -> list[dict]:
    """FDR guardrail. Hossfeld & Röthig 2016 showed COT "predictive"
    findings collapse after multiple-testing correction; with 13 contracts ×
    8 categories this is mandatory before trusting any signal.

    Benjamini-Hochberg FDR: controls the expected proportion of false discoveries.
    """
    try:
        from scipy import stats as scipy_stats
        from statsmodels.stats.multitest import multipletests
    except ImportError:
        return [{"warning": "scipy/statsmodels not installed — FDR guardrail inactive"}]

    # Collect one p-value per regime×instrument return
    pvals = []
    labels = []
    for regime in results:
        for inst in results[regime]:
            r = results[regime][inst]
            if r["n"] < 10:
                continue
            # Significance test of the average return: only aggregates are
            # retained, so use a binomial test of win-rate > 50% as the proxy
            wins = r["wins"]
            n = r["n"]
            p_val = 1 - scipy_stats.binom.cdf(wins - 1, n, 0.5)
            pvals.append(p_val)
            labels.append(f"{regime}/{inst}")

    if not pvals:
        return []

    # Benjamini-Hochberg FDR correction
    rejected, adjusted_p, _, _ = multipletests(pvals, alpha=0.05, method="fdr_bh")

    out = []
    for label, p_raw, p_adj, sig in zip(labels, pvals, adjusted_p, rejected, strict=False):
        out.append(
            {
                "signal": label,
                "p_raw": round(p_raw, 4),
                "p_fdr": round(p_adj, 4),
                "significant_after_fdr": sig,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch backtest")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    conn = sqlite3.connect(a.db)
    results = run_backtest(conn)
    conn.close()

    print("=== BACKTEST: Regime → Instrument Returns ===\n")
    for regime in sorted(results.keys()):
        print(f"  {regime}:")
        for inst in sorted(results[regime].keys()):
            r = results[regime][inst]
            if r["n"] < 5:
                continue
            print(
                f"    {inst:<8} n={r['n']:>4}  win={r['win_rate']:.0f}%  "
                f"avg={r['avg_return']:+.2f}%/day  total={r['total_ret']:+.1f}%"
            )

    fdr = _fdr_guardrail(results)
    if fdr:
        if "warning" in fdr[0]:
            print(f"\n=== FDR GUARDRAIL ===\n  ⚠ {fdr[0]['warning']}")
            return 0
        print("\n=== FDR GUARDRAIL (Benjamini-Hochberg α=0.05) ===")
        n_sig = sum(1 for f in fdr if f.get("significant_after_fdr"))
        print(f"  {n_sig}/{len(fdr)} signals PASS after multiple-testing correction:\n")
        for f in fdr:
            flag = "✅" if f.get("significant_after_fdr") else "❌"
            print(
                f"  {flag} {f.get('signal', 'N/A'):<28} p={f['p_raw']:.4f} → FDR={f['p_fdr']:.4f}"
            )
        if n_sig == 0:
            print("\n  ⚠ NO signals pass FDR — all findings are likely noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
