"""Tests for the Cleveland expectations-workbook parser (fetchers/cleve.py).

Builds a SYNTHETIC workbook (openpyxl writer) and injects it via the
module's memo — no network. Pins the three hazards the review flagged:
decimal→pct conversion, fragment-vs-digit-boundary column matching
('1 year' must not match '11 year' / ' 10 year'), and the no-sortedness
assumption (max-date scan, not rows[-1]).
"""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from arkwatch.fetchers import cleve


def _build_workbook(rows_inf, rows_rr, order="asc", premia_rows=None):
    """rows: [(date, {header: value})] — written in the given order."""
    wb = Workbook()

    def fill(sheet, rows, extra_headers):
        ws = wb.create_sheet(sheet)
        headers = ["Model Output Date"] + extra_headers
        ws.append(headers)
        for d, rec in rows:
            ws.append([d] + [rec.get(h) for h in extra_headers])

    fill(
        "Expected Inflation",
        rows_inf,
        ["1 year Expected Inflation", "10 year Expected Inflation", "11 year Expected Inflation"],
    )
    fill(
        "Real Interest Rate",
        rows_rr,
        ["Real Rate 1-month", "Real Rate 1-year", "Real Rate 10-year "],
    )
    if premia_rows is not None:
        fill(
            "Ten-year Expected Chart",
            premia_rows,
            ["10 year Expected Inflation", "Real Risk Premium", "Inflation Risk Premium"],
        )
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@pytest.fixture()
def patched(monkeypatch):
    def inject(rows_inf, rows_rr, order="asc", premia_rows=None):
        # the module's own parser is exercised by monkeypatching the download
        # to return the synthetic workbook bytes
        buf = _build_workbook(rows_inf, rows_rr, order, premia_rows=premia_rows)
        monkeypatch.setattr(
            cleve.requests,
            "get",
            lambda *a, **k: type("R", (), {"status_code": 200, "content": buf.getvalue()})(),
        )
        cleve._expectations_cache = None  # reset memo between tests

    yield inject
    cleve._expectations_cache = None


def test_decimal_to_pct_and_column_matching(patched):
    patched(
        [
            (
                datetime(2026, 8, 1),
                {
                    "1 year Expected Inflation": 0.0239,
                    "10 year Expected Inflation": 0.0249,
                    "11 year Expected Inflation": 0.025,
                },
            )
        ],
        [
            (
                datetime(2026, 8, 1),
                {
                    "Real Rate 1-year": 0.0214,
                    "Real Rate 10-year ": 0.0220,
                    "Real Rate 1-month": 0.0289,
                },
            )
        ],
    )
    assert cleve.fetch_latest("CLEVE:EXPINF_1Y") == {
        "ts": "2026-08-01",
        "value": pytest.approx(2.39, abs=0.01),
    }
    # digit boundary: '1 year' must NOT pick the 10y or 11y columns
    assert cleve.fetch_latest("CLEVE:EXPINF_10Y") == {
        "ts": "2026-08-01",
        "value": pytest.approx(2.49, abs=0.01),
    }
    # 'Real Rate 1-year' must not match 'Real Rate 1-month' / '10-year '
    assert cleve.fetch_latest("CLEVE:REALRATE_1Y") == {
        "ts": "2026-08-01",
        "value": pytest.approx(2.14, abs=0.01),
    }
    assert cleve.fetch_latest("CLEVE:REALRATE_10Y") == {
        "ts": "2026-08-01",
        "value": pytest.approx(2.20, abs=0.01),
    }


def test_unsorted_dates_max_wins(patched):
    """The workbook is date-ascending TODAY — not a contract (CBOE rows[-1]
    lesson): the parser must scan for the max date, not take the last row."""
    patched(
        [
            (datetime(2026, 9, 1), {"1 year Expected Inflation": 0.030}),  # newest but FIRST
            (datetime(2026, 8, 1), {"1 year Expected Inflation": 0.0239}),
            (datetime(2026, 7, 1), {"1 year Expected Inflation": 0.0250}),
        ],
        [(datetime(2026, 9, 1), {"Real Rate 10-year ": 0.022})],
    )
    assert cleve.fetch_latest("CLEVE:EXPINF_1Y")["ts"] == "2026-09-01"
    assert cleve.fetch_latest("CLEVE:EXPINF_1Y")["value"] == pytest.approx(3.0, abs=0.01)
    assert cleve.fetch_first_ts("CLEVE:EXPINF_1Y") == "2026-07-01"  # MIN, not rows[0]


def test_missing_cell_in_latest_month_survives(patched):
    """An empty 1y cell in the newest month: the LATEST row simply doesn't
    qualify for that column — the parser falls back to the newest month
    that HAS a value (header-based lookup keeps the column findable)."""
    patched(
        [
            (datetime(2026, 9, 1), {"10 year Expected Inflation": 0.025}),  # 1y cell empty
            (
                datetime(2026, 8, 1),
                {"1 year Expected Inflation": 0.0239, "10 year Expected Inflation": 0.0249},
            ),
        ],
        [(datetime(2026, 9, 1), {"Real Rate 1-year": 0.021})],
    )
    assert cleve.fetch_latest("CLEVE:EXPINF_1Y") == {
        "ts": "2026-08-01",
        "value": pytest.approx(2.39, abs=0.01),
    }
    assert (
        cleve.fetch_latest("CLEVE:EXPINF_10Y")["ts"] == "2026-09-01"
    )  # fresh leg wins its own column


def test_tolerant_key_forms(patched):
    patched(
        [
            (
                datetime(2026, 8, 1),
                {
                    "1 year Expected Inflation": 0.0239,
                    "10 year Expected Inflation": 0.0249,
                    "11 year Expected Inflation": 0.025,
                },
            )
        ],
        [
            (
                datetime(2026, 8, 1),
                {
                    "Real Rate 1-year": 0.0214,
                    "Real Rate 10-year ": 0.022,
                    "Real Rate 1-month": 0.0289,
                },
            )
        ],
    )
    # bare suffix
    assert cleve.fetch_latest("EXPINF_1Y")["value"] == pytest.approx(2.39, abs=0.01)
    # descriptive mangled ref (the verify_sources primary_source form)
    assert cleve.fetch_latest("inflation-expectations.csv (XLSX) sheet ... EXPINF_1Y")[
        "value"
    ] == pytest.approx(2.39, abs=0.01)
    with pytest.raises(cleve.CleveError, match="unrouted"):
        cleve.fetch_latest("CLEVE:NOPE")


def test_ambiguous_column_raises_loud(patched):
    """Two columns starting with the fragment = loud failure, never a wrong pick."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.create_sheet("Expected Inflation")
    ws.append(["Model Output Date", "1 year Expected Inflation", "1 year Expected Inflation (alt)"])
    ws.append([datetime(2026, 8, 1), 0.0239, 0.05])
    ws2 = wb.create_sheet("Real Interest Rate")
    ws2.append(["Model Output Date", "Real Rate 1-year"])
    ws2.append([datetime(2026, 8, 1), 0.0214])
    buf = io.BytesIO()
    wb.save(buf)
    monkeypatch_val = type("R", (), {"status_code": 200, "content": buf.getvalue()})()
    import pytest as _pytest

    cleve._expectations_cache = None
    try:
        # patch requests via monkeypatch-like manual swap
        orig = cleve.requests.get
        cleve.requests.get = lambda *a, **k: monkeypatch_val
        with _pytest.raises(cleve.CleveError, match="ambiguous"):
            cleve.fetch_latest("CLEVE:EXPINF_1Y")
    finally:
        cleve.requests.get = orig
        cleve._expectations_cache = None


def test_premia_percent_native_and_routing(patched):
    """The Ten-year sheet is PERCENT-NATIVE (0.4436 stays 0.4436 — the other
    sheets' ×100 must NOT apply), fetch_first_ts routes it, and 'Inflation
    Risk Premium'/'Real Risk Premium' match exactly one column each."""
    patched(
        [
            (
                datetime(2026, 8, 1),
                {
                    "1 year Expected Inflation": 0.0239,
                    "10 year Expected Inflation": 0.0249,
                    "11 year Expected Inflation": 0.025,
                },
            )
        ],
        [
            (
                datetime(2026, 8, 1),
                {
                    "Real Rate 1-year": 0.0214,
                    "Real Rate 10-year ": 0.022,
                    "Real Rate 1-month": 0.0289,
                },
            )
        ],
        premia_rows=[
            (
                datetime(2026, 7, 1),
                {
                    "10 year Expected Inflation": 2.50,
                    "Real Risk Premium": 1.30,
                    "Inflation Risk Premium": 0.40,
                },
            ),
            (
                datetime(2026, 8, 1),
                {
                    "10 year Expected Inflation": 2.49,
                    "Real Risk Premium": 1.338,
                    "Inflation Risk Premium": 0.4436,
                },
            ),
        ],
    )
    assert cleve.fetch_latest("CLEVE:IRP_10Y_MODEL") == {"ts": "2026-08-01", "value": 0.4436}
    assert cleve.fetch_latest("CLEVE:RRP_10Y_MODEL") == {"ts": "2026-08-01", "value": 1.338}
    # depth routing (the twice-shipped bug class: RECPROB, then premia)
    assert cleve.fetch_first_ts("CLEVE:IRP_10Y_MODEL") == "2026-07-01"
    assert cleve.fetch_first_ts("CLEVE:RRP_10Y_MODEL") == "2026-07-01"


def test_premia_absent_sheet_raises_loud(patched):
    """Workbook without the premia sheet: the 4 base series still serve;
    premia raise (never a silent wrong number)."""
    patched(
        [(datetime(2026, 8, 1), {"1 year Expected Inflation": 0.0239})],
        [(datetime(2026, 8, 1), {"Real Rate 1-year": 0.0214})],
    )
    assert cleve.fetch_latest("CLEVE:EXPINF_1Y")["value"] == pytest.approx(2.39, abs=0.01)
    with pytest.raises(cleve.CleveError):
        cleve.fetch_latest("CLEVE:IRP_10Y_MODEL")
    with pytest.raises(cleve.CleveError):
        cleve.fetch_first_ts("CLEVE:IRP_10Y_MODEL")


def test_nowcast_dispatch_untouched(monkeypatch):
    """CLEVE:NOWCAST still routes to the FusionCharts JSON path (a mocked
    empty-JSON response proves the dispatch went there, not to the workbook)."""

    class R:
        status_code = 200

        def json(self):
            return []

    monkeypatch.setattr(cleve.requests, "get", lambda *a, **k: R())
    with pytest.raises(cleve.CleveError, match="empty response"):
        cleve.fetch_latest("CLEVE:NOWCAST")
