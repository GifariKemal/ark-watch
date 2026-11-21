"""Offline test for the ATL:WGT parse fix (health-sweep 2026-09-21).

The original wiring looked for a 'data_overview' sheet that does not exist
(it is data_overall) and a 'date' text header that is not there either —
the fallback chain landed on data_chart1 and a zero-valued column, storing
0.0 as the wage-growth median and sailing through sanity_min=0.0. This test
recreates the REAL sheet shape (source row, 'Overall' header, TEXT values)
and pins the corrected behavior.
"""

from __future__ import annotations

from datetime import datetime

import openpyxl

from arkwatch.fetchers import atl


def _workbook(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data_overall"
    ws.append(["Sources: Current Population Survey etc."])
    ws.append([None, "Overall", "Services", "Full-time", "College degree"])
    ws.append([datetime(1997, 1, 1), "4.4", "4.5", "4.5", "4.5"])
    ws.append([datetime(2026, 7, 1), "3.8", "3.9", "4.0", "4.2"])
    ws.append([datetime(2026, 8, 1), "4.1", "4.2", "4.3", "4.5"])
    ws.append([None, None, None, None, None])  # trailing padding
    wb.create_sheet("data_chart1")  # the trap sheet the old fallback landed on
    path = tmp_path / "wgt.xlsx"
    wb.save(path)
    return path.read_bytes()


class TestWgt:
    def test_overall_column_text_numbers(self, tmp_path, monkeypatch):
        payload = _workbook(tmp_path)

        class R:
            status_code = 200
            content = payload

        monkeypatch.setattr(atl.requests, "get", lambda *a, **k: R())
        out = atl.fetch_latest()
        assert out == {"ts": "2026-08-01", "value": 4.1}  # Overall, NOT 0.0

    def test_url_follows_2026_site_move(self):
        """Atlanta Fed moved data files from /Documents/datafiles/chcs/ to
        /Documents/research-and-data/data/ (Nov 2026); the old path 302s to
        notfound.aspx (HTTP 404) and verify failed daily with 'not XLSX'."""
        assert "/Documents/research-and-data/data/wage-growth-tracker/" in atl.URL
        assert "/datafiles/chcs/" not in atl.URL

    def test_sanity_floor_blocks_exact_zero(self):
        """The meta-lesson: sanity_min=0.0 let literal zeros pass as a wage
        median. The registry floor is now 1.0 — assert it from the YAML so
        the gate cannot silently regress."""
        from arkwatch.qa.verify_sources import load_registry

        row = next(e for e in load_registry() if e["series_id"] == "ATL:WGT")
        assert row["sanity_min"] >= 1.0, "a 0.0 wage-growth median must FAIL the gate"
