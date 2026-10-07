"""playbook_tracker.py — Automated lifecycle state machine and outcome tracking for trading playbooks.

Tracks trading scenarios from generation through resolution:
  - PENDING_TRIGGER -> Waiting for price action to confirm trigger condition
  - ACTIVE -> Trigger condition verified; tracks real-time MFE (Max Favorable Excursion) and MAE (Max Adverse Excursion)
  - HIT_TARGET_WIN -> Price reached target profit
  - HIT_STOP_LOSS -> Price reached invalidation stop
  - CANCELLED_EXPIRED -> Session ended without trigger condition materializing
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any


def record_playbook_scenarios(
    conn: sqlite3.Connection,
    playbook_payload: dict[str, Any],
    *,
    cfd_basis_offset: float = 0.0,
) -> list[str]:
    """Extract and persist all generated scenarios into playbook_scenarios table."""
    symbol = playbook_payload["symbol"]
    now_utc = playbook_payload.get("as_of", datetime.now(UTC).isoformat(timespec="seconds"))
    session_id = playbook_payload.get("reference_levels", {}).get(
        "active_session_current", now_utc[:10]
    )

    scenarios = playbook_payload.get("scenarios", [])
    recorded_uids = []

    for sc in scenarios:
        sc_id = sc["id"]
        horizon = sc.get("horizon", "INTRADAY").upper()
        direction = sc["direction"].upper()
        if direction not in ("LONG", "SHORT", "NEUTRAL_RANGE"):
            direction = "NEUTRAL_RANGE"

        target_p = float(sc["target_profit"]) + cfd_basis_offset
        inval_p = float(sc["invalidation_level"]) + cfd_basis_offset
        rr = float(sc.get("risk_reward_ratio", 1.0))
        trigger_cond = sc["trigger_condition"]
        trigger_price = (
            float(sc.get("trigger_price", playbook_payload.get("last_price", 0.0)))
            + cfd_basis_offset
        )

        scenario_uid = f"{symbol}-{session_id}-{horizon}-{sc_id}"

        payload_json = json.dumps(
            {
                "scenario": sc,
                "catalysts": playbook_payload.get("catalysts", {}),
                "multi_domain": playbook_payload.get("multi_domain", {}),
                "amt_context": playbook_payload.get("amt_context", {}),
                "decision_log": [
                    {
                        "ts_utc": now_utc,
                        "event": "CREATED_PENDING",
                        "details": f"Scenario created with trigger {trigger_price}, TP {target_p}, SL {inval_p}",
                    }
                ],
            }
        )

        try:
            conn.execute(
                """
                INSERT INTO playbook_scenarios (
                    scenario_uid, symbol, horizon, direction, scenario_id, title,
                    trigger_condition, trigger_price, target_profit, invalidation_level,
                    risk_reward_ratio, created_at_utc, session_id, state,
                    cfd_basis_offset, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_TRIGGER', ?, ?)
                ON CONFLICT(scenario_uid) DO UPDATE SET
                    target_profit=excluded.target_profit,
                    invalidation_level=excluded.invalidation_level,
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
            )
            recorded_uids.append(scenario_uid)
        except sqlite3.Error:
            pass

    conn.commit()
    return recorded_uids


def evaluate_active_playbooks(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, int]:
    """Advance the state machine for all open scenarios using subsequent 1m/5m bars."""
    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of).astimezone(UTC)
    else:
        target_dt = as_of.astimezone(UTC)

    target_ts = target_dt.isoformat(timespec="seconds")

    pending_or_active = conn.execute(
        """
        SELECT scenario_uid, symbol, horizon, direction, scenario_id,
               trigger_price, target_profit, invalidation_level, risk_reward_ratio,
               created_at_utc, state, entry_price, mfe_points, mae_points
        FROM playbook_scenarios
        WHERE state IN ('PENDING_TRIGGER', 'ACTIVE')
        """
    ).fetchall()

    if not pending_or_active:
        return {"evaluated": 0, "activated": 0, "resolved_wins": 0, "resolved_losses": 0}

    stats = {
        "evaluated": len(pending_or_active),
        "activated": 0,
        "resolved_wins": 0,
        "resolved_losses": 0,
    }

    for row in pending_or_active:
        (
            uid,
            sym,
            horizon,
            direction,
            sc_id,
            trig_p,
            target_p,
            inval_p,
            rr,
            created_at,
            state,
            entry_p,
            mfe,
            mae,
        ) = row

        # Fetch subsequent bars since creation
        bars = conn.execute(
            """
            SELECT bar_ts_utc, open, high, low, close
            FROM intraday_bars
            WHERE symbol = ?
              AND bar_ts_utc >= ?
              AND bar_ts_utc <= ?
            ORDER BY bar_ts_utc ASC
            """,
            (sym, created_at, target_ts),
        ).fetchall()

        if not bars:
            continue

        if state == "PENDING_TRIGGER":
            # Check for activation trigger
            activated = False
            activation_bar = None
            for b in bars:
                b_high = b[2]
                b_low = b[3]
                b_close = b[4]
                if direction == "LONG":
                    # Long activates if price pushed above trigger price
                    if b_close >= trig_p or b_high >= trig_p:
                        activated = True
                        activation_bar = b
                        break
                elif direction == "SHORT":
                    # Short activates if price dropped below trigger price
                    if b_close <= trig_p or b_low <= trig_p:
                        activated = True
                        activation_bar = b
                        break

            if activated and activation_bar:
                entry_price = activation_bar[4]
                trig_time = activation_bar[0]

                # Update payload decision log
                cur_payload_row = conn.execute(
                    "SELECT payload_json FROM playbook_scenarios WHERE scenario_uid = ?", (uid,)
                ).fetchone()
                try:
                    cur_p = json.loads(cur_payload_row[0]) if cur_payload_row else {}
                except Exception:
                    cur_p = {}
                cur_p.setdefault("decision_log", []).append(
                    {
                        "ts_utc": trig_time,
                        "event": "TRIGGERED_ACTIVE",
                        "details": f"Trigger met at price {entry_price} on bar {trig_time}",
                    }
                )

                conn.execute(
                    """
                    UPDATE playbook_scenarios
                    SET state = 'ACTIVE', triggered_at_utc = ?, entry_price = ?,
                        mfe_points = 0.0, mae_points = 0.0, payload_json = ?
                    WHERE scenario_uid = ?
                    """,
                    (trig_time, entry_price, json.dumps(cur_p), uid),
                )
                stats["activated"] += 1
                state = "ACTIVE"
                entry_p = entry_price
                # Filter bars after activation
                bars = [b for b in bars if b[0] >= trig_time]

        if state == "ACTIVE" and entry_p:
            current_mfe = mfe or 0.0
            current_mae = mae or 0.0
            resolved_state = None
            exit_price = None
            resolved_time = None

            for b in bars:
                b_high = b[2]
                b_low = b[3]

                if direction == "LONG":
                    # Update MFE & MAE
                    fav = b_high - entry_p
                    adv = entry_p - b_low
                    current_mfe = max(current_mfe, fav)
                    current_mae = max(current_mae, adv)

                    # Check Target Hit (WIN)
                    if b_high >= target_p:
                        resolved_state = "HIT_TARGET_WIN"
                        exit_price = target_p
                        resolved_time = b[0]
                        break
                    # Check Stop Hit (LOSS)
                    if b_low <= inval_p:
                        resolved_state = "HIT_STOP_LOSS"
                        exit_price = inval_p
                        resolved_time = b[0]
                        break

                elif direction == "SHORT":
                    fav = entry_p - b_low
                    adv = b_high - entry_p
                    current_mfe = max(current_mfe, fav)
                    current_mae = max(current_mae, adv)

                    if b_low <= target_p:
                        resolved_state = "HIT_TARGET_WIN"
                        exit_price = target_p
                        resolved_time = b[0]
                        break
                    if b_high >= inval_p:
                        resolved_state = "HIT_STOP_LOSS"
                        exit_price = inval_p
                        resolved_time = b[0]
                        break

            if resolved_state:
                pnl = (exit_price - entry_p) if direction == "LONG" else (entry_p - exit_price)
                risk_dist = abs(entry_p - inval_p) or 1.0
                r_mult = round(pnl / risk_dist, 2)

                cur_payload_row = conn.execute(
                    "SELECT payload_json FROM playbook_scenarios WHERE scenario_uid = ?", (uid,)
                ).fetchone()
                try:
                    cur_p = json.loads(cur_payload_row[0]) if cur_payload_row else {}
                except Exception:
                    cur_p = {}
                cur_p.setdefault("decision_log", []).append(
                    {
                        "ts_utc": resolved_time,
                        "event": resolved_state,
                        "details": f"Exit reached at {exit_price}. PnL: {round(pnl, 2)} pts ({r_mult}R). MFE: +{round(current_mfe, 2)}, MAE: -{round(current_mae, 2)}",
                    }
                )

                conn.execute(
                    """
                    UPDATE playbook_scenarios
                    SET state = ?, resolved_at_utc = ?, exit_price = ?,
                        mfe_points = ?, mae_points = ?, pnl_points = ?, r_multiple = ?,
                        payload_json = ?
                    WHERE scenario_uid = ?
                    """,
                    (
                        resolved_state,
                        resolved_time,
                        exit_price,
                        round(current_mfe, 2),
                        round(current_mae, 2),
                        round(pnl, 2),
                        r_mult,
                        json.dumps(cur_p),
                        uid,
                    ),
                )
                if resolved_state == "HIT_TARGET_WIN":
                    stats["resolved_wins"] += 1
                else:
                    stats["resolved_losses"] += 1
            else:
                # Update current MFE/MAE snapshot while trade remains active
                conn.execute(
                    """
                    UPDATE playbook_scenarios
                    SET mfe_points = ?, mae_points = ?
                    WHERE scenario_uid = ?
                    """,
                    (round(current_mfe, 2), round(current_mae, 2), uid),
                )

    conn.commit()
    return stats


def get_playbook_performance_metrics(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    horizon: str | None = None,
) -> dict[str, Any]:
    """Calculate institutional performance metrics: Win Rate, Profit Factor, MFE/MAE, and R-Multiple."""
    where_clauses = []
    params = []

    if symbol:
        where_clauses.append("symbol = ?")
        params.append(symbol.strip().upper())
    if horizon:
        where_clauses.append("horizon = ?")
        params.append(horizon.strip().upper())

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    rows = conn.execute(
        f"""
        SELECT state, pnl_points, r_multiple, mfe_points, mae_points
        FROM playbook_scenarios
        {where_sql}
        """,
        params,
    ).fetchall()

    if not rows:
        return {
            "total_scenarios": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "completed_trades": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "active": 0,
            "avg_r_multiple": 0.0,
            "avg_mfe": 0.0,
            "avg_mae": 0.0,
        }

    wins = [r for r in rows if r[0] == "HIT_TARGET_WIN"]
    losses = [r for r in rows if r[0] == "HIT_STOP_LOSS"]
    pending = sum(1 for r in rows if r[0] == "PENDING_TRIGGER")
    active = sum(1 for r in rows if r[0] == "ACTIVE")
    completed = len(wins) + len(losses)

    win_rate = round((len(wins) / completed * 100), 1) if completed > 0 else 0.0

    gross_profit = sum(r[1] for r in wins if r[1] and r[1] > 0)
    gross_loss = abs(sum(r[1] for r in losses if r[1] and r[1] < 0))
    profit_factor = (
        round(gross_profit / gross_loss, 2)
        if gross_loss > 0
        else (9.9 if gross_profit > 0 else 0.0)
    )

    completed_r = [r[2] for r in (wins + losses) if r[2] is not None]
    avg_r = round(sum(completed_r) / len(completed_r), 2) if completed_r else 0.0

    all_mfe = [r[3] for r in (wins + losses) if r[3] is not None]
    avg_mfe = round(sum(all_mfe) / len(all_mfe), 2) if all_mfe else 0.0

    all_mae = [r[4] for r in (wins + losses) if r[4] is not None]
    avg_mae = round(sum(all_mae) / len(all_mae), 2) if all_mae else 0.0

    return {
        "total_scenarios": len(rows),
        "completed_trades": completed,
        "wins": len(wins),
        "losses": len(losses),
        "pending": pending,
        "active": active,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "avg_r_multiple": avg_r,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
    }


def evaluate_counterfactual_outcomes(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
    forward_hours: int = 2,
) -> dict[str, int]:
    """Evaluate subsequent 2h-4h price behavior after exit to audit whether stop-loss/invalidation was justified."""
    if as_of is None:
        target_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        target_dt = datetime.fromisoformat(as_of).astimezone(UTC)
    else:
        target_dt = as_of.astimezone(UTC)

    target_ts = target_dt.isoformat(timespec="seconds")

    # Find resolved trades
    rows = conn.execute(
        """
        SELECT scenario_uid, symbol, direction, target_profit, invalidation_level,
               entry_price, exit_price, state, resolved_at_utc, payload_json
        FROM playbook_scenarios
        WHERE state IN ('HIT_TARGET_WIN', 'HIT_STOP_LOSS')
          AND resolved_at_utc IS NOT NULL
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

        # Fetch bars in forward window after resolution
        resolved_dt = datetime.fromisoformat(resolved_ts).astimezone(UTC)
        window_end_dt = resolved_dt + timedelta(hours=forward_hours)
        # Only audit if window has elapsed
        if target_dt < window_end_dt:
            continue

        cf_bars = conn.execute(
            """
            SELECT bar_ts_utc, open, high, low, close
            FROM intraday_bars
            WHERE symbol = ?
              AND bar_ts_utc >= ?
              AND bar_ts_utc <= ?
            ORDER BY bar_ts_utc ASC
            """,
            (sym, resolved_ts, window_end_dt.isoformat(timespec="seconds")),
        ).fetchall()

        if len(cf_bars) < 6:
            continue

        cf_high = max(b[2] for b in cf_bars)
        cf_low = min(b[3] for b in cf_bars)
        cf_close = cf_bars[-1][4]
        risk_dist = abs(entry_p - inval_p) if (entry_p and inval_p) else 10.0

        verdict = "NEUTRAL_CONSOLIDATION"
        reason = "Price hovered near exit level during post-trade window."

        if state == "HIT_STOP_LOSS":
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

        elif state == "HIT_TARGET_WIN":
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
