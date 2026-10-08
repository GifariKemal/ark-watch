"""outbox.py — channel-agnostic outbox processor.

Atomic claim pending→sending (BEGIN IMMEDIATE) + re-claim after 10 minutes
(claimed_at) + attempt cap — then DISPATCH per channel via active_channels()
(base.py). Briefs go to the channel named on their outbox row; alerts
BROADCAST to every active channel.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from ..db import get_conn
from .base import active_channels

CLAIM_STALE_MIN = 10  # re-claim a sending row only after >10 min
BRIEF_MAX_ATTEMPTS = 3
ALERT_MAX_ATTEMPTS = 5
ALERT_MIN_GAP_S = 300  # minimum gap between alert attempts


def _claim_brief_rows(conn: sqlite3.Connection) -> list[tuple]:
    """Atomic claim — NEWEST BRIEF ONLY per channel (round-3 P2): a multi-day
    pause (D-023) accumulates one pending row per generated day; claiming all
    of them would blast the whole backlog as separate ~4KB messages on resume.
    Older pending rows are superseded (their content is stale by construction
    — a fresher brief exists for the same channel)."""
    now = datetime.now(UTC)
    stale = (now - timedelta(minutes=CLAIM_STALE_MIN)).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    # newest date per channel over ALL rows — if the newest was already
    # SENT, an older pending row must not resurrect as a stale delivery
    newest = conn.execute(
        "SELECT channel, MAX(brief_date) FROM brief_deliveries GROUP BY channel"
    ).fetchall()
    keep_ids: list[int] = []
    for channel, _max_date in newest:
        row = conn.execute(
            "SELECT id FROM brief_deliveries"
            " WHERE channel=? AND brief_date=?"
            " AND (status='pending'"
            "  OR (status='sending' AND (claimed_at IS NULL OR claimed_at < ?))"
            "  OR (status='failed' AND attempts < ?))"
            " ORDER BY id DESC LIMIT 1",
            (channel, _max_date, stale, BRIEF_MAX_ATTEMPTS),
        ).fetchone()
        if row:
            keep_ids.append(row[0])
    # supersede everything older still marked undelivered — but NEVER a
    # fresh 'sending' row (< CLAIM_STALE_MIN old): a second send_pending
    # inside the claim window would terminally kill the in-flight delivery
    if keep_ids:
        marks = ",".join("?" * len(keep_ids))
        conn.execute(
            "UPDATE brief_deliveries SET status='superseded'"
            f" WHERE id NOT IN ({marks}) AND status IN ('pending','failed')"
            " OR (id NOT IN (" + marks + ") AND status='sending'"
            " AND (claimed_at IS NULL OR claimed_at < ?))",
            (*keep_ids, *keep_ids, stale),
        )
    else:
        conn.execute(
            "UPDATE brief_deliveries SET status='superseded'"
            " WHERE status IN ('pending','failed')"
            " OR (status='sending' AND (claimed_at IS NULL OR claimed_at < ?))",
            (stale,),
        )
    rows = []
    for row_id in keep_ids:
        r = conn.execute(
            "SELECT id, brief_date, channel FROM brief_deliveries WHERE id=?",
            (row_id,),
        ).fetchone()
        conn.execute(
            "UPDATE brief_deliveries SET status='sending', claimed_at=?,"
            " attempts=attempts+1, last_error=NULL WHERE id=?",
            (now.isoformat(timespec="seconds"), row_id),
        )
        rows.append(r)
    conn.execute("COMMIT")
    return rows


def send_pending(db_path: str) -> dict:
    """Read pending outbox rows → dispatch per channel → update status."""
    conn = get_conn(db_path)
    results = {"sent": 0, "failed": 0, "messages": []}
    try:
        rows = _claim_brief_rows(conn)
        if rows:
            channels = active_channels()
            now = datetime.now(UTC).isoformat(timespec="seconds")
            for row_id, brief_date, channel_name in rows:
                ch = channels.get(channel_name)
                if ch is None:
                    conn.execute(
                        "UPDATE brief_deliveries SET status='failed', last_error=? WHERE id=?",
                        (f"channel '{channel_name}' not active (env not set)", row_id),
                    )
                    conn.commit()
                    results["failed"] += 1
                    continue
                md_row = conn.execute(
                    "SELECT markdown FROM brief_log WHERE date=?", (brief_date,)
                ).fetchone()
                if not md_row:
                    conn.execute(
                        "UPDATE brief_deliveries SET status='failed',"
                        " last_error='brief_log empty' WHERE id=?",
                        (row_id,),
                    )
                    conn.commit()
                    results["failed"] += 1
                    continue
                try:
                    ext_id = ch.send_text(md_row[0])
                except Exception as ex:
                    from ..qa.harvest import _redact  # webhook URL / bot token

                    print(f"  ⚠ {channel_name}: {_redact(str(ex))[:80]}")
                    ext_id = None
                if ext_id:
                    conn.execute(
                        "UPDATE brief_deliveries SET status='sent',"
                        " telegram_message_ids=?, sent_at=? WHERE id=?",
                        (ext_id, now, row_id),
                    )
                    results["sent"] += 1
                    results["messages"].append({"date": brief_date, "ids": ext_id})
                else:
                    conn.execute(
                        "UPDATE brief_deliveries SET status='failed',"
                        " last_error='send failed' WHERE id=?",
                        (row_id,),
                    )
                    results["failed"] += 1
                conn.commit()
    finally:
        conn.close()
    results["alerts"] = send_pending_alerts(db_path)
    return results


def send_pending_alerts(
    db_path: str, max_attempts: int = ALERT_MAX_ATTEMPTS, min_gap_s: int = ALERT_MIN_GAP_S
) -> dict:
    """Retry pending alerts — BROADCAST to every active channel.

    Success = ≥1 channel received it (the first channel's id is stored).
    last_attempt pacing: the watch cycle calls this every 60s; each row
    retries at most once per 5 minutes."""
    conn = get_conn(db_path)
    now = datetime.now(UTC)
    summary = {"sent": 0, "still_pending": 0}
    try:
        gap_before = (now - timedelta(seconds=min_gap_s)).isoformat(timespec="seconds")
        try:
            rows = conn.execute(
                "SELECT id, message, attempts FROM alert_deliveries "
                "WHERE status='pending' AND attempts < ? AND message IS NOT NULL "
                "AND (last_attempt IS NULL OR last_attempt < ?) "
                "ORDER BY triggered_at",
                (max_attempts, gap_before),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"sent": 0, "still_pending": 0}  # older DB — no last_attempt column yet
        if rows:
            channels = active_channels()
            now_iso = now.isoformat(timespec="seconds")
            for row_id, message, attempts in rows:
                conn.execute(
                    "UPDATE alert_deliveries SET attempts=attempts+1, last_attempt=? WHERE id=?",
                    (now_iso, row_id),
                )
                conn.commit()
                delivered_id = None
                for ch_name, ch in channels.items():
                    try:
                        ext_id = ch.send_text(message)
                    except Exception:
                        ext_id = None
                    if ext_id and delivered_id is None:
                        delivered_id = f"{ch_name}:{ext_id}"
                if delivered_id:
                    conn.execute(
                        "UPDATE alert_deliveries SET status='sent',"
                        " telegram_message_id=?, sent_at=? WHERE id=?",
                        (delivered_id, now_iso, row_id),
                    )
                    conn.commit()
                    summary["sent"] += 1
                elif attempts + 1 >= max_attempts:
                    # Terminal failure: without this the row stays 'pending'
                    # forever — excluded from retries yet still counted by the
                    # watcher's cooldown check, so a weekly-cadence alert
                    # (permanent cooldown key) would be lost AND block its
                    # snapshot from ever re-announcing. 'failed' is excluded
                    # from cooldown counting → the condition can re-fire.
                    conn.execute(
                        "UPDATE alert_deliveries SET status='failed', last_error=? WHERE id=?",
                        (f"send failed after {attempts + 1} attempts", row_id),
                    )
                    conn.commit()
                    summary["failed"] = summary.get("failed", 0) + 1
                else:
                    summary["still_pending"] += 1
    finally:
        conn.close()
    return summary
