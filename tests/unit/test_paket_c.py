"""Tests for paket C: SPF/anxious fetchers + recprob CSV + recession signal."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from openpyxl import Workbook

from arkwatch import db
from arkwatch.fetchers import cleve, philly
from arkwatch.signals.recession import (
    recession_brief_line,
    recession_snapshot,
    store_recession_signals,
)


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


# --- philly SPF parser --------------------------------------------------------


def _growth_workbook(rows_rgdp, rows_pgdp):
    wb = Workbook()
    for sheet, rows in (("RGDP", rows_rgdp), ("PGDP", rows_pgdp)):
        ws = wb.create_sheet(sheet)
        ws.append(["YEAR", "QUARTER", f"d{sheet.lower()}2", f"d{sheet.lower()}3"])
        for row in rows:
            ws.append(row)
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]
    return wb


@pytest.fixture()
def growth_patched(monkeypatch):
    def inject(rows_rgdp, rows_pgdp):
        wb = _growth_workbook(rows_rgdp, rows_pgdp)
        buf = io.BytesIO()
        wb.save(buf)

        class R:
            status_code = 200
            content = buf.getvalue()

        monkeypatch.setattr(philly, "_download", lambda url: R().content)
        philly._spf_cache = None

    yield inject
    philly._spf_cache = None


def test_spf_nowcast_column_and_quarter_ts(growth_patched):
    growth_patched(
        [(2026.0, 2.0, 2.10, 2.20), (2026.0, 3.0, 2.4624, 2.27)],
        [(2026.0, 3.0, 2.6871, 2.60)],
    )
    assert philly.fetch_latest("PHILLY:SPF_RGDP_NOW") == {"ts": "2026-07-01", "value": 2.4624}
    assert philly.fetch_latest("PHILLY:SPF_PGDP_NOW") == {"ts": "2026-07-01", "value": 2.6871}
    # quarter mapping: Q3 -> 07-01
    with pytest.raises(philly.PhillyError, match="bad quarter"):
        philly._quarter_start(2026, 5)


def test_spf_max_date_not_last_row(growth_patched):
    """Unsorted workbook: the max quarter wins, not rows[-1] (CBOE lesson)."""
    growth_patched(
        [(2026.0, 3.0, 2.4624, None), (2025.0, 4.0, 2.90, 2.80)],
        [(2026.0, 3.0, 2.69, None)],
    )
    assert philly.fetch_latest("PHILLY:SPF_RGDP_NOW")["ts"] == "2026-07-01"


def test_spf_unrouted_raises(growth_patched):
    growth_patched([(2026.0, 3.0, 2.46, 2.27)], [(2026.0, 3.0, 2.69, 2.60)])
    with pytest.raises(philly.PhillyError, match="unrouted"):
        philly.fetch_latest("PHILLY:SPF_CPI_NOW")


def test_anxious_skips_prefilled_grid(monkeypatch):
    """The anxious sheet pre-fills future quarters with EMPTY values — those
    must be skipped (they are not data), and the header sits at row index 3."""
    wb = Workbook()
    ws = wb.create_sheet("Data")
    ws.append([None, None, None, None])
    ws.append([None, None, None, None])
    ws.append([None, None, None, None])
    ws.append(["Obs Year", "Obs Quarter", "Anxious Index", "RECESS"])
    ws.append([1968, 1, None, None])
    ws.append([2026, 3, 17.5, 0])
    ws.append([2026, 4, 19.9667, 0])
    ws.append([2027, 1, None, None])  # pre-filled grid future
    buf = io.BytesIO()
    wb.save(buf)

    class R:
        status_code = 200
        content = buf.getvalue()

    monkeypatch.setattr(philly, "_download", lambda url: R().content)
    assert philly.fetch_latest("PHILLY:ANXIOUS") == {"ts": "2026-10-01", "value": 19.9667}


# --- cleve recprob CSV parser ----------------------------------------------------


@pytest.fixture()
def recprob_patched(monkeypatch):
    def inject(csv_text):
        class R:
            status_code = 200
            text = csv_text

        monkeypatch.setattr(cleve.requests, "get", lambda *a, **k: R())
        cleve._recprob_cache = None

    yield inject
    cleve._recprob_cache = None


def test_recprob_two_legs_and_f12_vintage_pairing(recprob_patched):
    recprob_patched(
        "date,recession_probability,recession_probability_forecast,recession\n"
        "6/1/2026,22.1,,0\n"
        "7/1/2026,23.5,,0\n"
        "8/1/2026,24.4423,,0\n"
        "8/1/2027,,12.2665,0\n"
    )
    assert cleve.fetch_latest("CLEVE:RECPROB") == {"ts": "2026-08-01", "value": 24.4423}
    # F12 re-anchored to the MODEL month (2026-08), value = the +12m row
    assert cleve.fetch_latest("CLEVE:RECPROB_F12") == {"ts": "2026-08-01", "value": 12.2665}
    # depth routing (the P1 fix): fetch_first_ts resolves both families
    assert cleve.fetch_first_ts("CLEVE:RECPROB") == "2026-06-01"  # min EST row
    assert cleve.fetch_first_ts("CLEVE:RECPROB_F12") == "2026-08-01"


def test_recprob_f12_never_borrows_neighbor_month(recprob_patched):
    """REGRESSION (review ronde-1): est 8/2026 with forecasts at +11m and
    +13m but NO +12m row — the old ±31d fallback silently bound the +11m
    value; the calendar-month pairing must SKIP the unpairable month."""
    recprob_patched(
        "date,recession_probability,recession_probability_forecast,recession\n"
        "8/1/2026,24.4,,0\n"
        "7/1/2027,,11.0,0\n"
        "9/1/2027,,13.0,0\n"
    )
    with pytest.raises(cleve.CleveError, match="F12 pairing empty"):
        cleve.fetch_latest("CLEVE:RECPROB_F12")


def test_recprob_skips_malformed_rows(recprob_patched):
    recprob_patched(
        "date,recession_probability,recession_probability_forecast,recession\n"
        "garbage,1,1,0\n"
        "8/1/2026,24.4,,0\n"
        ",,,\n"
    )
    assert cleve.fetch_latest("CLEVE:RECPROB")["value"] == 24.4


# --- recession triangulation signal ------------------------------------------------


def _seed(conn, sid, ts, value):
    conn.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit, "
        "value_format, freq, primary_source) VALUES (?,?,'D',0,'?','{:,.1f}','M',?)",
        (sid, sid, sid),
    )
    conn.execute(
        "INSERT OR REPLACE INTO raw_observations(series_id, ts, value, vintage_ts, "
        "source, fetched_at) VALUES (?,?,?,?, 'test', ?)",
        (sid, ts, value, "realtime", ts),
    )
    conn.commit()


def _q(n=0):
    d = date(datetime.now(UTC).year, datetime.now(UTC).month, 1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
    return d.isoformat()


def test_recession_line_and_state_buckets(conn):
    _seed(conn, "CLEVE:RECPROB", _q(), 24.4)
    _seed(conn, "PHILLY:ANXIOUS", _q(), 19.97)
    _seed(conn, "FRED:SAHMREALTIME", _q(), 0.42)
    line = recession_brief_line(conn)
    assert line == "Recession: model 12m 24% · anxious next-Q 20% · Sahm 0.42"
    store_recession_signals(conn)
    ts, value, state = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='recession_triangulation'"
    ).fetchone()
    assert value == 24.4
    assert state == "QUIET"  # none crosses its elevation bar (30/30/0.50)


def test_recession_all_three_elevated(conn):
    _seed(conn, "CLEVE:RECPROB", _q(), 44.0)
    _seed(conn, "PHILLY:ANXIOUS", _q(), 55.0)
    _seed(conn, "FRED:SAHMREALTIME", _q(), 0.61)
    store_recession_signals(conn)
    assert (
        conn.execute(
            "SELECT state FROM computed_signals WHERE signal_id='recession_triangulation'"
        ).fetchone()[0]
        == "ELEVATED_x3"
    )
    assert "Sahm 0.61 ⚠TRIG" in recession_brief_line(conn)


def test_recession_stale_legs_vanish(conn):
    _seed(conn, "CLEVE:RECPROB", _q(4), 24.4)  # 4 months old > M window
    snap = recession_snapshot(conn)
    assert snap["model_pct"] is None
    assert recession_brief_line(conn) is None


def test_recession_anxious_future_ts_aged_effectively(conn):
    """REGRESSION (review ronde-1): ANXIOUS rows are dated by the TARGET
    quarter (up to ~1 quarter in the FUTURE) — freshness must be measured
    against the effective survey date (target − 1Q), never the raw ts."""
    base = date(datetime.now(UTC).year, datetime.now(UTC).month, 1)
    near = (base + timedelta(days=60)).replace(day=1).isoformat()  # survey ≈ now
    _seed(conn, "PHILLY:ANXIOUS", near, 20.0)
    snap = recession_snapshot(conn)
    # fresh today (survey current), even though the ts is ~2 months in the
    # future — the effective date (ts − 1Q) is what the gate consumes
    assert snap["anxious_pct"] == 20.0
    # a survey 5 months dead: its row's effective date is ~2 months in the
    # PAST, beyond the Q window → the leg vanishes (raw-ts aging would have
    # kept a future-dated frozen row 'fresh' for months longer)
    conn.execute("DELETE FROM raw_observations WHERE series_id='PHILLY:ANXIOUS'")
    dead_survey = (base - timedelta(days=150) + timedelta(days=92)).replace(day=1).isoformat()
    _seed(conn, "PHILLY:ANXIOUS", dead_survey, 25.0)
    assert recession_snapshot(conn)["anxious_pct"] is None
    # stored ts uses EFFECTIVE dates — never a future date
    _seed(conn, "PHILLY:ANXIOUS", near, 20.0)  # back to the fresh row
    store_recession_signals(conn)
    ts = conn.execute(
        "SELECT ts FROM computed_signals WHERE signal_id='recession_triangulation'"
    ).fetchone()[0]
    assert ts <= datetime.now(UTC).date().isoformat()


def test_recession_sahm_publication_lag_window(conn):
    """Sahm publishes first-Friday M+1 — a 2-month-old print is NORMAL
    cadence, not stale (the old generic M window suppressed it most days)."""
    _seed(conn, "FRED:SAHMREALTIME", _q(2), 0.42)
    snap = recession_snapshot(conn)
    assert snap["sahm"] == 0.42
    assert "Sahm 0.42" in recession_brief_line(conn)


def test_recession_degrades_on_empty(conn):
    assert recession_brief_line(conn) is None
    assert store_recession_signals(conn) == 0
