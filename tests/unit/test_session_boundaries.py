"""Session boundary, DST and as_of regression tests for levels/amt/intraday/horizons."""

from __future__ import annotations

from datetime import UTC, date, datetime

from arkwatch import db
from arkwatch.signals import amt, horizons, intraday, levels


def _conn(bars, symbol="NQ1", interval="5m", source="YAHOO"):
    conn = db.get_conn(":memory:", allow_init=True)
    conn.executemany(
        "INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close,"
        " volume, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'now')",
        [(symbol, ts, interval, source, o, h, lo, c, v) for ts, o, h, lo, c, v in bars],
    )
    conn.commit()
    return conn


# Prior session (Thu 2026-10-01 RTH) shared by several tests
PRIOR = [
    ("2026-10-01T13:30:00+00:00", 100.0, 110.0, 95.0, 105.0, 100.0),
    ("2026-10-01T15:00:00+00:00", 105.0, 112.0, 100.0, 108.0, 100.0),
]


def test_dst_boundaries_use_zoneinfo():
    # US DST ended 2026-11-01 and starts 2027-03-14: both dates below are EST
    assert horizons.is_dst_edt(datetime(2026, 11, 2, 12, tzinfo=UTC)) is False
    assert horizons.is_dst_edt(datetime(2027, 3, 8, 12, tzinfo=UTC)) is False
    assert horizons.is_dst_edt(datetime(2027, 3, 15, 12, tzinfo=UTC)) is True
    assert not hasattr(levels, "_is_dst_edt")


def test_cme_session_date_starts_18et():
    # 22:00 UTC = 18:00 EDT -> next trading day; 21:55 UTC still today
    assert horizons.cme_session_date(datetime(2026, 10, 5, 22, tzinfo=UTC)).isoformat() == (
        "2026-10-06"
    )
    assert horizons.cme_session_date(datetime(2026, 10, 5, 21, 55, tzinfo=UTC)).isoformat() == (
        "2026-10-05"
    )
    # EST: 18:00 ET = 23:00 UTC
    assert horizons.cme_session_date(datetime(2026, 11, 2, 22, 30, tzinfo=UTC)).isoformat() == (
        "2026-11-02"
    )


def test_22utc_bar_is_overnight_not_rth():
    bars = [
        *[(ts.replace("10-01", "10-05"), *rest) for ts, *rest in PRIOR],
        ("2026-10-05T22:00:00+00:00", 200.0, 300.0, 199.0, 201.0, 50.0),  # 18:00 ET Globex open
        ("2026-10-06T13:30:00+00:00", 150.0, 152.0, 149.0, 151.0, 100.0),
        ("2026-10-06T13:35:00+00:00", 151.0, 153.0, 150.0, 152.0, 100.0),
        ("2026-10-06T13:40:00+00:00", 152.0, 154.0, 151.0, 153.0, 100.0),
    ]
    ref = levels.compute_session_reference_levels(
        _conn(bars), "NQ1", as_of="2026-10-06T14:00:00+00:00"
    )
    assert ref["provenance"]["overnight_bars_evaluated"] == 1
    assert ref["provenance"]["rth_bars_evaluated"] == 3
    assert ref["levels"]["ONH"] == 300.0
    assert ref["levels"]["OR15_HIGH"] == 154.0
    assert ref["levels"]["OR15_LOW"] == 149.0


def test_est_monday_after_dst_end_splits_rth_at_1430utc():
    bars = [
        ("2026-10-30T14:30:00+00:00", 100.0, 110.0, 95.0, 105.0, 100.0),
        ("2026-10-30T16:00:00+00:00", 105.0, 112.0, 100.0, 108.0, 100.0),
        ("2026-11-01T23:00:00+00:00", 106.0, 120.0, 106.0, 107.0, 50.0),  # 18:00 EST Sunday
        ("2026-11-02T14:00:00+00:00", 107.0, 108.0, 90.0, 107.0, 50.0),  # 09:00 EST premarket
        ("2026-11-02T14:30:00+00:00", 107.0, 109.0, 106.0, 108.0, 100.0),
        ("2026-11-02T14:35:00+00:00", 108.0, 110.0, 107.0, 109.0, 100.0),
        ("2026-11-02T14:40:00+00:00", 109.0, 111.0, 108.0, 110.0, 100.0),
    ]
    ref = levels.compute_session_reference_levels(
        _conn(bars), "NQ1", as_of="2026-11-02T15:00:00+00:00"
    )
    assert ref["reference_session_prior"] == "2026-10-30"
    assert ref["provenance"]["overnight_bars_evaluated"] == 2
    assert ref["provenance"]["rth_bars_evaluated"] == 3
    assert (ref["levels"]["ONH"], ref["levels"]["ONL"]) == (120.0, 90.0)
    assert (ref["levels"]["OR15_HIGH"], ref["levels"]["OR15_LOW"]) == (111.0, 106.0)


def test_initial_balance_ignores_globex_open_bars():
    bars = [("2026-10-05T22:00:00Z", 100.0, 500.0, 1.0, 100.0, 10.0)] + [
        (f"2026-10-06T13:{m:02d}:00Z", 100.0, 110.0, 100.0, 105.0, 10.0) for m in range(30, 60, 5)
    ]
    res = amt.analyze_initial_balance(bars, amt.get_asset_ib_timing("NQ1", date(2026, 10, 6))[0])
    assert (res["ib_high"], res["ib_low"]) == (110.0, 100.0)


def test_crypto_session_open_is_ib_start():
    bars = [
        ("2026-10-05T22:00:00Z", 100.0, 105.0, 99.0, 104.0, 10.0),
        ("2026-10-06T05:00:00Z", 104.0, 130.0, 104.0, 129.0, 10.0),
    ]
    pre, rth = amt.split_rth(bars, amt.get_asset_ib_timing("BTCUSD", date(2026, 10, 6))[0])
    assert pre == [] and rth == bars


def test_naive_as_of_equals_utc():
    bars = [
        *PRIOR,
        ("2026-10-02T13:30:00+00:00", 100.0, 101.0, 99.0, 100.5, 100.0),
        ("2026-10-02T13:35:00+00:00", 100.5, 102.0, 100.0, 101.5, 100.0),
    ]
    conn = _conn(bars)
    naive = levels.compute_session_reference_levels(conn, "NQ1", as_of="2026-10-02T14:00:00")
    aware = levels.compute_session_reference_levels(conn, "NQ1", as_of="2026-10-02T14:00:00Z")
    assert naive["as_of"] == aware["as_of"] == "2026-10-02T14:00:00+00:00"
    assert naive["levels"] == aware["levels"]


def test_weekend_uses_last_completed_session_as_prior():
    bars = [
        *PRIOR,
        ("2026-10-02T13:30:00+00:00", 100.0, 130.0, 99.0, 120.0, 100.0),
        ("2026-10-02T19:00:00+00:00", 120.0, 125.0, 118.0, 122.0, 100.0),
    ]
    ref = levels.compute_session_reference_levels(
        _conn(bars), "NQ1", as_of="2026-10-03T12:00:00+00:00"
    )
    assert ref["reference_session_prior"] == "2026-10-02"
    assert ref["levels"]["PDH"] == 130.0
    assert ref["last_price"] == 122.0
    assert ref["provenance"]["current_session_bars_evaluated"] == 0


def test_levels_ignore_other_intervals_and_duplicate_sources():
    bars = [*PRIOR, ("2026-10-02T13:30:00+00:00", 100.0, 101.0, 99.0, 100.5, 100.0)]
    conn = _conn(bars)
    conn.execute(
        "INSERT INTO intraday_bars VALUES ('NQ1','2026-10-01T13:31:00+00:00','1m','YAHOO',"
        "1,999,1,1,1,'now')"
    )
    conn.execute(
        "INSERT INTO intraday_bars VALUES ('NQ1','2026-10-01T13:30:00+00:00','5m','EODHD',"
        "100,110,95,105,100,'now')"
    )
    ref = levels.compute_session_reference_levels(conn, "NQ1", as_of="2026-10-02T14:00:00Z")
    assert ref["levels"]["PDH"] == 112.0
    assert ref["provenance"]["prior_session_bars_evaluated"] == 2


def test_ipda_dedupes_sources_and_handles_null_rows():
    bars = [*PRIOR, ("2026-10-02T13:30:00+00:00", 100.0, 101.0, 99.0, 100.5, 100.0)]
    conn = _conn(bars)
    conn.executemany(
        "INSERT INTO instrument_prices (symbol, ts, source, high, low) VALUES ('NQ1', ?, ?, ?, ?)",
        [
            ("2026-09-29", "YAHOO", 120.0, 80.0),
            ("2026-09-29", "EODHD", 120.0, 80.0),
            ("2026-09-30", "YAHOO", 110.0, 90.0),
            ("2026-09-30", "EODHD", 110.0, 90.0),
            ("2026-10-01", "EODHD", None, None),
            ("2026-10-02", "YAHOO", 999.0, 1.0),  # current session: not yet a completed day
        ],
    )
    ref = levels.compute_session_reference_levels(conn, "NQ1", as_of="2026-10-02T14:00:00Z")
    lv = ref["levels"]
    assert lv["IPDA_1D_HIGH"] is None
    assert lv["IPDA_2D_HIGH"] == 110.0
    assert (lv["IPDA_3D_HIGH"], lv["IPDA_3D_LOW"]) == (120.0, 80.0)


def test_active_quarter_evening_utc_is_next_day_q1():
    res = horizons.get_active_quarterly_cycles(datetime(2026, 10, 8, 22, tzinfo=UTC))
    assert res["active_quarter"] == "Q1_ASIA"
    assert res["quarter_start_utc"] == "2026-10-08T21:00:00+00:00"
    assert res["active_90m_sub_quarter"] == "Sub-1"
    late = horizons.get_active_quarterly_cycles(datetime(2026, 10, 8, 23, 50, tzinfo=UTC))
    assert late["active_quarter"] == "Q1_ASIA"
    assert late["active_90m_sub_quarter"] == "Sub-2"
    # Naive input is UTC, never host-local
    assert horizons.get_active_quarterly_cycles(datetime(2026, 10, 8, 22)) == res


def test_intraday_as_of_bounds_bar_close():
    bars = [
        ("2026-10-02T13:30:00+00:00", 500.0, 502.0, 499.0, 501.0, 1000.0),
        ("2026-10-02T13:35:00+00:00", 501.0, 503.0, 500.0, 502.0, 1000.0),
        ("2026-10-02T13:40:00+00:00", 502.0, 508.0, 501.0, 507.0, 3000.0),
    ]
    conn = _conn(bars, symbol="SPY")
    intel = intraday.session_intraday_intelligence(conn, "SPY", as_of="2026-10-02T13:40:00Z")
    assert intel["bars_in_session"] == 2
    assert intel["latest_bar_ts"] == "2026-10-02T13:35:00+00:00"
    naive = intraday.session_intraday_intelligence(conn, "SPY", as_of="2026-10-02T13:40:00")
    assert naive == intel


def test_intraday_session_spans_globex_open_and_dedupes():
    bars = [
        ("2026-10-01T22:30:00+00:00", 100.0, 101.0, 99.0, 100.0, 10.0),  # 18:30 ET -> Oct 2
        ("2026-10-02T13:30:00+00:00", 100.0, 102.0, 99.0, 101.0, 10.0),
    ]
    conn = _conn(bars, symbol="ES1")
    conn.execute(
        "INSERT INTO intraday_bars VALUES ('ES1','2026-10-02T13:30:00+00:00','5m','EODHD',"
        "100,102,99,101,10,'now')"
    )
    conn.execute(
        "INSERT INTO intraday_bars VALUES ('ES1','2026-10-02T13:31:00+00:00','1m','YAHOO',"
        "1,999,1,1,1,'now')"
    )
    intel = intraday.session_intraday_intelligence(conn, "ES1")
    assert intel["trade_date"] == "2026-10-02"
    assert intel["bars_in_session"] == 2
