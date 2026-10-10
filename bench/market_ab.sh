#!/bin/sh
# A/B the market job: old tree ~/ab-old vs new tree ~/arkbench, each on its own copy of the
# prod DB, through the same WARP egress. Usage: market_ab.sh <runs>
RUNS=${1:-3}
for side in old new; do
  dir=$HOME/ab-old; [ $side = new ] && dir=$HOME/arkbench
  cp $HOME/proddb/live.db /tmp/ab-$side.db && chmod 666 /tmp/ab-$side.db
  for i in $(seq 1 $RUNS); do
    docker run --rm --cpus 1 --memory 1g --network portfolio_arkwatch_default \
      -v $dir:/w -v /tmp/ab-$side.db:/data/arkwatch.db -w /w -e PYTHONUTF8=1 -e PYTHONPATH=/w \
      -e ARKWATCH_PROXY=socks5h://warp:9091 -e ARKWATCH_DATA_DIR=/data \
      ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim bash -c \
      's=$(date +%s.%N); .venv/bin/python -m arkwatch market --db /data/arkwatch.db >/tmp/o.txt 2>&1; rc=$?; e=$(date +%s.%N); \
       echo "'$side' run='$i' rc=$rc wall=$(python3 -c "print(round($e-$s,1))")s :: $(tail -2 /tmp/o.txt | tr "\n" " " | cut -c1-200)"'
  done
  python3 - /tmp/ab-$side.db $side <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(sys.argv[2], "fetch_log rows by status (this A/B):",
      dict(c.execute("select status,count(*) from fetch_log where ts>=datetime('now','-30 minutes') and fetcher='market' group by 1")))
PY
done
