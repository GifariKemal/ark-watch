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


def test_brief_terminal_failure_stays_failed_and_retry_redelivers(tmp_path, monkeypatch):
    """The newest brief that spent its attempts stays 'failed' (it used to be relabelled
    'superseded', hiding the failure and making the API retry answer 409); an API retry then
    delivers on the next sender run. An older day's undelivered row is still superseded."""
    import sqlite3

    from arkwatch import api
    from arkwatch import db as arkdb
    from arkwatch.senders import outbox

    path = tmp_path / "t.db"
    conn = arkdb.get_conn(path, allow_init=True)
    conn.execute(
        "INSERT INTO brief_log(date, markdown, regime_score, generated_at)"
        " VALUES ('2026-10-12', 'today', 0.1, '2026-10-12')"
    )
    conn.execute(
        "INSERT INTO brief_deliveries(brief_date, channel, status, attempts, created_at)"
        " VALUES ('2026-10-11', 'fake', 'failed', 1, 'x'), ('2026-10-12', 'fake', 'failed', 3, 'x')"
    )
    conn.commit()
    monkeypatch.setattr(outbox, "send_pending_alerts", lambda *a, **k: {"sent": 0})
    fake = FakeChannel()
    monkeypatch.setattr(outbox, "active_channels", lambda: {"fake": fake})

    outbox.send_pending(str(path))
    rows = dict(conn.execute("SELECT brief_date, status FROM brief_deliveries").fetchall())
    assert rows == {"2026-10-11": "superseded", "2026-10-12": "failed"}
    assert fake.sent == []

    new_id = conn.execute(
        "SELECT id FROM brief_deliveries WHERE brief_date='2026-10-12'"
    ).fetchone()[0]
    api.retry_outbox(conn, new_id, actor="test")
    res = outbox.send_pending(str(path))
    assert res["sent"] == 1 and fake.sent == ["today"]
    conn.close()
    assert sqlite3.connect(path).execute(
        "SELECT status FROM brief_deliveries WHERE id=?", (new_id,)
    ).fetchone() == ("sent",)


def test_alert_claim_is_atomic_across_senders(tmp_path, monkeypatch):
    """Two senders read the same pending row: only the one whose claim UPDATE wins sends."""
    from arkwatch import db as arkdb
    from arkwatch.senders import outbox

    path = tmp_path / "t.db"
    conn = arkdb.get_conn(path, allow_init=True)
    conn.execute(
        "INSERT INTO alert_deliveries(alert_type, triggered_at, cooldown_key, priority, status,"
        " message, attempts) VALUES ('copper', '2026-10-12T00:00:00', 'k', 'normal', 'pending', 'x', 0)"
    )
    conn.commit()
    conn.close()
    real_get_conn = outbox.get_conn

    class RivalAfterSelect:
        """Proxy connection: a rival sender claims the row right after our SELECT."""

        def __init__(self, c):
            self._c = c

        def execute(self, sql, *args):
            cur = self._c.execute(sql, *args)
            if sql.startswith("SELECT id, message, attempts FROM alert_deliveries"):
                rows = cur.fetchall()
                self._c.execute("UPDATE alert_deliveries SET attempts=attempts+1")
                self._c.commit()
                return type("Rows", (), {"fetchall": lambda _s: rows})()
            return cur

        def __getattr__(self, name):
            return getattr(self._c, name)

    fake = FakeChannel()
    monkeypatch.setattr(outbox, "active_channels", lambda: {"fake": fake})
    monkeypatch.setattr(outbox, "get_conn", lambda p: RivalAfterSelect(real_get_conn(p)))
    res = outbox.send_pending_alerts(str(path), max_attempts=5, min_gap_s=0)
    assert fake.sent == [] and res["sent"] == 0


def test_send_command_exits_1_on_failed_delivery(monkeypatch):
    import sys

    from arkwatch import __main__ as cli
    from arkwatch.senders import outbox

    monkeypatch.setattr(
        outbox, "send_pending", lambda _db: {"sent": 0, "failed": 1, "alerts": {"sent": 0}}
    )
    monkeypatch.setattr(sys, "argv", ["arkwatch", "send"])
    assert cli.main() == 1
    monkeypatch.setattr(
        outbox, "send_pending", lambda _db: {"sent": 1, "failed": 0, "alerts": {"sent": 0}}
    )
    assert cli.main() == 0


def test_brief_window_wib():
    from datetime import UTC, datetime

    from arkwatch.senders.outbox import in_brief_window

    # WIB = UTC+7: 04:16 WIB (Saturday edition) waits, 07:05 WIB delivers, 22:00 WIB stops
    assert not in_brief_window(datetime(2026, 10, 9, 21, 16, tzinfo=UTC))
    assert in_brief_window(datetime(2026, 10, 10, 0, 5, tzinfo=UTC))
    assert not in_brief_window(datetime(2026, 10, 10, 15, 0, tzinfo=UTC))


def test_watcher_delivers_briefs_only_inside_window(tmp_path, monkeypatch):
    from arkwatch.qa import watcher
    from arkwatch.senders import outbox

    calls = []
    monkeypatch.setattr(watcher, "check_all", lambda _c: [])
    monkeypatch.setattr(
        outbox,
        "send_pending",
        lambda _p: calls.append("brief") or {"sent": 0, "alerts": {"sent": 0}},
    )
    monkeypatch.setattr(
        outbox, "send_pending_alerts", lambda _p: calls.append("alerts") or {"sent": 0}
    )
    for inside in (True, False):
        monkeypatch.setattr(outbox, "in_brief_window", lambda inside=inside: inside)
        assert watcher.main(["--db", str(tmp_path / "w.db")]) == 0
    assert calls == ["brief", "alerts"]
