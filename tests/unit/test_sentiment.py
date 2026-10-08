"""test_sentiment.py — unit tests for news intelligence extraction and multi-asset sentiment radar."""

import json
from datetime import UTC, datetime, timedelta

from arkwatch import api, db
from arkwatch.signals import sentiment


def test_normalize_asset():
    assert sentiment._normalize_asset("NQ1") == "NQ1"
    assert sentiment._normalize_asset("nq1") == "NQ1"
    assert sentiment._normalize_asset("XAU") == "GC1"
    assert sentiment._normalize_asset("gold") == "GC1"
    assert sentiment._normalize_asset("WTI") == "CL1"
    assert sentiment._normalize_asset("brent") == "BZ1"
    assert sentiment._normalize_asset("SPY") == "ES1"
    assert sentiment._normalize_asset("btc") == "BTCUSD"
    assert sentiment._normalize_asset("UNKNOWN_TICKER") is None


def test_extract_news_intelligence_with_mock_llm(tmp_path, monkeypatch):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    # Insert candidate news in market_news
    conn.execute(
        """
        INSERT INTO market_news (
            news_id, source, title, url, summary, symbols_json, cluster_id,
            relevance, novelty, fetched_at, published_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "news-job-report-1",
            "RSS_FED",
            "U.S. Payrolls soften, reducing Fed rate hike odds",
            "https://fed.gov/jobs",
            "Labor market data cools down while Treasury yields decline.",
            "[]",
            "cluster-1",
            1.0,
            1.0,
            now_iso,
            now_iso,
        ),
    )
    conn.commit()

    mock_llm_response = json.dumps(
        {
            "article_analysis": "Weak jobs data leads to dovish Fed repricing and lower yields.",
            "primary_channel": "RATES_POLICY",
            "evidence_level": "OBSERVED",
            "asset_impacts": [
                {
                    "asset": "NQ1",
                    "stance": "BULLISH",
                    "magnitude": 0.5,
                    "confidence": 0.85,
                    "horizon": "SWING_MULTIDAY",
                    "macro_channel": "RATES_POLICY",
                    "evidence_level": "OBSERVED",
                    "transmission_rationale": "Yield drop provides tech valuation relief",
                    "evidence_quote": "Labor market data cools down while Treasury yields decline.",
                },
                {
                    "asset": "DXY",
                    "stance": "BEARISH",
                    "magnitude": 0.5,
                    "confidence": 0.80,
                    "horizon": "SWING_MULTIDAY",
                    "macro_channel": "RATES_POLICY",
                    "evidence_level": "OBSERVED",
                    "transmission_rationale": "Dovish repricing softens dollar",
                    "evidence_quote": "reducing Fed rate hike odds",
                },
                {
                    "asset": "GOLD",
                    "stance": "BULLISH",
                    "magnitude": 0.5,
                    "confidence": 0.75,
                    "horizon": "SWING_MULTIDAY",
                    "macro_channel": "RATES_POLICY",
                    "evidence_level": "OBSERVED",
                    "transmission_rationale": "Lower real yield drag boosts gold",
                    "evidence_quote": "Treasury yields decline",
                },
            ],
        }
    )

    monkeypatch.setattr(sentiment, "_call", lambda _cfg, **_kw: mock_llm_response)

    count = sentiment.extract_news_intelligence(conn, limit=10, cfg={})
    assert count == 1

    # Verify rows in news_intelligence
    rows = conn.execute(
        "SELECT asset, stance, magnitude, confidence, macro_channel, evidence_level FROM news_intelligence ORDER BY asset"
    ).fetchall()
    assert len(rows) == 3
    # GOLD mapped to GC1
    assets = [r[0] for r in rows]
    assert assets == ["DXY", "GC1", "NQ1"]

    # Verify DXY is bearish, NQ1 is bullish
    dxy_row = next(r for r in rows if r[0] == "DXY")
    assert dxy_row[1] == "BEARISH"
    assert dxy_row[4] == "RATES_POLICY"

    nq_row = next(r for r in rows if r[0] == "NQ1")
    assert nq_row[1] == "BULLISH"
    assert nq_row[2] == 0.5

    conn.close()


def test_compute_asset_sentiment_radar_aggregation(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)

    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    t_1h = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    t_40h = (now - timedelta(hours=40)).isoformat(timespec="seconds")

    # Insert market news records
    conn.execute(
        """
        INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id, relevance, novelty, fetched_at, published_at_utc)
        VALUES ('n1', 'RSS_FED', 'Rate Cut Imminent', 'https://fed.gov/1', 'Fed cuts rates', '[]', 'c1', 1.0, 1.0, ?, ?)
        """,
        (t_1h, t_1h),
    )
    conn.execute(
        """
        INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id, relevance, novelty, fetched_at, published_at_utc)
        VALUES ('n2', 'FMP', 'Tech Earnings Warning', 'https://fmp.com/2', 'Analyst warns of slow growth', '[]', 'c2', 0.8, 0.8, ?, ?)
        """,
        (t_40h, t_40h),
    )

    # Insert news intelligence:
    # n1: NQ1 BULLISH, strong, observed, very recent (1h ago)
    conn.execute(
        """
        INSERT INTO news_intelligence (
            news_id, asset, stance, magnitude, confidence, macro_channel,
            impact_horizon, evidence_level, evidence_quote, transmission_rationale,
            created_at, published_at_utc
        ) VALUES ('n1', 'NQ1', 'BULLISH', 0.8, 0.9, 'RATES_POLICY', 'SWING_MULTIDAY', 'OBSERVED', 'Fed cuts rates', 'Rate relief', ?, ?)
        """,
        (t_1h, t_1h),
    )
    # n2: NQ1 BEARISH, weak, inferred, older (40h ago)
    conn.execute(
        """
        INSERT INTO news_intelligence (
            news_id, asset, stance, magnitude, confidence, macro_channel,
            impact_horizon, evidence_level, evidence_quote, transmission_rationale,
            created_at, published_at_utc
        ) VALUES ('n2', 'NQ1', 'BEARISH', 0.2, 0.5, 'GROWTH_DEMAND', 'INTRADAY_VOLATILITY', 'INFERRED', 'Analyst warns', 'Valuation risk', ?, ?)
        """,
        (t_40h, t_40h),
    )
    conn.commit()

    radar = sentiment.compute_asset_sentiment_radar(conn, "NQ1", window_days=3, as_of=now)

    assert radar["asset"] == "NQ1"
    assert radar["sample_count"] == 2
    # The recent strong observed bullish event should dominate the decaying inferred bearish event
    assert radar["net_stance_score"] > 0.3
    assert radar["stance"] == "BULLISH"
    assert "RATES_POLICY" in radar["catalysts"]
    assert radar["evidence_breakdown"]["OBSERVED"] == 1
    assert radar["evidence_breakdown"]["INFERRED"] == 1
    assert len(radar["top_quotes"]) == 2

    # Test storing signals
    stored_ids = sentiment.store_asset_radars(conn, as_of=now)
    assert "news_radar_nq1" in stored_ids
    signal_row = conn.execute(
        "SELECT value, state, inputs_json FROM computed_signals WHERE signal_id='news_radar_nq1'"
    ).fetchone()
    assert signal_row is not None
    assert signal_row[1] == "BULLISH"
    assert json.loads(signal_row[2])["sample_count"] == 2

    conn.close()


def test_api_sentiment_integration(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

    conn.execute(
        """
        INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id, relevance, novelty, fetched_at, published_at_utc)
        VALUES ('api-news-1', 'RSS_OILPRICE', 'Oil Spikes On Conflict', 'https://oil.com/1', 'Strait of Hormuz tension', '[]', 'c1', 1.0, 1.0, ?, ?)
        """,
        (now_iso, now_iso),
    )
    conn.execute(
        """
        INSERT INTO news_intelligence (
            news_id, asset, stance, magnitude, confidence, macro_channel,
            impact_horizon, evidence_level, evidence_quote, transmission_rationale,
            created_at, published_at_utc
        ) VALUES ('api-news-1', 'CL1', 'BULLISH', 0.8, 0.85, 'SUPPLY_SHOCK', 'SWING_MULTIDAY', 'SOURCED', 'Strait of Hormuz tension', 'Supply disruption risk', ?, ?)
        """,
        (now_iso, now_iso),
    )
    conn.commit()
    conn.close()

    intel = api.get_news_intelligence(db_path=db_file, asset="CL1")
    assert len(intel) == 1
    assert intel[0]["asset"] == "CL1"
    assert intel[0]["stance"] == "BULLISH"
    assert intel[0]["evidence_quote"] == "Strait of Hormuz tension"

    radar = api.get_asset_sentiment_radar("CL1", db_path=db_file)
    assert radar["asset"] == "CL1"
    assert radar["stance"] == "BULLISH"
    assert radar["catalysts"].get("SUPPLY_SHOCK", 0) > 0.5


def test_single_stale_article_is_shrunk_toward_neutral(tmp_path):
    """Normalising by total weight cancels the decay: one 3.9h-old article used to keep its
    full score. Shrinkage toward 0 (prior pseudo-weight) lets decay reduce conviction."""
    conn = db.get_conn(tmp_path / "a.db", allow_init=True)
    now = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t_old = (now - timedelta(hours=3.9)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id,"
        " relevance, novelty, fetched_at, published_at_utc) VALUES ('o', 'RSS_FED', 't', 'u', 's',"
        " '[]', 'c', 1.0, 1.0, ?, ?)",
        (t_old, t_old),
    )
    conn.execute(
        "INSERT INTO news_intelligence (news_id, asset, stance, magnitude, confidence,"
        " macro_channel, impact_horizon, evidence_level, evidence_quote, transmission_rationale,"
        " created_at, published_at_utc) VALUES ('o', 'NQ1', 'BULLISH', 1.0, 1.0, 'RATES_POLICY',"
        " 'INTRADAY_VOLATILITY', 'OBSERVED', 'q', 'r', ?, ?)",
        (t_old, t_old),
    )
    conn.commit()

    radar = sentiment.compute_intraday_catalyst_radar(conn, "NQ1", window_hours=4, as_of=now)
    assert 0.0 < radar["net_stance_score"] < sentiment.STANCE_THRESHOLD
    assert radar["stance"] == "NEUTRAL"
