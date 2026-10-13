# head-to-head ark-watch (ours main) vs srikresna upstream/main, on copies of one prod snapshot.
# Setup (from the laptop):
#   git -c core.autocrlf=false archive HEAD | ssh vps 'mkdir -p ~/h2h/ours && tar -x -C ~/h2h/ours'
#   git -c core.autocrlf=false archive upstream/main | ssh vps 'mkdir -p ~/h2h/kresna && tar -x -C ~/h2h/kresna'
#   scp bench/{lib_bench,replay_freshness,h2h_static}.py vps:~/h2h/tools/
# Every step runs both trees in the same image with the same CPU/memory cap and the same WARP egress.
set -u
H=$HOME/h2h; OUT=$H/out; mkdir -p $OUT
IMG=ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim
NET=portfolio_arkwatch_warp-net
step() { echo; echo "=== $* ($(date -u +%H:%M:%S))"; }
run() { side=$1; shift; docker run --rm --cpus 1.5 --memory 2g --network $NET -v $H/$side:/w -v $H/db:/db -v $OUT:/out -v $H/tools:/tools \
  -w /w -e PYTHONUTF8=1 -e PYTHONPATH=/w -e ARKWATCH_PROXY=socks5h://warp:9091 -e ARKWATCH_DATA_DIR=/db/data-$side -e UV_CACHE_DIR=/db/uvcache $IMG bash -c "$*"; }

step snapshot prod DB
mkdir -p $H/db && rm -f $H/db/*.db
docker exec portfolio_arkwatch-daemon-1 python -c "
import sqlite3; from arkwatch import db
s=sqlite3.connect(f'file:{db.default_db_path()}?mode=ro', uri=True); d=sqlite3.connect('/tmp/h2h.db'); s.backup(d); d.close(); print('ok')"
docker cp portfolio_arkwatch-daemon-1:/tmp/h2h.db $H/db/snap.db && docker exec -u root portfolio_arkwatch-daemon-1 rm -f /tmp/h2h.db
python3 - <<PY
import sqlite3, shutil
shutil.copy("$H/db/snap.db", "$H/db/kresna.db"); shutil.copy("$H/db/snap.db", "$H/db/ours.db")
c = sqlite3.connect("$H/db/kresna.db"); c.execute("DELETE FROM schema_migrations WHERE version > 32"); c.commit()
print("versions", [sqlite3.connect(f"$H/db/{s}.db").execute("select max(version) from schema_migrations").fetchone()[0] for s in ("ours","kresna")])
PY
chmod 666 $H/db/*.db; mkdir -p $H/db/data-ours $H/db/data-kresna $H/db/uvcache; chmod 777 $H/db $H/db/*

step install deps
run ours   'uv sync --frozen --extra dev -q 2>&1 | tail -2; .venv/bin/python -c "import arkwatch; print(\"ours ok\")"'
run kresna 'rm -rf .venv; uv venv -q .venv && uv pip install -q -p .venv/bin/python -r pyproject.toml --extra dev 2>&1 | tail -2; .venv/bin/python -c "import arkwatch; print(\"kresna ok\")"'

step 0 static: size, lint, types, security, deps
for s in ours kresna; do run $s "uv pip install -q -p .venv/bin/python ruff==0.16.10 pyright==1.1.408 bandit==1.9.4 pip-audit==2.10.0 pytest-cov 2>&1 | tail -1; \
  .venv/bin/python /tools/h2h_static.py $s" | tee $OUT/static-$s.txt; done

step 1 test suites + coverage
for s in ours kresna; do run $s "s=\$(date +%s); .venv/bin/python -m pytest -q -p no:cacheprovider --cov=arkwatch --cov-report=term 2>&1 | grep -E 'passed|failed|^TOTAL' | tail -3; echo \"$s pytest wall=\$((\$(date +%s)-s))s\"" | tee $OUT/pytest-$s.txt; done

step 2 session-model replay, same bars
for s in ours kresna; do run $s ".venv/bin/python /tools/replay_freshness.py /db/snap.db arkwatch.qa.market_timeline 2026-10-05T00:00:00+00:00 2026-10-12T00:00:00+00:00 2>&1 | tail -1" | sed "s/^/$s /" | tee $OUT/replay-$s.txt; done

step 4 shared read functions, same data
for s in ours kresna; do cp $H/db/$s.db $H/db/lib-$s.db; chmod 666 $H/db/lib-$s.db; run $s ".venv/bin/python /tools/lib_bench.py /db/lib-$s.db $s 2>&1 | grep -v Warning" | tee $OUT/lib-$s.txt; done

step 3 market job, 3 runs each, own DB copy, same WARP egress, peak RSS
for s in ours kresna; do
  cp $H/db/$s.db $H/db/mkt-$s.db; chmod 666 $H/db/mkt-$s.db
  base=$(python3 -c "import sqlite3;c=sqlite3.connect('$H/db/mkt-$s.db');print(c.execute('select max(id) from fetch_log').fetchone()[0], c.execute('select count(*) from intraday_bars').fetchone()[0])")
  for i in 1 2 3; do
    run $s "t=\$(date +%s.%N); .venv/bin/python -c \"import resource,subprocess,sys;r=subprocess.run([sys.executable,'-m','arkwatch','market','--db','/db/mkt-$s.db'],capture_output=True,text=True,timeout=600);print(r.returncode, round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024), (r.stdout.strip().splitlines() or [''])[-1][:150])\" > /tmp/o.txt 2>&1; e=\$(date +%s.%N); read rc rss rest < /tmp/o.txt; echo \"$s run=$i rc=\$rc wall=\$(python3 -c \"print(round(\$e-\$t,1))\")s rss=\${rss}MB :: \$rest\""
  done | tee $OUT/market-$s.txt
  python3 - $H/db/mkt-$s.db $s $base <<'PY' | tee -a $OUT/market-$s.txt
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); side, fid, nbars = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
print(side, "fetch_log market rows by status:", dict(c.execute("select status,count(*) from fetch_log where id>? and fetcher='market' group by 1", (fid,))))
print(side, "fetch_log errors sample:", [r[0][:90] for r in c.execute("select distinct error from fetch_log where id>? and fetcher='market' and error is not null limit 3", (fid,))])
print(side, "new intraday bars:", c.execute("select count(*) from intraday_bars").fetchone()[0] - nbars)
PY
done
step done
