from datetime import UTC, date, datetime

from arkwatch.signals import horizons


def test_dst_and_session_window_mapping():
    # 2026-10-08 is in EDT (UTC-4)
    dt_summer = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert horizons.is_dst_edt(dt_summer) is True

    # Check NY time conversion: 12:00 UTC = 08:00 ET
    ny_dt = horizons.to_ny_time(dt_summer)
    assert ny_dt.hour == 8
    assert ny_dt.minute == 0

    target = date(2026, 10, 8)
    # Frankfurt: 02:00 - 11:00 ET (opens 1h before London at 02:00 ET)
    # In UTC (EDT): 02:00 ET = 06:00 UTC, 11:00 ET = 15:00 UTC
    ff_start, ff_end = horizons.get_session_window("FRANKFURT", target)
    assert ff_start == datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
    assert ff_end == datetime(2026, 10, 8, 15, 0, tzinfo=UTC)

    # Singapore: 21:00 ET (prev day) - 04:00 ET -> 01:00 UTC to 08:00 UTC
    sg_start, sg_end = horizons.get_session_window("SINGAPORE", target)
    assert sg_start == datetime(2026, 10, 8, 1, 0, tzinfo=UTC)
    assert sg_end == datetime(2026, 10, 8, 8, 0, tzinfo=UTC)

    # London / NY Overlap: 08:00 - 12:00 ET -> 12:00 - 16:00 UTC
    ov_start, ov_end = horizons.get_session_window("NY_LONDON_OVERLAP", target)
    assert ov_start == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert ov_end == datetime(2026, 10, 8, 16, 0, tzinfo=UTC)


def test_quarterly_theory_and_ipda_partitioning():
    target = date(2026, 10, 8)  # Thursday
    q_bounds = horizons.get_quarterly_session_bounds(target)
    assert "Q1_ASIA" in q_bounds
    assert "Q2_LONDON" in q_bounds
    assert "Q3_NY_AM" in q_bounds
    assert "Q4_NY_PM" in q_bounds

    # Check 90-min subdivision: Q2 London (00:00 - 06:00 ET = 6 hours) -> 4 quarters of 90 mins
    q2_s, q2_e = q_bounds["Q2_LONDON"]
    subs = horizons.subdivide_quarter_90m(q2_s, q2_e)
    assert len(subs) == 4
    for s, e in subs:
        assert (e - s).total_seconds() == 90 * 60

    # Check 22.5-min micro-subdivision
    micros = horizons.subdivide_micro_22m(subs[0][0], subs[0][1])
    assert len(micros) == 4
    for ms, me in micros:
        assert (me - ms).total_seconds() == 22.5 * 60

    # Weekly Day: Thursday should be Q4
    w_info = horizons.get_weekly_quarter(target)
    assert w_info["quarter"] == "Q4"
    assert w_info["is_friday"] is False

    # Friday test: 2026-10-09
    fri_info = horizons.get_weekly_quarter(date(2026, 10, 9))
    assert fri_info["quarter"] == "FRIDAY_SPECIAL"
    assert fri_info["is_friday"] is True

    # Monthly Quarter: 2026-10-08 is day 8 -> Q1
    m_info = horizons.get_monthly_quarter(target)
    assert m_info["quarter"] == "Q1"

    # Joker Week: 2026-11-30 is Week 5 of November 2026 (5-week month) -> Joker Week
    joker_info = horizons.get_monthly_quarter(date(2026, 11, 30))
    assert joker_info["is_joker_week"] is True
    assert joker_info["total_weeks_in_month"] == 5
    assert joker_info["quarter"] == "Q0"
    # IPDA Lookbacks
    now_utc = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    ipda = horizons.get_ipda_ranges(now_utc)
    assert "20D" in ipda
    assert "60D" in ipda
    assert "4H" in ipda
    assert (now_utc - ipda["4H"]).total_seconds() == 4 * 3600
