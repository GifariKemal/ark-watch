"""discord_webhook.py — Channel adapter via a Discord WEBHOOK.

A webhook beats a bot for this workload (1 brief/day + rare alerts; the 30
req/min webhook limit is <1% used; a bot would need a persistent WebSocket
gateway for nothing). Format: plain text ≤2000 chars/message, ``` code blocks
for monospace tables (Discord has no HTML).

Active only when DISCORD_WEBHOOK_URL is set (the URL is a CREDENTIAL: anyone
holding it can post → env-only, never logged).
"""

from __future__ import annotations

import os

import requests

MAX_LEN = 2000  # Discord hard limit per message


def _split_plain(text: str, max_len: int = MAX_LEN) -> list[str]:
    """Split on blank lines (plain text — no tags, no escaping)."""
    if len(text) <= max_len:
        return [text]
    parts, current, cur_len = [], [], 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if cur_len + line_len > max_len and current:
            parts.append("\n".join(current))
            current, cur_len = [], 0
        current.append(line)
        cur_len += line_len
    if current:
        parts.append("\n".join(current))
    return parts


class DiscordWebhookChannel:
    name = "discord"

    def send_text(self, text: str) -> str | None:
        """Send the text in <= 2000-char messages (a longer one is a 400 and was retried
        identically until its attempts ran out); None when any part fails."""
        url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not url:
            raise RuntimeError("DISCORD_WEBHOOK_URL not set")
        for part in _split_plain(text):
            r = requests.post(url, json={"content": part}, timeout=(10, 30))
            if r.status_code not in (200, 204):
                print(f"  ⚠ Discord: HTTP {r.status_code} {r.text[:80]}")
                return None
        # webhook responds 204 with no body; no id available → use a timestamp
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat(timespec="seconds")
