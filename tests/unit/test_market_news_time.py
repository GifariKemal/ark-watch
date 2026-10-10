"""One malformed `published` must not drop the whole source batch."""

import sqlite3

from arkwatch.fetchers import argus, cryptopanic, rss_news, tree_news
from arkwatch.qa import market_news


def test_bad_published_stamp_keeps_the_rest_of_the_batch(tmp_path, monkeypatch):
    rows = [
        {"source": "FMP", "title": "Oil jumps", "url": "https://a.test/1", "published": "junk"},
        {"source": "FMP", "title": "Fed holds", "url": "https://a.test/2", "published": None},
        {
            "source": "FMP",
            "title": "Gold flat",
            "url": "https://a.test/3",
            "published": "2026-10-09T12:00:00Z",
        },
    ]
    monkeypatch.setattr(market_news, "_fmp", lambda: rows)
    monkeypatch.setattr(market_news, "_eodhd", lambda: [])
    monkeypatch.setattr(market_news, "_gdelt_updates", lambda: {})
    for mod, fn in (
        (tree_news, "fetch_tree_news"),
        (rss_news, "fetch_all_rss_feeds"),
        (cryptopanic, "fetch_cryptopanic_posts"),
        (argus, "fetch_news"),
    ):
        monkeypatch.setattr(mod, fn, lambda: [])
    db = tmp_path / "arkwatch.db"

    assert market_news.run(str(db))["FMP"] == 3
    conn = sqlite3.connect(db)
    stamps = dict(conn.execute("SELECT title, published_at_utc FROM market_news"))
    assert stamps["Gold flat"] == "2026-10-09T12:00:00+00:00"
    assert stamps["Oil jumps"] and stamps["Fed holds"]  # fell back to the fetch time
    status, err = conn.execute(
        "SELECT status, error FROM fetch_log WHERE target='FMP' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert status == "OK" and err.startswith("2 rows with an invalid published time")
