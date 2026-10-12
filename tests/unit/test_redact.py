"""qa/redact.py: secrets are scrubbed wherever an exception or child output echoes them."""

import pytest

from arkwatch.qa.redact import _redact


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("https://api.stlouisfed.org/x?series_id=A&api_key=K3Y&f=1", "K3Y"),
        ("refused auth_token=sekrit123", "sekrit123"),
        ("password=hunter2 next", "hunter2"),
        ('{"api_key": "J5ON"}', "J5ON"),
        ("x-api-key: HDR1", "HDR1"),
        ("GET https://hc-ping.com/1b2c-3d4e/fail", "1b2c-3d4e"),
        ("https://api.telegram.org/bot123:AA-bb/sendMessage", "AA-bb"),
        ("https://ntfy.sh/arkwatch-topic", "arkwatch-topic"),
        ("Authorization: Bearer T0K", "T0K"),
    ],
)
def test_secret_is_scrubbed(raw, secret):
    assert secret not in _redact(raw)


def test_word_suffixes_are_not_mangled():
    assert _redact("monkey=1 donkey=2") == "monkey=1 donkey=2"


def test_news_urls_are_http_only():
    from arkwatch.qa.market_news import _canonical_url
    from arkwatch.server.models import NewsOut

    assert _canonical_url("javascript:alert(1)") is None
    assert (
        _canonical_url("HTTPS://Example.com/a/?utm_source=x&id=1") == "https://example.com/a?id=1"
    )
    base = {
        "news_id": "n",
        "published_at_utc": "t",
        "source": "s",
        "title": "t",
        "symbols": [],
        "cluster_id": "c",
    }
    assert NewsOut(**base, url="data:text/html,x").url is None
    assert NewsOut(**base, url="https://x.test/a").url == "https://x.test/a"


def test_job_env_drops_secrets_no_job_reads(monkeypatch):
    from arkwatch.qa.redact import job_env

    monkeypatch.setenv("ARKWATCH_API_KEY", "k")
    monkeypatch.setenv("LITESTREAM_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("FRED_API_KEY", "f")
    env = job_env()
    assert "ARKWATCH_API_KEY" not in env and "LITESTREAM_SECRET_ACCESS_KEY" not in env
    assert env["FRED_API_KEY"] == "f"  # what the fetchers need stays
