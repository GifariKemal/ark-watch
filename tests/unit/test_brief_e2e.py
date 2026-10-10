"""E2E brief-render guard on a seeded fixture (round-9 meta-gap fix).

Round-7 shipped three days of brief-render bugs with a green suite. The
only full-render tests were (a) test_golden_brief.py — structure-only,
on a COPY of the live DB, skipping in CI — and (b) the flows-section
fixtures in test_flows_extra.py. NOTHING asserted that content lines
stay coherent with their drivers: the R7 P0 smile-prefix bug (suffix
"(as of MM-DD)" breaking `== "WEAK"` → XAUUSD driver line dead +
EURUSD contradicting the Dollar header) would have passed every
existing test.

This module renders the FULL brief on a minimal seeded DB and asserts
content, not just section presence. Missing data must not raise (the
brief degrades gracefully) — the fixture is deliberately tiny.

The render clock is pinned to a WIB WEEKDAY: the Saturday variant cuts
Book implications (the smile-driven lines under test), so an unpinned
run would make the assertions depend on the day the suite executes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.signals.brief import generate_brief
from arkwatch.signals.pillars import WIB


class _PinnedDatetime(datetime):
    """datetime subclass whose now() returns a fixed WIB-weekday instant.

    Subclassing keeps fromisoformat/strftime arithmetic real; only now()
    is pinned so the Saturday/Sunday render variants are excluded
    deterministically (max rewind = 2 days; fixture data is seeded
    relative to the REAL clock, so all age gates still pass).
    """

    pin: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        base = cls.pin or datetime.now(UTC)
        return base if tz is None else base.astimezone(tz)


def _weekday_pin() -> datetime:
    now = datetime.now(UTC)
    while now.astimezone(WIB).weekday() >= 5:  # 5=Sat, 6=Sun
        now -= timedelta(days=1)
    return now


def _day(n: int = 0) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _seed(conn) -> None:
    """Minimal rows the renderer needs — everything else must degrade."""
    now = _now_iso()

    # series_registry: every seeded observation series (raw_observations
    # has an FK on it) — also the population the Quality header counts.
    conn.executemany(
        "INSERT INTO series_registry(series_id, name, block, tier, unit,"
        " value_format, freq, primary_source, active)"
        " VALUES (?,?,?,?,?,?,?, ?,1)",
        [
            ("FRED:DTWEXBGS", "Dollar Index", "B", 4, "idx", "{:.1f}", "D", "FRED"),
            ("FRED:VIXCLS", "VIX", "F", 2, "idx", "{:.1f}", "D", "FRED"),
            ("FRED:DFII10", "10Y Real Yield", "B", 2, "pct", "{:.2f}", "D", "FRED"),
        ],
    )

    def obs(sid: str, ts: str, value: float) -> tuple:
        return (sid, ts, value, "realtime", "TEST", now)

    # DTWEXBGS: 25 daily points, -0.2/day → 20d momentum ≈ -3.4% → WEAK
    # smile WITH the "(as of MM-DD)" suffix — exactly the R7 bug shape.
    dtw = [obs("FRED:DTWEXBGS", _day(24 - i), 120.0 - 0.2 * i) for i in range(25)]
    # VIX + real yield: two distinct prints each → overnight-changes lines
    vix = [obs("FRED:VIXCLS", _day(1), 18.0), obs("FRED:VIXCLS", _day(0), 19.5)]
    ry = [obs("FRED:DFII10", _day(1), 1.90), obs("FRED:DFII10", _day(0), 2.05)]
    conn.executemany(
        "INSERT INTO raw_observations(series_id, ts, value, vintage_ts, source,"
        " fetched_at) VALUES (?,?,?,?,?,?)",
        dtw + vix + ry,
    )

    # COT: one Gold Disaggregated MM row → Positioning block renders
    conn.execute(
        "INSERT INTO cot_raw(report_date, contract_code, report_type, release_ts,"
        " category, long, short, conc_top4_long, conc_top4_short, change_long,"
        " change_short, source, fetched_at)"
        " VALUES (?,?, 'disagg', ?, 'mm', 180000, 60000, 45.0, 30.0, 5000,"
        " -2000, 'TEST', ?)",
        (_day(2), "088691", _day(2), now),
    )

    # flows: one row → Flows: header + ETF/GLD/stablecoin lines
    conn.execute(
        "INSERT INTO flows_daily(date, btc_etf_musd, eth_etf_musd, gld_tonnes,"
        " stablecoin_usd) VALUES (?,?,?,?,?)",
        (_day(1), -46.6, 12.0, 1332.4, 252.3e9),
    )

    # FedWatch: one DIY snapshot for the next meeting → Policy line
    conn.execute(
        "INSERT INTO fedwatch_snapshots(date, meeting_date, source, prob_ease,"
        " prob_hold, prob_hike, implied_rate)"
        " VALUES (?,?,?,?,?,?,?)",
        (_day(0), _day(-30), "diy", 0.05, 0.90, 0.05, 4.00),
    )

    # events + indicator_stats: one scored release → Surprise/ESI line;
    # one upcoming high-importance release → Event radar
    conn.executemany(
        "INSERT INTO events(event_uid, ts_utc, release_ts, country, name, normalized_name,"
        " importance, consensus, actual, surprise_z, indicator_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("t-esi", now, now, "US", "TEST CPI", "TEST CPI", "medium", 0.2, 1.4, 1.2, "TESTCPI"),
            (
                "t-radar",
                f"{_day(-2)}T13:30:00+00:00",
                f"{_day(-2)}T13:30:00+00:00",
                "US",
                "TEST NFP",
                "TEST NFP",
                "high",
                None,
                None,
                None,
                None,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO indicator_stats(indicator, as_of, sigma, n_obs, window, low_conf)"
        " VALUES (?,?,?,?,?,0)",
        ("TESTCPI", _day(0), 1.0, 60, "5y"),
    )
    sigma_snapshot_id = f"fixture-{now}"
    conn.execute(
        "INSERT INTO indicator_sigma_snapshots"
        "(snapshot_id,as_of,calculated_at,n_indicators,method,payload_sha256) "
        "VALUES (?,?,?,?,?,?)",
        (sigma_snapshot_id, _day(0), now, 1, "fixture", "fixture"),
    )
    conn.execute(
        "INSERT INTO indicator_sigma_vintages"
        "(snapshot_id,indicator,sigma,n_obs,window,low_conf) VALUES (?,?,?,?,?,?)",
        (sigma_snapshot_id, "TESTCPI", 1.0, 60, "5y", 0),
    )

    # fetch_log: two OK fetches today → Sources ratio
    conn.executemany(
        "INSERT INTO fetch_log(ts, fetcher, target, status, rows) VALUES (?,?,?,?,1)",
        [(now, "test", "FRED:DTWEXBGS", "OK"), (now, "test", "FRED:VIXCLS", "OK")],
    )


@pytest.fixture()
def brief_conn(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "brief_e2e.db", allow_init=True)
    _seed(conn)
    _PinnedDatetime.pin = _weekday_pin()
    monkeypatch.setattr("arkwatch.signals.brief.datetime", _PinnedDatetime)
    yield conn
    _PinnedDatetime.pin = None
    conn.close()


def _render(conn, tmp_path) -> str:
    return generate_brief(conn, str(tmp_path / "out.db"))


def test_full_render_sections_and_content_coherence(brief_conn, tmp_path):
    text = _render(brief_conn, tmp_path)
    lines = text.split("\n")

    # --- headers (structure) ---
    assert lines[0].startswith("=== US MACRO BRIEF — ")
    regime = next(ln for ln in lines if ln.startswith("REGIME :"))
    assert any(lbl in regime for lbl in ("RISK-ON", "RISK-OFF", "NEUTRAL", "INSUFFICIENT DATA"))
    assert next(ln for ln in lines if ln.startswith("QUADRANT:"))
    assert next(ln for ln in lines if ln.startswith("Quality: ✓"))

    # --- smile content, not just presence: the label carries the
    # "(as of MM-DD)" suffix (round-6 honesty marker) — consumers must
    # prefix-match (the R7 P0 regression class)
    dollar = next(ln for ln in lines if ln.startswith("Dollar  :"))
    assert dollar.startswith("Dollar  : WEAK")
    assert "(as of " in dollar

    # --- sections fed by the seeded rows ---
    assert next(ln for ln in lines if ln.startswith("Overnight changes"))
    assert next(ln for ln in lines if ln.startswith("Surprise: ESI +1.20"))
    assert next(ln for ln in lines if ln.startswith("Pillars:"))
    for label in ("Policy", "RealYield", "Inflation", "Growth", "Liquidity", "Stress"):
        assert any(ln.strip().startswith(label) for ln in lines), label
    gold_mm = next(ln for ln in lines if "Gold" in ln and "MM" in ln)
    assert "net +120,000" in gold_mm
    assert next(ln for ln in lines if ln.startswith("Flows:"))
    etf = next(ln for ln in lines if "ETF flows" in ln)
    assert "BTC -47M$" in etf and "ETH +12M$" in etf
    assert next(ln for ln in lines if "GLD 1,332t" in ln)
    assert next(ln for ln in lines if "Stablecoin $252B" in ln)
    fw = next(ln for ln in lines if ln.startswith("Policy: FedWatch"))
    assert "hold 90%" in fw
    radar = next(ln for ln in lines if ln.startswith("Event radar (7 days):"))
    assert radar
    assert any("TEST NFP" in ln for ln in lines)

    # --- smile → book-implications coherence (the R7 ship-green class):
    # a WEAK dollar must drive the USD bias text on the metals line and
    # the EURUSD direction line — neither may fall back to 'neutral'
    xau = next(ln for ln in lines if ln.strip().startswith("XAUUSD"))
    assert "USD↓ tailwind" in xau
    assert "neutral" not in xau
    eur = next(ln for ln in lines if ln.strip().startswith("EURUSD"))
    assert "dollar weak" in eur
    assert "neutral" not in eur

    sources = next(ln for ln in lines if ln.startswith("Sources:"))
    assert "2/2 fetch OK (today)" in sources

    # None must never leak into a rendered line (parity with the golden test)
    for ln in lines:
        if any(ch.isdigit() for ch in ln):
            assert "None" not in ln, ln


def test_render_deterministic(brief_conn, tmp_path):
    """A second render from the same fixture is byte-identical.

    The pinned clock makes even the WIB header timestamp stable, so no
    normalization is needed — stricter than the golden test."""
    first = _render(brief_conn, tmp_path)
    second = _render(brief_conn, tmp_path)
    assert first == second


def test_empty_db_renders_without_raising(tmp_path, monkeypatch):
    """Degradation contract: a fresh schema (no data at all) still renders
    the core skeleton — the brief must degrade, never crash."""
    conn = db.get_conn(tmp_path / "empty.db", allow_init=True)
    _PinnedDatetime.pin = _weekday_pin()
    monkeypatch.setattr("arkwatch.signals.brief.datetime", _PinnedDatetime)
    try:
        text = generate_brief(conn, str(tmp_path / "out.db"))
    finally:
        _PinnedDatetime.pin = None
        conn.close()
    lines = text.split("\n")
    assert lines[0].startswith("=== US MACRO BRIEF — ")
    assert next(ln for ln in lines if ln.startswith("REGIME :"))
    assert next(ln for ln in lines if ln.startswith("Pillars:"))
    assert next(ln for ln in lines if ln.startswith("Sources: 0/0 fetch OK"))
    # no data → smile degrades to N/A, never to an exception or 'None'
    dollar = next(ln for ln in lines if ln.startswith("Dollar  :"))
    assert dollar == "Dollar  : N/A"
    assert "None" not in text
