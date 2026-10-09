"""Session/DST/as_of contracts for the AMT features merged from upstream (2026-10-09)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from test_session_boundaries import PRIOR, _conn

from arkwatch.signals import amt, amt_horizons, horizons, levels, playbook


def _ref(bars, as_of, symbol="NQ1", daily=()):
    conn = _conn(bars, symbol=symbol)
    conn.executemany(
        "INSERT INTO instrument_prices (symbol, ts, source, open, high, low, close, volume)"
        " VALUES (?, ?, 'YAHOO', ?, ?, ?, ?, 100.0)",
        [(symbol, *d) for d in daily],
    )
    return levels.compute_session_reference_levels(conn, symbol, as_of=as_of)


def test_ib_timing_uses_each_markets_own_dst():
    # US and UK DST differ: 2026-10-27 is GMT in London but still EDT in New York
    assert amt.get_asset_ib_timing("EURUSD", date(2026, 10, 27))[0] == time(8, 0)
    assert amt.get_asset_ib_timing("EURUSD", date(2026, 10, 6))[0] == time(7, 0)
    assert amt.get_asset_ib_timing("EURUSD", date(2027, 3, 16))[0] == time(8, 0)  # US EDT, UK GMT
    assert amt.get_asset_ib_timing("NQ1", date(2026, 11, 2))[0] == time(14, 30)
    assert amt.get_asset_ib_timing("NQ1", date(2027, 3, 8))[0] == time(14, 30)
    assert amt.get_asset_ib_timing("CL1", date(2026, 10, 6))[0] == time(13, 0)


def test_crypto_session_straddling_dst_change_is_all_rth():
    # Session 2026-11-01 opens Sat 18:00 EDT (22:00 UTC) and runs past the 02:00 fall-back;
    # session 2027-03-14 opens Sat 18:00 EST (23:00 UTC) and runs past the spring-forward.
    for prior, first, later, as_of in (
        (
            "2026-10-30T12:00:00Z",
            "2026-10-31T22:00:00Z",
            "2026-11-01T12:00:00Z",
            "2026-11-01T13:00Z",
        ),
        (
            "2027-03-12T12:00:00Z",
            "2027-03-13T23:00:00Z",
            "2027-03-14T12:00:00Z",
            "2027-03-14T13:00Z",
        ),
    ):
        bars = [(prior, 1, 2, 0.5, 1.5, 1), (first, 1, 3, 1, 2, 1), (later, 2, 4, 2, 3, 1)]
        ref = _ref(bars, as_of, symbol="BTCUSD")
        assert ref["active_session_current"] == as_of[:10]
        assert ref["provenance"]["current_session_bars_evaluated"] == 2
        assert ref["provenance"]["overnight_bars_evaluated"] == 0, as_of
        assert ref["provenance"]["rth_bars_evaluated"] == 2, as_of


def test_first_session_has_no_prior_session():
    # Only the current session has bars: it must not be reported as its own prior (T-1)
    bars = [("2026-10-02T13:30:00+00:00", 100.0, 101.0, 99.0, 100.5, 100.0)]
    assert _ref(bars, "2026-10-02T14:00:00Z") is None
    # the playbook caller emits no scenarios off a missing T-1 instead of a self-referencing one
    pb = playbook.generate_trading_playbook(_conn(bars), "NQ1", as_of="2026-10-02T14:00:00Z")
    assert pb is None


def test_overnight_cva_uses_session_boundaries():
    bars = [
        *[(ts.replace("10-01", "10-05"), *rest) for ts, *rest in PRIOR],
        ("2026-10-05T22:30:00+00:00", 200.0, 201.0, 199.0, 200.0, 7.0),  # 18:30 ET -> overnight
        ("2026-10-06T04:00:00+00:00", 200.0, 202.0, 199.0, 201.0, 11.0),
        ("2026-10-06T13:30:00+00:00", 150.0, 152.0, 149.0, 151.0, 1000.0),  # RTH
    ]
    cva = _ref(bars, "2026-10-06T14:00:00Z")["auction_context"]["overnight_cva"]
    assert cva["status"] == "COMPLETED"
    assert cva["total_volume"] == 18.0


def test_european_desk_ibs_follow_european_dst():
    # 2026-10-27: London 08:00 GMT = 08:00 UTC, Frankfurt 08:00 CET = 07:00 UTC
    bars = [
        ("2026-10-23T14:00:00+00:00", 100.0, 110.0, 90.0, 100.0, 10.0),
        ("2026-10-27T06:00:00+00:00", 100.0, 900.0, 100.0, 100.0, 10.0),  # before both opens
        ("2026-10-27T07:00:00+00:00", 100.0, 800.0, 100.0, 100.0, 10.0),  # Frankfurt open
        ("2026-10-27T08:00:00+00:00", 100.0, 105.0, 99.0, 100.0, 10.0),  # London open
    ]
    ib = _ref(bars, "2026-10-27T09:00:00Z")["auction_context"]["multi_desk_initial_balance"]
    assert ib["frankfurt_open_ib"]["ib_high"] == 800.0
    assert ib["london_open_ib"]["ib_high"] == 105.0


def test_daily_history_has_no_look_ahead():
    bars = [*PRIOR, ("2026-10-02T13:30:00+00:00", 800.0, 801.0, 799.0, 800.0, 100.0)]
    daily = [
        ("2026-08-03", 500, 510, 500, 505),
        ("2026-08-04", 500, 510, 500, 505),
        ("2026-09-01", 600, 610, 600, 605),
        ("2026-09-02", 600, 610, 600, 605),
        ("2026-10-01", 700, 710, 700, 705),
        ("2026-10-02", 700, 9999, 1, 705),  # current session: not a completed day yet
    ]
    ctx = _ref(bars, "2026-10-02T14:00:00Z", daily=daily)["auction_context"]
    monthly = ctx["hierarchical_naked_pocs"]["monthly_virgin_pocs"]
    assert monthly["total_naked_pocs"] == 2
    hi = ctx["multi_horizon_cva_map"]["high_horizon_cvas"]["ipda_multi_day_cvas"]["1D"]["high"]
    assert hi == 710.0


def test_session_profiles_stop_at_as_of():
    bars = [
        *PRIOR,
        ("2026-10-02T08:00:00+00:00", 100.0, 101.0, 99.0, 100.0, 10.0),  # 04:00 ET London
        ("2026-10-02T10:00:00+00:00", 100.0, 999.0, 99.0, 100.0, 10.0),  # after as_of
    ]
    ctx = _ref(bars, "2026-10-02T09:00:00Z")["auction_context"]
    london = ctx["session_profiles"]["london_desk"]
    assert (london["high"], london["bars_count"]) == (101.0, 1)
    assert ctx["quarterly_theory"]["active_quarter_amt"]["high"] == 101.0


def test_horizon_amt_uses_5m_bars_once():
    conn = _conn(
        [(f"2026-10-08T12:{m:02d}:00+00:00", 100.0, 101.0, 99.0, 100.0, 10.0) for m in (0, 5)]
    )
    conn.execute(
        "INSERT INTO intraday_bars VALUES ('NQ1','2026-10-08T12:00:00+00:00','5m','EODHD',"
        "100,101,99,100,10,'now')"
    )
    conn.execute(
        "INSERT INTO intraday_bars VALUES ('NQ1','2026-10-08T12:01:00+00:00','1m','YAHOO',"
        "100,999,99,100,10,'now')"
    )
    res = amt_horizons.compute_horizon_amt(
        conn, "NQ1", datetime(2026, 10, 8, 12, tzinfo=UTC), datetime(2026, 10, 8, 13, tzinfo=UTC)
    )
    assert (res["bars_count"], res["high"], res["total_volume"]) == (2, 101.0, 20.0)


def test_quarterly_theory_uses_the_qt_trading_day():
    # Mon 22:30 UTC = 18:30 EDT: Tuesday's Q1 Asia is live, Tuesday's Q2-Q4 are still ahead
    bars = [
        ("2026-10-05T14:00:00+00:00", 100.0, 101.0, 99.0, 100.0, 10.0),
        ("2026-10-05T22:20:00+00:00", 100.0, 101.0, 99.0, 100.0, 10.0),
    ]
    qt = _ref(bars, "2026-10-05T22:30:00Z")["auction_context"]["quarterly_theory"]
    assert qt["active_quarter"] == "Q1_ASIA"
    assert qt["all_daily_quarters"]["Q3_NY_AM"] == "Upcoming"


def test_weekly_days_use_the_session_date_and_todays_range():
    # Sun 23:00 UTC = 19:00 EDT: Monday's (2026-10-05) session is live
    bars = [
        ("2026-10-02T14:00:00+00:00", 100.0, 150.0, 50.0, 100.0, 10.0),
        ("2026-10-04T22:30:00+00:00", 100.0, 103.0, 98.0, 101.0, 10.0),
    ]
    days = _ref(bars, "2026-10-04T23:00:00Z")["auction_context"]["quarterly_theory"][
        "weekly_days_profile"
    ]
    assert days["Monday"]["date"] == "2026-10-05"
    assert days["Monday"]["status"] == "ACTIVE_TODAY"
    assert (days["Monday"]["high"], days["Monday"]["low"]) == (103.0, 98.0)


def test_intraday_90m_naked_pocs_align_to_qt_sub_quarters():
    # Q3 NY AM (06:00-12:00 EDT = 10:00-16:00 UTC) sub-quarters start 10:00, 11:30, 13:00 UTC
    bars = [
        *PRIOR,
        ("2026-10-06T10:00:00+00:00", 100.0, 101.0, 100.0, 100.5, 10.0),
        ("2026-10-06T11:30:00+00:00", 200.0, 201.0, 200.0, 200.5, 10.0),
        ("2026-10-06T13:00:00+00:00", 300.0, 301.0, 300.0, 300.5, 10.0),
    ]
    tier = _ref(bars, "2026-10-06T13:30:00Z")["auction_context"]["hierarchical_naked_pocs"][
        "intraday_90m_naked_pocs"
    ]
    assert [p["session_id"] for p in tier["all_naked_pocs"]] == [
        "2026-10-06T10:00:00+00:00",
        "2026-10-06T11:30:00+00:00",
    ]


def test_monthly_quarter_naive_datetime_is_utc():
    # Oct 2026 week 1 opens Sun 2026-10-04 18:00 EDT = 22:00 UTC
    aware = horizons.get_monthly_quarter(datetime(2026, 10, 4, 22, 30, tzinfo=UTC))
    assert aware["owning_year_month"] == "2026-10"
    assert horizons.get_monthly_quarter(datetime(2026, 10, 4, 22, 30)) == aware
