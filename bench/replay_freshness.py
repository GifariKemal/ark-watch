"""Replay assess_freshness over stored prod bars at every 5-minute tick, for a given module.

Usage: python bench/replay_freshness.py <db> <module.dotted.name> <start_iso> <end_iso>
Prints status counts per symbol: how often the session model would have flagged the data.
"""

import importlib
import sqlite3
import sys
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta

db, mod_path, start, end = sys.argv[1:5]
mt = importlib.import_module(mod_path)
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
total = Counter()
for sym in mt.TRACKED:
    if sym in ("BTCUSD", "ETHUSD"):
        continue
    stamps = [
        r[0]
        for r in conn.execute(
            "SELECT bar_ts_utc FROM intraday_bars WHERE symbol=? AND interval='5m' AND source='YAHOO'"
            " AND bar_ts_utc < ? ORDER BY bar_ts_utc",
            (sym, end),
        )
    ]
    parsed = [datetime.fromisoformat(s) for s in stamps]
    c = Counter()
    t = t0
    while t < t1:
        # what Yahoo would have returned at t: bars that started at least 5 minutes earlier
        i = bisect_right(parsed, t - timedelta(minutes=5))
        rows = [{"bar_ts_utc": stamps[i - 1]}] if i else []
        c[mt.assess_freshness(sym, rows, t).status] += 1
        t += timedelta(minutes=5)
    bad = c["STALE"] + c["FUTURE"]
    total.update(c)
    print(f"{sym:7s} bad={bad:4d} " + " ".join(f"{k}={v}" for k, v in sorted(c.items())))
print("TOTAL", dict(sorted(total.items())), "bad=", total["STALE"] + total["FUTURE"])
