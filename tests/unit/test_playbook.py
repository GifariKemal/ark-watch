"""test_playbook.py — unit tests for AMT levels, fast catalyst radar, and trading playbook."""

from datetime import UTC, datetime, timedelta

from arkwatch import api, db
from arkwatch.signals import levels, sentiment


def test_compute_value_area_discrete_bins():
    # Synthetic bars: high volume around price 100, low volume at extremes 90 and 110
    bars = [
        ("2026-10-04T14:00:00Z", 90.0, 92.0, 89.0, 91.0, 10.0),
        ("2026-10-04T15:00:00Z", 98.0, 102.0, 97.0, 100.0, 100.0),  # Heavy cluster at 100
        ("2026-10-04T16:00:00Z", 99.0, 101.0, 98.0, 100.5, 80.0),  # Heavy cluster at 100
        ("2026-10-04T17:00:00Z", 108.0, 110.0, 107.0, 109.0, 10.0),
    ]
    res = levels.compute_value_area(bars, num_bins=50, va_volume_ratio=0.70)
    assert res["poc"] is not None
    # POC should be near 100
    assert 97.0 <= res["poc"] <= 103.0
    assert res["vah"] >= res["poc"]
    assert res["val"] <= res["poc"]
    assert res["total_volume"] == 200.0
    assert res["bars_count"] == 4


def test_compute_session_reference_levels(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)

    # Insert 2 days of 5m bars for NQ1
    # Day 1: 2026-10-02 (Prior Day)
    # Day 2: 2026-10-05 (Current Day, includes overnight and cash session)
    bars = [
        # Day 1 bars (Prior Day: High = 20500, Low = 20000, Close = 20300)
        (
            "NQ1",
            "2026-10-02T13:30:00+00:00",
            "5m",
            "YAHOO",
            20100.0,
            20200.0,
            20000.0,
            20150.0,
            500.0,
            "now",
        ),
        (
            "NQ1",
            "2026-10-02T15:00:00+00:00",
            "5m",
            "YAHOO",
            20150.0,
            20500.0,
            20100.0,
            20400.0,
            800.0,
            "now",
        ),
        (
            "NQ1",
            "2026-10-02T20:00:00+00:00",
            "5m",
            "YAHOO",
            20400.0,
            20420.0,
            20250.0,
            20300.0,
            400.0,
            "now",
        ),
        # Day 2 bars (Overnight session: 04:00 UTC - High = 20350, Low = 20200)
        (
            "NQ1",
            "2026-10-05T04:00:00+00:00",
            "5m",
            "YAHOO",
            20300.0,
            20350.0,
            20200.0,
            20320.0,
            300.0,
            "now",
        ),
        # Day 2 cash session: 14:00 UTC (last price 20450)
        (
            "NQ1",
            "2026-10-05T14:00:00+00:00",
            "5m",
            "YAHOO",
            20350.0,
            20480.0,
            20340.0,
            20450.0,
            900.0,
            "now",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    conn.commit()

    ref = levels.compute_session_reference_levels(conn, "NQ1", as_of="2026-10-05T14:30:00+00:00")
    assert ref is not None
    assert ref["symbol"] == "NQ1"
    assert ref["last_price"] == 20450.0
    levs = ref["levels"]
    assert levs["PDH"] == 20500.0
    assert levs["PDL"] == 20000.0
    assert levs["PDC"] == 20300.0
    assert levs["ONH"] == 20350.0
    assert levs["ONL"] == 20200.0
    assert levs["VAH"] is not None
    assert levs["VAL"] is not None
    assert levs["POC"] is not None

    # Check provenance
    prov = ref["provenance"]
    assert prov["prior_session_bars_evaluated"] == 3
    assert prov["current_session_bars_evaluated"] == 2
    assert prov["overnight_bars_evaluated"] == 1
    assert "weekly_bars_accumulated" in prov
    assert prov["session_convention"] == "CME_Globex_18ET_to_17ET"
    conn.close()


def test_fast_intraday_catalyst_radar(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)

    now = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    t_1h = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    t_10h = (now - timedelta(hours=10)).isoformat(timespec="seconds")

    conn.execute(
        """
        INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id, relevance, novelty, fetched_at, published_at_utc)
        VALUES ('fast-1', 'RSS_FED', 'Fed Rate Cut Signal', 'https://fed.gov/1', 'Dovish pivot', '[]', 'c1', 1.0, 1.0, ?, ?)
        """,
        (t_1h, t_1h),
    )
    conn.execute(
        """
        INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id, relevance, novelty, fetched_at, published_at_utc)
        VALUES ('fast-old', 'FMP', 'Ancient news', 'https://fmp.com/old', 'Old commentary', '[]', 'c2', 0.8, 0.8, ?, ?)
        """,
        (t_10h, t_10h),
    )
    conn.execute(
        """
        INSERT INTO news_intelligence (
            news_id, asset, stance, magnitude, confidence, macro_channel,
            impact_horizon, evidence_level, evidence_quote, transmission_rationale,
            created_at, published_at_utc
        ) VALUES ('fast-1', 'NQ1', 'BULLISH', 0.8, 0.9, 'RATES_POLICY', 'INTRADAY_VOLATILITY', 'OBSERVED', 'Dovish pivot', 'Yield drop', ?, ?)
        """,
        (t_1h, t_1h),
    )
    conn.execute(
        """
        INSERT INTO news_intelligence (
            news_id, asset, stance, magnitude, confidence, macro_channel,
            impact_horizon, evidence_level, evidence_quote, transmission_rationale,
            created_at, published_at_utc
        ) VALUES ('fast-old', 'NQ1', 'BEARISH', 0.5, 0.5, 'GROWTH_DEMAND', 'SWING_MULTIDAY', 'INFERRED', 'Old comment', 'Risk', ?, ?)
        """,
        (t_10h, t_10h),
    )
    conn.commit()

    # Fast radar with 4-hour window should only capture fast-1 (1h ago) and ignore fast-old (10h ago)
    radar = sentiment.compute_intraday_catalyst_radar(conn, "NQ1", window_hours=4, as_of=now)
    assert radar["sample_count"] == 1
    assert radar["stance"] == "BULLISH"
    assert radar["net_stance_score"] > 0.5
    assert len(radar["top_intraday_quotes"]) == 1
    assert radar["provenance"]["window_hours"] == 4

    conn.close()


def test_generate_trading_playbook_scenarios_and_api(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    now = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    now_iso = now.isoformat(timespec="seconds")
    t_1h = (now - timedelta(hours=1)).isoformat(timespec="seconds")

    # Seed intraday bars
    bars = [
        (
            "NQ1",
            (now - timedelta(days=1, hours=2)).isoformat(timespec="seconds"),
            "5m",
            "YAHOO",
            20000.0,
            20300.0,
            19900.0,
            20200.0,
            500.0,
            now_iso,
        ),
        (
            "NQ1",
            (now - timedelta(days=1, hours=1)).isoformat(timespec="seconds"),
            "5m",
            "YAHOO",
            20200.0,
            20400.0,
            20100.0,
            20350.0,
            600.0,
            now_iso,
        ),
        (
            "NQ1",
            (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
            "5m",
            "YAHOO",
            20350.0,
            20450.0,
            20300.0,
            20420.0,
            400.0,
            now_iso,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    # Seed news
    conn.execute(
        """
        INSERT INTO market_news (news_id, source, title, url, summary, symbols_json, cluster_id, relevance, novelty, fetched_at, published_at_utc)
        VALUES ('pb-1', 'RSS_FED', 'Rate Cut Imminent', 'https://fed.gov/pb', 'Dovish rates', '[]', 'c1', 1.0, 1.0, ?, ?)
        """,
        (t_1h, t_1h),
    )
    conn.execute(
        """
        INSERT INTO news_intelligence (
            news_id, asset, stance, magnitude, confidence, macro_channel,
            impact_horizon, evidence_level, evidence_quote, transmission_rationale,
            created_at, published_at_utc
        ) VALUES ('pb-1', 'NQ1', 'BULLISH', 0.8, 0.9, 'RATES_POLICY', 'INTRADAY_VOLATILITY', 'OBSERVED', 'Dovish rates', 'Yield relief', ?, ?)
        """,
        (t_1h, t_1h),
    )
    conn.commit()
    conn.close()

    # Call playbook generation
    pb = api.get_trading_playbook("NQ1", db_path=db_file, as_of=now)
    assert pb is not None
    assert pb["symbol"] == "NQ1"
    assert "reference_levels" in pb
    assert "catalysts" in pb
    assert "amt_context" in pb
    assert "open_type" in pb["amt_context"]
    assert "scenarios" in pb
    assert len(pb["scenarios"]) >= 1
    scenario = pb["scenarios"][0]
    assert "trigger_condition" in scenario
    assert "target_profit" in scenario
    assert "invalidation_level" in scenario
    assert "empirical_support" in scenario
    assert "source" in scenario["empirical_support"]
    assert "open_type_gate" in scenario["empirical_support"]
    # Verify levels API
    lev = api.get_session_levels("NQ1", db_path=db_file, as_of=now)
    assert lev is not None
    assert lev["symbol"] == "NQ1"
    assert "PDH" in lev["levels"]
    assert "VAH" in lev["levels"]
