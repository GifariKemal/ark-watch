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


def test_dalton_4_open_types():
    atr = 10.0
    pdh, pdl, vah, val = 110.0, 90.0, 105.0, 95.0

    # Open Drive Bullish: opens at 100, low 99.8 (tail <= 0.5), drives to 108
    bars_od = [
        ("2026-10-05T13:30:00Z", 100.0, 104.0, 99.8, 103.5, 100.0),
        ("2026-10-05T13:35:00Z", 103.5, 106.0, 103.0, 105.5, 100.0),
        ("2026-10-05T13:40:00Z", 105.5, 108.0, 105.0, 107.5, 100.0),
    ]
    res_od = amt.classify_open_type(bars_od, pdh, pdl, vah, val, atr)
    assert res_od["open_type"] == "OPEN_DRIVE_BULLISH"
    assert res_od["conviction"] == "HIGHEST_CONVICTION"

    # Open Rejection-Reverse Bearish: spikes above PDH (112), closes back below (108)
    bars_orr = [
        ("2026-10-05T13:30:00Z", 108.0, 113.0, 107.0, 112.0, 100.0),
        ("2026-10-05T13:35:00Z", 112.0, 112.5, 109.0, 109.5, 100.0),
        ("2026-10-05T13:40:00Z", 109.5, 110.0, 107.0, 108.0, 100.0),
    ]
    res_orr = amt.classify_open_type(bars_orr, pdh, pdl, vah, val, atr)
    assert res_orr["open_type"] == "OPEN_REJECTION_REVERSE_BEARISH"

    # Open in Value: opens at 100 (inside 95-105)
    bars_oiv = [
        ("2026-10-05T13:30:00Z", 100.0, 102.0, 99.0, 101.0, 50.0),
        ("2026-10-05T13:35:00Z", 101.0, 101.5, 99.5, 100.5, 50.0),
        ("2026-10-05T13:40:00Z", 100.5, 101.0, 99.8, 100.2, 50.0),
    ]
    res_oiv = amt.classify_open_type(bars_oiv, pdh, pdl, vah, val, atr)
    assert res_oiv["open_type"] == "OPEN_IN_VALUE"


def test_participant_activity_classification():
    vah = 105.0
    val = 95.0
    bull_bar = ("ts", 106.0, 108.0, 105.5, 107.5, 100.0)
    bear_bar = ("ts", 107.0, 107.5, 105.5, 106.0, 100.0)
    discount_bull_bar = ("ts", 93.0, 94.5, 92.5, 94.0, 100.0)

    # Initiative Buying: price > VAH and bar is bullish
    act_ib = amt.classify_participant_activity(107.5, vah, val, bull_bar)
    assert act_ib["activity"] == "INITIATIVE_BUYING"

    # Responsive Selling: price > VAH and bar is bearish
    act_rs = amt.classify_participant_activity(106.0, vah, val, bear_bar)
    assert act_rs["activity"] == "RESPONSIVE_SELLING"

    # Responsive Buying: price < VAL and bar is bullish
    act_rb = amt.classify_participant_activity(94.0, vah, val, discount_bull_bar)
    assert act_rb["activity"] == "RESPONSIVE_BUYING"


def test_value_migration_relationships():
    # Higher Value: curr_val (110) > prior_vah (105)
    mig_high = amt.classify_value_migration(120.0, 110.0, 115.0, 105.0, 95.0, 100.0)
    assert mig_high["relationship"] == "HIGHER_VALUE"
    assert mig_high["bias"] == "STRONG_BULLISH"

    # Lower Value: curr_vah (90) < prior_val (95)
    mig_low = amt.classify_value_migration(90.0, 80.0, 85.0, 105.0, 95.0, 100.0)
    assert mig_low["relationship"] == "LOWER_VALUE"
    assert mig_low["bias"] == "STRONG_BEARISH"

    # Inside Value: completely contained
    mig_in = amt.classify_value_migration(103.0, 97.0, 100.0, 105.0, 95.0, 100.0)
    assert mig_in["relationship"] == "INSIDE_VALUE"


def test_dynamic_cva_and_dalton_targets():
    # 3 sessions with overlapping Value Areas
    session_bars = {
        "2026-10-01": [("ts1", 100.0, 106.0, 96.0, 102.0, 100.0)],
        "2026-10-02": [("ts2", 101.0, 107.0, 97.0, 103.0, 100.0)],
        "2026-10-03": [("ts3", 102.0, 108.0, 98.0, 104.0, 100.0)],
    }
    cva = amt.compute_dynamic_cva(session_bars, min_sessions=2, max_sessions=5)
    assert cva is not None
    assert cva["composite_days_count"] >= 2
    assert cva["dalton_measured_move"]["upside_breakout_target"] is not None
    assert cva["dalton_measured_move"]["downside_breakout_target"] is not None


def test_find_naked_pocs():
    # Session 1 POC at 100. Session 2 never touches 100 (traded 110 to 120).
    session_bars = {
        "2026-10-01": [("ts1", 98.0, 102.0, 97.0, 100.0, 200.0)],  # POC ~100
        "2026-10-02": [
            ("ts2", 110.0, 120.0, 109.0, 115.0, 100.0)
        ],  # Range 109-120 (never touches 100)
    }
    res = amt.find_naked_pocs(session_bars, current_price=115.0)
    assert res["total_naked_pocs"] == 1
    assert res["nearest_naked_poc_below"] is not None
    assert 97.0 <= res["nearest_naked_poc_below"]["poc"] <= 103.0


def test_get_asset_ib_timing():
    t_eq, lbl_eq = amt.get_asset_ib_timing("NQ1", is_dst=True)
    assert t_eq == time(13, 30)
    assert lbl_eq == "US_CASH_OPEN_0930ET"

    t_oil, lbl_oil = amt.get_asset_ib_timing("CL1", is_dst=True)
    assert t_oil == time(13, 0)
    assert lbl_oil == "NYMEX_ENERGY_PIT_0900ET"

    t_gold, lbl_gold = amt.get_asset_ib_timing("GC1", is_dst=True)
    assert t_gold == time(12, 20)
    assert lbl_gold == "COMEX_METALS_PIT_0820ET"

    t_btc, lbl_btc = amt.get_asset_ib_timing("BTCUSD", is_dst=True)
    assert t_btc == time(22, 0)
    assert lbl_btc == "CRYPTO_SESSION_OPEN"
