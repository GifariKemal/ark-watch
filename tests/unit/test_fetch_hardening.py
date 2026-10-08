"""Silent-failure, XML-hardening, host-allowlist and redaction contracts."""

import sqlite3
from urllib.error import URLError

import pytest

from arkwatch.fetchers import cryptopanic, geo, quikstrike, rss_news, tree_news
from arkwatch.qa import market_news
from arkwatch.qa.harvest import _redact


def _fail(*_a, **_kw):
    raise URLError("refused auth_token=sekrit123")


@pytest.fixture
def offline_news(monkeypatch):
    monkeypatch.setenv("CRYPTOPANIC_API_KEY", "sekrit123")
    monkeypatch.setattr(market_news, "_fmp", lambda: [])
    monkeypatch.setattr(market_news, "_eodhd", lambda: [])
    monkeypatch.setattr(market_news, "_gdelt_updates", lambda: {})
    monkeypatch.setattr(tree_news, "fetch_tree_news", lambda: [])
    monkeypatch.setattr(cryptopanic.urllib.request, "urlopen", _fail)
    monkeypatch.setattr(rss_news, "fetch_rss_feed", lambda *_a, **_kw: _fail())


def test_news_source_failures_reach_fetch_log_redacted(tmp_path, offline_news):
    db = tmp_path / "arkwatch.db"
    out = market_news.run(str(db))
    assert out["CRYPTOPANIC"] == -1 and out["RSS_FEEDS"] == -1
    assert out["TREE_NEWS"] == 0  # the other sources still ran (degrade, not abort)
    rows = dict(
        sqlite3.connect(db)
        .execute(
            "SELECT target, status || '|' || error FROM fetch_log"
            " WHERE target IN ('CRYPTOPANIC','RSS_FEEDS')"
        )
        .fetchall()
    )
    for target in ("CRYPTOPANIC", "RSS_FEEDS"):
        assert rows[target].startswith("ERROR|")
        assert "sekrit123" not in rows[target]


def test_rss_partial_failure_degrades_to_the_live_feeds(monkeypatch):
    def one_alive(name, _url):
        if name == "FED":
            return [{"title": "ok"}]
        raise URLError("down")

    monkeypatch.setattr(rss_news, "fetch_rss_feed", one_alive)
    assert rss_news.fetch_all_rss_feeds() == [{"title": "ok"}]


def test_rss_rejects_entity_expansion(monkeypatch):
    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>
    <rss><channel><item><title>&b;</title></item></channel></rss>"""

    class Resp:
        def read(self):
            return bomb

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

    monkeypatch.setattr(rss_news.urllib.request, "urlopen", lambda *_a, **_kw: Resp())
    try:
        from curl_cffi import requests as creq

        monkeypatch.setattr(creq.Session, "get", _fail)
    except ImportError:
        pass
    with pytest.raises(Exception, match="(?i)entit"):
        rss_news.fetch_rss_feed("FED", "https://example.com/rss")


class _Page:
    status_code = 200

    def __init__(self, text):
        self.text = text


@pytest.mark.parametrize(
    ("href", "expect_fallback"),
    [
        ("/media/docs/food_price_indices_data.csv?v=1", False),
        ("https://www.fao.org/x/food_price_indices_data.csv", False),
        ("https://evil.example/food_price_indices_data.csv", True),
        ("//evil.example/food_price_indices_data.csv", True),
        ("http://www.fao.org/food_price_indices_data.csv", True),
    ],
)
def test_fao_scraped_link_host_allowlist(monkeypatch, href, expect_fallback):
    monkeypatch.setattr(geo.requests, "get", lambda *_a, **_kw: _Page(f'<a href="{href}">x</a>'))
    url = geo._get_fao_csv_url()
    assert (url == geo.FAO_FALLBACK_URL) is expect_fallback
    assert url.startswith("https://www.fao.org/")


@pytest.mark.parametrize(
    ("action", "allowed"),
    [
        ("./QuikStrikeView.aspx?insid=1&amp;qsid=2", True),
        ("https://evil.example/steal", False),
        ("//evil.example/steal", False),
    ],
)
def test_quikstrike_form_action_host_allowlist(action, allowed):
    url = quikstrike._form_action(f'<form method="post" action="{action}">')
    assert (url != quikstrike.QS_BASE) is allowed
    assert url.startswith("https://cmegroup-tools.quikstrike.net/")


def test_redact_scrubs_bot_tokens_webhooks_and_cookies():
    text = (
        "url: /bot123456:AAH-x_y/sendMessage "
        "https://discord.com/api/webhooks/987/abcDEF "
        "api_key=k1&x=1 Cookie: ASP.NET_SessionId=s3cr3t"
    )
    out = _redact(text)
    for secret in ("AAH-x_y", "987/abcDEF", "k1", "s3cr3t"):
        assert secret not in out
    assert "x=1" in out


def test_gdelt_disabled_by_env_skips_collection(tmp_path, offline_news, monkeypatch):
    """GDELT is a raw archive nothing consumes (~1.5 GB/day): GDELT_ENABLED=0 must not
    touch the network or the gdelt tables, and must say so as SKIPPED, not ERROR."""
    monkeypatch.setenv("GDELT_ENABLED", "0")

    def boom():
        raise AssertionError("GDELT must not be contacted when disabled")

    monkeypatch.setattr(market_news, "_gdelt_updates", boom)
    db = tmp_path / "arkwatch.db"
    out = market_news.run(str(db))
    assert out["GDELT"] == out["GDELT_MENTIONS"] == out["GDELT_GKG"] == 0
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT status, error FROM fetch_log WHERE target LIKE 'GDELT:%'"
    ).fetchall()
    assert len(rows) == 3 and all(s == "SKIPPED" and "GDELT_ENABLED=0" in e for s, e in rows)
    assert conn.execute("SELECT COUNT(*) FROM gdelt_events").fetchone()[0] == 0
