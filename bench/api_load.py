"""API latency + load benchmark for the ark-watch read API (stdlib only).

Usage: ARKWATCH_API_KEY=... python bench/api_load.py http://arkwatch-api:8000 [--out result.json]
Phase 1: per-endpoint sequential latency (N requests each).
Phase 2: mixed workload at increasing concurrency; reports RPS, p50/p95/p99, errors.
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ENDPOINTS = [
    "/v1/health",
    "/v1/regime",
    "/v1/freshness",
    "/v1/signals?limit=200",
    "/v1/signals?prefix=polymarket:",
    "/v1/odds",
    "/v1/series?limit=200",
    "/v1/series/DGS10/observations?limit=500",
    "/v1/sessions/NQ1/levels",
    "/v1/sessions/ES1/levels",
    "/v1/playbooks?limit=50",
    "/v1/playbook/performance",
    "/v1/playbook/scorecard",
    "/v1/risk/book",
    "/v1/briefs/latest",
    "/v1/news?limit=50",
    "/v1/sentiment",
    "/v1/calendar?limit=100",
    "/v1/alerts/summary",
    "/v1/prices/GC1",
    "/v1/cot/GC",
    "/v1/graph",
]


def hit(base: str, path: str, key: str) -> tuple[float, int]:
    req = urllib.request.Request(base + path, headers={"x-arkwatch-key": key})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    return (time.perf_counter() - t) * 1000, code


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)


def main() -> None:
    base, key = sys.argv[1].rstrip("/"), os.environ["ARKWATCH_API_KEY"]
    out = {"endpoints": {}, "load": []}
    n = int(os.environ.get("BENCH_N", "15"))
    for ep in ENDPOINTS:
        hit(base, ep, key)  # warm
        res = [hit(base, ep, key) for _ in range(n)]
        lat = [r[0] for r in res]
        codes = sorted({r[1] for r in res})
        out["endpoints"][ep] = {
            "p50": pct(lat, 0.5),
            "p95": pct(lat, 0.95),
            "max": round(max(lat), 1),
            "codes": codes,
        }
        print(
            f"{ep:45s} p50={pct(lat, 0.5):8.1f} p95={pct(lat, 0.95):8.1f} codes={codes}", flush=True
        )
    mix = [e for e in ENDPOINTS if out["endpoints"][e]["codes"] == [200]]
    for conc in (1, 4, 16, 32):
        total = conc * int(os.environ.get("BENCH_PER_WORKER", "25"))
        paths = [mix[i % len(mix)] for i in range(total)]
        t = time.perf_counter()
        with ThreadPoolExecutor(conc) as ex:
            res = list(ex.map(lambda p: hit(base, p, key), paths))
        wall = time.perf_counter() - t
        lat = [r[0] for r in res]
        errs = sum(1 for r in res if r[1] != 200)
        row = {
            "concurrency": conc,
            "requests": total,
            "rps": round(total / wall, 1),
            "p50": pct(lat, 0.5),
            "p95": pct(lat, 0.95),
            "p99": pct(lat, 0.99),
            "mean": round(statistics.fmean(lat), 1),
            "errors": errs,
        }
        out["load"].append(row)
        print("LOAD", row, flush=True)
    if "--out" in sys.argv:
        with open(sys.argv[sys.argv.index("--out") + 1], "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
