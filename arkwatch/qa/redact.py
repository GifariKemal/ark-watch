"""redact.py — secret scrubber, kept dependency-free: harvest imports every
fetcher (openpyxl, curl_cffi), so loggers importing _redact from there paid
that whole graph just to log one fetch."""

from __future__ import annotations

import re

_SECRET_RE = re.compile(
    r"((?:api_key|api_token|apikey|token|key)=)[^&\s]+"
    r"|(/bot)\d+:[\w-]+"  # Telegram bot token in an echoed URL path
    r"|(/api/webhooks/)[^\s'\"]+"  # Discord webhook id/token
    r"|(ntfy[\w.:-]*/)[^\s'\"]+"  # ntfy topic (the topic name is the secret)
    r"|(bearer\s+)[^\s'\"]+"  # Authorization: Bearer <token> echo
    r"|((?:set-)?cookie[\"']?\s*[:=]\s*)[^\n]+",  # Cookie header/env echo
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """ROUND-4 (security): fetch exceptions echo the failing URL — keys ride
    query params (api_key=…&api_token=…). One 4xx away from a key persisting
    into fetch_log; scrub centrally at both writers."""
    return _SECRET_RE.sub(lambda m: next(g for g in m.groups() if g) + "REDACTED", text)
