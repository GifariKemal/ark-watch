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
