"""playbook_tracker.py — Automated lifecycle state machine and outcome tracking for trading playbooks.

Tracks trading scenarios from generation through resolution:
  - PENDING_TRIGGER -> Waiting for price action to confirm trigger condition
  - ACTIVE -> Trigger condition verified; tracks MFE (Max Favorable Excursion) and MAE (Max
    Adverse Excursion), always rescanned from the activation bar
  - resolved -> the precise outcome lives in payload_json["outcome"]:
      trades (entry filled): HIT_TARGET_WIN, HIT_STOP_LOSS, HIT_BREAKEVEN, EARLY_FULL_TP,
                             FLIPPED, TIME_EXIT (session expiry, exit at last bar close)
      non-trades:            NO_TRIGGER (session expired), INVALIDATED_PRE_ENTRY, SUPERSEDED,
                             MISSED_ENTRY (fill gapped beyond target)

The `state` column keeps the coarse legacy label because the db.py CHECK constraint only allows
PENDING_TRIGGER / ACTIVE / HIT_TARGET_WIN / HIT_STOP_LOSS / CANCELLED_EXPIRED.

Fill model on OHLC bars (conservative): entry fills at max(trigger, open) for LONG (min for
SHORT); on the activation bar only the adverse extreme counts; a gap through a stop fills at the
open; a stop touched in the same bar as the target or the +1R ratchet wins.

All levels are stored in the symbol's own bar price space; `cfd_basis_offset` is kept only as a
reference column (CFD price = stored level + offset).
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..timezones import format_session_id, format_ts_display, resolve_timezone
from .asof import parse_as_of
from .horizons import cme_session_date

NEWS_SHOCK_THRESHOLD = 0.30  # |intraday catalyst score| that triggers the defensive stop
SWING_EXPIRY_DAYS = 7  # ponytail: calendar days; switch to exchange sessions if swing grows
_ET = ZoneInfo("America/New_York")
_SESSION_CLOSE_ET = time(17, 0)  # CME Globex session close
_OPEN = ("PENDING_TRIGGER", "ACTIVE")

# How a scenario fills, so the track record scores the thesis the scenario states (2026-10-12
# audit: every type filled on a touch, so "sweep above PDH then close back below" was scored as
# "short PDH on touch"). Default "touch": entry at the trigger (or a gapped open).
#   close_beyond   the first 5m CLOSE on the trade side of the trigger; entry at that close
#   sweep_reclaim  a bar trades through the trigger against the trade, then a close back on the
#                  trade side (same bar or later); entry at that close
FILL_RULES = {
    "SCENARIO_INTRADAY_EXPANSION_LONG": "close_beyond",
    "SCENARIO_INTRADAY_EXPANSION_SHORT": "close_beyond",
    "SCENARIO_SWING_CVA_EXPANSION_LONG": "close_beyond",
    "SCENARIO_SWING_CVA_EXPANSION_SHORT": "close_beyond",
    "SCENARIO_INTRADAY_OVERNIGHT_CVA_EXPANSION_LONG": "close_beyond",
    "SCENARIO_INTRADAY_OVERNIGHT_CVA_EXPANSION_SHORT": "close_beyond",
    "SCENARIO_INTRADAY_SWEEP_SHORT": "sweep_reclaim",
    "SCENARIO_INTRADAY_SWEEP_LONG": "sweep_reclaim",
}


def fill_rule(scenario_id: str | None) -> str:
    return FILL_RULES.get(scenario_id or "", "touch")


def _expiry(horizon: str, session_id: str) -> datetime | None:
    """Session close (17:00 ET on the session date); SWING gets SWING_EXPIRY_DAYS more."""
    try:
        d = date.fromisoformat(session_id[:10])
    except ValueError:
        return None
    close = datetime.combine(d, _SESSION_CLOSE_ET, tzinfo=_ET).astimezone(UTC)
    return close + timedelta(days=SWING_EXPIRY_DAYS) if horizon == "SWING" else close


def _update(conn: sqlite3.Connection, uid: str, event: str | None, ts: str, details: str, **cols):
    """Append a decision-log event and update columns. `outcome=` resolves the scenario."""
    row = conn.execute(
        "SELECT payload_json FROM playbook_scenarios WHERE scenario_uid = ?", (uid,)
    ).fetchone()
    try:
        p = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        p = {}
    if event:
        p.setdefault("decision_log", []).append({"ts_utc": ts, "event": event, "details": details})
    outcome = cols.pop("outcome", None)
    if outcome:
        p["outcome"] = outcome
        cols["state"] = (
            outcome if outcome in ("HIT_TARGET_WIN", "HIT_STOP_LOSS") else ("CANCELLED_EXPIRED")
        )
        cols["resolved_at_utc"] = ts
    cols["payload_json"] = json.dumps(p)
    # column names are internal literals, never user input. The state guard keeps a concurrent
    # resolution (api.cancel_playbook on another connection) from being overwritten.
    sets = ", ".join(f"{k} = ?" for k in cols)
    return conn.execute(
        f"UPDATE playbook_scenarios SET {sets} WHERE scenario_uid = ?"
        " AND state IN ('PENDING_TRIGGER', 'ACTIVE')",
        (*cols.values(), uid),
    ).rowcount


def _log_events(conn: sqlite3.Connection, uid: str) -> list[dict]:
    row = conn.execute(
        "SELECT payload_json FROM playbook_scenarios WHERE scenario_uid = ?", (uid,)
    ).fetchone()
    try:
        return json.loads(row[0]).get("decision_log", []) if row and row[0] else []
    except ValueError:
        return []


def _bars(conn: sqlite3.Connection, sym: str, start: str, end: str) -> list[tuple]:
    """5m bars only, one row per timestamp across providers (same rule as levels.py)."""
    rows: dict[str, tuple] = {}
    for r in conn.execute(
        """
        SELECT bar_ts_utc, open, high, low, close, COALESCE(volume, 0.0)
        FROM intraday_bars
        WHERE symbol = ? AND interval = '5m' AND bar_ts_utc >= ? AND bar_ts_utc <= ?
        ORDER BY bar_ts_utc ASC, source ASC
        """,
        (sym, start, end),
    ):
        rows.setdefault(r[0], tuple(r))
    return list(rows.values())


def _simulate(
    direction: str,
    entry: float,
    stop: float,
    target: float,
    bars: list[tuple],
    shock_ts: str | None = None,
    filled_on_open: bool = True,
) -> dict[str, Any]:
    """Walk bars from the activation bar (bars[0]). Pure function, so rescans are idempotent.
    filled_on_open: bars[0] is the bar the fill happened INSIDE (its favourable extreme may
    precede the fill). A close fill passes the bars after the fill bar with False."""
    sign = 1.0 if direction == "LONG" else -1.0
    risk = abs(entry - stop) or 1.0
    res: dict[str, Any] = {"outcome": None, "exit": None, "ts": None, "mfe": 0.0, "mae": 0.0}
    res["partial_ts"] = None
    be = shocked = False
    cur_stop = stop
    pv = vol = 0.0
    vwap_losses = 0
    for i, (ts, o, h, lo, c, v) in enumerate(bars):
        fav, adv = (h, lo) if sign > 0 else (lo, h)
        if i == 0 and filled_on_open:
            o = fav = entry  # activation bar: its favourable extreme may precede the fill
        if shock_ts and ts > shock_ts and not shocked:
            # adverse news known at shock_ts: lock breakeven if in profit, else halve the risk
            shocked = True
            if res["mfe"] > 0:
                be = True
            else:
                cur_stop = entry - sign * 0.5 * risk
        eff_stop = entry if be else cur_stop
        res["mfe"] = max(res["mfe"], sign * (fav - entry))
        res["mae"] = max(res["mae"], sign * (entry - adv))
        res["last_close"], res["last_ts"] = c, ts
        # stop first: a stop touched in the same bar as target / ratchet wins
        if sign * (adv - eff_stop) <= 0:
            fill = o if sign * (o - eff_stop) < 0 else eff_stop  # gap through stop -> open
            res.update(outcome="HIT_BREAKEVEN" if be else "HIT_STOP_LOSS", exit=fill, ts=ts)
            break
        if res["partial_ts"] is None and res["mfe"] >= risk:
            res["partial_ts"] = ts
            be = True  # breakeven ratchet protects from the next bar on
        if sign * (fav - target) >= 0:
            res.update(outcome="HIT_TARGET_WIN", exit=target, ts=ts)
            break
        w = max(1.0, float(v or 0.0))
        pv += (h + lo + c) / 3.0 * w
        vol += w
        # early full exit on two consecutive closes through the anchored VWAP after the partial
        if res["partial_ts"] and sign * (c - pv / vol) < 0:
            vwap_losses += 1
            if vwap_losses >= 2 and sign * (c - entry) > 0:
                res.update(outcome="EARLY_FULL_TP", exit=c, ts=ts)
                break
        else:
            vwap_losses = 0
    return res


def _trade_bars(
    conn: sqlite3.Connection, sym: str, triggered_at: str, end_ts: str, rule: str
) -> tuple[list[tuple], bool]:
    """Bars a filled trade walks, and whether bars[0] is the bar the fill happened inside.
    A close fill happened at the END of its bar: the trade starts with the next bar."""
    bars = _bars(conn, sym, triggered_at, end_ts)
    if rule == "touch":
        return bars, True
    return [b for b in bars if b[0] > triggered_at], False


def _pnl(direction: str, entry: float, exit_p: float, risk: float, partial: bool) -> float:
    """Points PnL of one unit; a partial means half was banked at +1R."""
    raw = (exit_p - entry) if direction == "LONG" else (entry - exit_p)
    return 0.5 * risk + 0.5 * raw if partial else raw


def _close_trade(
    conn, uid, outcome, sim, direction, entry, stop, exit_p, ts, stats, note="", notify=None
):
    """Resolve a filled scenario; R is measured against the risk at the fill (original stop)."""
    risk = abs(entry - stop) or 1.0
    pnl = _pnl(direction, entry, exit_p, risk, bool(sim["partial_ts"]))
    r = round(pnl / risk, 2)
    if _update(
        conn,
        uid,
        outcome,
        ts,
        f"{note}Exit at {exit_p}. Net PnL: {round(pnl, 2)} pts ({r}R). "
        f"MFE: +{round(sim['mfe'], 2)}, MAE: -{round(sim['mae'], 2)}",
        outcome=outcome,
        exit_price=exit_p,
        pnl_points=round(pnl, 2),
        r_multiple=r,
        mfe_points=round(sim["mfe"], 2),
        mae_points=round(sim["mae"], 2),
    ):
        stats[
            "resolved_wins" if r > 0 else "resolved_losses" if r < 0 else "resolved_breakeven"
        ] += 1
        if notify is not None:
            notify.append((uid, outcome, ts, f"Closed at {exit_p} ({round(pnl, 2)} pts / {r}R)"))


def _shock_ts(events: list[dict]) -> str | None:
    return next((e["ts_utc"] for e in events if e.get("event") == "NEWS_SHOCK"), None)


def record_playbook_scenarios(
    conn: sqlite3.Connection,
    playbook_payload: dict[str, Any],
    *,
    cfd_basis_offset: float = 0.0,
) -> list[str]:
    """Persist generated scenarios. Levels must be in bar price space (the tracker compares them
    with the symbol's own bars); the offset is only stored for reference."""
    symbol = playbook_payload["symbol"]
    now_utc = playbook_payload.get("as_of", datetime.now(UTC).isoformat(timespec="seconds"))
    # the CME trading date, never the UTC date: 18:00-20:00 ET already belongs to the next session
    session_id = playbook_payload.get("session_id") or cme_session_date(now_utc).isoformat()

    scenarios = playbook_payload.get("scenarios", [])
    recorded_uids = []

    for sc in scenarios:
        sc_id = sc["id"]
        horizon = sc.get("horizon", "INTRADAY").upper()
        direction = sc["direction"].upper()
        if direction not in ("LONG", "SHORT", "NEUTRAL_RANGE"):
            direction = "NEUTRAL_RANGE"

        target_p = float(sc["target_profit"])
        inval_p = float(sc["invalidation_level"])
        rr = float(sc.get("risk_reward_ratio", 1.0))
        trigger_cond = sc["trigger_condition"]
        trigger_price = float(sc.get("trigger_price", playbook_payload.get("last_price", 0.0)))

        scenario_uid = f"{symbol}-{session_id}-{horizon}-{sc_id}"

        log = [
            {
                "ts_utc": now_utc,
                "event": "CREATED_PENDING",
                "details": f"Scenario created with trigger {trigger_price}, TP {target_p}, SL {inval_p}",
            }
        ]
        prev = conn.execute(
            "SELECT state, trigger_price, target_profit, invalidation_level, payload_json"
            " FROM playbook_scenarios WHERE scenario_uid = ?",
            (scenario_uid,),
        ).fetchone()
        if prev:
            if prev[0] != "PENDING_TRIGGER":
                continue  # left PENDING: its levels are what the trade was judged on
            # a refresh keeps the original creation event; each level change is audited
            with contextlib.suppress(TypeError, ValueError, AttributeError):
                log = json.loads(prev[4]).get("decision_log") or log
            changes = [
                f"{name} {old} -> {new}"
                for name, old, new in (
                    ("trigger", prev[1], trigger_price),
                    ("TP", prev[2], target_p),
                    ("SL", prev[3], inval_p),
                )
                if old != new
            ]
            if changes:
                log.append(
                    {"ts_utc": now_utc, "event": "LEVELS_REVISED", "details": ", ".join(changes)}
                )

        payload_json = json.dumps(
            {
                # which config/params_playbook.yaml vintage built this scenario
                "params_version": (playbook_payload.get("provenance") or {}).get("params_version"),
                "scenario": sc,
                "catalysts": playbook_payload.get("catalysts", {}),
                "multi_domain": playbook_payload.get("multi_domain", {}),
                "amt_context": playbook_payload.get("amt_context", {}),
                "decision_log": log,
            }
        )

        try:
            written = conn.execute(
                """
                INSERT INTO playbook_scenarios (
                    scenario_uid, symbol, horizon, direction, scenario_id, title,
                    trigger_condition, trigger_price, target_profit, invalidation_level,
                    risk_reward_ratio, created_at_utc, session_id, state,
                    cfd_basis_offset, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_TRIGGER', ?, ?)
                ON CONFLICT(scenario_uid) DO UPDATE SET
                    -- revised levels exist from the revision on: bars before it must never fill,
                    -- stop or gap-miss them (the decision log keeps the original creation)
                    created_at_utc=CASE WHEN trigger_price IS NOT excluded.trigger_price
                        OR target_profit IS NOT excluded.target_profit
                        OR invalidation_level IS NOT excluded.invalidation_level
                        THEN excluded.created_at_utc ELSE created_at_utc END,
                    target_profit=excluded.target_profit,
                    invalidation_level=excluded.invalidation_level,
                    trigger_price=excluded.trigger_price,
                    risk_reward_ratio=excluded.risk_reward_ratio,
                    payload_json=excluded.payload_json
                WHERE state = 'PENDING_TRIGGER'
                """,
                (
                    scenario_uid,
                    symbol,
                    horizon,
                    direction,
                    sc_id,
                    sc["title"],
                    trigger_cond,
                    trigger_price,
                    target_p,
                    inval_p,
                    rr,
                    now_utc,
                    session_id,
                    cfd_basis_offset,
                    payload_json,
                ),
            ).rowcount
            if written:  # 0 = a conflict on a row that already left PENDING_TRIGGER
                recorded_uids.append(scenario_uid)
        except sqlite3.Error:
            pass

    conn.commit()
    return recorded_uids


def track_scenarios(
    conn: sqlite3.Connection,
    playbook_payload: dict,
    *,
    as_of: datetime | str | None = None,
    cfd_basis_offset: float = 0.0,
) -> list[str]:
    """Judge open scenarios first, then record this run's set, then judge again.

    The record upsert refreshes a PENDING row's levels, so recording first would re-derive a
    trigger that price already crossed and the fill would be missed (upstream 97e1443). The
    second pass lets a newly recorded scenario see the bars after its creation."""
    evaluate_active_playbooks(conn, as_of=as_of)
    uids = record_playbook_scenarios(conn, playbook_payload, cfd_basis_offset=cfd_basis_offset)
    evaluate_active_playbooks(conn, as_of=as_of)
    return uids


def evaluate_active_playbooks(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, int]:
    """Advance the state machine for all open scenarios using bars up to `as_of`."""
    target_dt = parse_as_of(as_of)
    target_ts = target_dt.isoformat(timespec="seconds")
    stats: dict[str, int] = dict.fromkeys(
        (
            "evaluated",
            "activated",
            "resolved_wins",
            "resolved_losses",
            "resolved_breakeven",
            "resolved_invalidated",
        ),
        0,
    )
    notify: list[tuple] = []  # (uid, outcome or None for an activation, ts, summary)
    # ponytail: rows are processed in creation order; two opposite scenarios triggering in the
    # same evaluation window are not interleaved bar by bar.
    rows = conn.execute(
        """
        SELECT scenario_uid, symbol, horizon, direction, trigger_price, target_profit,
               invalidation_level, created_at_utc, session_id, scenario_id
        FROM playbook_scenarios
        WHERE state IN ('PENDING_TRIGGER', 'ACTIVE')
        ORDER BY created_at_utc, rowid
        """
    ).fetchall()
    stats["evaluated"] = len(rows)

    for uid, sym, horizon, direction, trig_p, target_p, stop_p, created_at, sess_id, sc_id in rows:
        rule = fill_rule(sc_id)
        # fresh state: an earlier iteration in this batch may have resolved or activated it
        cur = conn.execute(
            "SELECT state, entry_price, triggered_at_utc FROM playbook_scenarios"
            " WHERE scenario_uid = ?",
            (uid,),
        ).fetchone()
        if not cur or cur[0] not in _OPEN:
            continue
        state, entry_p, triggered_at = cur
        if parse_as_of(triggered_at or created_at) > target_dt:
            continue  # evaluating an earlier instant must not rewrite a later live state
        expiry = _expiry(horizon, sess_id)
        expired = expiry is not None and target_dt >= expiry
        end_ts = (
            expiry.isoformat(timespec="seconds") if expiry is not None and expired else target_ts
        )
        sign = 1.0 if direction == "LONG" else -1.0

        if state == "PENDING_TRIGGER":
            act_bar = None
            swept = False
            for b in _bars(conn, sym, created_at, end_ts) if direction in ("LONG", "SHORT") else []:
                o, fav, adv = b[1], (b[2] if sign > 0 else b[3]), (b[3] if sign > 0 else b[2])
                stop_hit = sign * (adv - stop_p) <= 0
                if rule == "touch":
                    trig_hit = sign * (fav - trig_p) >= 0
                elif rule == "close_beyond":
                    trig_hit = sign * (b[4] - trig_p) >= 0
                else:  # sweep_reclaim
                    swept = swept or sign * (adv - trig_p) < 0
                    trig_hit = swept and sign * (b[4] - trig_p) > 0
                # a close rule only knows its fill at the close: a stop touched inside that same
                # bar came first
                if stop_hit and (not trig_hit or rule != "touch" or sign * (o - stop_p) <= 0):
                    _update(
                        conn,
                        uid,
                        "INVALIDATED_PRE_ENTRY",
                        b[0],
                        f"Price breached invalidation {stop_p} on bar {b[0]} before trigger {trig_p}",
                        outcome="INVALIDATED_PRE_ENTRY",
                    )
                    stats["resolved_invalidated"] += 1
                    break
                if trig_hit:
                    act_bar = b
                    break
            else:
                if expired:
                    _update(
                        conn,
                        uid,
                        "NO_TRIGGER",
                        end_ts,
                        "Session expired before the trigger",
                        outcome="NO_TRIGGER",
                    )
                    stats["resolved_invalidated"] += 1
            if act_bar is None:
                continue

            trig_time = act_bar[0]
            if rule == "touch":
                entry_p = max(trig_p, act_bar[1]) if sign > 0 else min(trig_p, act_bar[1])
            else:
                entry_p = act_bar[4]
            if sign * (target_p - entry_p) <= 0:
                _update(
                    conn,
                    uid,
                    "MISSED_ENTRY",
                    trig_time,
                    f"Fill {entry_p} already beyond target {target_p} (gap)",
                    outcome="MISSED_ENTRY",
                )
                stats["resolved_invalidated"] += 1
                continue
            if not _update(
                conn,
                uid,
                "TRIGGERED_ACTIVE",
                trig_time,
                f"Trigger met ({rule}), filled at {entry_p} on bar {trig_time}",
                state="ACTIVE",
                triggered_at_utc=trig_time,
                entry_price=entry_p,
                mfe_points=0.0,
                mae_points=0.0,
            ):
                continue  # resolved concurrently (manual cancel): no flip, no supersede
            stats["activated"] += 1
            notify.append((uid, None, trig_time, ""))
            state, triggered_at = "ACTIVE", trig_time
            opposite = "SHORT" if direction == "LONG" else "LONG"

            # 1. Close opposite ACTIVE scenarios at this fill (reversal flip) unless their own
            #    bars already resolved them before this trigger
            for o_uid, o_entry, o_stop, o_target, o_trig_at, o_sc in conn.execute(
                """
                SELECT scenario_uid, entry_price, invalidation_level, target_profit,
                       triggered_at_utc, scenario_id
                FROM playbook_scenarios
                WHERE symbol = ? AND session_id = ? AND horizon = ? AND state = 'ACTIVE'
                  AND direction = ? AND triggered_at_utc < ?
                """,
                (sym, sess_id, horizon, opposite, trig_time),
            ).fetchall():
                o_bars, o_open = _trade_bars(conn, sym, o_trig_at, trig_time, fill_rule(o_sc))
                o_sim = _simulate(
                    opposite,
                    o_entry,
                    o_stop,
                    o_target,
                    [b for b in o_bars if b[0] < trig_time],
                    _shock_ts(_log_events(conn, o_uid)),
                    o_open,
                )
                if o_sim["outcome"] is None:
                    _close_trade(
                        conn,
                        o_uid,
                        "FLIPPED",
                        o_sim,
                        opposite,
                        o_entry,
                        o_stop,
                        entry_p,
                        trig_time,
                        stats,
                        note=f"Opposing setup {uid} triggered. ",
                        notify=notify,
                    )

            # 2. Cancel opposite-direction pending scenarios on the same symbol/session/horizon
            for (o_uid,) in conn.execute(
                """
                SELECT scenario_uid FROM playbook_scenarios
                WHERE symbol = ? AND session_id = ? AND horizon = ?
                  AND state = 'PENDING_TRIGGER' AND direction = ? AND created_at_utc <= ?
                """,
                (sym, sess_id, horizon, opposite, trig_time),
            ).fetchall():
                _update(
                    conn,
                    o_uid,
                    "SUPERSEDED",
                    trig_time,
                    f"Cancelled because opposite scenario {uid} activated first",
                    outcome="SUPERSEDED",
                )
                stats["resolved_invalidated"] += 1

        if state != "ACTIVE" or entry_p is None:
            continue
        events = _log_events(conn, uid)
        bars, on_open = _trade_bars(conn, sym, triggered_at, end_ts, rule)
        sim = _simulate(direction, entry_p, stop_p, target_p, bars, _shock_ts(events), on_open)
        if sim["partial_ts"] and not any(e.get("event") == "PARTIAL_TP_50" for e in events):
            _update(
                conn,
                uid,
                "PARTIAL_TP_50",
                sim["partial_ts"],
                f"Hit +1.0R: scaled out 50% (+0.50R), stop to breakeven at {entry_p}",
            )
        if sim["outcome"]:
            _close_trade(
                conn,
                uid,
                sim["outcome"],
                sim,
                direction,
                entry_p,
                stop_p,
                sim["exit"],
                sim["ts"],
                stats,
                notify=notify,
            )
        elif expired:
            _close_trade(
                conn,
                uid,
                "TIME_EXIT",
                sim,
                direction,
                entry_p,
                stop_p,
                sim.get("last_close", entry_p),
                end_ts,
                stats,
                note="Session expired. ",
                notify=notify,
            )
        else:
            _update(
                conn,
                uid,
                None,
                target_ts,
                "",
                mfe_points=round(sim["mfe"], 2),
                mae_points=round(sim["mae"], 2),
            )
            if _shock_ts(events) is None:
                # live defense: only bars after this instant are affected (no look-ahead)
                try:
                    from .sentiment import compute_intraday_catalyst_radar

                    cat = compute_intraday_catalyst_radar(
                        conn, sym, window_hours=2, as_of=target_dt
                    )
                    score = cat.get("net_stance_score", 0.0)
                except Exception:
                    score = 0.0
                if sign * score <= -NEWS_SHOCK_THRESHOLD:
                    _update(
                        conn,
                        uid,
                        "NEWS_SHOCK",
                        target_ts,
                        f"Adverse catalyst score {score}: breakeven if in profit, else stop"
                        " tightened to 0.5R for subsequent bars",
                    )
    conn.commit()
    # Telegram after the commit (no network call while holding the write lock), and only for
    # events of the last hour: a replay or a catch-up after downtime must not spam old trades.
    live_from = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    for n_uid, n_outcome, n_ts, n_summary in notify:
        if n_ts < live_from:
            continue
        with contextlib.suppress(Exception):
            if n_outcome is None:
                dispatch_telegram_trading_signal(conn, n_uid)
            else:
                dispatch_telegram_outcome_update(conn, n_uid, n_outcome, n_summary)
    conn.commit()  # the dispatch stores the message ids; do not leave the write lock open
    return stats


def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """Wilson score 95% interval for a binomial proportion, in percent."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (centre - half), 1), round(100 * (centre + half), 1)]


def get_playbook_performance_metrics(
    conn: sqlite3.Connection,
    symbol: str | None = None,
    horizon: str | None = None,
    outcome: str | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Trade statistics. A trade = resolved scenario with a fill; every trade is scored by its
    realized r_multiple (win > 0 > loss), whatever its exit type. Non-trades are counted apart."""
    where_clauses = []
    params = []
    if symbol:
        where_clauses.append("symbol = ?")
        params.append(symbol.strip().upper())
    if horizon:
        where_clauses.append("horizon = ?")
        params.append(horizon.strip().upper())
    if outcome:
        out_u = outcome.strip().upper()
        # a trade = filled and resolved, scored by its realized R (as in the totals below)
        trade_sql = (
            "entry_price IS NOT NULL AND state NOT IN ('PENDING_TRIGGER', 'ACTIVE',"
            " 'CANCELLED_MANUAL') AND "
        )
        if out_u in ("WIN", "WINS"):
            where_clauses.append(trade_sql + "r_multiple > 0")
        elif out_u in ("LOSS", "LOSE", "LOSSES"):
            where_clauses.append(trade_sql + "r_multiple < 0")
        elif out_u in ("BE", "BREAKEVEN"):
            where_clauses.append(trade_sql + "COALESCE(r_multiple, 0) = 0")
        elif out_u == "PENDING":
            where_clauses.append("state = 'PENDING_TRIGGER'")
        elif out_u == "ACTIVE":
            where_clauses.append("state = 'ACTIVE'")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = conn.execute(
        f"""
        SELECT state, entry_price, r_multiple, mfe_points, mae_points,
               CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.outcome') END
        FROM playbook_scenarios
        {where_sql}
        """,
        params,
    ).fetchall()

    closed = [r for r in rows if r[0] not in _OPEN]
    # manual cancels carry an operator-chosen exit: counted as non-trades, as in the scorecard
    trades = [r for r in closed if r[1] is not None and r[0] != "CANCELLED_MANUAL"]
    rs = [r[2] or 0.0 for r in trades]
    n = len(rs)
    wins = sum(x > 0 for x in rs)
    losses = sum(x < 0 for x in rs)
    gross_win = sum(x for x in rs if x > 0)
    gross_loss = -sum(x for x in rs if x < 0)
    expectancy = round(sum(rs) / n, 2) if n else None
    non_trades = Counter(r[5] or r[0] for r in closed if r[1] is None or r[0] == "CANCELLED_MANUAL")

    def _avg(i: int) -> float | None:
        if not symbol:  # MFE/MAE are raw points: an SPX and a BTC trade do not average
            return None
        vals = [r[i] for r in trades if r[i] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    res = {
        "total_scenarios": len(rows),
        "completed_trades": n,
        "wins": wins,
        "breakevens": n - wins - losses,
        "losses": losses,
        "invalidated": sum(non_trades.values()),
        "non_trades": dict(non_trades),
        "pending": sum(1 for r in rows if r[0] == "PENDING_TRIGGER"),
        "active": sum(1 for r in rows if r[0] == "ACTIVE"),
        "win_rate_pct": round(100 * wins / n, 1) if n else None,
        "win_rate_ci95_pct": _wilson(wins, n),
        # in R so it is comparable across symbols; None when undefined (no losing trade)
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_r": expectancy,
        "avg_r_multiple": expectancy,
        "avg_mfe": _avg(3),
        "avg_mae": _avg(4),
    }

    if detail:
        trade_rows = conn.execute(
            f"""
            SELECT rowid, scenario_uid, symbol, horizon, direction, scenario_id, title,
                   trigger_price, target_profit, invalidation_level, risk_reward_ratio,
                   state, entry_price, exit_price, pnl_points, r_multiple,
                   mfe_points, mae_points, created_at_utc, triggered_at_utc, resolved_at_utc, payload_json
            FROM playbook_scenarios
            {where_sql}
            ORDER BY rowid DESC
            """,
            params,
        ).fetchall()
        trades_out = []
        for t in trade_rows:
            try:
                p_data = json.loads(t[21]) if t[21] else {}
            except Exception:
                p_data = {}
            trades_out.append(
                {
                    "id": t[0],
                    "uid": t[1],
                    "symbol": t[2],
                    "horizon": t[3],
                    "direction": t[4],
                    "scenario_id": t[5],
                    "title": t[6],
                    "trigger_price": t[7],
                    "target_profit": t[8],
                    "invalidation_level": t[9],
                    "risk_reward_ratio": t[10],
                    "state": t[11],
                    "outcome": p_data.get("outcome"),
                    "entry_price": t[12],
                    "exit_price": t[13],
                    "pnl_points": t[14],
                    "r_multiple": t[15],
                    "mfe_points": t[16],
                    "mae_points": t[17],
                    "created_at_utc": t[18],
                    "triggered_at_utc": t[19],
                    "resolved_at_utc": t[20],
                    "decision_log": p_data.get("decision_log", []),
                }
            )
        breakdowns = build_tracker_breakdowns(trades_out)
        res["breakdown_by_symbol"] = breakdowns["by_symbol"]
        res["breakdown_by_scenario"] = breakdowns["by_scenario"]
        res["trades"] = trades_out

    return res


def build_tracker_breakdowns(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Breakdowns by symbol and by scenario. Same trade definition as the totals: a filled,
    resolved scenario (manual cancels excluded), scored by its realized R."""
    from collections import defaultdict

    def _table(key: str, label: str) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in trades:
            groups[str(t.get(key) or "UNKNOWN").replace("SCENARIO_", "")].append(t)
        out = []
        for name, ts in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True):
            done = [
                t
                for t in ts
                if t.get("state") not in _OPEN
                and t.get("entry_price") is not None
                and t.get("state") != "CANCELLED_MANUAL"
            ]
            rs = [t.get("r_multiple") or 0.0 for t in done]
            wins, losses = sum(r > 0 for r in rs), sum(r < 0 for r in rs)
            gross_loss = -sum(r for r in rs if r < 0)
            out.append(
                {
                    label: name,
                    "total_scenarios": len(ts),
                    "completed_trades": len(rs),
                    "wins": wins,
                    "losses": losses,
                    "breakevens": len(rs) - wins - losses,
                    "win_rate_pct": round(100 * wins / len(rs), 1) if rs else 0.0,
                    "net_pnl_points": round(sum(t.get("pnl_points") or 0.0 for t in done), 2),
                    # in R, as in the totals; None when undefined (no losing trade)
                    "profit_factor": round(sum(r for r in rs if r > 0) / gross_loss, 2)
                    if gross_loss
                    else None,
                    "avg_r_multiple": round(sum(rs) / len(rs), 2) if rs else 0.0,
                }
            )
        return out

    symbol_table = _table("symbol", "symbol")
    scenario_table = _table("scenario_id", "scenario")

    return {"by_symbol": symbol_table, "by_scenario": scenario_table}


def get_trade_by_uid(conn: sqlite3.Connection, identifier: str | int) -> dict[str, Any] | None:
    """Fetch exact trade row and parsed decision log by short numeric ID or scenario UID."""
    raw_str = str(identifier).strip().lstrip("#")
    if raw_str.isdigit():
        row = conn.execute(
            """
            SELECT rowid, scenario_uid, symbol, horizon, direction, scenario_id, title,
                   trigger_price, target_profit, invalidation_level, risk_reward_ratio,
                   state, entry_price, exit_price, pnl_points, r_multiple,
                   mfe_points, mae_points, created_at_utc, triggered_at_utc, resolved_at_utc, payload_json
            FROM playbook_scenarios
            WHERE rowid = ?
            """,
            (int(raw_str),),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT rowid, scenario_uid, symbol, horizon, direction, scenario_id, title,
                   trigger_price, target_profit, invalidation_level, risk_reward_ratio,
                   state, entry_price, exit_price, pnl_points, r_multiple,
                   mfe_points, mae_points, created_at_utc, triggered_at_utc, resolved_at_utc, payload_json
            FROM playbook_scenarios
            WHERE scenario_uid = ? OR scenario_uid LIKE ?
            """,
            (raw_str, f"%{raw_str}%"),
        ).fetchone()
    if not row:
        return None
    try:
        p_data = json.loads(row[21]) if row[21] else {}
    except Exception:
        p_data = {}
    return {
        "id": row[0],
        "uid": row[1],
        "symbol": row[2],
        "horizon": row[3],
        "direction": row[4],
        "scenario_id": row[5],
        "title": row[6],
        "trigger_price": row[7],
        "target_profit": row[8],
        "invalidation_level": row[9],
        "risk_reward_ratio": row[10],
        "state": row[11],
        "entry_price": row[12],
        "exit_price": row[13],
        "pnl_points": row[14],
        "r_multiple": row[15],
        "mfe_points": row[16],
        "mae_points": row[17],
        "created_at_utc": row[18],
        "triggered_at_utc": row[19],
        "resolved_at_utc": row[20],
        "decision_log": p_data.get("decision_log", []),
    }


def format_tracker_detailed_report(
    res: dict[str, Any],
    display_tz: str | None = None,
    limit_trades: int = 15,
) -> str:
    """Format full tracker metrics, asset breakdown, setup breakdown, and journal into terminal dashboard."""
    _, tz_lbl = resolve_timezone(display_tz)

    lines = []
    lines.append("=" * 105)
    lines.append(
        f"                    ARK-WATCH PLAYBOOK PERFORMANCE TRACKER REPORT (TZ: {tz_lbl})"
    )
    lines.append("=" * 105)
    lines.append(
        f"  Total Skenario   : {res.get('total_scenarios', 0):<4}                    "
        f"Win Rate        : {res.get('win_rate_pct', 0.0)}% ({res.get('wins', 0)}W / {res.get('losses', 0)}L / {res.get('breakevens', 0)}BE)"
    )
    lines.append(
        f"  Completed Trades : {res.get('completed_trades', 0):<4}                    "
        f"Profit Factor   : {res.get('profit_factor', 0.0)}"
    )
    lines.append(
        f"  Pending Trigger  : {res.get('pending', 0):<4}                    "
        f"Avg R-Multiple  : {res.get('avg_r_multiple') or 0.0:+.2f}R"
    )
    lines.append(
        f"  Active Running   : {res.get('active', 0):<4}                    "
        f"Avg MFE / MAE   : +{res.get('avg_mfe', 0.0)} pts / -{res.get('avg_mae', 0.0)} pts"
    )
    lines.append(f"  Invalidated/Exp  : {res.get('invalidated', 0):<4}")
    lines.append("-" * 105)

    # 1. Asset Breakdown
    by_sym = res.get("breakdown_by_symbol", [])
    if by_sym:
        lines.append("📈 PERFORMA PER INSTRUMEN (ASSET BREAKDOWN):")
        lines.append(
            f"  {'Simbol':<8} | {'Total':>5} | {'Done':>5} | {'Win':>4} | {'Loss':>4} | {'BE':>3} | {'WinRate':>7} | {'Net PnL':>11} | {'ProfitFac':>9} | {'Avg R':>7}"
        )
        lines.append("  " + "-" * 95)
        for d in by_sym:
            lines.append(
                f"  {d['symbol']:<8} | {d['total_scenarios']:>5} | {d['completed_trades']:>5} | {d['wins']:>4} | {d['losses']:>4} | {d['breakevens']:>3} | {d['win_rate_pct']:>6.1f}% | {d['net_pnl_points']:>11.2f} | {'-' if d['profit_factor'] is None else format(d['profit_factor'], '.2f'):>9} | {d['avg_r_multiple']:>+6.2f}R"
            )
        lines.append("-" * 105)

    # 2. Setup Breakdown
    by_sc = res.get("breakdown_by_scenario", [])
    if by_sc:
        lines.append("🎯 PERFORMA PER TIPE SETUP / SKENARIO LELANG (SETUP BREAKDOWN):")
        lines.append(
            f"  {'Setup Pattern':<44} | {'Trades':>6} | {'Done':>5} | {'Win':>4} | {'Loss':>4} | {'BE':>3} | {'WinRate':>7} | {'Avg R':>7}"
        )
        lines.append("  " + "-" * 95)
        for d in by_sc:
            lines.append(
                f"  {d['scenario']:<44} | {d['total_scenarios']:>6} | {d['completed_trades']:>5} | {d['wins']:>4} | {d['losses']:>4} | {d['breakevens']:>3} | {d['win_rate_pct']:>6.1f}% | {d['avg_r_multiple']:>+6.2f}R"
            )
        lines.append("-" * 105)

    # 3. Recent Trades Journal
    trades = res.get("trades", [])
    if trades:
        lines.append(f"📜 JURNAL TRANSAKSI TERAKHIR (RECENT TRADES JOURNAL - Waktu: {tz_lbl}):")
        lines.append(
            f"  {'#ID':<5} | {'Waktu Selesai':<18} | {'Sym':<6} | {'Dir':<5} | {'Setup Name':<28} | {'Entry':>9} | {'Exit':>9} | {'PnL Pts':>8} | {'R-Mult':>6} | {'Status':<14}"
        )
        lines.append("  " + "-" * 120)
        for t in trades[:limit_trades]:
            t_id = f"#{t.get('id', '-')}"
            ts_res = t.get("resolved_at_utc")
            ts_str = format_session_id(ts_res, display_tz) if ts_res else "RUNNING"
            sym = t.get("symbol", "-")
            d = t.get("direction", "-")
            title = t.get("title", t.get("scenario_id", "-"))[:28]
            ent = f"{t['entry_price']:.2f}" if t.get("entry_price") is not None else "-"
            ex = f"{t['exit_price']:.2f}" if t.get("exit_price") is not None else "-"
            pnl = f"{t['pnl_points']:+.2f}" if t.get("pnl_points") is not None else "-"
            r = f"{t['r_multiple']:+.2f}R" if t.get("r_multiple") is not None else "-"
            st = t.get("state", "-").replace("HIT_TARGET_", "").replace("CANCELLED_", "")
            lines.append(
                f"  {t_id:<5} | {ts_str:<18} | {sym:<6} | {d:<5} | {title:<28} | {ent:>9} | {ex:>9} | {pnl:>8} | {r:>6} | {st:<14}"
            )
        lines.append("=" * 105)

    return "\n".join(lines)


def format_trade_decision_log(trade: dict[str, Any], display_tz: str | None = None) -> str:
    """Format comprehensive chronological decision log for a single trade scenario."""
    _, tz_lbl = resolve_timezone(display_tz)
    lines = []
    lines.append("=" * 95)
    lines.append(
        f"                    DETAIL AUDIT TRANSAKSI #{trade.get('id')}: {trade.get('uid')}"
    )
    lines.append(
        f"  Simbol    : {trade.get('symbol')} ({trade.get('horizon')})           "
        f"Arah       : {trade.get('direction')}"
    )
    lines.append(f"  Setup     : {trade.get('title')}")
    lines.append(
        f"  Status    : {trade.get('state')}                     "
        f"Risk/Reward: 1 : {trade.get('risk_reward_ratio')}"
    )
    lines.append(
        f"  Trigger   : {trade.get('trigger_price')}                  "
        f"Take Profit: {trade.get('target_profit')}  |  Stop Loss: {trade.get('invalidation_level')}"
    )
    ent = f"{trade['entry_price']:.2f}" if trade.get("entry_price") is not None else "-"
    ex = f"{trade['exit_price']:.2f}" if trade.get("exit_price") is not None else "-"
    pnl = f"{trade['pnl_points']:+.2f}" if trade.get("pnl_points") is not None else "-"
    r_val = f"{trade['r_multiple']:+.2f}R" if trade.get("r_multiple") is not None else "-"
    lines.append(f"  Eksekusi  : Entry {ent} -> Exit {ex} | PnL: {pnl} pts ({r_val})")
    lines.append(
        f"  Ekskursi  : MFE (Max Run-up) +{trade.get('mfe_points', 0)} pts  |  MAE (Max Drawdown) -{trade.get('mae_points', 0)} pts"
    )
    lines.append("-" * 95)
    lines.append(f"📜 KRONOLOGI DECISION LOG RIIL (Waktu: {tz_lbl}):")
    logs = trade.get("decision_log", [])
    if logs:
        for item in logs:
            ts_str = format_session_id(item.get("ts_utc", ""), display_tz)
            lines.append(f"  [{ts_str}] {item.get('event', ''):<20} -> {item.get('details', '')}")
    else:
        lines.append("  (Belum ada decision log tercatat)")
    lines.append("=" * 95)
    return "\n".join(lines)


def dispatch_telegram_trading_signal(
    conn: sqlite3.Connection,
    scenario_uid: str,
    *,
    display_tz: str | None = None,
) -> bool:
    """Send an institutional formatted trading signal directly to Telegram when a scenario becomes ACTIVE."""
    import html
    import os

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False

    trade = get_trade_by_uid(conn, scenario_uid)
    if not trade:
        return False

    trade_id = trade.get("id", "-")
    sym = trade.get("symbol", "-")
    direction = str(trade.get("direction", "-")).upper()
    dir_emoji = (
        "🟢 LONG" if direction == "LONG" else ("🔴 SHORT" if direction == "SHORT" else direction)
    )
    title = html.escape(str(trade.get("title", "Trading Setup")))
    horizon = trade.get("horizon", "INTRADAY")
    trig_time = trade.get("triggered_at_utc") or trade.get("created_at_utc")
    time_str = format_ts_display(trig_time, display_tz) if trig_time else "Just Now"

    entry_p = trade.get("entry_price") or trade.get("trigger_price")
    target_p = trade.get("target_profit")
    inval_p = trade.get("invalidation_level")
    rr = trade.get("risk_reward_ratio")

    ent_str = f"{entry_p:,.2f}" if isinstance(entry_p, int | float) else str(entry_p)
    tp_str = f"{target_p:,.2f}" if isinstance(target_p, int | float) else str(target_p)
    sl_str = f"{inval_p:,.2f}" if isinstance(inval_p, int | float) else str(inval_p)

    payload = conn.execute(
        "SELECT payload_json FROM playbook_scenarios WHERE scenario_uid = ?", (scenario_uid,)
    ).fetchone()
    p_data = json.loads(payload[0]) if payload and payload[0] else {}
    sc_data = p_data.get("scenario", {})
    decision_val = sc_data.get("decision_system_validation", {})
    m_struct = decision_val.get("market_structure", {})
    m15_trend = m_struct.get("m15_trend", "N/A")
    struct_conf = m_struct.get("structural_confluence", "N/A")
    m_accept = decision_val.get("multi_horizon_acceptance", {})
    accept_lon = m_accept.get("london_desk", "N/A")
    tg = sc_data.get("timing_gate", {})
    timing_cycle = tg.get("current_cycle") or tg.get("recommended_quarter") or "Active Session"
    magnets = decision_val.get("liquidity_magnets", {})
    t1_npoc = magnets.get("tier1_90m_npoc")
    npoc_str = (
        f"90m NPOC at {t1_npoc.get('poc', 'N/A')}"
        if isinstance(t1_npoc, dict)
        else "Dynamic Target"
    )

    trig_cond = html.escape(
        str(
            trade.get("trigger_condition")
            or sc_data.get("trigger_condition", "Level Breakout / Acceptance")
        )
    )

    msg = (
        f"🚨 <b>ARK-WATCH TRADING SIGNAL [ACTIVE]</b> 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Aset :</b> <code>{sym}</code> (#{trade_id})\n"
        f"⚡ <b>Arah :</b> <b>{dir_emoji}</b>\n"
        f"🎯 <b>Setup :</b> {title}\n"
        f"⏱ <b>Horizon :</b> {horizon}\n"
        f"🕒 <b>Waktu Masuk :</b> {time_str}\n\n"
        f"📊 <b>LEVEL EKSEKUSI:</b>\n"
        f"  • <b>Entry Price :</b> <code>{ent_str}</code>\n"
        f"  • <b>Take Profit :</b> <code>{tp_str}</code>\n"
        f"  • <b>Stop Loss   :</b> <code>{sl_str}</code>\n"
        f"  • <b>Risk/Reward :</b> <b>1 : {rr}</b>\n\n"
        f"🧠 <b>VALIDASI SISTEM (AMT & STRUKTUR):</b>\n"
        f"  • <b>Struktur M15 :</b> {m15_trend} ({struct_conf})\n"
        f"  • <b>Penerimaan   :</b> {accept_lon}\n"
        f"  • <b>Pintu Waktu  :</b> {timing_cycle}\n"
        f"  • <b>Magnet POC   :</b> {npoc_str}\n\n"
        f"💡 <b>Pemicu:</b>\n"
        f"<i>{trig_cond}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <i>ark-watch auto-signal engine</i>"
    )

    try:
        from ..senders.telegram import broadcast_message

        mids = broadcast_message(conn, msg)
        if mids:
            p_data.setdefault("decision_log", []).append(
                {
                    "ts_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "event": "TELEGRAM_SIGNAL_DISPATCHED",
                    "details": f"Telegram trading signal broadcasted to {len(mids)} subscriber(s) (Msg IDs: {mids})",
                }
            )
            p_data["telegram_message_ids"] = mids
            p_data["telegram_sent_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE playbook_scenarios SET payload_json = ? WHERE scenario_uid = ?",
                (json.dumps(p_data), scenario_uid),
            )
            return True
    except Exception as e:
        print(f"  ⚠ Telegram dispatch error: {e}")
        return False
    return False


def dispatch_telegram_outcome_update(
    conn: sqlite3.Connection,
    scenario_uid: str,
    outcome_event: str,
    details_str: str,
    *,
    display_tz: str | None = None,
) -> bool:
    """Send outcome / exit notification directly to Telegram when a trade closes."""
    import html
    import os

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False

    trade = get_trade_by_uid(conn, scenario_uid)
    if not trade:
        return False

    trade_id = trade.get("id", "-")
    sym = trade.get("symbol", "-")
    direction = str(trade.get("direction", "-")).upper()
    dir_emoji = (
        "🟢 LONG" if direction == "LONG" else ("🔴 SHORT" if direction == "SHORT" else direction)
    )
    title = html.escape(str(trade.get("title", "Trading Setup")))
    pnl = trade.get("pnl_points") or 0.0
    r_mult = trade.get("r_multiple") or 0.0

    if outcome_event in ("HIT_TARGET_WIN", "EARLY_FULL_TP", "PARTIAL_TP_50"):
        head = "🎯 <b>TRADE UPDATE: TAKE PROFIT HIT [WIN]</b>"
        pnl_str = f"<b>+{pnl:,.2f} pts (+{r_mult:.2f}R)</b>"
    elif outcome_event == "HIT_BREAKEVEN":
        head = "🛡 <b>TRADE UPDATE: BREAK-EVEN EXIT [BE]</b>"
        pnl_str = f"<b>+{pnl:,.2f} pts (+{r_mult:.2f}R)</b>"
    elif outcome_event == "HIT_STOP_LOSS":
        head = "🛑 <b>TRADE UPDATE: STOP LOSS HIT [LOSS]</b>"
        pnl_str = f"<b>{pnl:,.2f} pts ({r_mult:.2f}R)</b>"
    else:  # FLIPPED / TIME_EXIT: exit at market, labelled by its realized R
        tag = "WIN" if r_mult > 0 else "LOSS" if r_mult < 0 else "BE"
        head = f"🔁 <b>TRADE UPDATE: {html.escape(outcome_event)} [{tag}]</b>"
        pnl_str = f"<b>{pnl:+,.2f} pts ({r_mult:+.2f}R)</b>"

    exit_p = trade.get("exit_price")
    exit_str = f"{exit_p:,.2f}" if isinstance(exit_p, int | float) else str(exit_p)

    msg = (
        f"{head}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Aset :</b> <code>{sym}</code> (#{trade_id})\n"
        f"⚡ <b>Arah :</b> <b>{dir_emoji}</b>\n"
        f"🎯 <b>Setup :</b> {title}\n"
        f"🚪 <b>Exit Price :</b> <code>{exit_str}</code>\n"
        f"💰 <b>Hasil PnL  :</b> {pnl_str}\n\n"
        f"📋 <b>Detail:</b>\n"
        f"<i>{html.escape(details_str)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <i>ark-watch auto-signal engine</i>"
    )

    try:
        from ..senders.telegram import broadcast_message

        mids = broadcast_message(conn, msg)
        return bool(mids)
    except Exception as e:
        print(f"  ⚠ Telegram outcome update error: {e}")
        return False


def evaluate_counterfactual_outcomes(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
    forward_hours: int = 2,
) -> dict[str, int]:
    """Evaluate subsequent 2h-4h price behavior after exit to audit whether stop-loss/invalidation was justified."""
    target_dt = parse_as_of(as_of)

    target_ts = target_dt.isoformat(timespec="seconds")

    # Find resolved trades
    rows = conn.execute(
        """
        SELECT scenario_uid, symbol, direction, target_profit, invalidation_level,
               entry_price, exit_price, state, resolved_at_utc, payload_json
        FROM playbook_scenarios
        WHERE state NOT IN ('PENDING_TRIGGER', 'ACTIVE', 'CANCELLED_MANUAL')
          AND entry_price IS NOT NULL AND resolved_at_utc IS NOT NULL
        """
    ).fetchall()

    stats = {
        "audited": 0,
        "good_stop_loss": 0,
        "whipsaw_stop": 0,
        "clean_win": 0,
        "runner_continuation": 0,
    }

    for r in rows:
        uid, sym, direction, target_p, inval_p, entry_p, exit_p, state, resolved_ts, payload_str = r
        try:
            payload = json.loads(payload_str)
        except Exception:
            payload = {}

        # Skip if already audited
        if "counterfactual_audit" in payload:
            continue
        # breakeven / early-TP exits are stored as CANCELLED_EXPIRED: the payload holds the truth
        outcome = payload.get("outcome") or state

        # Fetch bars in forward window after resolution
        resolved_dt = parse_as_of(resolved_ts)
        window_end_dt = resolved_dt + timedelta(hours=forward_hours)
        # Only audit if window has elapsed
        if target_dt < window_end_dt:
            continue

        cf_bars = _bars(conn, sym, resolved_ts, window_end_dt.isoformat(timespec="seconds"))

        if len(cf_bars) < 6:
            continue

        cf_high = max(b[2] for b in cf_bars)
        cf_low = min(b[3] for b in cf_bars)
        cf_close = cf_bars[-1][4]
        risk_dist = abs(entry_p - inval_p) if (entry_p and inval_p) else 10.0

        verdict = "NEUTRAL_CONSOLIDATION"
        reason = "Price hovered near exit level during post-trade window."

        if outcome in ("HIT_STOP_LOSS", "HIT_BREAKEVEN"):
            if direction == "LONG":
                # Did price drop further after stop loss?
                if cf_low < exit_p - (0.25 * risk_dist):
                    verdict = "GOOD_STOP_LOSS (Capital Saved)"
                    reason = f"Price continued to decline to {round(cf_low, 2)} after stop-loss. Cut-loss prevented deeper drawdown."
                    stats["good_stop_loss"] += 1
                # Did price reverse back and hit the original target?
                elif cf_high >= target_p:
                    verdict = "WHIPSAW_STOP (Bad Stop Placement)"
                    reason = f"Price reversed after stop-out and reached target profit ({target_p}). Stop loss was placed too tightly on a wick."
                    stats["whipsaw_stop"] += 1
            elif direction == "SHORT":
                if cf_high > exit_p + (0.25 * risk_dist):
                    verdict = "GOOD_STOP_LOSS (Capital Saved)"
                    reason = f"Price continued to rally to {round(cf_high, 2)} after stop-loss. Cut-loss prevented deeper drawdown."
                    stats["good_stop_loss"] += 1
                elif cf_low <= target_p:
                    verdict = "WHIPSAW_STOP (Bad Stop Placement)"
                    reason = f"Price reversed after stop-out and reached target profit ({target_p}). Stop loss was placed too tightly on a wick."
                    stats["whipsaw_stop"] += 1

        elif outcome in ("HIT_TARGET_WIN", "EARLY_FULL_TP"):
            if direction == "LONG":
                if cf_high > target_p + (0.50 * risk_dist):
                    verdict = "RUNNER_CONTINUATION (Extended Win)"
                    reason = f"Price continued advancing to {round(cf_high, 2)} after target hit. Setup had additional continuation potential."
                    stats["runner_continuation"] += 1
                else:
                    verdict = "CLEAN_WIN (Optimal Exit)"
                    reason = (
                        "Target profit was hit at the auction extreme before price consolidated."
                    )
                    stats["clean_win"] += 1
            elif direction == "SHORT":
                if cf_low < target_p - (0.50 * risk_dist):
                    verdict = "RUNNER_CONTINUATION (Extended Win)"
                    reason = f"Price continued dropping to {round(cf_low, 2)} after target hit. Setup had additional continuation potential."
                    stats["runner_continuation"] += 1
                else:
                    verdict = "CLEAN_WIN (Optimal Exit)"
                    reason = (
                        "Target profit was hit at the auction extreme before price consolidated."
                    )
                    stats["clean_win"] += 1

        payload["counterfactual_audit"] = {
            "verdict": verdict,
            "reason": reason,
            "post_exit_high": round(cf_high, 4),
            "post_exit_low": round(cf_low, 4),
            "post_exit_close": round(cf_close, 4),
            "forward_bars_evaluated": len(cf_bars),
            "audited_at_utc": target_ts,
        }

        conn.execute(
            "UPDATE playbook_scenarios SET payload_json = ? WHERE scenario_uid = ?",
            (json.dumps(payload), uid),
        )
        stats["audited"] += 1

    conn.commit()
    return stats


def scan_market_opportunities(
    conn: sqlite3.Connection,
    *,
    symbols: list[str] | tuple[str, ...] | None = None,
    as_of: datetime | str | None = None,
    min_rr: float = 1.5,
) -> list[dict[str, Any]]:
    """Continuous Opportunity Scanner: Scans the tracked book and returns active/imminent trade opportunities."""
    from .playbook import generate_trading_playbook

    target_symbols = symbols or ("NQ1", "ES1", "YM1", "GC1", "CL1", "BTCUSD", "ETHUSD", "EURUSD")
    opportunities = []

    for sym in target_symbols:
        pb = generate_trading_playbook(conn, sym, as_of=as_of)
        if not pb:
            continue

        scenarios = pb.get("scenarios", [])
        last_price = pb.get("last_price", 0.0)
        confluence_status = pb.get("multi_domain", {}).get("confluence_status", {})
        alignment_state = confluence_status.get("alignment_state", "NEUTRAL_BALANCED")
        amt_ctx = pb.get("amt_context", {})

        # Filter out halt states
        if alignment_state == "EVENT_HALT_REQUIRED":
            continue

        for sc in scenarios:
            # Skip pure chop rotations unless explicitly high R:R
            if sc.get("direction") == "NEUTRAL_RANGE":
                continue

            rr = float(sc.get("risk_reward_ratio", 1.0))
            if rr < min_rr:
                continue

            opportunities.append(
                {
                    "symbol": sym,
                    "horizon": sc.get("horizon", "INTRADAY"),
                    "direction": sc.get("direction"),
                    "scenario_title": sc.get("title"),
                    "last_price": last_price,
                    "trigger_price": sc.get("trigger_price"),
                    "target_profit": sc.get("target_profit"),
                    "invalidation_level": sc.get("invalidation_level"),
                    "risk_reward_ratio": rr,
                    "open_type": amt_ctx.get("open_type"),
                    "alignment_state": alignment_state,
                    "catalyst_stance": pb.get("catalysts", {}).get("intraday_fast_stance"),
                }
            )

    # Sort opportunities by Risk-Reward ratio descending
    return sorted(opportunities, key=lambda x: x["risk_reward_ratio"], reverse=True)


def format_report(res: dict[str, Any], *, journal: int = 15) -> str:
    """Terminal report from get_playbook_performance_metrics(detail=True): totals, per-symbol and
    per-setup tables, recent journal (ET). Paper results on 5m bars: no slippage, spread or fees."""
    rows = res.get("trades", [])
    fills = [t for t in rows if t["entry_price"] is not None and t["state"] not in _OPEN]

    def table(key, title: str, label: str) -> list[str]:
        groups: dict[str, list[dict]] = {}
        for t in rows:
            groups.setdefault(key(t), []).append(t)
        out = [title, f"{label:<44}| Total | Done | Win | Loss | BE | WinRate | Net R | PF | Avg R"]
        for k, ts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            d = [t for t in ts if t in fills]
            rs = [t["r_multiple"] or 0.0 for t in d]
            w, lo = sum(r > 0 for r in rs), sum(r < 0 for r in rs)
            gl = -sum(r for r in rs if r < 0)
            pf = f"{sum(r for r in rs if r > 0) / gl:.2f}" if gl else "-"
            wr = f"{100 * w / len(d):.0f}%" if d else "-"
            avg = f"{sum(rs) / len(d):+.2f}R" if d else "-"
            out.append(
                f"{k:<44}| {len(ts):>5} | {len(d):>4} | {w:>3} | {lo:>4} | {len(d) - w - lo:>2} "
                f"| {wr:>7} | {sum(rs):>+5.1f} | {pf:>4} | {avg}"
            )
        return out

    ci, n = res.get("win_rate_ci95_pct"), res["completed_trades"]
    lines = [
        "ARK-WATCH PLAYBOOK TRACKER REPORT (paper, TZ: ET)",
        f"Scenarios {res['total_scenarios']} | trades {n} ({res['wins']}W/{res['losses']}L/"
        f"{res['breakevens']}BE) | active {res['active']} | pending {res['pending']} | "
        f"non-trades {res['invalidated']}",
        f"Win rate {res['win_rate_pct']}% (CI95 {ci}) | PF {res['profit_factor']} | "
        f"expectancy {res['expectancy_r']}R | MFE/MAE {res['avg_mfe']}/{res['avg_mae']} pts",
    ]
    if n < 100:
        lines.append(f"! n={n} < 100: not enough to claim an edge; per-row numbers are noise.")
    lines += [""] + table(lambda t: t["symbol"], "BY SYMBOL", "Symbol")
    lines += [""] + table(lambda t: t["scenario_id"].removeprefix("SCENARIO_"), "BY SETUP", "Setup")
    lines += [
        "",
        "RECENT JOURNAL (running first, then newest resolved)",
        "Resolved (ET)    | Sym    | Dir   | R     | Status | Setup",
    ]
    for t in sorted(rows, key=lambda t: t["resolved_at_utc"] or "9", reverse=True)[:journal]:
        ts = t["resolved_at_utc"]
        when = (
            datetime.fromisoformat(ts).astimezone(_ET).strftime("%m-%d %H:%M") if ts else "running"
        )
        lines.append(
            f"{when:<16} | {t['symbol']:<6} | {t['direction']:<5} | "
            f"{(t['r_multiple'] or 0.0):>+5.2f} | {t['outcome'] or t['state']:<16} | {t['title'][:48]}"
        )
    return "\n".join(lines)
