"""Unit tests for RSS and Tree News fetchers (fetchers/rss_news.py and fetchers/tree_news.py)."""

from __future__ import annotations

from urllib.error import URLError

from arkwatch.fetchers import rss_news, tree_news


def test_parse_rss_feed_sample_xml(monkeypatch):
    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Federal Reserve Press Releases</title>
        <item>
          <title>Federal Reserve Board announces rate cut</title>
          <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20261002a.htm</link>
          <description>The Federal Reserve Board announced a 25 basis point cut.</description>
          <pubDate>Fri, 2 Oct 2026 20:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>"""

    class FakeResponse:
        def read(self):
            return sample_xml

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(rss_news.urllib.request, "urlopen", lambda _req, **_kw: FakeResponse())

    items = rss_news.fetch_rss_feed("FED", "https://example.com/rss")
    assert len(items) == 1
    assert items[0]["title"] == "Federal Reserve Board announces rate cut"
    assert items[0]["source"] == "RSS_FED"
    assert items[0]["symbols"] == ["$FED"]
    assert "2026-10-02" in items[0]["published"]


def test_fetch_tree_news_sample_json(monkeypatch):
    sample_json = b"""[
      {
        "title": "US Treasury announces new debt ceiling guidelines",
        "source": "Bloomberg",
        "url": "https://bloomberg.com/news/123",
        "time": 1791138302000,
        "symbols": ["$USD", "SPY"],
        "body": "The US Treasury released updated guidelines."
      }
    ]"""

    class FakeResponse:
        def read(self):
            return sample_json

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(tree_news.urllib.request, "urlopen", lambda _req, **_kw: FakeResponse())

    items = tree_news.fetch_tree_news()
    assert len(items) == 1
    assert items[0]["title"] == "US Treasury announces new debt ceiling guidelines"
    assert items[0]["source"] == "TREE_BLOOMBERG"
    assert items[0]["symbols"] == ["$USD", "SPY"]


def test_network_errors_return_empty_gracefully(monkeypatch):
    def fake_fail(_req, **_kw):
        raise URLError("Connection refused")

    monkeypatch.setattr(rss_news.urllib.request, "urlopen", fake_fail)
    monkeypatch.setattr(tree_news.urllib.request, "urlopen", fake_fail)

    assert rss_news.fetch_rss_feed("FED", "https://example.com") == []
    assert rss_news.fetch_all_rss_feeds() == []
    assert tree_news.fetch_tree_news() == []
