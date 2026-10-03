"""Offline tests for the same-month dated-contract energy signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.qa import energy


class TestMonthCodes:
    def test_next_month(self):
        assert energy.next_month_code(11, 26) == "Z26"
        assert energy.next_month_code(12, 26) == "F27"
        assert energy.next_month_code(1, 26) == "G26"


class TestLegs:
    def test_eodhd_primary_yahoo_fallback(self, monkeypatch):
        calls = []

        def fake_eodhd(root, code):
            calls.append(f"eodhd:{root}{code}")
            if root == "CL":
                return {"ts": "2026-09-21", "close": 93.72, "source": "EODHD"}
            return None  # EODHD down for RB/HO

        def fake_yahoo(root, code):
            calls.append(f"yahoo:{root}{code}")
            return {"ts": "2026-09-21", "close": 3.18, "source": "YAHOO"}

        monkeypatch.setattr(energy, "_eodhd_dated", fake_eodhd)
        monkeypatch.setattr(energy, "_yahoo_dated", fake_yahoo)
        cl = energy._leg("CL", "X26")
        rb = energy._leg("RB", "X26")
        assert cl["source"] == "EODHD"
        assert rb["source"] == "YAHOO"  # fell back
        assert "eodhd:CLX26" in calls and "yahoo:RBX26" in calls

    def test_both_sources_dead_raises(self, monkeypatch):
        monkeypatch.setattr(energy, "_eodhd_dated", lambda r, c: None)
        monkeypatch.setattr(energy, "_yahoo_dated", lambda r, c: None)
        with pytest.raises(energy.EnergyError, match="both failed"):
            energy._leg("CL", "X26")


class TestCompute:
    def _seed_spots(self, conn):
        for sid in ("FRED:DCOILBRENTEU", "FRED:DCOILWTICO"):
            conn.execute(
                "INSERT INTO series_registry(series_id,name,block,tier,unit,"
                "value_format,freq,ts_convention,primary_source,active)"
                " VALUES (?,'t','C',0,'x','level','D','obs_day','test',1)",
                (sid,),
            )
        d = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        for sid, v in (("FRED:DCOILBRENTEU", 130.80), ("FRED:DCOILWTICO", 107.02)):
            conn.execute(
                "INSERT INTO raw_observations(series_id, ts, release_ts, value,"
                " vintage_ts, source, fetched_at) VALUES (?,?,'na',?,'realtime','FMP','t')",
                (sid, d, v),
            )
        conn.commit()

    def test_same_month_cracks(self, tmp_path, monkeypatch):
        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        self._seed_spots(conn)

        legs = {
            ("CL", "X26"): {"ts": "2026-09-20", "close": 93.72, "source": "EODHD"},
            ("RB", "X26"): {"ts": "2026-09-20", "close": 3.1818, "source": "EODHD"},
            ("HO", "X26"): {"ts": "2026-09-20", "close": 4.7186, "source": "EODHD"},
            ("CL", "Z26"): {"ts": "2026-09-20", "close": 90.12, "source": "EODHD"},
        }
        monkeypatch.setattr(energy, "_front_month", lambda: "X26")
        monkeypatch.setattr(
            energy,
            "_curve_legs",
            lambda front, second: {
                "cl1": legs[("CL", front)],
                "rb1": legs[("RB", front)],
                "ho1": legs[("HO", front)],
                "cl2": legs[("CL", second)],
            },
        )
        out = energy.compute(conn)
        assert out["energy_crack_gas"]["value"] == round(3.1818 * 42 - 93.72, 2)
        assert out["energy_crack_ho"]["value"] == round(4.7186 * 42 - 93.72, 2)
        assert out["energy_wti_bwd"]["value"] == round(93.72 - 90.12, 2)
        assert out["energy_brent_wti_spot"]["value"] == 23.78
        n = conn.execute("SELECT COUNT(*) FROM instrument_prices WHERE symbol='CL2'").fetchone()[0]
        assert n == 1
        conn.close()

    def test_curve_falls_back_as_one_provider(self, monkeypatch):
        def fake_eodhd(root, code):
            if root == "RB":
                return None
            return {"ts": "2026-09-20", "close": 1.0, "source": "EODHD"}

        monkeypatch.setattr(energy, "_eodhd_dated", fake_eodhd)
        monkeypatch.setattr(
            energy,
            "_yahoo_dated",
            lambda root, code: {"ts": "2026-09-20", "close": 2.0, "source": "YAHOO"},
        )
        legs = energy._curve_legs("X26", "Z26")
        assert {leg["source"] for leg in legs.values()} == {"YAHOO"}

    def test_curve_rejects_mixed_dates(self, monkeypatch):
        def mismatched(root, code):
            ts = "2026-09-19" if root == "RB" else "2026-09-20"
            return {"ts": ts, "close": 1.0, "source": "test"}

        monkeypatch.setattr(energy, "_eodhd_dated", mismatched)
        monkeypatch.setattr(energy, "_yahoo_dated", mismatched)
        with pytest.raises(energy.EnergyError, match="same-date"):
            energy._curve_legs("X26", "Z26")


class TestCrack321:
    def test_crack_321_formula_in_compute(self, monkeypatch):
        monkeypatch.setattr(energy, "_front_month", lambda: "X26")
        legs = {
            "cl1": {"ts": "2026-09-21", "close": 90.00, "source": "YAHOO"},
            "rb1": {"ts": "2026-09-21", "close": 3.00, "source": "YAHOO"},
            "ho1": {"ts": "2026-09-21", "close": 3.50, "source": "YAHOO"},
            "cl2": {"ts": "2026-09-21", "close": 89.00, "source": "YAHOO"},
        }
        monkeypatch.setattr(energy, "_curve_legs", lambda front, second: legs)
        monkeypatch.setattr(energy, "_spot_pair", lambda conn: ("2026-09-21", 95.0, 90.0))

        conn = db.get_conn(":memory:", allow_init=True)
        out = energy.compute(conn)
        conn.close()

        # (2 * 126 + 1 * 147 - 3 * 90) / 3 = (252 + 147 - 270) / 3 = 129 / 3 = 43.0
        assert "energy_crack_321" in out
        assert out["energy_crack_321"]["value"] == 43.00
        assert out["energy_crack_321"]["ts"] == "2026-09-21"

    def test_compute_crack_321_history(self):
        conn = db.get_conn(":memory:", allow_init=True)
        conn.execute(
            "INSERT INTO instrument_prices VALUES ('CL1', '2026-09-21', 'YAHOO', 90.0, 91, 89, 90.0, 1000, 0)"
        )
        conn.execute(
            "INSERT INTO instrument_prices VALUES ('RB1', '2026-09-21', 'YAHOO', 3.0, 3.1, 2.9, 3.0, 1000, 0)"
        )
        conn.execute(
            "INSERT INTO instrument_prices VALUES ('HO1', '2026-09-21', 'YAHOO', 3.5, 3.6, 3.4, 3.5, 1000, 0)"
        )
        conn.commit()

        history = energy.compute_crack_321_history(conn, limit=10)
        assert len(history) == 1
        assert history[0] == ("2026-09-21", 43.0)
        conn.close()
