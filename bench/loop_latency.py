"""Scheduler health from a daemon log on stdin: watcher gaps and market-bucket coverage."""

import re
import sys
from datetime import datetime

watch, market = [], []
for line in sys.stdin:
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
    if not m:
        continue
    t = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S")
    if "▶ watch " in line:
        watch.append(t)
    elif "▶ market " in line:
        market.append(t)
gaps = sorted((b - a).total_seconds() for a, b in zip(watch, watch[1:], strict=False))
span_h = (watch[-1] - watch[0]).total_seconds() / 3600 if watch else 0
late = sum(1 for g in gaps if g > 90)
print(f"span_hours={span_h:.1f} watcher_runs={len(watch)} expected~={int(span_h * 60)}")
print(
    f"watcher gap p50={gaps[len(gaps) // 2]:.0f}s p99={gaps[int(0.99 * len(gaps))]:.0f}s max={gaps[-1]:.0f}s late(>90s)={late}"
)
buckets = {(t.strftime("%Y%m%d%H"), t.minute // 5) for t in market}
exp = int(span_h * 12)
print(
    f"market buckets run={len(buckets)} expected~={exp} coverage={100 * len(buckets) / max(exp, 1):.1f}%"
)
