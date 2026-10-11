"""Time the read functions both trees share in arkwatch/api.py, on one DB. Usage: python libbench.py <db> <label>"""

import json
import statistics
import sys
import time
from pathlib import Path

from arkwatch import api

db, label = sys.argv[1:3]
CALLS = {
    "get_regime_snapshot": {},
    "get_session_levels": {"symbol": "NQ1"},
    "get_trading_playbook": {"symbol": "GC1"},
    "get_playbook_performance": {},
    "get_economic_calendar": {},
    "get_market_news": {},
    "get_all_sentiment_radars": {},
    "get_crypto_intelligence": {},
    "get_options_intelligence": {},
    "get_futures_flow_intelligence": {},
    "get_session_intraday_intelligence": {},
    "get_etf_flows_intelligence": {},
    "get_energy_intelligence": {},
    "get_news_intelligence": {},
    "get_intraday_catalyst_radar": {"asset": "GC1"},
    "scan_opportunities": {},
}
out = {}
for name, kw in CALLS.items():
    fn = getattr(api, name)
    ts = []
    err = None
    res = None
    for _ in range(5):
        t = time.perf_counter()
        try:
            res = fn(db_path=db, **kw)
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:120]}"
            break
        ts.append((time.perf_counter() - t) * 1000)
    out[name] = {"ms_median": round(statistics.median(ts), 1) if ts else None, "error": err}
    if name == "get_regime_snapshot" and isinstance(res, dict):
        out[name]["regime"] = {
            k: res.get(k) for k in ("label", "regime", "score", "quadrant") if k in res
        }
    print(f"{label} {name:36s} {out[name]}", flush=True)
Path(f"/out/lib-{label}.json").write_text(json.dumps(out, indent=1, default=str))
