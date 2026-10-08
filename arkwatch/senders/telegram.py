"""telegram.py — Telegram Channel adapter.

Pure adapter: token/chat from env, send + escape + split ≤4000 (<pre>-aware).
Outbox processing (claim/retry/status) lives in outbox.py (channel-agnostic).
"""

from __future__ import annotations

import html
import json
import os
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    return t


def _chat_id() -> str:
    c = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not c:
        raise RuntimeError("TELEGRAM_CHAT_ID not set")
    return c


def _send_message(text: str, chat_id: str) -> int | None:
    """Send one message; return message_id or None."""
    r = requests.post(
        API.format(token=_token(), method="sendMessage"),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "protect_content": True,
        },
        timeout=(10, 30),
    )
    j = r.json()
    if not j.get("ok"):
        print(f"  ⚠ Telegram: {j.get('description', 'unknown')}")
        return None
    return j["result"]["message_id"]


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split on blank lines until each chunk < max_len — operates on the RAW
    (pre-escape) text: <pre> detection needs the raw lines, and the caller
    escapes each chunk after splitting (escaping first would make <pre>
    detection impossible)."""
    if len(text) <= max_len:
        return [text]
    parts, current, cur_len, in_pre = [], [], 0, False
    for line in text.split("\n"):
        line_len = len(line) + 1
        if cur_len + line_len > max_len - 30 and current:  # margin for <pre> tags
            chunk = "\n".join(current)
            if in_pre:
                chunk += "\n</pre>"
            parts.append(chunk)
            current = ["<pre>"] if in_pre else []
            cur_len = (len(current[0]) + 1) if current else 0
        if "<pre>" in line:
            in_pre = True
        if "</pre>" in line:
            in_pre = False
        current.append(line)
        cur_len += line_len
    if current:
        chunk = "\n".join(current)
        if in_pre:
            chunk += "\n</pre>"
        parts.append(chunk)
    return parts


class TelegramChannel:
    """Telegram channel: <pre>-aware splitting + HTML escape + 0.5s rate limit."""

    name = "telegram"

    def send_text(self, text: str) -> str | None:
        """Send a message (split automatically); return a JSON list of message ids."""
        chat_id = _chat_id()
        chunks = [html.escape(c) for c in _split_message(text)]
        msg_ids, all_ok = [], True
        for chunk in chunks:
            try:
                mid = _send_message(chunk, chat_id)
            except Exception as ex:  # network error — do not abort the loop
                from ..qa.harvest import _redact  # requests echoes /bot<token>/ URLs

                print(f"  ⚠ send failed: {_redact(str(ex))[:80]}")
                mid = None
            if mid:
                msg_ids.append(mid)
                time.sleep(0.5)  # rate limit
            else:
                all_ok = False
                break
        if not all_ok:
            return None
        return json.dumps(msg_ids)
