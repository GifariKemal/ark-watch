"""Fetchers that used to degrade silently (2026-10-12 audit, D12-D14, D18)."""

from __future__ import annotations

import pytest

from arkwatch.fetchers import fedsurvey, sep, spdr


def test_sep_layout_change_raises_and_newest_vintage_is_strict(monkeypatch):
    with pytest.raises(sep.SepError):
        sep._parse_fed_funds_median("Federal Funds Rate | Actual | Median | 4.1 |")  # no years
    monkeypatch.setattr(sep, "sep_dates", lambda: ["2026-09-16", "2026-06-17"])

    def parse(d):
        if d == "2026-09-16":
            raise sep.SepError("layout changed")
        return {"2026": 3.9}

    monkeypatch.setattr(sep, "parse_sep", parse)
    with pytest.raises(sep.SepError):  # no silent fallback to the June vintage
        sep.series_rows()


def test_gld_latest_skips_a_weekend_print():
    arch = [
        {"ts": "2026-09-11", "oz_per_share": 0.09, "total_oz": 1, "tonnes": 960.0},  # Friday
        {"ts": "2026-09-12", "oz_per_share": 0.09, "total_oz": 1, "tonnes": 1006.2},  # Saturday
    ]
    assert spdr.fetch_gld_tonnes(arch)["tonnes"] == 960.0


def test_scoos_short_page_is_an_error(monkeypatch):
    monkeypatch.setattr(fedsurvey, "_fetch", lambda _u: "<html>maintenance</html>")
    with pytest.raises(fedsurvey.FedSurveyError, match="too short"):
        fedsurvey.fetch_scoos("202609")


def test_yahoo_daily_is_dated_in_the_exchange_time_zone(monkeypatch):
    from datetime import UTC, datetime

    from arkwatch.fetchers import yahoo

    mon_london = int(datetime(2026, 10, 11, 23, 0, tzinfo=UTC).timestamp())  # Mon 00:00 BST
    body = {"chart": {"result": [{
        "meta": {"exchangeTimezoneName": "Europe/London"},
        "timestamp": [mon_london],
        "indicators": {"quote": [{"open": [1.1], "high": [1.2], "low": [1.0], "close": [1.15], "volume": [0]}]},
    }]}}  # fmt: skip

    class R:
        status_code = 200

        def json(self):
            return body

    monkeypatch.setattr(yahoo.SESSION, "get", lambda *a, **k: R())
    monkeypatch.setattr(yahoo, "_throttle", lambda: None)
    assert yahoo.fetch_daily("EURUSD=X")[0]["ts"] == "2026-10-12"


def test_stale_pillar_series_drops_out_of_the_regime():
    from datetime import UTC, datetime, timedelta

    from arkwatch import db
    from arkwatch.signals import pillars

    conn = db.get_conn(":memory:", allow_init=True)
    fresh = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    for sid, ts in (("FRED:WALCL", "2026-01-07"), ("FRED:T10Y3M", fresh)):
        conn.execute(
            "INSERT INTO series_registry(series_id, name, block, tier, unit, value_format, freq,"
            " primary_source) VALUES (?, ?, 'A', 0, 'x', 'x', 'D', 'FRED')",
            (sid, sid),
        )
        conn.execute(
            "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at)"
            " VALUES (?, ?, 1.0, 'FRED', 'x')",
            (sid, ts),
        )
    out = {"E": {"z": 1.2, "detail": "WALCL"}, "A": {"z": 0.4, "detail": "curve"}}
    pillars._gate_stale(conn, out)
    conn.close()
    assert out["E"]["z"] is None and out["E"]["stale"] and "STALE" in out["E"]["detail"]
    assert out["A"]["z"] == 0.4


def test_cot_aggregate_scales_each_contract_by_its_open_interest():
    from arkwatch import db
    from arkwatch.signals.cot_signals import _cross_contract_aggregate

    conn = db.get_conn(":memory:", allow_init=True)
    # a huge-count contract slightly short (-1% of OI) vs two small ones clearly long (+40%)
    rows = [("13874+", "lev", 0, 50_000, 5_000_000), ("133741", "lev", 400, 0, 1_000),
            ("099741", "lev", 400, 0, 1_000)]  # fmt: skip
    conn.executemany(
        "INSERT INTO cot_raw(report_date, contract_code, report_type, release_ts, category, long,"
        " short, open_interest_all, source, fetched_at) VALUES ('2026-10-06', ?, 'tff', 'x', ?, ?, ?, ?, 'C', 'x')",
        rows,
    )
    agg = _cross_contract_aggregate(conn)
    conn.close()
    assert agg["total_net"] < 0  # the raw sum would have said RISK-OFF
    assert agg["signal"] == "RISK-ON" and agg["n_contracts"] == 3


def test_calibration_leaves_out_markets_already_decided_at_the_horizon():
    from datetime import UTC, datetime, timedelta

    from arkwatch.signals import calibration

    t = datetime(2026, 9, 30, tzinfo=UTC)
    snaps = lambda p: [(t - timedelta(days=d), p) for d in range(39, -1, -1)]  # noqa: E731 ascending
    markets = [
        {"won": 1, "t": t, "topic": "fed", "snaps": snaps(0.99)},  # public before the close
        {"won": 0, "t": t, "topic": "fed", "snaps": snaps(0.40)},
    ]
    out = calibration.score_binary(markets)
    h = next(iter(out.values()))
    assert h["excluded_already_decided"] == 1 and h["n"] == 1
