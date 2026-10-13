from arkwatch import db
from arkwatch.senders import telegram, telegram_bot


def test_subscriber_registration_and_broadcast_list(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "bot.db", allow_init=True)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "10001")

    # Register two external users
    u1 = telegram_bot.register_or_touch_user(conn, "20002", "alice", "Alice")
    assert u1["is_new"] is True
    assert u1["subscribed"] == 1
    assert u1["preferred_tz"] == "ET"

    u2 = telegram_bot.register_or_touch_user(conn, "30003", "bob", "Bob")
    assert u2["is_new"] is True

    # All 3 chat IDs should be in the active broadcast list
    cids = telegram.get_subscribed_chat_ids(conn)
    assert set(cids) == {"10001", "20002", "30003"}

    conn.close()


def test_handle_incoming_commands(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "bot_cmd.db", allow_init=True)
    sent_messages = []

    def fake_send(text: str, chat_id: str):
        sent_messages.append((chat_id, text))
        return 999

    monkeypatch.setattr(telegram_bot, "_send_message", fake_send)

    # 1. /start command from any user
    telegram_bot.handle_incoming_message(
        conn,
        {
            "chat": {"id": 55555},
            "from": {"username": "trader1", "first_name": "Budi"},
            "text": "/start",
        },
    )
    assert len(sent_messages) == 1
    assert sent_messages[-1][0] == "55555"
    assert "Halo <b>Budi</b>" in sent_messages[-1][1]
    assert "AKTIF" in sent_messages[-1][1]

    # 2. /tz WIB command
    telegram_bot.handle_incoming_message(
        conn,
        {
            "chat": {"id": 55555},
            "from": {"username": "trader1", "first_name": "Budi"},
            "text": "/tz WIB",
        },
    )
    assert "WIB" in sent_messages[-1][1]

    # 3. /unsubscribe command
    telegram_bot.handle_incoming_message(
        conn,
        {
            "chat": {"id": 55555},
            "from": {"username": "trader1", "first_name": "Budi"},
            "text": "/unsubscribe",
        },
    )
    assert "DINONAKTIFKAN" in sent_messages[-1][1]

    row = conn.execute(
        "SELECT preferred_tz, subscribed FROM telegram_subscribers WHERE chat_id='55555'"
    ).fetchone()
    assert row[0] == "WIB"
    assert row[1] == 0

    # 4. /tracker command
    telegram_bot.handle_incoming_message(
        conn,
        {
            "chat": {"id": 55555},
            "from": {"username": "trader1", "first_name": "Budi"},
            "text": "/tracker",
        },
    )
    assert "PERFORMANCE TRACKER" in sent_messages[-1][1]

    conn.close()


def test_redact_telegram_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEF_SECRET_TOKEN")
    raw_err = "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: /bot123456:ABCDEF_SECRET_TOKEN/sendMessage"
    clean = telegram.redact_telegram_secrets(raw_err)
    assert "ABCDEF_SECRET_TOKEN" not in clean
    assert "/bot[REDACTED]/sendMessage" in clean
