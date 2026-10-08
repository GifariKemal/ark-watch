"""Tests for the Channel contract and multi-channel outbox dispatch."""

from __future__ import annotations

from arkwatch.senders.base import active_channels
from arkwatch.senders.discord_webhook import MAX_LEN, _split_plain


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent: list[str] = []

    def send_text(self, text: str) -> str | None:
        self.sent.append(text)
        return f"id-{len(self.sent)}"


def test_active_channels_env_gated(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert active_channels() == {}
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/xxx")
    ch = active_channels()
    assert set(ch) == {"discord"}  # telegram stays inactive without its env vars


def test_discord_split_2000():
    text = "\n".join(f"line {i} " + "y" * 60 for i in range(90))
    assert len(text) > MAX_LEN
    parts = _split_plain(text)
    assert len(parts) >= 2
    assert all(len(p) <= MAX_LEN for p in parts)
    assert sum(p.count("line") for p in parts) == 90


def test_telegram_channel_protocol():
    from arkwatch.senders.telegram import TelegramChannel

    ch = TelegramChannel()
    assert ch.name == "telegram"
    assert hasattr(ch, "send_text")


def test_alert_failed_terminal_at_attempt_cap(tmp_path, monkeypatch):
    """An alert that exhausts its attempts becomes status='failed' (terminal,
    last_error set) instead of sticking as 'pending' forever. A stuck
    'pending' row is never retried yet still counted by the watcher's
    permanent cooldown — the weekly-cadence alert would be lost AND its
    snapshot blocked from ever re-announcing, with zero visibility."""
    from arkwatch import db as arkdb
    from arkwatch.senders import outbox

    conn = arkdb.get_conn(tmp_path / "t.db", allow_init=True)
    conn.execute(
        "INSERT INTO alert_deliveries(alert_type, triggered_at, cooldown_key,"
        " priority, status, message, attempts, last_attempt)"
        " VALUES ('soma_roll_off', '2026-09-04T00:00:00', 'soma_roll_off@2026-09-02',"
        " 'normal', 'pending', 'x', 4, '2026-09-03T00:00:00')"
    )
    conn.commit()
    conn.close()

    class DeadChannel:
        name = "dead"

        def send_text(self, text):
            raise RuntimeError("telegram down")

    monkeypatch.setattr(outbox, "active_channels", lambda: {"dead": DeadChannel()})
    res = outbox.send_pending_alerts(str(tmp_path / "t.db"), max_attempts=5, min_gap_s=0)
    assert res.get("failed") == 1

    import sqlite3

    c2 = sqlite3.connect(tmp_path / "t.db")
    status, attempts, err = c2.execute(
        "SELECT status, attempts, last_error FROM alert_deliveries"
    ).fetchone()
    c2.close()
    assert status == "failed"
    assert attempts == 5
    assert err  # visible in DB, not silent


def test_outbox_dispatch_per_channel(tmp_path, monkeypatch):
    """Outbox rows dispatch to their registered channel; an unknown channel is marked failed."""
    from arkwatch import db as arkdb
    from arkwatch.senders import outbox

    conn = arkdb.get_conn(tmp_path / "t.db", allow_init=True)
    conn.execute(
        "INSERT INTO brief_log(date, markdown, regime_score, generated_at)"
        " VALUES ('2026-09-02', 'test brief content', 0.1, '2026-09-02')"
    )
    conn.execute(
        "INSERT INTO brief_deliveries(brief_date, channel, status, created_at)"
        " VALUES ('2026-09-02', 'fake', 'pending', '2026-09-02'),"
        "        ('2026-09-02', 'dead', 'pending', '2026-09-02')"
    )
    conn.commit()
    conn.close()

    fake = FakeChannel()
    monkeypatch.setattr(outbox, "active_channels", lambda: {"fake": fake})
    monkeypatch.setattr(
        outbox, "send_pending_alerts", lambda *a, **k: {"sent": 0, "still_pending": 0}
    )
    res = outbox.send_pending(str(tmp_path / "t.db"))
    assert res["sent"] == 1 and res["failed"] == 1
    assert fake.sent == ["test brief content"]
    # verify the final delivery statuses were persisted
    import sqlite3

    c2 = sqlite3.connect(tmp_path / "t.db")
    rows = dict(c2.execute("SELECT channel, status FROM brief_deliveries").fetchall())
    c2.close()
    assert rows["fake"] == "sent"
    assert rows["dead"] == "failed"


def test_alerts_with_no_channel_are_skipped_not_failed(tmp_path, monkeypatch):
    """No alert channel configured: rows become 'skipped' (attempts untouched, reason visible)
    and still count toward the watcher cooldown, so a standing condition fires once per
    cooldown window instead of piling up 'failed' rows every ~25 minutes."""
    import sqlite3
    from datetime import UTC, datetime

    from arkwatch import db as arkdb
    from arkwatch.qa import watcher
    from arkwatch.senders import outbox

    conn = arkdb.get_conn(tmp_path / "t.db", allow_init=True)
    conn.execute(
        "INSERT INTO alert_deliveries(alert_type, triggered_at, cooldown_key,"
        " priority, status, message) VALUES ('copper', ?, 'copper', 'normal',"
        " 'pending', 'x')",
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(outbox, "active_channels", lambda: {})
    res = outbox.send_pending_alerts(str(tmp_path / "t.db"), max_attempts=5, min_gap_s=0)
    assert res.get("skipped") == 1 and not res.get("failed")

    c2 = sqlite3.connect(tmp_path / "t.db")
    status, attempts, err = c2.execute(
        "SELECT status, attempts, last_error FROM alert_deliveries"
    ).fetchone()
    assert (status, attempts) == ("skipped", 0) and "unconfigured" in err
    assert watcher._cooldown_active(c2, "copper") is True
    c2.close()
