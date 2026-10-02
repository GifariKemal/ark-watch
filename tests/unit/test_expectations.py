"""Tests for signals/expectations.py (Cleveland model vs market reads)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.signals.expectations import (
    expectations_brief_line,
    inflation_risk_premium,
    store_expectations_signals,
)


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


def _month(n: int = 0) -> str:
    """A monthly ts `n` months back, clamped to the 1st (model-leg shape)."""
    d = date(datetime.now(UTC).year, datetime.now(UTC).month, 1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
    return d.isoformat()


def _day(n: int = 0) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


def _seed_obs(conn, sid, ts, value):
    conn.execute(
        "INSERT OR REPLACE INTO series_registry(series_id, name, block, tier, unit, "
        "value_format, freq, primary_source) VALUES (?,?,'B',0,'?','{:,.1f}','M',?)",
        (sid, sid, sid),
    )
    conn.execute(
        "INSERT OR REPLACE INTO raw_observations(series_id, ts, value, vintage_ts, "
        "source, fetched_at) VALUES (?,?,?,?, 'test', ?)",
        (sid, ts, value, "realtime", ts),
    )
    conn.commit()


def _seed_full(conn, exp10=2.49, exp1=2.39, be=2.42, rr=2.20, dfii=2.34, stale=False):
    m = _month(4 if stale else 0)
    _seed_obs(conn, "CLEVE:EXPINF_10Y", m, exp10)
    _seed_obs(conn, "CLEVE:EXPINF_1Y", m, exp1)
    _seed_obs(conn, "CLEVE:REALRATE_10Y", m, rr)
    _seed_obs(conn, "FRED:T10YIE", _day(0), be)
    _seed_obs(conn, "FRED:DFII10", _day(0), dfii)


def test_irp_math(conn):
    """IRP = breakeven − model expectation (bp); TIPS liq = DFII − model real."""
    _seed_full(conn)
    e = inflation_risk_premium(conn)
    assert e["irp_10y_bp"] == pytest.approx((2.42 - 2.49) * 100)  # −7bp
    assert e["tips_liq_10y_bp"] == pytest.approx((2.34 - 2.20) * 100)  # +14bp
    assert e["expinf_1y"] == pytest.approx(2.39)
    assert e["model_stale"] is False


def test_degrades_when_model_leg_missing(conn):
    _seed_obs(conn, "FRED:T10YIE", _day(0), 2.42)
    assert inflation_risk_premium(conn) is None
    assert expectations_brief_line(conn) is None
    assert store_expectations_signals(conn) == 0


def test_market_legs_optional(conn):
    """Model leg present, market legs absent → None spreads but the level
    reads survive (the brief renders 1y/10y without IRP)."""
    _seed_obs(conn, "CLEVE:EXPINF_10Y", _month(0), 2.49)
    _seed_obs(conn, "CLEVE:EXPINF_1Y", _month(0), 2.39)
    e = inflation_risk_premium(conn)
    assert e["irp_10y_bp"] is None and e["tips_liq_10y_bp"] is None
    line = expectations_brief_line(conn)
    assert line == "Exp: 1y 2.4% · 10y 2.5%"


def test_stale_model_leg_marked(conn):
    _seed_full(conn, stale=True)  # model month 3 back
    e = inflation_risk_premium(conn)
    assert e["model_stale"] is True
    line = expectations_brief_line(conn)
    assert "⚠(model" in line and "stale)" in line


def test_brief_line_full_render(conn):
    _seed_full(conn)
    _seed_obs(conn, "CLEVE:IRP_10Y_MODEL", _month(0), 0.4436)
    line = expectations_brief_line(conn)
    assert line == "Exp: 1y 2.4% · 10y 2.5% · IRP 10y -7bp · IRP mdl +44bp · TIPS liq +14bp"


def test_brief_model_irp_stale_suppressed(conn):
    """The model-IRP segment follows the same 45d model-leg staleness gate —
    a stale official decomposition must not print beside fresh market legs."""
    _seed_full(conn)
    _seed_obs(conn, "CLEVE:IRP_10Y_MODEL", _month(4), 0.4436)  # 4 months old
    line = expectations_brief_line(conn)
    assert "IRP mdl" not in line
    assert "IRP 10y -7bp" in line


def test_tips_liq_floor(conn):
    """|TIPS liq| < 10bp renders as noise → segment omitted."""
    _seed_full(conn, dfii=2.24)  # +4bp
    assert "TIPS liq" not in expectations_brief_line(conn)


def test_store_persists_with_model_ts_and_buckets(conn):
    _seed_full(conn)
    assert store_expectations_signals(conn) == 1
    ts, value, state = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='irp_10y'"
    ).fetchone()
    assert ts == _month(0)  # ts = the MODEL leg's month (slower cadence governs)
    assert value == pytest.approx(-7.0)
    assert state == "FLAT"  # within ±25bp
    # re-run: same model month → REPLACE, no duplicate history
    store_expectations_signals(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id='irp_10y'"
    ).fetchone()[0]
    assert n == 1


def test_store_state_buckets(conn):
    _seed_full(conn, be=2.80)  # +31bp → PREMIUM
    store_expectations_signals(conn)
    assert (
        conn.execute(
            "SELECT state FROM computed_signals WHERE signal_id='irp_10y'"
        ).fetchone()[0]
        == "PREMIUM"
    )
