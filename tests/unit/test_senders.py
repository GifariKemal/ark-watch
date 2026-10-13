"""Telegram and Discord adapters: splitting, partial failure, no secret in the output."""

from __future__ import annotations

import json

import pytest

from arkwatch.senders import discord_webhook, telegram

TOKEN = "123456:AAsecretTOKEN"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, json.dumps(body or {})

    def json(self):
        return self._body


@pytest.fixture()
def tg(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)
    return []


def test_telegram_splits_escapes_and_returns_ids(tg, monkeypatch):
    def post(url, json, timeout):
        tg.append(json["text"])
        return Resp(body={"ok": True, "result": {"message_id": len(tg)}})

    monkeypatch.setattr(telegram.requests, "post", post)
    text = "\n\n".join(f"<b>line {i}</b> " + "x" * 90 for i in range(80))
    ids = json.loads(telegram.TelegramChannel().send_text(text))
    assert len(ids) == len(tg) >= 2 and all(len(t) <= 4096 for t in tg)
    assert "&lt;b&gt;" in tg[0]  # escaped: model text is never parsed as HTML


def test_telegram_partial_failure_is_none_and_token_never_printed(tg, monkeypatch, capsys):
    def post(url, json, timeout):
        tg.append(1)
        if len(tg) >= 2:  # the second chunk fails on every retry
            raise ConnectionError(f"HTTPSConnectionPool: {url}")
        return Resp(body={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(telegram.requests, "post", post)
    text = "\n\n".join("y" * 100 for _ in range(80))
    assert telegram.TelegramChannel().send_text(text) is None
    assert TOKEN.split(":")[1] not in capsys.readouterr().out


def test_discord_splits_long_messages_and_fails_on_any_part(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/api/webhooks/1/abc")
    sent, codes = [], iter([204, 204, 204, 204, 204, 400])
    monkeypatch.setattr(
        discord_webhook.requests,
        "post",
        lambda url, json, timeout: sent.append(json["content"]) or Resp(next(codes)),
    )
    text = "\n".join("z" * 99 for _ in range(50))  # ~5000 chars
    assert discord_webhook.DiscordWebhookChannel().send_text(text)
    assert len(sent) == 3 and all(len(p) <= discord_webhook.MAX_LEN for p in sent)
    assert discord_webhook.DiscordWebhookChannel().send_text(text) is None  # third part 400
