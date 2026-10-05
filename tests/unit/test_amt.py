"""test_amt.py — unit tests for advanced Auction Market Theory (AMT) engine."""

from datetime import time

from arkwatch.signals import amt


def test_tpo_profile_and_brackets():
    # 4 bars across 2 hours (4 x 30m)
    bars = [
        ("2026-10-05T13:30:00Z", 100.0, 105.0, 95.0, 102.0, 100.0),  # Bracket A
        ("2026-10-05T14:00:00Z", 102.0, 108.0, 100.0, 106.0, 150.0),  # Bracket B
        ("2026-10-05T14:30:00Z", 106.0, 110.0, 104.0, 108.0, 200.0),  # Bracket C
        ("2026-10-05T15:00:00Z", 108.0, 109.0, 106.0, 107.0, 80.0),  # Bracket D
    ]
    tpo = amt.compute_tpo_profile(bars, num_bins=30)
    assert tpo["tpo_poc"] is not None
    assert tpo["tpo_vah"] >= tpo["tpo_poc"]
    assert tpo["tpo_val"] <= tpo["tpo_poc"]
    assert "A" in tpo["brackets"]
    assert "B" in tpo["brackets"]
    assert "C" in tpo["brackets"]
    assert "D" in tpo["brackets"]


def test_initial_balance_and_day_types():
    cash_open = time(13, 30)
    # 12 bars of IB (13:30 - 14:30) with range 100 to 110 (IB range = 10)
    ib_bars = (
        [
            ("2026-10-05T13:30:00Z", 100.0, 105.0, 100.0, 102.0, 50.0),
            ("2026-10-05T13:35:00Z", 102.0, 110.0, 101.0, 108.0, 50.0),
        ]
        + [
            (f"2026-10-05T13:{m:02d}:00Z", 105.0, 108.0, 104.0, 106.0, 50.0)
            for m in range(40, 60, 5)
        ]
        + [
            (f"2026-10-05T14:{m:02d}:00Z", 106.0, 109.0, 105.0, 107.0, 50.0)
            for m in range(0, 30, 5)
        ]
    )
    # Post-IB breakout to 140 (high extension > 2.5x -> TREND_DAY)
    post_ib = [
        ("2026-10-05T14:35:00Z", 110.0, 120.0, 109.0, 119.0, 200.0),
        ("2026-10-05T15:00:00Z", 120.0, 140.0, 118.0, 138.0, 300.0),
    ]
    res = amt.analyze_initial_balance(ib_bars + post_ib, cash_open)
    assert res["ib_high"] == 110.0
    assert res["ib_low"] == 100.0
    assert res["ib_range"] == 10.0
    assert res["day_type"] == "TREND_DAY"
    assert res["high_extension_ratio"] >= 2.5


def test_profile_shape_classification():
    # P-Shape: POC near top (e.g. 108 in range 100-110)
    p_shape = amt.classify_profile_shape(108.0, 110.0, 105.0, 110.0, 100.0)
    assert p_shape["shape"] == "P_SHAPE"

    # b-Shape: POC near bottom (e.g. 102 in range 100-110)
    b_shape = amt.classify_profile_shape(102.0, 106.0, 100.0, 110.0, 100.0)
    assert b_shape["shape"] == "b_SHAPE"

    # D-Shape: POC in middle (e.g. 105 in range 100-110)
    d_shape = amt.classify_profile_shape(105.0, 108.0, 102.0, 110.0, 100.0)
    assert d_shape["shape"] == "D_SHAPE"


def test_auction_extremes_excess_and_poor():
    atr = 10.0
    # Bar with a long excess wick at high (wick >= 0.15 * 10 = 1.5)
    excess_bars = [
        ("2026-10-05T13:30:00Z", 100.0, 102.0, 99.0, 101.0, 50.0),
        (
            "2026-10-05T13:35:00Z",
            101.0,
            110.0,
            100.0,
            102.0,
            80.0,
        ),  # High = 110, close = 102 -> wick = 8.0 (>1.5)
        ("2026-10-05T13:40:00Z", 102.0, 104.0, 101.0, 103.0, 50.0),
        ("2026-10-05T13:45:00Z", 103.0, 105.0, 102.0, 104.0, 50.0),
        ("2026-10-05T13:50:00Z", 104.0, 105.0, 103.0, 103.5, 50.0),
    ]
    res = amt.evaluate_auction_extremes(excess_bars, atr)
    assert "EXCESS_SELLING_TAIL" in res["high_structure"]


def test_composite_value_area_2d():
    session_bars = {
        "2026-10-01": [
            ("2026-10-01T14:00:00Z", 100.0, 105.0, 95.0, 102.0, 50.0),
            ("2026-10-01T15:00:00Z", 102.0, 106.0, 98.0, 104.0, 100.0),
        ],
        "2026-10-02": [
            ("2026-10-02T14:00:00Z", 104.0, 108.0, 101.0, 105.0, 60.0),
            ("2026-10-02T15:00:00Z", 105.0, 107.0, 102.0, 106.0, 120.0),
        ],
    }
    cva = amt.compute_composite_value_area(session_bars, num_sessions=2)
    assert cva is not None
    assert cva["composite_name"] == "2D_CVA"
    assert cva["c_poc"] is not None
    assert cva["bars_evaluated"] == 4


def test_time_acceptance_evaluation():
    vah = 105.0
    val = 95.0

    # 13 consecutive closes above VAH (13 x 5m = 65m > 60m)
    recent_closes = [100.0] + [106.0] * 13
    res = amt.evaluate_time_acceptance(recent_closes, vah, val)
    assert res["status"] == "ACCEPTED_ABOVE_VAH"
    assert "DEFINITIVE_ACCEPTANCE_60M" in res["acceptance_level"]
    assert res["duration_minutes"] == 65

    # 4 consecutive closes above VAH (4 x 5m = 20m < 30m) -> premature spike
    recent_closes_spike = [100.0] + [106.0] * 4
    res_spike = amt.evaluate_time_acceptance(recent_closes_spike, vah, val)
    assert res_spike["status"] == "SPIKE_ABOVE_VAH"
    assert "PREMATURE_UNDER_30M" in res_spike["acceptance_level"]
