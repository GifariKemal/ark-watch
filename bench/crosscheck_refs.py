"""Cross-check ark-watch against popular reference libraries on the same data.

Usage (throwaway venv with the references, repo on PYTHONPATH, read-only DB copy):
    pip install git+https://github.com/bfolkens/py-market-profile cot_reports
    python bench/crosscheck_refs.py /path/snapshot.db

A) Value area: amt.compute_value_area vs market_profile (mode='vol', same row size), per CME
   session on stored 5m bars. The reference puts a bar's whole volume at its close; ours spreads
   it over high-low, so deltas measure method spread. The invariant that holds for any method is
   checked on ours: volume inside [VAL, VAH] must reach the 70% target.
B) COT: cot_raw long/short vs cot_reports (CFTC files) for the latest stored report week.
"""

import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

import pandas as pd
from market_profile import MarketProfile

from arkwatch.signals.amt import ASSET_TICK_SIZES, compute_value_area
from arkwatch.signals.levels import _get_cme_session_id

db = sys.argv[1]
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def coverage(bars, val, vah):
    """Share of volume inside [val, vah] with each bar's volume spread uniformly over high-low."""
    tot = inside = 0.0
    for _ts, _o, h, lo, _c, v in bars:
        vol = max(1.0, v or 1.0)
        tot += vol
        if h <= lo:
            inside += vol if val <= h <= vah else 0.0
        else:
            inside += vol * max(0.0, min(h, vah) - max(lo, val)) / (h - lo)
    return inside / tot if tot else 0.0


# --- A) value area ------------------------------------------------------------------------
print("=== A) value area vs market_profile (per CME session, last 10 complete sessions)")
rows_out, short_va = [], []
for sym in sorted(ASSET_TICK_SIZES):
    bars = c.execute(
        "SELECT bar_ts_utc, open, high, low, close, volume FROM intraday_bars"
        " WHERE symbol=? AND interval='5m' AND source='YAHOO' ORDER BY bar_ts_utc",
        (sym,),
    ).fetchall()
    sessions = defaultdict(list)
    for b in bars:
        if None not in b[1:5]:
            sessions[_get_cme_session_id(b[0])].append(b)
    done = [s for s in sorted(sessions) if len(sessions[s]) >= 100][-11:-1]  # skip the live one
    for sid in done:
        sb = sessions[sid]
        ours = compute_value_area(sb, tick_size=ASSET_TICK_SIZES.get(sym))
        bin_size = ours["bin_size"]
        df = pd.DataFrame(
            [
                (datetime.fromisoformat(b[0]), b[1], b[2], b[3], b[4], max(1.0, b[5] or 1.0))
                for b in sb
            ],
            columns=["ts", "Open", "High", "Low", "Close", "Volume"],
        ).set_index("ts")
        ref = MarketProfile(df, tick_size=bin_size, mode="vol")[df.index[0] : df.index[-1]]
        rval, rvah = ref.value_area
        rng = max(b[2] for b in sb) - min(b[3] for b in sb)
        cov = coverage(sb, ours["val"], ours["vah"])
        if cov < 0.70 - 1e-6:
            short_va.append((sym, sid, round(cov, 3)))
        rows_out.append(
            {
                "sym": sym,
                "dpoc": abs(ours["poc"] - ref.poc_price) / rng * 100,
                "dvah": abs(ours["vah"] - rvah) / rng * 100,
                "dval": abs(ours["val"] - rval) / rng * 100,
                "bins_poc": abs(ours["poc"] - ref.poc_price) / bin_size,
                "cov": cov,
            }
        )

if rows_out:
    for k, label in (("dpoc", "POC"), ("dvah", "VAH"), ("dval", "VAL")):
        v = sorted(r[k] for r in rows_out)
        print(
            f"  |d{label}| as % of session range: median {statistics.median(v):.1f}%,"
            f" p90 {v[int(len(v) * 0.9) - 1]:.1f}%, max {v[-1]:.1f}%"
        )
    print(f"  |dPOC| in bins: median {statistics.median(r['bins_poc'] for r in rows_out):.1f}")
    covs = [r["cov"] for r in rows_out]
    print(
        f"  ours VA volume coverage: min {min(covs):.3f}, median {statistics.median(covs):.3f}"
        f" (target 0.70) over {len(rows_out)} sessions, {len({r['sym'] for r in rows_out})} symbols"
    )
    print(f"  sessions where ours covers < 70%: {len(short_va)} {short_va[:8]}")

# --- B) COT --------------------------------------------------------------------------------
print("\n=== B) COT vs cot_reports (latest stored report week)")
import cot_reports as cot  # noqa: E402

latest = c.execute("SELECT MAX(report_date) FROM cot_raw").fetchone()[0]
year = int(latest[:4])
SPEC = {
    "tff": (
        "traders_in_financial_futures_fut",
        {"dealer": "dealer", "am": "asset_mgr", "lev": "lev_money", "other": "other_rept"},
    ),
    "tff_c": (
        "traders_in_financial_futures_futopt",
        {"dealer": "dealer", "am": "asset_mgr", "lev": "lev_money", "other": "other_rept"},
    ),
    "disagg": (
        "disaggregated_fut",
        {
            "prod": "prod_merc",
            "swap": "swap",
            "mm": "m_money",
            "other": "other_rept",
            "nonrep": "nonrept",
        },
    ),
    "disagg_c": (
        "disaggregated_futopt",
        {
            "prod": "prod_merc",
            "swap": "swap",
            "mm": "m_money",
            "other": "other_rept",
            "nonrep": "nonrept",
        },
    ),
}


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


checked = mismatched = missing = 0
examples = []
for rtype, (lib_type, cats) in SPEC.items():
    df = cot.cot_year(year=year, cot_report_type=lib_type)
    df.columns = [norm(x) for x in df.columns]
    date_col = next(x for x in df.columns if "yyyy_mm_dd" in x)
    code_col = next(x for x in df.columns if "contract_market_code" in x)
    df[date_col] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
    df[code_col] = df[code_col].astype(str).str.strip()
    wk = df[df[date_col] == latest]
    for code, cat, lng, sht in c.execute(
        "SELECT contract_code, category, long, short FROM cot_raw WHERE report_date=? AND report_type=?",
        (latest, rtype),
    ):
        row = wk[wk[code_col] == code]
        prefix = cats.get(cat)
        if row.empty or prefix is None:
            missing += 1
            continue
        lcol = next(
            (
                x
                for x in df.columns
                if x.startswith(prefix) and x.endswith("long_all") and "spread" not in x
            ),
            None,
        )
        scol = next(
            (
                x
                for x in df.columns
                if x.startswith(prefix) and x.endswith("short_all") and "spread" not in x
            ),
            None,
        )
        if not lcol or not scol:
            missing += 1
            continue
        rl, rs = int(row.iloc[0][lcol]), int(row.iloc[0][scol])
        checked += 1
        if (rl, rs) != (lng, sht):
            mismatched += 1
            examples.append((rtype, code, cat, (lng, sht), (rl, rs)))
print(
    f"  week {latest}: {checked} category rows compared, {mismatched} mismatched, {missing} not matched"
)
for e in examples[:8]:
    print("   mismatch", e)
