"""Hermetic test environment: a dev machine with real keys in its shell (or a .env)
must run the suite exactly like CI. Tests that need a variable set it explicitly."""

from __future__ import annotations

import os

import pytest

# every env var arkwatch reads that can change behaviour (grep os.environ/getenv)
_PREFIXES = ("NLP_", "TELEGRAM_", "NTFY_", "ARGUS_", "ARKWATCH_")
_NAMES = {
    "ZAI_API_KEY",
    "Z_AI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "FMP_API_KEY",
    "EODHD_API_TOKEN",
    "FRED_API_KEY",
    "DISCORD_WEBHOOK_URL",
    "HEALTHCHECK_PING_URL",
    "CRYPTOPANIC_API_KEY",
    "LME_COOKIE",
    "GDELT_ENABLED",
    "MARKET_NEWS_RETENTION_DAYS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
}


def _ambient() -> list[str]:
    return [k for k in os.environ if k.upper() in _NAMES or k.upper().startswith(_PREFIXES)]


# import-time constants (ARKWATCH_CONFIG, ARKWATCH_DATA_DIR) are read when arkwatch is
# first imported, before any fixture runs: scrub once here as well
for _k in _ambient():
    del os.environ[_k]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for k in _ambient():
        monkeypatch.delenv(k)
    # load_dotenv() must not re-populate what was just removed
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    from arkwatch.fetchers import nlp

    monkeypatch.setattr(nlp, "_breaker", {})
