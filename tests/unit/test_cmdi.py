"""Offline tests for the CMDI reactivation (owner GO 2026-09-20).

Pins the phantom-feed fix: the 2026-09-17 deactivation was a missing
pagination walk (1 obs stored vs 1,129 expected while fetch_log read OK).
  - pagination walks 100/page until a short page (runaway backstop too)
  - three series map to their columns (market/ig/hy)
  - window floor reaches the FULL 2005->now history (total gap-heal)
  - the 45d lag-aware stale guard does not false-fire on a 2wk-old print
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arkwatch.fetchers import eodhd


def _page(n, base_year=2005):
    """n rows of weekly data counting forward from base_year."""
    rows = []
    for i in range(n):
        d = datetime(base_year, 1, 7) + timedelta(weeks=i)
        rows.append(
            {
                "as_of_date": d.date().isoformat(),
                "market_cmdi": 0.20 + (i % 10) / 100,
                "ig_cmdi": 0.27,
                "hy_cmdi": 0.08,
                "source": "ny_fed",
            }
        )
    return rows


class TestCmdi:
    def test_pagination_walk_until_short_page(self, monkeypatch):
        # realistic volume: >500 rows so the production short-history guard passes
        pages = {str(o): _page(100, base_year=2005 + o // 5200) for o in range(0, 500, 100)}
        pages["500"] = _page(29, base_year=2015)  # the short page ends the walk
        calls = []

        def fake_get(path, params=None):
            calls.append(params.get("page[offset]"))
            return pages[str(params["page[offset]"])]

        monkeypatch.setattr(eodhd, "_get", fake_get)
        monkeypatch.setattr(eodhd, "_cmdi_cache", None)
        rows = eodhd.fetch_cmdi_all()
        assert calls == [0, 100, 200, 300, 400, 500]
        assert len(rows) == 529  # 5×100 + 29, sorted ascending
        assert rows[0]["as_of_date"] < rows[-1]["as_of_date"]

    def test_runaway_backstop(self, monkeypatch):
        def fake_get(path, params=None):
            return _page(100)  # always a full page — never ends

        monkeypatch.setattr(eodhd, "_get", fake_get)
        monkeypatch.setattr(eodhd, "_cmdi_cache", None)
        with pytest.raises(eodhd.EodhdError, match="pagination exceeded"):
            eodhd.fetch_cmdi_all()

    def test_three_series_columns_and_full_walk(self, monkeypatch):
        # 129 weekly rows ENDING 30d ago (the OBSERVED worst lag) — the
        # window must return the ENTIRE walk (audit: the old 8,000d rolling
        # floor would have expired 2026-12-03; date math is now gone)
        newest = datetime.now(UTC) - timedelta(days=30)
        rows = []
        for i in range(129):
            d = (newest - timedelta(weeks=128 - i)).date().isoformat()
            rows.append(
                {
                    "as_of_date": d,
                    "market_cmdi": 0.20 + (i % 10) / 100,
                    "ig_cmdi": 0.27,
                    "hy_cmdi": 0.08,
                    "source": "ny_fed",
                }
            )
        monkeypatch.setattr(eodhd, "_cmdi_cache", rows)
        oldest = rows[0]["as_of_date"]
        for sid, val in (("EODHD:CMDI", None), ("EODHD:CMDI_IG", 0.27), ("EODHD:CMDI_HY", 0.08)):
            pts = eodhd.fetch_window(sid, days=10)
            assert pts and pts[0]["ts"] == oldest  # the walk IS the window
            if val is not None:
                assert pts[0]["value"] == val
        # unrouted key still honestly unsupported (empty, not an error)
        assert eodhd.fetch_window("EODHD:NOPE", days=10) == []

    def test_stale_guard_lag_aware(self, monkeypatch):
        # OBSERVED worst alive-lag is 30d (2026-09-20) — must NOT trip;
        # the guard fires only on a genuinely dead feed (>60d)
        newest = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
        monkeypatch.setattr(
            eodhd,
            "_cmdi_cache",
            [
                {
                    "as_of_date": newest,
                    "market_cmdi": 0.21,
                    "ig_cmdi": 0.27,
                    "hy_cmdi": 0.08,
                }
            ],
        )
        pts = eodhd.fetch_window("EODHD:CMDI", days=10)
        assert len(pts) == 1  # 30d old print must NOT trip the 60d guard

        dead = (datetime.now(UTC) - timedelta(days=70)).date().isoformat()
        monkeypatch.setattr(
            eodhd,
            "_cmdi_cache",
            [
                {
                    "as_of_date": dead,
                    "market_cmdi": 0.21,
                    "ig_cmdi": 0.27,
                    "hy_cmdi": 0.08,
                }
            ],
        )
        with pytest.raises(eodhd.EodhdError, match="stale"):
            eodhd.fetch_window("EODHD:CMDI", days=10)

    def test_fetch_latest_maps_columns(self, monkeypatch):
        monkeypatch.setattr(
            eodhd,
            "_get",
            lambda path, params=None: [
                {
                    "as_of_date": "2026-08-21T00:00:00+00:00",
                    "market_cmdi": 0.21,
                    "ig_cmdi": 0.27,
                    "hy_cmdi": 0.08,
                },
            ],
        )
        assert eodhd.fetch_latest("EODHD:CMDI") == {"ts": "2026-08-21", "value": 0.21}
        assert eodhd.fetch_latest("EODHD:CMDI_IG") == {"ts": "2026-08-21", "value": 0.27}
        assert eodhd.fetch_latest("EODHD:CMDI_HY") == {"ts": "2026-08-21", "value": 0.08}
