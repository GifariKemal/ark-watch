"""python -m arkwatch — CLI entry."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT_DB = str(
    Path(os.environ.get("ARKWATCH_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    / "arkwatch.db"
)


def print_help() -> int:
    help_text = """
========================================================================================
                      ARK-WATCH — US-MACRO & AUCTION TRADING CLI
========================================================================================

PENGGUNAAN:
  arkwatch <perintah> [argumen] [opsi]
  python -m arkwatch <perintah> [argumen] [opsi]

[1. TRADING PLAYBOOK & EKSEKUSI LELANG (AMT)]
  scanner [--min-rr FLOAT]       Memindai 31 aset & menyaring peluang (default: R:R >= 1.5)
  playbook [SYMBOL]              Playbook Dual-Horizon (Intraday & Swing) per instrumen
  tracker [SYMBOL] [--detail]    Pelacak performa trading (Win Rate, PnL, R-Mult, Log)
  levels [SYMBOL]                Level lelang AMT (VAH, VAL, POC, TPO, CVA, Naked POC)
  sentiment [--limit INT]        Ekstraksi sentimen berita 7-dimensi via model AI

[2. REAL-TIME MARKET & MIKROSTRUKTUR]
  market [--interval 5m|1m]      Pemanenan bar harga real-time 31 aset portofolio
  market-news                    Pemanenan berita breaking dari 14 sumber resmi
  breadth                        S&P 500 Constituent Breadth (504 saham)
  crypto                         Sinyal derivatif & likuidasi kripto (OKX/Bybit)
  polymarket                     Probabilitas crowd Polymarket untuk topik makro/geo
  calibration [--history N]      Brier score probabilitas Polymarket & FedWatch (1d/7d/30d)

[3. INSPEKSI SISTEM & RISET DATA]
  watch                          Pemeriksaan alert anomali makro & lelang
  coverage                       Audit kelengkapan & kesegaran seluruh series registry
  verify [--limit INT]           Verifikasi kelayakan endpoint seluruh provider data
  explore <target> [--trace]     Inspeksi interaktif data database & trace komponen
  backtest                       Uji statistik sinyal makro dengan Benjamini-Hochberg FDR

========================================================================================
"""
    print(help_text.strip())
    return 0


def main() -> int:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if cmd in ("help", "--help", "-h", ""):
        return print_help()
    if cmd == "verify":
        from .qa.verify_sources import main as verify_main

        return verify_main(sys.argv[2:])
    if cmd == "coverage":
        from .qa.coverage import main as coverage_main

        return coverage_main(sys.argv[2:])
    if cmd == "fedsurvey":
        from .qa.fedsurvey_harvest import main as fedsurvey_main

        return fedsurvey_main(sys.argv[2:])
    if cmd == "backfill":
        from .qa.backfill import main as backfill_main

        return backfill_main(sys.argv[2:])
    if cmd == "instruments":
        from .qa.instruments import main as instr_main

        return instr_main(sys.argv[2:])
    if cmd == "market":
        from .qa.market_timeline import main as market_main

        return market_main(sys.argv[2:])
    if cmd == "market-news":
        from .qa.market_news import main as market_news_main

        return market_news_main(sys.argv[2:])
    if cmd == "gdelt-retention":
        from .qa.gdelt_retention import main as gdelt_retention_main

        return gdelt_retention_main(sys.argv[2:])
    if cmd == "breadth":
        from .qa.equity_breadth import main as breadth_main

        return breadth_main(sys.argv[2:])
    if cmd == "polymarket":
        from .fetchers import polymarket

        return polymarket.main(sys.argv[2:])
    if cmd == "calibration":
        from .signals import calibration

        return calibration.main(sys.argv[2:])
    if cmd == "liquidations":
        from .qa.okx_liquidations import main as liquidations_main

        return liquidations_main(sys.argv[2:])
    if cmd == "crypto":
        import argparse

        from . import db
        from .signals.crypto import store_crypto_signals

        p = argparse.ArgumentParser(prog="arkwatch crypto")
        p.add_argument("--db", default=str(_DEFAULT_DB))
        a = p.parse_args(sys.argv[2:])
        conn = db.get_conn(a.db, allow_init=True)
        n = store_crypto_signals(conn)
        conn.close()
        print(f"=== crypto signals: {n} signals stored ===")
        return 0
    if cmd == "sentiment":
        import argparse

        from dotenv import load_dotenv

        load_dotenv()

        from . import db
        from .signals.sentiment import extract_news_intelligence, store_asset_radars

        p = argparse.ArgumentParser(prog="arkwatch sentiment")
        p.add_argument("--db", default=str(_DEFAULT_DB))
        p.add_argument("--limit", type=int, default=15)
        a = p.parse_args(sys.argv[2:])
        conn = db.get_conn(a.db, allow_init=True)
        n = extract_news_intelligence(conn, limit=a.limit)
        radars = store_asset_radars(conn)
        conn.close()
        print(f"=== sentiment: extracted {n} articles, updated {len(radars)} asset radars ===")
        return 0
    if cmd == "playbook":
        import argparse
        import json

        from dotenv import load_dotenv

        load_dotenv()

        from . import db
        from .signals.playbook import generate_trading_playbook

        p = argparse.ArgumentParser(prog="arkwatch playbook")
        p.add_argument("symbol", nargs="?", default="NQ1", help="symbol to generate playbook for")
        p.add_argument(
            "--cfd-offset", type=float, default=0.0, help="offset in points to match CFD quotes"
        )
        p.add_argument("--db", default=str(_DEFAULT_DB))
        a = p.parse_args(sys.argv[2:])
        conn = db.get_conn(a.db, allow_init=True)
        res = generate_trading_playbook(conn, a.symbol, cfd_basis_offset=a.cfd_offset)
        conn.close()
        if not res:
            print(f"No intraday bars found for {a.symbol}")
            return 1
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "tracker":
        import argparse
        import json

        from dotenv import load_dotenv

        load_dotenv()

        from . import db
        from .signals.playbook_tracker import get_playbook_performance_metrics

        p = argparse.ArgumentParser(prog="arkwatch tracker")
        p.add_argument("symbol", nargs="?", default=None, help="filter by symbol")
        p.add_argument("--horizon", choices=["INTRADAY", "SWING"], default=None)
        p.add_argument(
            "--detail", action="store_true", help="show individual trade details and decision logs"
        )
        p.add_argument("--report", action="store_true", help="human-readable tables, not JSON")
        p.add_argument("--db", default=str(_DEFAULT_DB))
        a = p.parse_args(sys.argv[2:])
        conn = db.get_conn(a.db, allow_init=True)
        res = get_playbook_performance_metrics(
            conn, symbol=a.symbol, horizon=a.horizon, detail=a.detail or a.report
        )
        conn.close()
        if a.report:
            from .signals.playbook_tracker import format_report

            print(format_report(res))
            return 0
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "scanner":
        import argparse
        import json

        from dotenv import load_dotenv

        load_dotenv()

        from . import db
        from .signals.playbook_tracker import scan_market_opportunities

        p = argparse.ArgumentParser(prog="arkwatch scanner")
        p.add_argument("--min-rr", type=float, default=1.5, help="minimum risk-to-reward ratio")
        p.add_argument("--db", default=str(_DEFAULT_DB))
        a = p.parse_args(sys.argv[2:])
        conn = db.get_conn(a.db, allow_init=True)
        opps = scan_market_opportunities(conn, min_rr=a.min_rr)
        conn.close()
        print(f"=== OPPORTUNITY SCANNER: {len(opps)} HIGH-CONVICTION SETUPS FOUND ===")
        print(json.dumps(opps, indent=2))
        return 0
    if cmd == "levels":
        import argparse
        import json

        from dotenv import load_dotenv

        load_dotenv()

        from . import db
        from .signals.levels import compute_session_reference_levels

        p = argparse.ArgumentParser(prog="arkwatch levels")
        p.add_argument("symbol", nargs="?", default="NQ1", help="symbol to compute levels for")
        p.add_argument("--db", default=str(_DEFAULT_DB))
        a = p.parse_args(sys.argv[2:])
        conn = db.get_conn(a.db, allow_init=True)
        res = compute_session_reference_levels(conn, a.symbol)
        conn.close()
        if not res:
            print(f"No closed 5m bars with a completed prior session for {a.symbol}")
            return 1
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "harvest":
        from .qa.harvest import main as harvest_main

        return harvest_main(sys.argv[2:])
    if cmd == "calendar":
        from .qa.calendar import main as cal_main

        return cal_main(sys.argv[2:])
    if cmd == "calibrate":
        from .qa.calibrate import main as calibrate_main

        return calibrate_main(sys.argv[2:])
    if cmd == "surprise":
        from .qa.surprise import main as surp_main

        return surp_main(sys.argv[2:])
    if cmd == "alfred":
        from .qa.alfred import main as alfred_main

        return alfred_main(sys.argv[2:])
    if cmd == "backup":
        from .qa.backup import main as backup_main

        return backup_main(sys.argv[2:])
    if cmd == "daemon":
        from .daemon import main as daemon_main

        return daemon_main(sys.argv[2:])
    if cmd == "healthcheck":
        from .daemon import healthcheck

        return healthcheck()
    if cmd == "cme":
        from .qa.cme_harvest import main as cme_main

        return cme_main(sys.argv[2:])
    if cmd == "f2":
        from .qa.f2_harvest import main as f2_main

        return f2_main(sys.argv[2:])
    if cmd == "soma":
        from .qa.soma_harvest import main as soma_main

        return soma_main(sys.argv[2:])
    if cmd == "nyfed":
        from .qa.nyfed_harvest import main as nyfed_main

        return nyfed_main(sys.argv[2:])
    if cmd == "fiscalx":
        from .qa.fiscalx_harvest import main as fiscalx_main

        return fiscalx_main(sys.argv[2:])
    if cmd == "f4":
        from .qa.f4 import main as f4_main

        return f4_main(sys.argv[2:])
    if cmd == "flows":
        # manual WGC gold input (monthly, from the WGC PDF) → flows_periodic
        # usage: arkwatch flows add wgc <tonnes> <YYYY-MM>
        if len(sys.argv) >= 3 and sys.argv[2] == "add" and sys.argv[3:6]:
            kind, val, period = sys.argv[3], float(sys.argv[4]), sys.argv[5]
            import sqlite3

            dbp = sys.argv[6] if len(sys.argv) > 6 else _DEFAULT_DB
            conn = sqlite3.connect(dbp)
            conn.execute(
                "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value)"
                " VALUES (?,?,?,'manual',1,?)"
                " ON CONFLICT(period,kind) DO UPDATE SET value=excluded.value",
                (period, f"wgc_{kind}", val, val),
            )
            conn.commit()
            conn.close()
            print(f"=== flows: wgc_{kind} {val} ({period}) saved ===")
            return 0
        print("usage: arkwatch flows add wgc <tonnes> <YYYY-MM> [db]")
        return 2
    if cmd == "brief":
        import sqlite3
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI

        from dotenv import load_dotenv

        from .signals.compute import run as brief_run

        load_dotenv()
        dbp = _DEFAULT_DB if len(sys.argv) < 3 else sys.argv[2]
        md = brief_run(dbp)
        print(md)
        # honesty (audit round-2): Sundays deliberately SKIP generation and
        # serve the stored brief — the old unconditional 'saved + pending'
        # banner claimed a write that never happened
        try:
            conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            gen = conn.execute(
                "SELECT 1 FROM brief_log WHERE date=?",
                (_dt.now(_ZI("Asia/Jakarta")).date().isoformat(),),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            gen = True  # cannot tell — do not claim the skip either
        print(
            "\n=== brief saved + outbox pending ==="
            if gen
            else "\n=== Sunday skip — stored brief served (no new generation) ==="
        )
        return 0
    if cmd == "send":
        from dotenv import load_dotenv

        from .senders.outbox import send_pending

        load_dotenv()
        result = send_pending(_DEFAULT_DB if len(sys.argv) < 3 else sys.argv[2])
        print(
            f"=== Telegram sender: {result['sent']} sent · {result['failed']} failed"
            f" · alerts {result.get('alerts', {}).get('sent', 0)} ==="
        )
        return 0
    if cmd == "export":
        import csv
        import sqlite3
        from pathlib import Path

        # ROUND-4: the docs' `export block F --csv` shape crashed with a
        # traceback AND left a 0-byte file literally named '--csv' — argv
        # starting with '--' is a flag, never an output path; refuse it.
        args = [a for a in sys.argv[2:]]
        csv_flag = "--csv" in args
        if csv_flag:
            args.remove("--csv")
        pos = [a for a in args if not a.startswith("--")]
        if len(pos) < 2:
            print("usage: arkwatch export <BLOCK> <file.csv> [db] [--csv]")
            print("  (--csv is accepted for doc compatibility; the .csv path is required)")
            return 2
        block = pos[0].upper()
        out_csv = pos[1]
        dbp = pos[2] if len(pos) > 2 else _DEFAULT_DB
        if not Path(dbp).exists():
            print(f"✗ db not found: {dbp}")
            return 2
        conn = sqlite3.connect(f"file:{dbp}?mode=rw", uri=True)
        rows = conn.execute(
            "SELECT r.series_id, r.ts, r.value, r.vintage_ts FROM raw_observations r "
            "JOIN series_registry g ON g.series_id = r.series_id "
            "WHERE g.block=? ORDER BY r.series_id, r.ts",
            (block,),
        ).fetchall()
        conn.close()
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["series_id", "ts", "value", "vintage_ts"])
            w.writerows(rows)
        print(f"=== export block {block}: {len(rows)} rows → {out_csv} ===")
        return 0
    if cmd == "energy":
        from .qa.energy import main as energy_main

        return energy_main(sys.argv[2:])
    if cmd == "watch":
        from .qa.watcher import main as watch_main

        return watch_main(sys.argv[2:])
    if cmd == "backtest":
        from .qa.backtest import main as bt_main

        return bt_main(sys.argv[2:])
    if cmd == "explore":
        from .qa.explore import main as ex_main

        return ex_main(sys.argv[2:])
    print(f"arkwatch: perintah '{cmd}' tidak dikenali. Ketik 'arkwatch help' untuk panduan.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
