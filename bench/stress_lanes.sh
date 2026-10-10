#!/bin/bash
# Concurrency stress for the lane design: several real jobs + a synthetic bulk writer + API
# readers hammer ONE SQLite copy of the prod DB. Counts failures and 'database is locked'.
# Usage (inside the dev container, cwd = repo, network with warp): stress_lanes.sh <seconds>
DUR=${1:-360}
DB=/tmp/arkwatch.db  # = $ARKWATCH_DATA_DIR/arkwatch.db, so watch/scanner use it too
cp /proddb/live.db $DB && chmod 666 $DB
.venv/bin/python -c "from arkwatch import db; db.get_conn('$DB', allow_init=True).close()"  # migrate
export ARKWATCH_DATA_DIR=/tmp PYTHONPATH=/w
END=$(( $(date +%s) + DUR ))
loop() {  # name, sleep, cmd...
  name=$1; pause=$2; shift 2; n=0; bad=0
  while [ $(date +%s) -lt $END ]; do
    t0=$(date +%s.%N)
    if ! "$@" >/tmp/$name.out 2>&1; then bad=$((bad+1)); cp /tmp/$name.out /tmp/$name.fail.$bad; fi
    n=$((n+1)); echo "$(python3 -c "print(round($(date +%s.%N)-$t0,2))")" >> /tmp/$name.dur
    grep -qi "database is locked" /tmp/$name.out && echo locked >> /tmp/$name.locked
    sleep $pause
  done
  echo "$name runs=$n fail=$bad locked=$(cat /tmp/$name.locked 2>/dev/null | wc -l) p50/max_s=$(sort -n /tmp/$name.dur | awk '{a[NR]=$1} END{print a[int(NR/2)+1]"/"a[NR]}')"
}
rm -f /tmp/*.dur /tmp/*.locked /tmp/*.fail.*
loop watch 10 .venv/bin/python -m arkwatch watch &
loop market 0 .venv/bin/python -m arkwatch market --db $DB &
loop scanner 5 .venv/bin/python -m arkwatch scanner &
loop polymarket 20 .venv/bin/python -m arkwatch polymarket --db $DB &
# synthetic harvest-like bulk writer: 20k-row transaction every 15 s
loop bulk 15 .venv/bin/python - <<'PY' &
import sqlite3, time
c = sqlite3.connect("/tmp/arkwatch.db", timeout=10); c.execute("PRAGMA busy_timeout=10000")
c.execute("CREATE TABLE IF NOT EXISTS stress_bulk(i INTEGER, v TEXT)")
with c:
    c.executemany("INSERT INTO stress_bulk VALUES (?, ?)", ((i, "x" * 64) for i in range(20000)))
    time.sleep(1.5)  # hold the write lock like a slow harvest commit
c.execute("DELETE FROM stress_bulk"); c.commit()
PY
# API readers: uvicorn on the same DB + 4 concurrent clients
ARKWATCH_DB=$DB ARKWATCH_API_KEY=stress-key-0123456789abcdef0123456789 .venv/bin/uvicorn arkwatch.server.app:app --port 8011 >/tmp/api.log 2>&1 &
API=$!
# the API applies pending migrations at startup (v34 indexes on a 100 MB copy): wait for it
until .venv/bin/python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8011/v1/health', timeout=2)" 2>/dev/null; do sleep 1; done
.venv/bin/python - <<PY &
import time, urllib.request, urllib.error, statistics
from concurrent.futures import ThreadPoolExecutor
end = time.time() + $DUR - 10
paths = ["/v1/regime", "/v1/freshness", "/v1/signals?limit=200", "/v1/playbooks?limit=50", "/v1/odds", "/v1/sessions/NQ1/levels", "/v1/health"]
lat, err = [], {}
def hit(p):
    req = urllib.request.Request("http://127.0.0.1:8011" + p, headers={"x-arkwatch-key": "stress-key-0123456789abcdef0123456789"})
    t = time.perf_counter()
    try:
        urllib.request.urlopen(req, timeout=30).read(); return (time.perf_counter() - t) * 1000, None
    except Exception as ex:
        return (time.perf_counter() - t) * 1000, f"{type(ex).__name__} {getattr(ex, 'code', '')}"
i = 0
with ThreadPoolExecutor(4) as ex:
    while time.time() < end:
        for ms, e in ex.map(hit, [paths[(i + k) % len(paths)] for k in range(8)]):
            lat.append(ms)
            if e: err[e] = err.get(e, 0) + 1
        i += 8
lat.sort()
print(f"api requests={len(lat)} errors={err} p50={lat[len(lat)//2]:.1f}ms p95={lat[int(.95*len(lat))]:.1f}ms p99={lat[int(.99*len(lat))]:.1f}ms max={lat[-1]:.1f}ms")
PY
wait %1 %2 %3 %4 %5 %7  # not %6: uvicorn never exits on its own
kill $API 2>/dev/null
echo "integrity: $(.venv/bin/python -c "import sqlite3; print(sqlite3.connect('$DB').execute('PRAGMA integrity_check').fetchone()[0])")"
for f in /tmp/*.fail.*; do [ -f "$f" ] && { echo "--- $f"; grep -iE "error|locked|Traceback" "$f" | head -5; }; done
