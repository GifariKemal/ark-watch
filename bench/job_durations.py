"""Per-job duration stats from the daemon log lines '✓ <job> (<n>s)' / '✗ <job> ...'. stdin -> table."""

import re
import sys
from collections import defaultdict

ok, fail = defaultdict(list), defaultdict(int)
for line in sys.stdin:
    m = re.search(r"([✓✗]) (\S[\w -]*?) \((\d+)s\)", line)
    if m:
        if m[1] == "✓":
            ok[m[2]].append(int(m[3]))
        else:
            fail[m[2]] += 1
print(
    f"{'job':22s} {'runs':>5s} {'fail':>4s} {'p50':>5s} {'p95':>5s} {'max':>5s} {'wall-s/day':>9s}"
)
for job in sorted(ok, key=lambda j: -sum(ok[j])):
    d = sorted(ok[job])
    p = lambda q, d=d: d[min(len(d) - 1, int(q * len(d)))]  # noqa: E731
    print(f"{job:22s} {len(d):5d} {fail[job]:4d} {p(0.5):5d} {p(0.95):5d} {d[-1]:5d} {sum(d):9d}")
for job in fail:
    if job not in ok:
        print(f"{job:22s} {0:5d} {fail[job]:4d}")
