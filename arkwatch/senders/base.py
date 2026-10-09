"""base.py — the Channel contract.

The outbox is channel-agnostic (the `channel` column); a gateway is a
per-channel adapter implementing this contract. Telegram is the first
adapter; a Discord webhook follows (active only when DISCORD_WEBHOOK_URL is
set). Thin purpose-built adapters (Apprise's idea, without its dependency
weight): fine-grained per-channel control of parse-mode/split/rate-limit
matters more than support for 100+ services.
"""

from __future__ import annotations

from typing import Protocol


class Channel(Protocol):
    """One-way contract: send text → return an external message id (or None).

    - split/escape is the adapter's responsibility (limits & formats differ
      per channel: Telegram 4000-char HTML; Discord 2000-char plain)
    - a failed send returns None or raises; the outbox handles retry/status
    - `name` is the `channel` column value in brief_deliveries/alert_deliveries
    """

    name: str

    def send_text(self, text: str) -> str | None:
        """Send ONE already-formatted message; return the external id."""
        ...


def active_channels() -> dict[str, Channel]:
    """Active-channel registry based on env (missing env → not active)."""
    import os

    channels: dict[str, Channel] = {}
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from .telegram import TelegramChannel

        ch = TelegramChannel()
        channels[ch.name] = ch
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        from .discord_webhook import DiscordWebhookChannel

        ch = DiscordWebhookChannel()
        channels[ch.name] = ch
    if os.environ.get("NTFY_URL"):
        from .ntfy import NtfyChannel

        ch = NtfyChannel()
        channels[ch.name] = ch
    return channels
