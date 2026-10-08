"""Offline tests for the GEO: geopolitics fetchers (energy channel tier 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arkwatch.fetchers import geo


class TestGprParsers:
    def test_monthly_serial_dates_and_blank_cells(self):
        rows = [
            ["month", "GPR", "GPRT", "var_name", "var_label"],
            [40000.0, "", "", "", ""],
            [46235.0, 117.92, 130.72, "GPR", "Geopolitical Risk"],
        ]
        out = geo.parse_gpr_rows(rows)
        assert out == [{"ts": "2026-08-01", "value": 117.92}]

    def test_daily_int_dates(self):
        rows = [
            ["day", "GPRD", "GPRD_ACT"],
            [20260914.0, 189.53, 246.45],
            [20260915.0, "", ""],
        ]
        out = geo.parse_gprd_rows(rows)
        assert out == [{"ts": "2026-09-14", "value": 189.53}]

    def test_fao_csv_preamble_and_columns(self):
        text = (
            "FAO Food Price Index,,,,\n"
            "2014-2016=100,,,,\n"
            "Date,Food Price Index,Meat,Dairy,Cereals,Oils,Sugar\n"
            "2026-07,130.8,127.9,119.2,116.0,196.9,106.4\n"
            "2026-08,133.3,127.9,119.2,116.3,196.9,106.4\n"
        )
        assert geo.parse_fao_csv(text)[-1] == {"ts": "2026-08-01", "value": 133.3}
        assert geo.parse_fao_csv(text, column="Cereals")[-1]["value"] == 116.3

    def test_harpex_payload(self):
        payload = {
            "harpex": [
                {"date": "2026-09-11T00:00:00", "value": 2447.44},
                {"date": "2026-09-18T00:00:00", "value": 2450.42},
            ]
        }
        assert geo.parse_harpex(payload)[-1] == {"ts": "2026-09-18", "value": 2450.42}


class TestGeoRegistry:
    def test_every_geo_series_registered(self):
        from arkwatch.qa.verify_sources import ROUTES, load_registry

        assert "GEO:" in ROUTES and ROUTES["GEO:"] is geo
        reg_ids = {e["series_id"] for e in load_registry()}
        missing = [f"GEO:{k}" for k in geo.GEO_SERIES if f"GEO:{k}" not in reg_ids]
        assert not missing, f"unregistered: {missing}"

    def test_window_floors(self, monkeypatch):
        recent_date = (datetime.now(UTC).date() - timedelta(days=9)).isoformat()
        rows = [{"ts": recent_date, "value": 189.53}]
        monkeypatch.setitem(geo.GEO_SERIES, "GPRD", lambda: rows)
        assert geo.fetch_window("GEO:GPRD", days=1) == rows
