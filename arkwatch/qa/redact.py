"""redact.py — secret scrubber, kept dependency-free: harvest imports every
fetcher (openpyxl, curl_cffi), so loggers importing _redact from there paid
that whole graph just to log one fetch."""

from __future__ import annotations

import os
import re

_SECRET_RE = re.compile(
    r"((?<![a-z0-9])(?:api_key|api_token|apikey|token|key|secret|password|passwd|auth|sig)=)[^&\s]+"
    r"|([\"'](?:api_key|apikey|api_token|token|secret|password)[\"']\s*:\s*[\"'])[^\"']+"  # JSON
    r"|(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"  # header echo
    r"|(hc-ping\.com/)[\w-]+"  # healthchecks.io ping URL (the uuid is the secret)
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


# Secrets the daemon container may hold that no job reads (the API key belongs to the api
# service, Litestream keys to the sidecar). Job subprocesses parse PDF/XLSX/ZIP from the
# internet: a parser exploit must not find them in its environment.
_NOT_FOR_JOBS = ("ARKWATCH_API_KEY", "ZONELAB_ARKWATCH_KEY", "LITESTREAM_", "EASYPANEL_")


def job_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_NOT_FOR_JOBS)}
