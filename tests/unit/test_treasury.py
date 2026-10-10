"""Treasury par-curve CSV fetcher — hermetic (real CSV rows captured 2026-10-10)."""

from __future__ import annotations

import pytest

from arkwatch.fetchers import treasury

HEADER = (
    'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr",'
    '"5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
)
CSV_2026 = HEADER + (
    "10/09/2026,4.13,4.13,4.13,4.25,4.29,4.32,4.47,4.80,4.89,5.02,5.13,5.24,5.65,5.60\n"
    "10/08/2026,4.14,4.14,4.13,4.23,4.29,4.30,4.44,4.75,4.85,4.99,5.11,5.22,5.64,5.60\n"
)
CSV_2025 = HEADER + (
    "12/31/2025,3.74,3.75,3.67,3.67,3.63,3.59,3.48,3.47,3.55,3.73,3.94,4.18,4.79,4.84\n"
    "01/02/2025,4.45,,4.36,4.36,4.31,4.25,4.17,4.25,4.29,4.38,4.47,4.57,4.86,4.79\n"
)


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text


@pytest.fixture
def served(monkeypatch):
    """year -> CSV body; records every request so the per-year cache is checked."""
    calls: list[str] = []
    bodies: dict[int, str] = {}

    def fake_get(url, params, timeout):
        calls.append(url)
        assert params["_format"] == "csv"
        return _Resp(bodies[int(params["field_tdr_date_value"])])

    monkeypatch.setattr(treasury.requests, "get", fake_get)
    treasury._entries.cache_clear()
    yield bodies, calls
    treasury._entries.cache_clear()


def _freeze_year(monkeypatch, year):
    real = treasury.datetime

    class _DT(real):
        @classmethod
        def now(cls, tz=None):
            return real(year, 1, 2, tzinfo=tz)

    monkeypatch.setattr(treasury, "datetime", _DT)


def test_latest_per_tenor_one_request_per_year(served, monkeypatch):
    bodies, calls = served
    bodies[2026] = CSV_2026
    _freeze_year(monkeypatch, 2026)
    assert treasury.fetch_latest("TREASURY:PAR_7Y") == {"ts": "2026-10-09", "value": 5.13}
    assert treasury.fetch_latest("TREASURY:PAR_20Y") == {"ts": "2026-10-09", "value": 5.65}
    assert treasury.fetch_latest("TREASURY:PAR_1_5Y") == {"ts": "2026-10-09", "value": 4.13}
    assert len(calls) == 1  # ~17 s server latency: tenors share one fetch


def test_january_falls_back_to_previous_year(served, monkeypatch):
    bodies, _ = served
    bodies[2027], bodies[2026] = "", CSV_2026  # live: an unstarted year is a 0-byte CSV
    _freeze_year(monkeypatch, 2027)
    assert treasury.fetch_latest("TREASURY:PAR_7Y") == {"ts": "2026-10-09", "value": 5.13}


def test_blank_cells_skipped_and_unknown_tenor(served, monkeypatch):
    bodies, _ = served
    bodies[2025] = CSV_2025
    rows = treasury._entries(2025)
    assert "1.5 Month" not in rows[1] and rows[1]["ts"] == "2025-01-02"
    with pytest.raises(treasury.TreasuryError, match="unknown tenor"):
        treasury.fetch_latest("TREASURY:PAR_4Y")  # Treasury publishes no 4Y
