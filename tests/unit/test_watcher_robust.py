"""watcher robustness: _fire rollback, per-trigger isolation, outbox-only delivery, lazy cme."""

from datetime import UTC, datetime

import pytest

from arkwatch import db
from arkwatch.qa import watcher


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    c = db.get_conn(tmp_path / "w.db", allow_init=True)
    yield c
    c.close()


def test_failed_insert_rolls_back_so_later_fires_still_work(conn):
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON alert_deliveries WHEN NEW.alert_type='boom'"
        " BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(Exception, match="boom"):
        watcher._fire(conn, "boom", "c", "x", "a")
    assert not conn.in_transaction
    assert watcher._fire(conn, "ok", "c", "x", "a")


def test_one_raising_trigger_does_not_kill_the_rest(conn, monkeypatch, capsys):
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO alert_deliveries (alert_type, triggered_at, cooldown_key, status)"
        " VALUES ('x', ?, 'dup@1', 'sent')",
        [(now,)] * 3,
    )

    def broken(*_a, **_kw):
        raise RuntimeError("bad row")

    monkeypatch.setattr(watcher, "latest_value", broken)
    monkeypatch.setattr(watcher, "recent_values", broken)
    assert watcher.check_all(conn) == ["alert_spam_tripwire"]  # the last trigger still ran
    out = capsys.readouterr().out
    assert "vix_backwardation trigger skipped: RuntimeError: bad row" in out
    assert "copper_stocks_drain trigger skipped" in out


def test_fire_leaves_row_pending_for_the_broadcast(conn, monkeypatch):
    """The Telegram-only fast path marked rows 'sent' so ntfy/Discord never saw them."""
    from arkwatch.senders import telegram

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = []
    monkeypatch.setattr(telegram, "_send_message", lambda *a: sent.append(a) or 1)
    assert watcher._fire(conn, "vix_backwardation", "c", "x", "a")
    assert sent == []
    assert conn.execute("SELECT status FROM alert_deliveries").fetchone() == ("pending",)


def test_cme_is_not_imported_at_module_level():
    assert not hasattr(watcher, "cme")
