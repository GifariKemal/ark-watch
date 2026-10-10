"""watcher.py — alert watcher: macro/COT triggers with dynamic cooldown.

Triggers: gold↔RY divergence · VIX/VXV backwardation · HY extreme
percentile · COT crowded · Fed-BS reversal · surprise flip · SOMA roll-off ·
SOMA specials-new · SOMA WALCL-gap · options PCR extreme · FX swap-line draw
(urgent) · dealer stress · Fed-ops resume · auction demand weak · extreme
funding · copper stocks drain.
COT bonus: cot_broad_divergence, cot_covering_{gold,spx},
cot_short_cover_{gold,spx}. Dynamic cooldown (1 hour under VIX stress, 6
hours default).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import db

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

URGENT = {"net_liq_reversal", "vix_backwardation", "fx_swap_draw"}

# Trigger thresholds come from config (params_signals.yaml) — the same file the
# brief/signal engine reads; the literals are only the unreadable-config fallback.
try:
    from ..config import load_params_signals

    _PS = load_params_signals()
except Exception:
    _PS = {}
VIX_BACKWARDATION_RATIO = float(_PS.get("vix_backwardation_ratio", 1.0))
HY_EXTREME_PCT_LOW = int(_PS.get("hy_extreme_pct_low", 10))
HY_EXTREME_PCT_HIGH = int(_PS.get("hy_extreme_pct_high", 90))
# funding_extreme_bps orphan load REMOVED 2026-09-17 (round-2: Bybit retired,
# no consumer; the yaml key stays for provenance)
COPPER_DRAIN_20D_PCT = float(_PS.get("copper_drain_20d_pct", -0.15))
COPPER_DRAIN_STREAK_WEEKS = int(_PS.get("copper_drain_streak_weeks", 5))
SOMA_ROLL_OFF_7D_ALERT_B = float(_PS.get("soma_roll_off_7d_alert_b", 65))
SOMA_SPECIALS_NEW_PAR_B = float(_PS.get("soma_specials_new_par_b", 10))
SOMA_WALCL_GAP_ALERT_B = float(_PS.get("soma_walcl_gap_alert_b", 80))
OPTIONS_PCR_MIN_OBS = int(_PS.get("options_pcr_min_obs", 20))
OPTIONS_PCR_EXTREME_RATIO = float(_PS.get("options_pcr_extreme_ratio", 0.30))
DEALER_STRESS_4W_PCT = float(_PS.get("dealer_stress_4w_pct", -3.0))
FED_OPS_RESUME_QUIET_DAYS = int(_PS.get("fed_ops_resume_quiet_days", 30))
FD_AUCTION_WEAK_PCT = float(_PS.get("fd_auction_weak_pct", 10))
ECB_HIGH_CONVICT_PROB = float(_PS.get("ecb_high_conviction_prob", 0.85))
ECB_HIGH_CONVICT_DAYS = int(_PS.get("ecb_high_conviction_days", 7))
COOLDOWN_HOURS_DEFAULT = int(_PS.get("cooldown_hours_default", 6))
COOLDOWN_HOURS_STRESS = int(_PS.get("cooldown_hours_stress", 1))
VIX_STRESS_LEVEL = int(_PS.get("vix_stress_level", 25))
VIX_Z_STRESS = float(_PS.get("vix_z_stress", 1.0))
ESI_FLIP_MIN_ABS = float(_PS.get("esi_flip_min_abs", 0.25))
GOLD_RY_DIV_MIN_BP = float(_PS.get("gold_ry_div_min_bp", 5.0))  # DFII10 pct-points×100
GOLD_RY_DIV_MIN_GOLD_PCT = float(_PS.get("gold_ry_div_min_gold_pct", 0.5))  # ±% over 20d
NET_LIQ_MIN_ABS_B = float(_PS.get("net_liq_min_abs_b", 10.0))  # $B deadband
COT_BROAD_DIV_MIN = int(_PS.get("cot_broad_div_min", 3))
COT_COVERING_NET_MIN = int(_PS.get("cot_covering_net_min", 50000))
COT_COVERING_CHG_MIN = int(_PS.get("cot_covering_chg_min", 10000))


def _dynamic_cooldown_hours(conn) -> int:
    """VIX-conditioned cooldown (Cheng-Kirilenko-Xiong 2015: during VIX spikes
    all categories reposition quickly → COT alerts need a faster cadence too).

    VIX > vix_stress_level (default 25) OR z-VIX(60d) > 1 → stress cooldown
    (default 1 hour); otherwise the default cooldown (6 hours). Levels live in
    params_signals.yaml.
    """
    rows = conn.execute(
        "SELECT value FROM raw_observations WHERE series_id='FRED:VIXCLS' "
        "AND vintage_ts='realtime' ORDER BY ts DESC LIMIT 60"
    ).fetchall()
    vals = [r[0] for r in rows if r[0] is not None]
    if not vals:
        return COOLDOWN_HOURS_DEFAULT
    if vals[0] > VIX_STRESS_LEVEL:
        return COOLDOWN_HOURS_STRESS  # stress = fast repositioning = short cooldown
    if len(vals) >= 40:
        mean = sum(vals) / len(vals)
        std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        if std > 0 and (vals[0] - mean) / std > VIX_Z_STRESS:
            return COOLDOWN_HOURS_STRESS
    return COOLDOWN_HOURS_DEFAULT  # normal


def _cooldown_active(conn, alert_type: str, *, permanent: bool = False) -> bool:
    """Windowed cooldown by default; permanent=True ignores time entirely.

    Weekly-cadence triggers (SOMA) pass permanent=True: a given snapshot's
    number never changes, so any prior delivery of that cooldown_key — however
    old — means the announcement is done. The windowed form alone would
    re-announce the same weekly figure every cooldown window for the
    snapshot's whole freshness life (14d)."""
    if permanent:
        # ROUND-3: the ROUND-2 root-key backward-compat is REVERTED — counting
        # the root form suppressed NEW snapshots forever (any historical bare
        # row permanently silenced every future @-key of that trigger, live:
        # soma_roll_off/gold_ry/copper). Transition coverage is handled by a
        # one-time data migration suffixing bare keys with their snapshot
        # date instead (see docs/audits 2026-09-17).
        row = conn.execute(
            "SELECT COUNT(*) FROM alert_deliveries WHERE cooldown_key=? "
            "AND status IN ('pending','sending','sent','skipped')",
            (alert_type,),
        ).fetchone()
        return row[0] > 0
    cooldown_h = _dynamic_cooldown_hours(conn)
    cutoff = (datetime.now(UTC) - timedelta(hours=cooldown_h)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM alert_deliveries WHERE cooldown_key=? "
        "AND triggered_at > ? AND status IN ('pending','sending','sent','skipped')",
        (alert_type, cutoff),
    ).fetchone()
    return row[0] > 0


def latest_value(conn, sid: str) -> tuple[str, float] | None:
    # Delegate to queries.py
    from ..queries import latest_observation

    return latest_observation(conn, sid)


def recent_values(conn, sid: str, n: int = 5) -> list[float]:
    from ..queries import series_values

    return series_values(conn, sid, n)


def _table_exists(conn, name: str) -> bool:
    """Single-table existence probe — the v9 tables (migration ny-build) land
    separately from this code; every v9 trigger degrades on its own."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row and row[0] == 1)


# Candidate date columns for the (schema-unconfirmed) fxs_operations table —
# probed in order of likelihood; an unrecognized schema = silent no-op rather
# than a crash on a table this layer never defined.
_FXS_DATE_COLS = ("operation_date", "op_date", "trade_date", "date", "ts")


def _fxs_recent_ops(conn, days: int = 7) -> list[tuple[str, float | None]]:
    """(date, amount $B) of FX-swap-line operations inside `days`, ascending.

    Defensive about WHERE the fetch layer stores them: fed_operations rows
    with family='fxs' first, else a dedicated fxs_operations table (date
    column probed). Neither table / no recognizable date column → [] — the
    v9 fetch layer may not have landed, and this tripwire stays silent.
    Swap-line operations are EMPTY in normal times; ANY recent draw is the
    event (11 standby counterparties as of 2026-09-03).
    """
    cutoff = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
    if _table_exists(conn, "fed_operations"):
        rows = conn.execute(
            "SELECT operation_date, amount FROM fed_operations "
            "WHERE family='fxs' AND operation_date > ? ORDER BY operation_date",
            (cutoff,),
        ).fetchall()
        if rows:
            return [(d, None if a is None else a * 1e-9) for d, a in rows]
    if _table_exists(conn, "fxs_operations"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fxs_operations)").fetchall()}
        date_col = next((c for c in _FXS_DATE_COLS if c in cols), None)
        if date_col is None:
            return []
        amt_col = "amount" if "amount" in cols else None
        rows = conn.execute(
            f"SELECT {date_col}{f', {amt_col}' if amt_col else ''} "
            f"FROM fxs_operations WHERE {date_col} > ? ORDER BY {date_col}",
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            d = str(r[0])[:10]
            a = r[1] if (amt_col and len(r) > 1) else None
            out.append((d, None if a is None else a * 1e-9))
        return out
    return []


# Recent window for the fed_ops_resume scan ("latest 14d of Results"); the
# quiet-days threshold itself is config (fed_ops_resume_quiet_days).
FED_OPS_RESUME_RECENT_DAYS = 14


def _fed_ops_resume(conn, quiet_days: int) -> tuple[str, str, str, str] | None:
    """(operation_id, family, direction, op_date) of the EARLIEST Results
    operation in the last FED_OPS_RESUME_RECENT_DAYS whose (family,
    direction) mix had NO prior occurrence within quiet_days before it — a
    desk resuming a side it had not touched for a month (e.g. first outright
    sales after purchases-only, or the ambs desk's first purchases).

    Window anchored on MAX(operation_date), not today: a dead harvest must
    not re-scan a stale 'recent' window daily. fxs rows are excluded (their
    tripwire is fx_swap_draw, not a pacing signal). None = nothing resumed /
    table missing (pre-v9 no-op).

    History-depth gate: proving 'absent for quiet_days' needs quiet_days +
    FED_OPS_RESUME_RECENT_DAYS of stored Results. On a younger table (fresh
    v9 migration, or a cadence so fast that the 14-op fetch window spans
    <30d) a mix gap cannot be distinguished from 'no history' — stay quiet
    instead of announcing a phantom resume.
    """
    if not _table_exists(conn, "fed_operations"):
        return None
    anchor = conn.execute(
        "SELECT MAX(operation_date) FROM fed_operations WHERE status='Results'"
    ).fetchone()[0]
    if not anchor:
        return None
    oldest = conn.execute(
        "SELECT MIN(operation_date) FROM fed_operations WHERE status='Results'"
    ).fetchone()[0]
    if not oldest:
        return None
    if (
        date.fromisoformat(anchor) - date.fromisoformat(oldest)
    ).days < quiet_days + FED_OPS_RESUME_RECENT_DAYS:
        return None
    recent = conn.execute(
        "SELECT operation_id, family, direction, operation_date FROM fed_operations "
        "WHERE status='Results' AND COALESCE(family,'') <> 'fxs' "
        "AND operation_date > date(?, ?) ORDER BY operation_date",
        (anchor, f"-{FED_OPS_RESUME_RECENT_DAYS} day"),
    ).fetchall()
    for op_id, family, direction, op_date in recent:
        if direction is None:
            continue
        prior = conn.execute(
            "SELECT COUNT(*) FROM fed_operations WHERE status='Results' "
            "AND family IS ? AND direction=? AND operation_date < ? "
            "AND operation_date >= date(?, ?)",
            (family, direction, op_date, op_date, f"-{quiet_days} day"),
        ).fetchone()[0]
        if prior == 0:
            return op_id, family, direction, op_date
    return None


@contextmanager
def _trigger(name: str) -> Iterator[None]:
    """Isolate one trigger: its failure is logged and the remaining triggers still run."""
    try:
        yield
    except Exception as ex:
        print(f"⚠ {name} trigger skipped: {type(ex).__name__}: {str(ex)[:120]}")


def _fire(
    conn,
    alert_type: str,
    condition: str,
    context: str,
    action: str,
    cooldown_key: str | None = None,
) -> bool:
    """Send an alert to the outbox unless it is cooling down.

    Alert messages are persisted so failed deliveries can be retried. Without
    persistence, a failed send is lost permanently while still counting toward
    the cooldown window (suppressing the next alert for hours).
    cooldown_key: dedup-key override (default = alert_type). Weekly-cadence
    triggers key per snapshot (e.g. 'soma_roll_off@2026-08-26') and the
    dedup becomes PERMANENT for that key — one announcement per snapshot,
    ever (a snapshot's numbers never change; a time-windowed check alone
    would re-announce the same weekly figure every 6h for 14 days).
    """
    key = cooldown_key or alert_type
    if _cooldown_active(conn, key, permanent=cooldown_key is not None):
        return False
    now = datetime.now(UTC).isoformat(timespec="seconds")
    now_wib = datetime.now(ZoneInfo("Asia/Jakarta"))
    priority = "urgent" if alert_type in URGENT else "normal"
    # Display stamp in WIB (triggered_at stays UTC-based)
    msg = (
        f"⚠ WIB {now_wib.strftime('%H:%M')} · {alert_type.upper()}\n"
        f"{condition}\n{context}\n💡 {action}"
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO alert_deliveries"
            "(alert_type, triggered_at, cooldown_key, priority, status, message)"
            " VALUES (?,?,?,?, 'pending', ?)",
            (alert_type, now, key, priority, msg),
        )
        conn.execute("COMMIT")
    except BaseException:
        # a transaction left open makes every later BEGIN IMMEDIATE in check_all raise
        conn.execute("ROLLBACK")
        raise
    # No Telegram-only fast path: it marked the row 'sent', so ntfy/Discord never got it.
    # The row stays 'pending'; send_pending_alerts (run right after check_all in main and
    # on every watch cycle) broadcasts it to every active channel.
    return True


def check_all(conn) -> list[str]:
    """Run all triggers; return the list of alerts fired."""
    fired = []

    # 1. VIX/VXV backwardation (VIX > VXV)
    with _trigger("vix_backwardation"):
        vix = latest_value(conn, "FRED:VIXCLS")
        vxv = latest_value(conn, "FRED:VXVCLS")
        if vix and vxv and vix[1] > vxv[1]:
            ratio = vix[1] / vxv[1]
            if ratio > VIX_BACKWARDATION_RATIO:
                if _fire(
                    conn,
                    "vix_backwardation",
                    f"VIX {vix[1]:.1f} > VXV {vxv[1]:.1f} (ratio {ratio:.2f})",
                    "Historically: acute short-term stress",
                    "Reduce US100 sizing; no new longs until ratio < 1.0",
                    cooldown_key=f"vix_backwardation@{vix[0][:10]}",
                ):
                    fired.append("vix_backwardation")

    # 2. HY extreme percentile (<p10 or >p90 — thresholds in params_signals.yaml)
    with _trigger("hy_extreme"):
        hy = recent_values(conn, "FRED:BAMLH0A0HYM2", 756)  # 3y window
        hy_lv = latest_value(conn, "FRED:BAMLH0A0HYM2")
        if len(hy) > 100:
            cur = hy[-1]
            pct = sum(1 for v in hy if v <= cur) / len(hy) * 100
            if pct < HY_EXTREME_PCT_LOW:
                if _fire(
                    conn,
                    "hy_extreme_low",
                    f"HY OAS {cur:.2f}% = percentile {pct:.0f} (very tight)",
                    "Very tight spreads = complacency; risk of repricing",
                    "Beware mean-reversion; do not chase risk-on",
                    cooldown_key=f"hy_extreme@{hy_lv[0][:10] if hy_lv else '?'}",
                ):
                    fired.append("hy_extreme_low")
            elif pct > HY_EXTREME_PCT_HIGH:
                if _fire(
                    conn,
                    "hy_extreme_high",
                    f"HY OAS {cur:.2f}% = percentile {pct:.0f} (very wide)",
                    "High credit stress; historical contrarian-buy zone",
                    "Watch for a contrarian entry if it stabilizes",
                    cooldown_key=f"hy_extreme@{hy_lv[0][:10] if hy_lv else '?'}",
                ):
                    fired.append("hy_extreme_high")

    # 3. COT crowded (|z| > cot_crowded_z) — one shared z definition AND one
    # shared threshold for brief + alert (signals/cot_signals)
    latest_cot = None  # trigger 7/8 read it even if this block fails
    with _trigger("cot_crowded"):
        from ..signals.cot_signals import COT_CROWDED_Z, _cot_zscore

        # shared per-report anchor: COT is WEEKLY — a windowed 6h cooldown
        # re-announced the same report 4×/day all week once the daemon went
        # 24/7 on the server (live: cot_broad_divergence 06:06 AND 12:07 WIB
        # from the same 09-11 report). Per-snapshot keys = one announcement
        # per report, ever (the SOMA pattern).
        latest_cot = conn.execute("SELECT MAX(report_date) FROM cot_raw").fetchone()[0]
        for code, name in [("088691", "Gold"), ("084691", "Silver"), ("085692", "Copper")]:
            z = _cot_zscore(conn, code)
            if z is not None and abs(z) > COT_CROWDED_Z:
                direction = "LONG" if z > 0 else "SHORT"
                if _fire(
                    conn,
                    f"cot_crowded_{name.lower()}",
                    f"{name} MM z={z:+.1f} CROWDED {direction}",
                    "Positioning extreme vs 3y history",
                    f"{'Do not chase' if z > 0 else 'Beware a squeeze'}",
                    cooldown_key=f"cot_crowded_{name.lower()}@{latest_cot}",
                ):
                    fired.append(f"cot_crowded_{name.lower()}")

    # 4. Fed-BS reversal (ΔWoW sign change). Computed from ΔWALCL only
    # (RRP/TGA not merged yet → the label reads "Fed BS", not full net-liq;
    # a full net-liq would merge WALCL−RRP−TGA once 3-series weekly
    # alignment is ready)
    with _trigger("net_liq_reversal"):
        walcl = recent_values(conn, "FRED:WALCL", 10)
        walcl_lv = latest_value(conn, "FRED:WALCL")
        if len(walcl) >= 5:
            delta = walcl[-1] - walcl[-5]
            prev_delta = walcl[-5] - walcl[-9] if len(walcl) >= 9 else None
            # ROUND-6: magnitude deadband — ±0.01-0.3% balance-sheet noise fired
            # two contradictory URGENT alerts 18h apart; a reversal must MOVE.
            # ROUND-8: the ×1000 claimed by 9ddd78a never landed (pattern
            # mismatch) — WALCL is stored $M, the threshold is $B; the message's
            # own /1000 display proves the unit convention.
            if (
                prev_delta is not None
                and abs(delta) >= NET_LIQ_MIN_ABS_B * 1000
                and ((delta > 0) != (prev_delta > 0))
            ):
                direction = "EXPANSION" if delta > 0 else "CONTRACTION"
                if _fire(
                    conn,
                    "net_liq_reversal",
                    f"Fed BS ΔWoM: {delta / 1000:+.0f}B (from {prev_delta / 1000:+.0f}B)",
                    f"Reversal to {direction} (liquidity proxy)",
                    "This is a liquidity-regime signal; adjust BTC/index risk",
                    cooldown_key=(f"net_liq_reversal@{walcl_lv[0][:10] if walcl_lv else '?'}"),
                ):
                    fired.append("net_liq_reversal")

    # 4b. Surprise flip — ESI sign change. Requires |ESI| ≥ 0.25 on both
    # sides to avoid noise around zero; the ESI history comes from
    # computed_signals (stored by each brief/watch cycle)
    try:
        from .surprise import store_esi

        esi_now = store_esi(conn)
        esi_rows = conn.execute(
            "SELECT ts, value FROM computed_signals WHERE signal_id='esi' ORDER BY ts DESC LIMIT 2"
        ).fetchall()
        if esi_now is not None and len(esi_rows) >= 2:
            esi_prev = esi_rows[1][1]
            if esi_prev is not None and ((esi_prev <= 0 < esi_now) or (esi_prev >= 0 > esi_now)):
                if abs(esi_now) >= ESI_FLIP_MIN_ABS and abs(esi_prev) >= ESI_FLIP_MIN_ABS:
                    new_dir = "positive" if esi_now > 0 else "negative"
                    if _fire(
                        conn,
                        "surprise_flip",
                        f"ESI {esi_prev:+.2f} → {esi_now:+.2f}",
                        f"Economic data momentum turned {new_dir} (90d decay)",
                        "Data momentum = the regime's prevailing wind; guard against a contra-data bias",
                        cooldown_key=f"surprise_flip@{esi_rows[0][0]}",
                    ):
                        fired.append("surprise_flip")
    except Exception:
        pass

    # 4c-4e. SOMA triggers — one guard so a bad SOMA row cannot kill the
    # later triggers in this watch cycle (same isolation as the surprise
    # trigger). Missing/empty/stale soma tables → None → no fire (pre-v7 safe).
    try:
        from ..signals.soma import (
            soma_roll_off_alert,
            soma_specials_new_alert,
            soma_walcl_gap_alert,
        )

        # 4c. Roll-off wall: par maturing within 7 days exceeds
        # soma_roll_off_7d_alert_b ($65B ≈ p98 of the 108-week backfill).
        soma_alert = soma_roll_off_alert(conn, threshold_b=SOMA_ROLL_OFF_7D_ALERT_B)
        if soma_alert:
            if _fire(
                conn,
                "soma_roll_off",
                f"SOMA roll-off 7d: ${soma_alert['rolling_off_7d_b']:.0f}B "
                f"(as of {soma_alert['as_of_date']})",
                "Treasury supply pressure — issues maturing this week",
                "Watch curve steepening; consider reducing duration",
                cooldown_key=f"soma_roll_off@{soma_alert['as_of_date']}",
            ):
                fired.append("soma_roll_off")

        # 4d. Float scarcity: NEW issues reaching the Fed's 70% ownership cap
        # with par ≥ soma_specials_new_par_b ($10B) — almost no free float
        # left → repo-special candidates. Rare by construction (1 event in
        # 104 live weeks), unlike a plain >30% count (66-93 issues/week).
        sp = soma_specials_new_alert(conn, min_par_b=SOMA_SPECIALS_NEW_PAR_B)
        if sp:
            cusips = ", ".join(e["cusip"] for e in sp["entrants"])
            if _fire(
                conn,
                "soma_specials_new",
                f"{sp['n']} NEW issue(s) at the Fed 70% cap, ${sp['par_b']:.0f}B par ({cusips})",
                "Fed owns nearly all free float of these issues — repo-special candidates",
                "Watch specialness in repo; these issues can trade rich vs the curve",
                cooldown_key=f"soma_specials_new@{sp['as_of_date']}",
            ):
                fired.append("soma_specials_new")

        # 4e. WALCL↔SOMA identity (BUILD-PLAN §6.3): |ΔWALCL − ΔSOMA| beyond
        # soma_walcl_gap_alert_b ($80B > the ±$75B historical envelope) =
        # harvest integrity tripwire — suspect data, not markets.
        wg = soma_walcl_gap_alert(conn, threshold_b=SOMA_WALCL_GAP_ALERT_B)
        if wg:
            if _fire(
                conn,
                "soma_walcl_divergence",
                f"WALCL Δ {wg['dwalcl_b']:+.0f}B vs SOMA Δ {wg['dsoma_b']:+.0f}B "
                f"— gap {wg['gap_b']:+.0f}B (as of {wg['as_of_date']})",
                "Beyond the historical ±$75B envelope — data integrity suspect (broken harvest?)",
                "Verify soma harvest + WALCL fetch before trusting liquidity reads",
                cooldown_key=f"soma_walcl_divergence@{wg['as_of_date']}",
            ):
                fired.append("soma_walcl_divergence")
    except Exception as ex:
        # fail-visible: an import error (e.g. a bad yaml value) or a bad SOMA
        # row must not silently disable all three triggers — the daemon logs
        # this subprocess's stdout
        print(f"⚠ soma triggers skipped: {str(ex)[:120]}")

    # 4f. Options PCR extreme — gold front put/call OI ratio beyond its
    # trailing range by options_pcr_extreme_ratio × range. Needs
    # options_pcr_min_obs prior days, so it is quiet BY CONSTRUCTION until
    # the daily harvest has ~a month of history (kalibrasi after ≥8 weeks).
    # Daily cadence, settlements final → per-day cooldown key (one
    # announcement per trade_date, ever — soma per-snapshot pattern).
    try:
        from ..signals.options import options_pcr_extreme_alert

        opt_alert = options_pcr_extreme_alert(
            conn, min_obs=OPTIONS_PCR_MIN_OBS, ratio=OPTIONS_PCR_EXTREME_RATIO
        )
        if opt_alert:
            dir_txt = (
                "put-side dominates — hedging demand up"
                if opt_alert["direction"] == "PUT_HEAVY"
                else "call-side dominates — one-sided complacency"
            )
            if _fire(
                conn,
                "options_pcr_extreme",
                f"Gold options PCR {opt_alert['pcr']:.2f} vs {opt_alert['n_obs']}d range "
                f"[{opt_alert['hist_min']:.2f}, {opt_alert['hist_max']:.2f}] "
                f"— {opt_alert['direction']}",
                f"Front-contract put/call OI ratio, {dir_txt} (daily positioning read "
                "complementing the weekly COT)",
                "Context, not a trade signal — cross-check COT gold MM z + price action",
                cooldown_key=f"options_pcr_extreme@{opt_alert['trade_date']}",
            ):
                fired.append("options_pcr_extreme")
    except Exception as ex:
        # fail-visible: a bad options row must not silently disable the
        # trigger (same isolation as the SOMA block above)
        print(f"⚠ options triggers skipped: {str(ex)[:120]}")

    # 4g. FX swap-line draw — any fxs operation within 7 days = acute global
    # dollar-funding stress (operations are EMPTY in normal times; only the
    # 14 standby central-bank counterparties exist otherwise). Urgent class.
    # Defensive about storage: fed_operations family='fxs' OR a dedicated
    # fxs_operations table; neither → silent no-op (pre-v9 DB). Cooldown per
    # OPERATION DAY (a multi-day draw announces each day once, ever).
    try:
        fxs_ops = _fxs_recent_ops(conn, days=7)
        if fxs_ops:
            op_date, amt_b = fxs_ops[-1]  # newest draw
            amt_txt = f" ${amt_b:.1f}B" if amt_b is not None else ""
            if _fire(
                conn,
                "fx_swap_draw",
                f"FX swap line draw{amt_txt} on {op_date} — {len(fxs_ops)} op(s) in 7d",
                "Central banks drawing USD swap lines = global dollar-funding stress "
                "(normally zero usage)",
                "Urgent: check XCCY basis + repo/OIS spreads; de-risk USD-negative carry",
                cooldown_key=f"fx_swap_draw@{op_date}",
            ):
                fired.append("fx_swap_draw")
    except Exception as ex:
        print(f"⚠ fxs trigger skipped: {str(ex)[:120]}")

    # 4h. Dealer stress — UST dealer inventory Δ4w at/below
    # dealer_stress_4w_pct (placeholder −3.0% ≈ −$13B on the $436B scale;
    # calibration anchor = the live 2026-08 slide 460→436B, −5%). Weekly
    # cadence → PERMANENT per-snapshot cooldown key (soma pattern: a survey
    # week's number never changes).
    try:
        from ..signals.dealers import UST_KEYID, dealers_snapshot

        snap = dealers_snapshot(conn)
        ust = snap.get(UST_KEYID)
        # freshness cap (soma convention): a restored/old DB must not fire on a
        # stale survey week whose Δ4w happens to clear the threshold
        fresh = ust and (datetime.now(UTC).date() - date.fromisoformat(ust["asofdate"])).days <= 10
        if ust and fresh and ust.get("delta_4w_pct") is not None:
            if ust["delta_4w_pct"] <= DEALER_STRESS_4W_PCT:
                if _fire(
                    conn,
                    "dealer_stress",
                    f"UST dealer inventory ${ust['value_musd'] * 1e-3:.0f}B, Δ4w "
                    f"{ust['delta_4w_musd'] * 1e-3:+.0f}B ({ust['delta_4w_pct']:+.1f}%) "
                    f"(as of {ust['asofdate']})",
                    "Primary dealers shedding Treasury inventory fast — balance-sheet "
                    "capacity tightening",
                    "Watch dealer-capacity proxies: repo specials, auction tails, new-issue concessions",
                    cooldown_key=f"dealer_stress@{ust['asofdate']}",
                ):
                    fired.append("dealer_stress")
    except Exception as ex:
        print(f"⚠ dealer triggers skipped: {str(ex)[:120]}")

    # 4i. Fed ops resume — a Results (family, direction) mix in the latest 14d
    # that was ABSENT the prior fed_ops_resume_quiet_days (first 'S' sales
    # after purchases-only, first ambs purchases, …) = a QT/pacing regime
    # signal. Permanent cooldown keyed on the triggering op's date: the same
    # regime change announces once.
    try:
        resume = _fed_ops_resume(conn, quiet_days=FED_OPS_RESUME_QUIET_DAYS)
        if resume:
            _op_id, family, direction, op_date = resume
            side = "purchases" if direction == "P" else "sales"
            if _fire(
                conn,
                "fed_ops_resume",
                f"Fed {family} desk resumes {side} ({op_date})",
                f"An operation mix absent the prior {FED_OPS_RESUME_QUIET_DAYS}d is back — "
                "QT pace/direction signal",
                "Cross-check the SOMA weekly_change trajectory + upcoming announcements",
                cooldown_key=f"fed_ops_resume@{op_date}",
            ):
                fired.append("fed_ops_resume")
    except Exception as ex:
        print(f"⚠ fed-ops trigger skipped: {str(ex)[:120]}")

    # 4j. Auction demand weak — the latest 10Y (5Y fallback) Treasury auction
    # bid-to-cover at/below percentile fd_auction_weak_pct of the FULL 1979+
    # same-term auction history (fd_auctions, migration v10). Weak-tail demand
    # = absorption stress: the marginal buyer needed a concession, which lands
    # on dealer balance sheets (connects to the DLR dealer-capacity story).
    # Permanent cooldown per AUCTION date — an auction's number never changes
    # (soma per-snapshot pattern); freshness-capped ≤10d so a dead harvest
    # cannot re-alert.
    try:
        from ..signals.fiscal import auction_demand_weak_alert

        fd_alert = auction_demand_weak_alert(conn, weak_pct=FD_AUCTION_WEAK_PCT)
        if fd_alert:
            if _fire(
                conn,
                "auction_demand_weak",
                f"{fd_alert['term']} auction b/c {fd_alert['bid_to_cover']:.2f} = "
                f"p{fd_alert['percentile']:.0f} of {fd_alert['n_history']} auctions "
                f"({fd_alert['auction_date']})",
                "Weak auction demand = absorption stress — the marginal buyer needed a "
                "concession; connects to the dealer-capacity story (see DLR line)",
                "Context, not a trade signal — watch the next auctions' tails + dealer inventory",
                # term in the key: 10Y and a fallback 5Y weak on the SAME
                # auction date must not silence each other
                cooldown_key=(f"auction_demand_weak@{fd_alert['term']}@{fd_alert['auction_date']}"),
            ):
                fired.append("auction_demand_weak")
    except Exception as ex:
        print(f"⚠ fiscal trigger skipped: {str(ex)[:120]}")

    # 5. Extreme funding — RETIRED 2026-09-16 (Bybit unreachable from all our
    # networks; served one alert at 22% fill rate over 23 days). Historical
    # funding values remain in flows_daily but nothing writes new ones.
    # The FUNDING_EXTREME_BPS threshold stays in params_signals.yaml (provenance).

    # 6. Gold↔RY divergence (simple form: RY up & gold up)
    with _trigger("gold_ry_divergence"):
        ry = recent_values(conn, "FRED:DFII10", 60)
        ry_lv = latest_value(conn, "FRED:DFII10")
        gold = conn.execute(
            "SELECT close FROM instrument_prices WHERE symbol='XAUUSD' AND source='EODHD' "
            "AND (SELECT MAX(ts) FROM instrument_prices WHERE symbol='XAUUSD' "
            "AND source='EODHD' AND close IS NOT NULL) >= date('now','-4 day') "
            "ORDER BY ts DESC LIMIT 60"
        ).fetchall()
        if len(ry) >= 20 and len(gold) >= 20:
            # ROUND-6: the gold list is DESC (newest first) — the old
            # gold[-1]-gold[-20] compared the OLDEST sessions in the 60-row
            # window (ancient data, wrong direction). Compare the LATEST 20
            # sessions: newest − 20-sessions-ago.
            ry_m = ry[-1] - ry[-20]
            gold_m = gold[0][0] - gold[19][0]
            # ROUND-6: magnitude gate on the gold leg — a +$4 (+0.09%) drift
            # was announced 8× as a 'structural bid'; require a real move
            gold_pct = gold_m / gold[19][0] if gold[19][0] else 0.0
            if ry_m > GOLD_RY_DIV_MIN_BP / 100 and gold_pct >= GOLD_RY_DIV_MIN_GOLD_PCT:
                if _fire(
                    conn,
                    "gold_ry_divergence",
                    f"Gold +{gold_pct:+.1%} (+{gold_m:.0f}) BUT DFII10 +{ry_m * 100:.0f}bps (20d)",
                    "Divergence: gold rising despite rising real yields = structural bid (CB?)",
                    "Watch PBoC/CB buying in the flows data",
                    cooldown_key=f"gold_ry_divergence@{ry_lv[0][:10] if ry_lv else '?'}",
                ):
                    fired.append("gold_ry_divergence")

    # 7. COT Lev-vs-AM divergence (broad across financials = strong signal)
    # (latest_cot is fetched before trigger 3 — shared per-report anchor)
    with _trigger("cot_broad_divergence"):
        if latest_cot:
            diverge_count = 0
            for code in ("099741", "133741", "13874+", "209742"):
                lev = conn.execute(
                    "SELECT long, short FROM cot_raw WHERE contract_code=? "
                    "AND report_date=? AND report_type='tff' AND category='lev'",
                    (code, latest_cot),
                ).fetchone()
                am = conn.execute(
                    "SELECT long, short FROM cot_raw WHERE contract_code=? "
                    "AND report_date=? AND report_type='tff' AND category='am'",
                    (code, latest_cot),
                ).fetchone()
                if lev and am:
                    lev_net = (lev[0] or 0) - (lev[1] or 0)
                    am_net = (am[0] or 0) - (am[1] or 0)
                    if (lev_net > 0) != (am_net > 0):
                        diverge_count += 1
            if diverge_count >= COT_BROAD_DIV_MIN:  # 3+ financials aligned = strong signal
                if _fire(
                    conn,
                    "cot_broad_divergence",
                    f"{diverge_count}/4 financials: Lev vs AM in opposite directions",
                    "A broad fast-money vs real-money split — resolution = volatility",
                    "Watch for the breaking direction; reduce leverage until it is clear",
                    cooldown_key=f"cot_broad_divergence@{latest_cot}",
                ):
                    fired.append("cot_broad_divergence")

    # 8. COT covering (a large position shrinking fast, >10K/week). Category
    # per report type: Gold = disagg (mm only), SPX = tff (lev only)
    with _trigger("cot_covering"):
        for code, name, cat in [("088691", "Gold", "mm"), ("13874+", "SPX", "lev")]:
            row = (
                conn.execute(
                    "SELECT change_long, change_short, long, short FROM cot_raw "
                    "WHERE contract_code=? AND report_date=? AND category=? "
                    "AND report_type NOT LIKE '%_c'",
                    (code, latest_cot, cat),
                ).fetchone()
                if latest_cot
                else None
            )
            if row and row[2] is not None:
                net = row[2] - (row[3] or 0)
                chg = (row[0] or 0) - (row[1] or 0)
                # Covering = a large position shrinking fast
                if net > COT_COVERING_NET_MIN and chg < -COT_COVERING_CHG_MIN:
                    if _fire(
                        conn,
                        f"cot_covering_{name.lower()}",
                        f"{name} {cat.upper()} net={net:+,} Δ{chg:+,}/w (COVERING)",
                        f"Large {cat.upper()} position being unwound rapidly",
                        "Momentum fade — watch for a reversal",
                        cooldown_key=f"cot_covering_{name.lower()}@{latest_cot}",
                    ):
                        fired.append(f"cot_covering_{name.lower()}")
                elif net < -COT_COVERING_NET_MIN and chg > COT_COVERING_CHG_MIN:
                    if _fire(
                        conn,
                        f"cot_short_cover_{name.lower()}",
                        f"{name} {cat.upper()} net={net:+,} Δ{chg:+,}/w (SHORT COVERING)",
                        "Large short being covered = squeeze potential",
                        "Beware a sustained rally",
                        cooldown_key=f"cot_short_cover_{name.lower()}@{latest_cot}",
                    ):
                        fired.append(f"cot_short_cover_{name.lower()}")

    # 9. Copper stocks drain (LME physical). Drain = 20-trading-day Δ ≤
    # copper_drain_20d_pct (default −15%) OR a ≥copper_drain_streak_weeks
    # (default 5) consecutive down streak; HG curve context from
    # cme_settlements (backwardation corroborates a squeeze) + 3y percentile
    with _trigger("copper_stocks_drain"):
        cu = recent_values(conn, "LME:CA_STOCKS", 800)
        # AUDIT P2 (2026-09-13): the channel is a MONTHLY XLSX (newest point ages
        # 1 day..~5 weeks mid-month) — Δ20d/streak on a frozen window is noise.
        # Gate: skip the trigger entirely when the newest observation is >45 days
        # old (publication stalled), and label the intra-month age otherwise.
        cu_ts = conn.execute(
            "SELECT MAX(ts) FROM raw_observations WHERE series_id='LME:CA_STOCKS'"
            " AND vintage_ts='realtime'"
        ).fetchone()[0]
        cu_age = (
            (datetime.now(UTC).date() - datetime.fromisoformat(cu_ts[:10]).date()).days
            if cu_ts
            else 9999
        )
        if cu_age > 45:
            print(f"  ⚠ copper trigger skipped: LME stocks frozen {cu_age}d ({cu_ts})")
        elif len(cu) >= 60:
            lvl = cu[-1]
            d20 = (cu[-1] / cu[-21] - 1) if len(cu) >= 21 else None
            streak, i = 0, len(cu) - 1  # a week ≈ 5 trading days
            while i - 5 >= 0 and cu[i] < cu[i - 5]:
                streak += 1
                i -= 5
            if (
                d20 is not None and d20 <= COPPER_DRAIN_20D_PCT
            ) or streak >= COPPER_DRAIN_STREAK_WEEKS:
                # Sort contracts by approximate IMM date: alphabetical month order
                # would compare 'APR 27' vs 'APR 28' (distant contracts) and
                # mislabel the curve; same approach as transforms/xccy._imm_approx
                from ..fetchers.cme import PRODUCTS  # lazy: the watcher spawns every 60 s

                hg = PRODUCTS["HG"]
                curve = conn.execute(
                    "SELECT month, settle FROM cme_settlements WHERE product_id=? "
                    "AND trade_date=(SELECT MAX(trade_date) FROM cme_settlements "
                    "WHERE product_id=?)",
                    (hg, hg),
                ).fetchall()
                _MON = {
                    "JAN": 1,
                    "FEB": 2,
                    "MAR": 3,
                    "APR": 4,
                    "MAY": 5,
                    "JUN": 6,
                    "JUL": 7,
                    "AUG": 8,
                    "SEP": 9,
                    "OCT": 10,
                    "NOV": 11,
                    "DEC": 12,
                }

                def _imm(m_code):
                    try:
                        mon, yr = m_code.upper().split()
                        return (2000 + int(yr), _MON[mon])
                    except (ValueError, KeyError, AttributeError):
                        return (9999, 12)

                curve = sorted([c for c in curve if c[1] is not None], key=lambda c: _imm(c[0]))
                if len(curve) >= 2 and curve[0][1] and curve[1][1]:
                    curve_txt = (
                        "backwardation curve (corroborates the squeeze)"
                        if curve[0][1] > curve[1][1]
                        else "contango curve"
                    )
                else:
                    curve_txt = "curve N/A"
                win = cu[-750:] if len(cu) >= 750 else cu
                pct = 100 * sum(1 for v in win if v < lvl) / len(win)
                d20_txt = f"Δ20d {d20:+.0%}" if d20 is not None else ""
                # off-warrant shadow supply (daily OWSR, T+3): thick shadow supply
                # can cap a squeeze (hidden metal can be warranted anytime); thin
                # supply makes the same drain far more serious
                ow = conn.execute(
                    "SELECT period, value FROM flows_periodic WHERE kind='lme_owsr_cu'"
                    " AND period=(SELECT MAX(period) FROM flows_periodic"
                    "             WHERE kind='lme_owsr_cu')"
                ).fetchone()
                ow_txt = ""
                if ow and ow[1] and lvl:
                    ow_txt = f"; off-warrant {ow[1]:,.0f}t = {ow[1] / lvl * 100:.0f}% of LME"
                if _fire(
                    conn,
                    "copper_stocks_drain",
                    f"LME Cu stocks {lvl:,.0f}t ({d20_txt} streak {streak}w, data {cu_age}d old)",
                    f"Physical tightness: {curve_txt}; percentile {pct:.0f} of 3y{ow_txt}",
                    "XCUUSD squeeze-watch: avoid fresh shorts; check COT top-4 HG",
                    cooldown_key=f"copper_stocks_drain@{cu_ts[:10]}",
                ):
                    fired.append("copper_stocks_drain")

    # ECBWatch (D-006 ESTRWatch) — policy-probability triggers. Quiet when
    # no diy_ecb rows exist (funding-NULL convention: a missing source is
    # never an alert).
    with _trigger("ecb_watch"):
        ecb_dates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT date FROM fedwatch_snapshots WHERE source='diy_ecb'"
                " ORDER BY date DESC LIMIT 2"
            ).fetchall()
        ]
        if ecb_dates:

            def _nearest(d: str):
                return conn.execute(
                    "SELECT meeting_date, prob_ease, prob_hold, prob_hike, implied_rate"
                    " FROM fedwatch_snapshots WHERE source='diy_ecb' AND date=?"
                    " AND meeting_date >= date('now') ORDER BY meeting_date LIMIT 1",
                    (d,),
                ).fetchone()

            cur = _nearest(ecb_dates[0])
            if cur:

                def _dominant(r):
                    vals = {"cut": r[1], "hold": r[2], "hike": r[3]}
                    return max(vals, key=vals.get)

                act, prob = _dominant(cur), max(cur[1], cur[2], cur[3])
                # (a) flip: dominant action changed vs the previous snapshot
                # date. Cooldown key = SNAPSHOT date (the SOMA pattern), NOT
                # the meeting: probabilities rewrite daily, so a per-meeting
                # permanent key would swallow a genuine re-flip (hike→cut→hike)
                # for the same meeting — the most tradeable signal of all
                if len(ecb_dates) == 2:
                    prev = _nearest(ecb_dates[1])
                    if prev and _dominant(prev) != act:
                        if _fire(
                            conn,
                            "ecb_watch_flip",
                            f"ECB {cur[0]}: market pricing flipped to {act.upper()}",
                            f"Nearest GC meeting {cur[0]}: {act} {prob:.0%}"
                            f" (prev snapshot {ecb_dates[1]}: {_dominant(prev)})",
                            "EUR crosses / DXY: policy-path repricing in motion",
                            cooldown_key=f"ecb_flip@{ecb_dates[0]}",
                        ):
                            fired.append("ecb_watch_flip")
                # (b) high conviction inside the decision window — one alert
                # per snapshot date (≤1/day across the window)
                if prob >= ECB_HIGH_CONVICT_PROB and act in ("hike", "cut"):
                    days_left = (
                        datetime.fromisoformat(cur[0]).date() - datetime.now(UTC).date()
                    ).days
                    if 0 <= days_left <= ECB_HIGH_CONVICT_DAYS:
                        if _fire(
                            conn,
                            "ecb_high_conviction",
                            f"ECB {cur[0]} (in {days_left}d): {act} priced {prob:.0%}",
                            f"ESR-implied DFR after meeting: {cur[4]:.2f}%"
                            if cur[4] is not None
                            else "implied rate n/a",
                            "EUR-cross book: position for the decision window",
                            cooldown_key=f"ecb_conviction@{ecb_dates[0]}",
                        ):
                            fired.append("ecb_high_conviction")

    # Alert-spam tripwire (round-2): the Telegram channel is the only active
    # delivery path — a dedup regression here spam-burned the user for 17
    # days before anyone noticed. Any cooldown_key delivered >2x within 7d
    # means the suppression machinery itself is broken: announce THAT.
    try:
        spam = conn.execute(
            "SELECT cooldown_key, COUNT(*) c FROM alert_deliveries"
            " WHERE triggered_at > ? AND status IN ('pending','sending','sent')"
            " GROUP BY cooldown_key HAVING c > 2 ORDER BY c DESC LIMIT 3",
            ((datetime.now(UTC) - timedelta(days=7)).isoformat(),),
        ).fetchall()
        if spam:
            txt = "; ".join(f"{k} ×{c}" for k, c in spam)
            if _fire(
                conn,
                "alert_spam_tripwire",
                f"Suppression regression suspected: {txt}",
                "A cooldown_key delivered >2x in 7d — the dedup machinery itself is failing",
                "Check watcher cooldown keys + alert_deliveries before trusting any alert cadence",
                # PERMANENT per offender: the historical rows stay in the 7d
                # window for days — a windowed cooldown would re-announce the
                # same known episode 4x/day. One announcement per key, ever.
                cooldown_key=f"alert_spam_tripwire@{spam[0][0]}",
            ):
                fired.append("alert_spam_tripwire")
    except Exception as ex:
        print(f"⚠ spam tripwire skipped: {str(ex)[:100]}")

    return fired


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch watch")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    conn = db.get_conn(a.db, allow_init=True)
    fired = check_all(conn)
    conn.close()
    # Retry pending alerts on every watch cycle (5-minute per-row pacing in
    # the sender)
    try:
        from ..senders.outbox import send_pending_alerts

        retried = send_pending_alerts(a.db)
        if retried["sent"]:
            print(f"  ↻ retry alerts sent: {retried['sent']}")
    except Exception:
        pass  # no token/env — skipped (the 07:05 send job still runs)
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    if fired:
        print(f"=== watcher {ts}: {len(fired)} alerts fired: {', '.join(fired)} ===")
    else:
        print(f"=== watcher {ts}: 0 alerts (all conditions normal / cooldown) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Legacy aliases
_recent = recent_values
_latest = latest_value
