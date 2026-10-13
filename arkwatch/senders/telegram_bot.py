"""telegram_bot.py — Interactive Multi-User Telegram Bot Engine for ark-watch.

Allows ANY Telegram user or group to interact with @ark_trading_bot on demand
and subscribe to real-time trading signals:
  - /start, /help        -> Register subscriber & show command menu
  - /playbook [SYMBOL]   -> Live AMT Playbook, levels, structure & scenarios
  - /tracker [SYM] [WIN] -> Performance dashboard, breakdowns & recent trades
  - /id <NUM>            -> Deep chronological audit of trade #<NUM>
  - /scanner             -> Scan active & pending setups across all assets
  - /tz <ET|WIB|UTC>     -> Set personal display timezone preference
  - /subscribe           -> Enable automatic trading signal broadcasts
  - /unsubscribe         -> Disable automatic broadcasts
"""

from __future__ import annotations

import html
import logging
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from ..timezones import format_session_id, resolve_timezone
from .telegram import API, _send_message, _token, redact_telegram_secrets

logger = logging.getLogger("arkwatch.telegram_bot")

BOT_COMMANDS = [
    {"command": "start", "description": "Daftar sinyal otomatis & menu panduan"},
    {"command": "playbook", "description": "Playbook AMT & skenario live (contoh: /playbook NQ1)"},
    {"command": "tracker", "description": "Statistik win rate & jurnal (contoh: /tracker NQ1 win)"},
    {"command": "id", "description": "Audit detail transaksi (contoh: /id 90)"},
    {"command": "scanner", "description": "Pindai setup aktif di seluruh instrumen"},
    {"command": "tz", "description": "Ubah zona waktu tampilan (contoh: /tz WIB atau /tz ET)"},
    {"command": "subscribe", "description": "Aktifkan langganan sinyal trading otomatis"},
    {"command": "unsubscribe", "description": "Matikan langganan sinyal otomatis"},
    {"command": "help", "description": "Tampilkan panduan perintah lengkap"},
]


def ensure_subscribers_table(conn: sqlite3.Connection) -> None:
    """Create telegram_subscribers table if it does not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_subscribers (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            preferred_tz TEXT DEFAULT 'ET',
            subscribed INTEGER DEFAULT 1,
            joined_at_utc TEXT NOT NULL,
            last_active_utc TEXT NOT NULL
        )
        """
    )


def register_or_touch_user(
    conn: sqlite3.Connection,
    chat_id: str,
    username: str = "",
    first_name: str = "",
    *,
    force_subscribe: bool = False,
) -> dict[str, Any]:
    """Upsert user into telegram_subscribers and return their preferences."""
    ensure_subscribers_table(conn)
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT preferred_tz, subscribed FROM telegram_subscribers WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO telegram_subscribers (
                chat_id, username, first_name, preferred_tz, subscribed, joined_at_utc, last_active_utc
            ) VALUES (?, ?, ?, 'ET', 1, ?, ?)
            """,
            (str(chat_id), username, first_name, now_iso, now_iso),
        )
        conn.commit()
        return {"preferred_tz": "ET", "subscribed": 1, "is_new": True}

    new_sub = 1 if force_subscribe else row[1]
    conn.execute(
        """
        UPDATE telegram_subscribers
        SET username = COALESCE(NULLIF(?, ''), username),
            first_name = COALESCE(NULLIF(?, ''), first_name),
            subscribed = ?,
            last_active_utc = ?
        WHERE chat_id = ?
        """,
        (username, first_name, new_sub, now_iso, str(chat_id)),
    )
    conn.commit()
    return {"preferred_tz": row[0] or "ET", "subscribed": new_sub, "is_new": False}


def register_bot_commands_menu() -> bool:
    """Register clickable slash command menu in Telegram client UI."""
    try:
        r = requests.post(
            API.format(token=_token(), method="setMyCommands"),
            json={"commands": BOT_COMMANDS},
            timeout=10,
        )
        return bool(r.json().get("ok"))
    except Exception as e:
        logger.warning(f"Failed to set bot commands menu: {e}")
        return False


def _format_help_message(first_name: str, tz_lbl: str, subscribed: bool) -> str:
    sub_status = (
        "✅ <b>AKTIF (Menerima Sinyal Otomatis)</b>" if subscribed else "🔕 <b>NONAKTIF</b>"
    )
    name_str = html.escape(first_name or "Trader")
    return (
        f"👋 Halo <b>{name_str}</b>! Selamat datang di <b>ARK Quant Trading Bot</b>.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Status Sinyal :</b> {sub_status}\n"
        f"🕒 <b>Zona Waktu    :</b> <code>{tz_lbl}</code>\n\n"
        f"📋 <b>DAFTAR PERINTAH INTERAKTIF:</b>\n\n"
        f"1️⃣ <b>Playbook &amp; Level Lelang AMT:</b>\n"
        f"  • <code>/playbook NQ1</code> — Playbook Nasdaq 100\n"
        f"  • <code>/playbook GC1</code> — Playbook Emas (Gold)\n"
        f"  • <code>/playbook CL1</code> — Playbook Minyak (Crude Oil)\n"
        f"  • <code>/playbook BTCUSD</code> — Playbook Bitcoin\n"
        f"  <i>(Mendukung: NQ1, ES1, YM1, GC1, SI1, CL1, BTCUSD, ETHUSD, EURUSD, GBPUSD, USDJPY, DXY)</i>\n\n"
        f"2️⃣ <b>Performance Tracker &amp; Jurnal:</b>\n"
        f"  • <code>/tracker</code> — Dashboard performa seluruh aset\n"
        f"  • <code>/tracker NQ1</code> — Performa khusus NQ1\n"
        f"  • <code>/tracker NQ1 win</code> — Filter hanya trade WIN di NQ1\n"
        f"  • <code>/tracker loss</code> — Evaluasi seluruh trade LOSS\n\n"
        f"3️⃣ <b>Audit Detail Transaksi (#ID):</b>\n"
        f"  • <code>/id 90</code> — Lihat kronologi eksekusi &amp; log trade #90\n"
        f"  • <code>/id 40</code> — Lihat kronologi eksekusi trade #40\n\n"
        f"4️⃣ <b>Market Scanner &amp; Pengaturan:</b>\n"
        f"  • <code>/scanner</code> — Pindai peluang setup yang sedang aktif\n"
        f"  • <code>/tz WIB</code> — Ubah tampilan waktu ke WIB (UTC+7)\n"
        f"  • <code>/tz ET</code> — Ubah tampilan waktu ke New York (UTC-4)\n"
        f"  • <code>/subscribe</code> — Aktifkan sinyal otomatis\n"
        f"  • <code>/unsubscribe</code> — Matikan sinyal otomatis\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <i>Ketik perintah di atas kapan saja!</i>"
    )


def _handle_playbook_cmd(conn: sqlite3.Connection, args: list[str], user_tz: str) -> str:
    from ..signals.playbook import generate_trading_playbook

    sym = args[0].strip().upper() if args else "NQ1"
    pb = generate_trading_playbook(conn, sym, display_tz=user_tz)
    if not pb:
        return f"⚠ Data bar intraday tidak ditemukan untuk simbol <code>{html.escape(sym)}</code>."

    lv = pb.get("reference_levels", {})
    pa = pb.get("price_action", {})
    cat = pb.get("catalysts", {})
    amt = pb.get("amt_context", {})
    m_st = amt.get("multi_timeframe_market_structure", {})
    m15_t = m_st.get("m15_structure", {}).get("trend", "N/A")
    h1_t = m_st.get("h1_structure", {}).get("trend", "N/A")
    qt = amt.get("quarterly_theory", {})
    on_cva = amt.get("overnight_cva", {})

    lines = [
        f"📊 <b>ARK-WATCH LIVE PLAYBOOK: {html.escape(sym)}</b>",
        f"🕒 <i>{html.escape(pb.get('as_of_display', ''))}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 <b>Last Price :</b> <code>{pb.get('last_price')}</code> (VWAP: <code>{pa.get('vwap')}</code>)",
        f"📈 <b>Prior Day  :</b> VAH <code>{lv.get('VAH')}</code> | POC <code>{lv.get('POC')}</code> | VAL <code>{lv.get('VAL')}</code>",
        f"🛡 <b>Extremes   :</b> PDH <code>{lv.get('PDH')}</code> | PDL <code>{lv.get('PDL')}</code>",
        f"🌏 <b>Overnight  :</b> CVA VAH <code>{on_cva.get('c_vah')}</code> | POC <code>{on_cva.get('c_poc')}</code> | VAL <code>{on_cva.get('c_val')}</code>",
        f"🧭 <b>Siklus QT  :</b> {qt.get('active_quarter')} | {qt.get('active_90m_sub_quarter')} | {qt.get('active_22m_micro_cycle')}",
        f"📐 <b>Struktur   :</b> M15: <code>{m15_t}</code> | H1: <code>{h1_t}</code>",
        f"📰 <b>Katalis 4H :</b> {cat.get('intraday_fast_stance')} (Score: {cat.get('intraday_fast_score')})",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    scenarios = pb.get("scenarios", [])
    if not scenarios:
        lines.append(
            "⏳ <b>SKENARIO AKTIF:</b>\n<i>Harga sedang berada di zona rotasi tengah. Menunggu harga menguji batas VAH/VAL atau pemicu sesi berikutnya.</i>"
        )
    else:
        lines.append(f"🎯 <b>SKENARIO TERDETEKSI ({len(scenarios)} Setup):</b>")
        for idx, s in enumerate(scenarios, 1):
            d_emoji = "🟢 LONG" if s.get("direction") == "LONG" else "🔴 SHORT"
            lines.append(
                f"\n<b>{idx}. [{d_emoji}] {html.escape(str(s.get('title')))}</b>\n"
                f"  • <b>Trigger :</b> <code>{s.get('trigger_price')}</code>\n"
                f"  • <b>Target  :</b> <code>{s.get('target_profit')}</code>\n"
                f"  • <b>StopLoss:</b> <code>{s.get('invalidation_level')}</code>\n"
                f"  • <b>R : R   :</b> <b>1 : {s.get('risk_reward_ratio')}</b>\n"
                f"  • <b>Syarat  :</b> <i>{html.escape(str(s.get('trigger_condition')))}</i>"
            )

    return "\n".join(lines)


def _handle_tracker_cmd(conn: sqlite3.Connection, args: list[str], user_tz: str) -> str:
    from ..signals.playbook_tracker import get_playbook_performance_metrics

    sym = None
    outcome = None
    outcome_keywords = {
        "WIN": "WIN",
        "WINS": "WIN",
        "LOSS": "LOSS",
        "LOSE": "LOSS",
        "LOSSES": "LOSS",
        "BE": "BE",
        "BREAKEVEN": "BE",
        "PENDING": "PENDING",
        "ACTIVE": "ACTIVE",
    }

    for arg in args:
        u = arg.strip().upper()
        if u in outcome_keywords:
            outcome = outcome_keywords[u]
        else:
            sym = u

    res = get_playbook_performance_metrics(conn, symbol=sym, outcome=outcome, detail=True)
    _, tz_lbl = resolve_timezone(user_tz)

    filter_title = f" ({sym})" if sym else " (ALL ASSETS)"
    if outcome:
        filter_title += f" [{outcome}]"

    lines = [
        f"📈 <b>PERFORMANCE TRACKER{html.escape(filter_title)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"• <b>Total Skenario   :</b> {res.get('total_scenarios', 0)}",
        f"• <b>Completed Trades :</b> {res.get('completed_trades', 0)} ({res.get('wins', 0)}W / {res.get('losses', 0)}L / {res.get('breakevens', 0)}BE)",
        f"• <b>Win Rate         :</b> <b>{res.get('win_rate_pct', 0.0)}%</b>",
        f"• <b>Profit Factor    :</b> <b>{res.get('profit_factor', 0.0)}</b>",
        f"• <b>Avg R-Multiple   :</b> <b>{res.get('avg_r_multiple') or 0.0:+.2f}R</b>",
        f"• <b>Avg MFE / MAE    :</b> +{res.get('avg_mfe', 0.0)} / -{res.get('avg_mae', 0.0)} pts",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    by_sym = res.get("breakdown_by_symbol", [])
    if by_sym and not sym:
        lines.append("🏆 <b>PERFORMA PER ASET:</b>")
        for d in by_sym[:8]:
            lines.append(
                f"  • <code>{d['symbol']:<6}</code>: <b>{d['win_rate_pct']:.1f}% WR</b> ({d['wins']}W/{d['losses']}L/{d['breakevens']}BE) | <b>{d['avg_r_multiple']:+.2f}R</b>"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")

    trades = res.get("trades", [])
    if trades:
        lines.append(f"📜 <b>JURNAL TRANSAKSI TERAKHIR ({tz_lbl}):</b>")
        for t in trades[:10]:
            tid = t.get("id", "-")
            s_code = t.get("symbol", "-")
            d_str = "🟢L" if t.get("direction") == "LONG" else "🔴S"
            r_val = f"{t['r_multiple']:+.2f}R" if t.get("r_multiple") is not None else "-"
            st = t.get("state", "").replace("HIT_TARGET_", "").replace("CANCELLED_", "")
            ts_res = t.get("resolved_at_utc") or t.get("created_at_utc")
            ts_short = format_session_id(ts_res, user_tz)[5:] if ts_res else "RUN"
            lines.append(
                f"  <code>#{tid:<3}</code> {ts_short} | <b>{s_code}</b> {d_str} | <b>{r_val}</b> ({st})"
            )
        lines.append(
            "\n💡 <i>Ketik</i> <code>/id &lt;nomor&gt;</code> <i>(contoh:</i> <code>/id 90</code><i>) untuk melihat detail audit transaksi!</i>"
        )
    else:
        lines.append("<i>Belum ada transaksi untuk filter ini.</i>")

    return "\n".join(lines)


def _handle_id_cmd(conn: sqlite3.Connection, args: list[str], user_tz: str) -> str:
    from ..signals.playbook_tracker import get_trade_by_uid

    if not args:
        return "⚠ Masukkan nomor ID transaksi. Contoh: <code>/id 90</code> atau <code>/id 40</code>"

    trade = get_trade_by_uid(conn, args[0])
    if not trade:
        return f"⚠ Transaksi <code>#{html.escape(args[0])}</code> tidak ditemukan."

    _, tz_lbl = resolve_timezone(user_tz)
    d_emoji = "🟢 LONG" if trade.get("direction") == "LONG" else "🔴 SHORT"
    ent = f"{trade['entry_price']:.2f}" if trade.get("entry_price") is not None else "-"
    ex = f"{trade['exit_price']:.2f}" if trade.get("exit_price") is not None else "-"
    pnl = f"{trade['pnl_points']:+.2f}" if trade.get("pnl_points") is not None else "-"
    r_val = f"{trade['r_multiple']:+.2f}R" if trade.get("r_multiple") is not None else "-"

    lines = [
        f"🔍 <b>DETAIL AUDIT TRANSAKSI #{trade.get('id')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📌 <b>Simbol    :</b> <code>{trade.get('symbol')}</code> ({trade.get('horizon')})",
        f"⚡ <b>Arah      :</b> <b>{d_emoji}</b>",
        f"🎯 <b>Setup     :</b> {html.escape(str(trade.get('title')))}",
        f"📊 <b>Status    :</b> <code>{trade.get('state')}</code>",
        f"🎚 <b>Trigger   :</b> <code>{trade.get('trigger_price')}</code> | <b>TP:</b> <code>{trade.get('target_profit')}</code> | <b>SL:</b> <code>{trade.get('invalidation_level')}</code>",
        f"💰 <b>Eksekusi  :</b> Entry <code>{ent}</code> → Exit <code>{ex}</code>",
        f"🏆 <b>Hasil PnL :</b> <b>{pnl} pts ({r_val})</b>",
        f"📈 <b>MFE / MAE :</b> +{trade.get('mfe_points', 0)} pts / -{trade.get('mae_points', 0)} pts",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📜 <b>KRONOLOGI DECISION LOG ({tz_lbl}):</b>",
    ]

    for item in trade.get("decision_log", []):
        ts_str = format_session_id(item.get("ts_utc", ""), user_tz)
        ev = html.escape(str(item.get("event", "")))
        det = html.escape(str(item.get("details", "")))
        lines.append(f"• <code>[{ts_str}]</code> <b>{ev}</b>:\n  <i>{det}</i>")

    return "\n".join(lines)


def _handle_scanner_cmd(conn: sqlite3.Connection, user_tz: str) -> str:
    rows = conn.execute(
        """
        SELECT rowid, symbol, horizon, direction, title, trigger_price, target_profit,
               invalidation_level, risk_reward_ratio, state, created_at_utc
        FROM playbook_scenarios
        WHERE state IN ('ACTIVE', 'PENDING_TRIGGER')
        ORDER BY state ASC, risk_reward_ratio DESC
        LIMIT 15
        """
    ).fetchall()

    if not rows:
        return "📡 <b>MARKET SCANNER:</b>\n<i>Saat ini tidak ada setup yang berstatus ACTIVE atau PENDING. Ketik /playbook NQ1 untuk memindai manual.</i>"

    lines = [
        f"📡 <b>LIVE MARKET OPPORTUNITY SCANNER ({len(rows)} Setups)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for r in rows:
        tid, sym, hor, direction, title, trig, tp, sl, rr, st, _created = r
        d_emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
        st_badge = "🔥 ACTIVE" if st == "ACTIVE" else "⏳ PENDING"
        lines.append(
            f"\n<code>#{tid}</code> <b>{sym}</b> [{d_emoji}] — <b>{st_badge}</b>\n"
            f"  • <b>Setup  :</b> {html.escape(str(title))}\n"
            f"  • <b>Entry  :</b> <code>{trig}</code> | <b>TP:</b> <code>{tp}</code> | <b>SL:</b> <code>{sl}</code>\n"
            f"  • <b>R : R  :</b> <b>1 : {rr}</b> ({hor})"
        )
    lines.append(
        "\n💡 <i>Ketik</i> <code>/id &lt;nomor&gt;</code> <i>untuk rincian lengkap setup.</i>"
    )
    return "\n".join(lines)


def handle_incoming_message(conn: sqlite3.Connection, message: dict[str, Any]) -> None:
    """Process a single incoming Telegram message from any user or group."""
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    from_user = message.get("from", {})
    username = from_user.get("username") or chat.get("username") or ""
    first_name = from_user.get("first_name") or chat.get("title") or "Trader"

    parts = text.split()
    cmd_raw = parts[0].lower().split("@")[0]  # strip @ark_trading_bot suffix in groups
    args = parts[1:]

    prefs = register_or_touch_user(
        conn, chat_id, username, first_name, force_subscribe=(cmd_raw in ("/start", "/subscribe"))
    )
    user_tz = prefs["preferred_tz"]
    _, tz_lbl = resolve_timezone(user_tz)

    try:
        if cmd_raw in ("/start", "/help"):
            reply = _format_help_message(first_name, tz_lbl, bool(prefs["subscribed"]))
        elif cmd_raw in ("/playbook", "/pb"):
            reply = _handle_playbook_cmd(conn, args, user_tz)
        elif cmd_raw in ("/tracker", "/tr"):
            reply = _handle_tracker_cmd(conn, args, user_tz)
        elif cmd_raw == "/id":
            reply = _handle_id_cmd(conn, args, user_tz)
        elif cmd_raw in ("/scanner", "/scan"):
            reply = _handle_scanner_cmd(conn, user_tz)
        elif cmd_raw == "/tz":
            if not args:
                reply = f"🕒 Zona waktu Anda saat ini: <code>{tz_lbl}</code>.\nKetik <code>/tz WIB</code> atau <code>/tz ET</code> atau <code>/tz UTC</code> untuk mengubah."
            else:
                _, new_lbl = resolve_timezone(args[0])
                conn.execute(
                    "UPDATE telegram_subscribers SET preferred_tz = ? WHERE chat_id = ?",
                    (args[0].strip().upper(), chat_id),
                )
                conn.commit()
                reply = f"✅ Zona waktu tampilan Anda berhasil diubah ke: <b>{html.escape(new_lbl)}</b> ({html.escape(args[0].upper())})."
        elif cmd_raw == "/subscribe":
            conn.execute(
                "UPDATE telegram_subscribers SET subscribed = 1 WHERE chat_id = ?", (chat_id,)
            )
            conn.commit()
            reply = "✅ <b>Langganan Sinyal Trading Otomatis DIAKTIFKAN!</b>\nAnda akan menerima setiap sinyal Playbook yang berstatus <b>ACTIVE</b> maupun hasil penutupannya secara real-time."
        elif cmd_raw in ("/unsubscribe", "/stop"):
            conn.execute(
                "UPDATE telegram_subscribers SET subscribed = 0 WHERE chat_id = ?", (chat_id,)
            )
            conn.commit()
            reply = "🔕 <b>Langganan Sinyal Otomatis DINONAKTIFKAN.</b>\nAnda tetap dapat menggunakan perintah interaktif seperti <code>/playbook</code> dan <code>/tracker</code> kapan saja. Ketik <code>/subscribe</code> untuk mengaktifkan kembali."
        else:
            reply = "❓ Perintah tidak dikenali. Ketik <code>/help</code> untuk melihat daftar perintah yang tersedia."
    except Exception as e:
        safe_err = redact_telegram_secrets(str(e))
        logger.error(f"Error handling command {cmd_raw} for chat {chat_id}: {safe_err}")
        reply = f"⚠ Terjadi kesalahan saat memproses perintah: <code>{html.escape(safe_err[:120])}</code>"

    _send_message(reply, chat_id)


def run_bot_polling(db_path: str | Path) -> None:
    """Long-polling loop to receive and respond to Telegram commands from any user 24/7."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        logger.warning("TELEGRAM_BOT_TOKEN not set; interactive Telegram bot polling disabled.")
        return

    register_bot_commands_menu()
    offset = 0
    logger.info("Interactive Telegram Bot (@ark_trading_bot) polling started — open to all users.")

    while True:
        try:
            r = requests.get(
                API.format(token=_token(), method="getUpdates"),
                params={"offset": offset, "timeout": 20, "allowed_updates": ["message"]},
                timeout=30,
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue

            updates = data.get("result", [])
            if updates:
                from .. import db

                conn = db.get_conn(db_path, allow_init=True)
                ensure_subscribers_table(conn)
                for upd in updates:
                    offset = max(offset, int(upd["update_id"]) + 1)
                    msg = upd.get("message")
                    if msg:
                        handle_incoming_message(conn, msg)
                conn.close()
        except Exception as e:
            logger.debug(f"Telegram polling transient error: {redact_telegram_secrets(str(e))}")
            time.sleep(5)


def start_bot_background_thread(db_path: str | Path) -> threading.Thread | None:
    """Start the interactive Telegram Bot polling loop in a background daemon thread."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return None
    t = threading.Thread(
        target=run_bot_polling,
        args=(db_path,),
        name="arkwatch-telegram-bot",
        daemon=True,
    )
    t.start()
    return t
