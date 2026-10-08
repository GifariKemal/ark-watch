"""Tests for the flows-extra expansion (audit sumber 2026-09-10).

Covers the Farside window/per-issuer parser (D-021: the table renders
OLDEST-first, so DOM position ≠ latest), the SAFE month-pairing fix (tonnage
sits duplicated across USD/SDR column pairs — the unlabelled SDR side
previously fell back to now() and mislabeled the datum to the RUN month),
LBMA gold + full history, TIC multi-row, CNN components/momentum/history,
and the fetch_log staleness gates that make a silent freeze visible.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.fetchers import cnn, flows_extra


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


def _day(n: int = 0) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


# --------------------------------------------------------------------------
# Farside parser — fixture HTML mirrors the live shape (2026-09-10):
#   thead: nbsp-padded 'Total', blanks, issuer names, 'Fee'
#   tbody: <tr><td><span class="tabletext">DD MMM YYYY</span></td> + cells
#   negatives = parenthesized inside redFont spans
# --------------------------------------------------------------------------

_BTC_ISSUERS = [
    "IBIT",
    "FBTC",
    "BITB",
    "ARKB",
    "BTCO",
    "EZBC",
    "BRRR",
    "HODL",
    "BTCW",
    "MSBT",
    "GBTC",
    "BTC",
]


def _farside_html(rows: list[tuple[str, list[str]]], footer: list[str] | None = None) -> str:
    th = "".join(f"<th>{'&nbsp;' * 12}{n}</th>" for n in ["Total", ""] + _BTC_ISSUERS)
    th += "<th>Fee</th>"
    trs = []
    for date, cells in rows:
        tds = ""
        for v in cells:
            if v.startswith("("):
                tds += f'<td><span class="redFont">{v}</span></td>'
            else:
                tds += f'<td><span class="tabletext">{v}</span></td>'
        trs.append(f'<tr>\n<td><span class="tabletext">{date}</span></td>{tds}\n</tr>')
    foot = ""
    if footer is not None:
        ftds = "".join(
            f'<td><span class="{"redFont" if v.startswith("(") else "tabletext"}">{v}</span></td>'
            for v in footer
        )
        foot = f'<tr><td><span class="tabletext">Total</span>{ftds}</tr>'
    return f"<html><table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}{foot}</tbody></table></html>"


def _mock_farside(monkeypatch, html: str):
    class R:
        status_code = 200
        text = html

    monkeypatch.setattr(flows_extra.requests, "get", lambda *a, **k: R())


def test_farside_latest_by_parsed_date_not_dom(monkeypatch):
    """D-021 REGRESSION: rows render OLDEST-first; trs[0] was 19 days stale."""
    _mock_farside(
        monkeypatch,
        _farside_html(
            [
                ("24 Aug 2026", ["1"] * 12 + ["337.6"]),
                ("08 Sep 2026", ["10.7", "(17.1)"] + ["0.0"] * 8 + ["(65.5)", "0.0", "(46.6)"]),
            ]
        ),
    )
    r = flows_extra.fetch_farside_btc()
    assert r["latest"]["date_iso"] == "2026-09-08"
    assert r["latest"]["net_flow_musd"] == -46.6
    assert len(r["rows"]) == 2


def test_farside_issuer_mapping_and_signs(monkeypatch):
    _mock_farside(
        monkeypatch,
        _farside_html(
            [("08 Sep 2026", ["10.7", "(17.1)"] + ["0.0"] * 8 + ["(65.5)", "0.0", "(46.6)"])]
        ),
    )
    r = flows_extra.fetch_farside_btc()
    iss = r["latest"]["issuers"]
    assert iss["IBIT"] == 10.7
    assert iss["FBTC"] == -17.1  # parenthesized redFont = negative (F-K6)
    assert iss["GBTC"] == -65.5
    assert set(iss) == set(_BTC_ISSUERS)


def test_farside_placeholder_and_partial_rows_skipped(monkeypatch):
    """A bare '0.0' single cell = intraday skeleton; a 3-cell row is partial.
    Neither must land (a fake zero-flow day or a misaligned mapping)."""
    _mock_farside(
        monkeypatch,
        _farside_html(
            [
                ("09 Sep 2026", ["0.0"]),  # placeholder
                ("08 Sep 2026", ["1.0", "2.0", "3.0"]),  # partial (< issuer count)
                ("07 Sep 2026", ["5.0"] * 12 + ["60.0"]),  # complete
            ]
        ),
    )
    r = flows_extra.fetch_farside_btc()
    assert [x["date_iso"] for x in r["rows"]] == ["2026-09-07"]


def test_farside_issuer_only_row_has_no_total(monkeypatch):
    """A 12-cell row (all issuers, Total cell not yet rendered) must NOT
    guess the aggregate — the last cell is the BTC-mini issuer, not Total."""
    _mock_farside(
        monkeypatch,
        _farside_html(
            [("09 Sep 2026", ["0.0", "0.0", "(78.0)"] + ["0.0"] * 6 + ["(27.2)", "0.0", "(100.7)"])]
        ),
    )
    r = flows_extra.fetch_farside_btc()
    assert r["latest"]["net_flow_musd"] is None
    assert len(r["latest"]["issuers"]) == 12
    assert r["latest"]["issuers"]["BTC"] == -100.7  # issuer value, kept as issuer


def test_farside_cumulative_footer(monkeypatch):
    _mock_farside(
        monkeypatch,
        _farside_html(
            [("08 Sep 2026", ["10.7"] * 11 + ["0.0", "(46.6)"])],
            footer=["64,067", "10,325"] + ["1,000"] * 8 + ["(27,746)", "2,924", "55,539"],
        ),
    )
    r = flows_extra.fetch_farside_btc()
    assert r["cumulative"]["IBIT"] == 64067
    assert r["cumulative"]["GBTC"] == -27746
    assert r["cumulative"]["Total"] == 55539


def test_farside_invalid_date_rejected(monkeypatch):
    """A non-calendar date must not poison the max-date pick."""
    _mock_farside(
        monkeypatch,
        _farside_html(
            [
                ("99 Xxx 2026", ["1"] * 12 + ["9.9"]),
                ("08 Sep 2026", ["1"] * 12 + ["46.6"]),
            ]
        ),
    )
    r = flows_extra.fetch_farside_btc()
    assert r["latest"]["date_iso"] == "2026-09-08"


# --------------------------------------------------------------------------
# Farside landing (f2): window upsert self-heal + per-issuer + fetch_log
# --------------------------------------------------------------------------


def _window(start_days_ago: int, n_days: int):
    rows = []
    for i in range(n_days):
        d = datetime.now(UTC).date() - timedelta(days=start_days_ago - i)
        rows.append(
            {
                "ts": d.strftime("%d %b %Y"),
                "date_iso": d.isoformat(),
                "net_flow_musd": 100.0 + i,
                "issuers": {"IBIT": 50.0 + i, "GBTC": -(50.0 + i)},
            }
        )
    return rows


def _harvest_farside_only(conn, monkeypatch, window):
    from arkwatch.qa import f2_harvest

    pkg = {
        "rows": window,
        "issuers": ["IBIT", "GBTC"],
        "latest": window[-1],
        "cumulative": {"IBIT": 64067.0, "GBTC": -27746.0, "Total": 55539.0},
    }
    monkeypatch.setattr(flows_extra, "fetch_farside_btc", lambda: pkg)
    monkeypatch.setattr(flows_extra, "fetch_farside_eth", lambda: pkg)

    def dead(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(flows_extra, "fetch_pboc_gold", dead)
    monkeypatch.setattr(flows_extra, "fetch_lbma_vault", dead)
    monkeypatch.setattr(flows_extra, "fetch_tic_slt5", dead)
    f2_harvest._harvest_flows_extra(conn)


def test_farside_landing_window_selfheal_and_issuers(conn, monkeypatch):
    # day 1 lands a 12-row window; day 2's window overlaps + extends
    _harvest_farside_only(conn, monkeypatch, _window(12, 12))
    _harvest_farside_only(conn, monkeypatch, _window(11, 12))
    dates = [r[0] for r in conn.execute("SELECT date FROM flows_daily ORDER BY date")]
    assert len(dates) == 13  # union of both windows: gaps self-heal
    n_iss = conn.execute("SELECT COUNT(*) FROM etf_flows_issuer").fetchone()[0]
    # 13 dates x 2 issuers x 2 etfs
    assert n_iss == 13 * 2 * 2
    cum = conn.execute("SELECT value FROM flows_periodic WHERE kind='farside_cum_btc'").fetchone()
    assert cum[0] == 55539.0
    ok = conn.execute(
        "SELECT status FROM fetch_log WHERE target='FARSIDE:BTC' ORDER BY id DESC"
    ).fetchone()
    assert ok[0] == "OK"


def test_farside_stale_gate_fires(conn, monkeypatch):
    """A frozen page (latest > 3 business days old) must log ERROR, not OK."""
    _harvest_farside_only(conn, monkeypatch, _window(10, 2))
    row = conn.execute(
        "SELECT status, error FROM fetch_log WHERE target='FARSIDE:BTC' ORDER BY id DESC"
    ).fetchone()
    assert row[0] == "ERROR"
    assert "stale" in row[1]


def test_farside_fetch_failure_logged(conn, monkeypatch):
    from arkwatch.qa import f2_harvest

    def dead(*a, **k):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(flows_extra, "fetch_farside_btc", dead)
    monkeypatch.setattr(flows_extra, "fetch_farside_eth", dead)
    monkeypatch.setattr(flows_extra, "fetch_pboc_gold", dead)
    monkeypatch.setattr(flows_extra, "fetch_lbma_vault", dead)
    monkeypatch.setattr(flows_extra, "fetch_tic_slt5", dead)
    f2_harvest._harvest_flows_extra(conn)  # must not raise
    rows = conn.execute(
        "SELECT target, status FROM fetch_log WHERE target LIKE 'FARSIDE:%'"
    ).fetchall()
    assert len(rows) == 2
    assert all(s == "ERROR" for _t, s in rows)


# --------------------------------------------------------------------------
# TIC
# --------------------------------------------------------------------------


def test_tic_multirow(monkeypatch):
    html = """
    <table>
    <tr><th>2026-06</th><th>2026-05</th><th>2026-04</th><th>2026-03</th><th>2026-02</th></tr>
    <tr><td>Grand Total</td><td>9,299.0</td></tr>
    <tr><td>Japan</td><td>1,116.7</td></tr>
    <tr><td>China, Mainland</td><td>633.4</td></tr>
    <tr><td>Belgium</td><td>482.5</td></tr>
    <tr><td>Cayman Islands</td><td>453.1</td></tr>
    <tr><td>Of Which: Foreign Official Treasury Bills</td><td>360.6</td></tr>
    </table>
    """
    calls = []

    class S:
        def get(self, url, **k):
            calls.append(url)

            class R:
                status_code = 200
                text = html

            return R()

    monkeypatch.setattr(flows_extra.creq, "Session", lambda **k: S())
    r = flows_extra.fetch_tic_slt5()
    assert calls[0].startswith("https://ticdata.treasury.gov/")
    assert r["ts"] == "2026-06"
    assert r["values"]["china"] == 633.4
    assert r["values"]["belgium"] == 482.5
    assert r["values"]["japan"] == 1116.7
    assert r["values"]["grand_total"] == 9299.0
    assert r["values"]["official_bills"] == 360.6


def test_tic_china_required(monkeypatch):
    class S:
        def get(self, url, **k):
            class R:
                status_code = 200
                text = "<table><tr><td>Japan</td><td>1.0</td></tr></table>"

            return R()

    monkeypatch.setattr(flows_extra.creq, "Session", lambda **k: S())
    with pytest.raises(RuntimeError, match="China"):
        flows_extra.fetch_tic_slt5()


# --------------------------------------------------------------------------
# SAFE — a real in-memory XLSX mirroring the live Sheet1 layout
# --------------------------------------------------------------------------


def _safe_xlsx(months: dict[str, list[str]]) -> bytes:
    """months: {'2026.08': [fx, gold_val, total], ...} — USD values only."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["官方储备资产"])
    ws.append(["  Official reserve assets"])
    ws.append([])
    # row 3 (0-based 3): date headers at odd cols
    hdr: list = ["项目  Item"]
    for m in sorted(months):
        hdr += [m, ""]
    ws.append(hdr)
    units: list = [""]
    for _m in months:
        units += ["亿美元", "亿SDR"]
    ws.append(units)
    fx: list = ["1.  外汇储备"]
    gold: list = ["4.  黄金"]
    ton: list = [""]
    tot: list = ["合计"]
    for m in sorted(months):
        vals = months[m]
        fx += [vals[0], ""]
        gold += [vals[1], ""]
        ton += [f"{vals[3]}万盎司", f"{vals[3]}万盎司"]  # duplicated USD/SDR pair
        tot += [vals[2], ""]
    for row in (fx, ["Foreign currency reserves"], gold, ["Gold"], ton, [], tot, ["Total"]):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _mock_safe(monkeypatch, xlsx: bytes):
    class R:
        status_code = 200
        text = '<a href="/en/file/file/20260907/x.xlsx">data</a>'

    class R2:
        status_code = 200
        content = xlsx

    class S:
        def get(self, url, **k):
            return R2() if url.endswith(".xlsx") else R()

    monkeypatch.setattr(flows_extra.creq, "Session", lambda **k: S())


def test_safe_month_pairing_sdr_column(monkeypatch):
    """D-021 REGRESSION: the tonnage cell duplicated onto the SDR side of the
    Aug pair must still bind to 2026-08 — never the RUN month."""
    _mock_safe(
        monkeypatch,
        _safe_xlsx(
            {
                "2026.07": ["34187.76", "3063.54", "37916.02", "7608"],
                "2026.08": ["34383.25", "3500.80", "38548.85", "7673"],
            }
        ),
    )
    r = flows_extra.fetch_pboc_gold()
    assert r["ts"] == "2026-08"  # NOT the run month
    assert r["wan_oz"] == 7673
    assert [m["ts"] for m in r["months"]] == ["2026-07", "2026-08"]
    assert r["months"][-1]["tonnes"] == pytest.approx(7673 * 10000 / 32150.7466, abs=0.1)


def test_safe_reserve_composition(monkeypatch):
    _mock_safe(
        monkeypatch,
        _safe_xlsx({"2026.08": ["34383.25", "3500.80", "38548.85", "7673"]}),
    )
    r = flows_extra.fetch_pboc_gold()
    assert r["gold_share_pct"] == pytest.approx(3500.80 / 38548.85 * 100, abs=0.01)
    assert r["gold_value_usd_yi"] == 3500.80
    assert r["total_reserves_usd_yi"] == 38548.85
    assert r["fx_reserves_usd_yi"] == 34383.25


# --------------------------------------------------------------------------
# LBMA
# --------------------------------------------------------------------------


def _lbma_xlsx(rows: list) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "London Vault Holdings Data"
    ws.append(["London Vault Holdings Data"])
    ws.append(["Month End", "Gold", "Silver"])
    ws.append([None, "Troy Ounces ('000s)", "Troy Ounces ('000s)"])
    for label, g, s in rows:
        ws.append([label, g, s])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _mock_lbma(monkeypatch, xlsx: bytes, url="...August-2026.xlsx"):
    class R:
        status_code = 200
        text = f'<a href="https://cdn.lbma.org.uk/downloads/x{url}">x</a>'

    class R2:
        status_code = 200
        content = xlsx

    class S:
        def get(self, *a, **k):
            return R2() if str(a[0]).endswith(".xlsx") else R()

    monkeypatch.setattr(flows_extra.creq, "Session", lambda **k: S())


def test_lbma_gold_silver_full_history(monkeypatch):
    from datetime import date

    _mock_lbma(
        monkeypatch,
        _lbma_xlsx(
            [
                ("2026-08", 309681, 914082),  # string label era
                (date(2026, 7, 1), 306526.610788, 907058.9632960),  # datetime era
                ("2026-06", 304285, 902843),
            ]
        ),
    )
    r = flows_extra.fetch_lbma_vault()
    assert r["ts"] == "2026-08"
    assert len(r["months"]) == 3
    by_ts = {m["ts"]: m for m in r["months"]}
    assert by_ts["2026-08"]["gold_tonnes"] == pytest.approx(309681 * 31.1034768 / 1000, abs=0.1)
    assert by_ts["2026-07"]["silver_tonnes"] == pytest.approx(907058.96 * 0.0311034768, abs=0.1)
    # legacy keys kept for f2 compat
    assert r["koz"] == 914082 and r["tonnes"] == by_ts["2026-08"]["silver_tonnes"]


def test_lbma_fallback_url_uses_downloads_pattern(monkeypatch):
    """The old media/lbv-*.xlsx fallback is a guaranteed 403 since the CDN
    restructure; the fallback must mirror the /downloads/ pattern."""
    seen = []

    class R:
        status_code = 500
        text = ""

    class R2:
        status_code = 200
        content = _lbma_xlsx([("2026-08", 309681, 914082)])

    class S:
        def get(self, url, *a, **k):
            seen.append(url)
            return R2() if "downloads" in url else R()

    monkeypatch.setattr(flows_extra.creq, "Session", lambda **k: S())
    r = flows_extra.fetch_lbma_vault()
    assert any("/downloads/LBMA-London-Vault-Holdings-Data-" in u for u in seen)
    assert r["ts"] == "2026-08"


# --------------------------------------------------------------------------
# CNN
# --------------------------------------------------------------------------


def _cnn_payload(score=39.0, ts=None):
    # TIME-BOMB FIX (ronde-5): a fixed 2026-09-09 date trips the 4-day
    # stale gate once the wall clock moves on — build from today instead
    ts = ts or (datetime.now(UTC).date().isoformat() + "T23:59:55+00:00")
    return {
        "fear_and_greed": {
            "score": score,
            "rating": "fear",
            "timestamp": ts,
            "previous_close": 40.6,
            "previous_1_week": 33.0,
            "previous_1_month": 64.4,
            "previous_1_year": 57.9,
        },
        "fear_and_greed_historical": {
            "data": [
                {"x": 1757462400000.0, "y": 57.9, "rating": "greed"},  # 2025-09-09
                {"x": 1788998395000.0, "y": score, "rating": "fear"},  # 2026-09-09
            ]
        },
        "stock_price_strength": {
            "score": 10,
            "rating": "extreme fear",
            "timestamp": 1.0,
            "data": [{"x": 1788998395000.0, "y": 3.58, "rating": "x"}],
        },
        "junk_bond_demand": {
            "score": 74.8,
            "rating": "greed",
            "timestamp": 1.0,
            "data": [{"x": 1788998395000.0, "y": 1.27, "rating": "x"}],
        },
        "put_call_options": {
            "score": 61.2,
            "rating": "greed",
            "timestamp": 1.0,
            "data": [
                {"x": 1757462400000.0, "y": 0.6607, "rating": "x"},
                {"x": 1788998395000.0, "y": 0.7029, "rating": "x"},
            ],
        },
        "market_volatility_vix": {
            "score": 50,
            "rating": "neutral",
            "timestamp": 1.0,
            "data": [{"x": 1788998395000.0, "y": 16.2, "rating": "x"}],
        },
    }


def _mock_cnn(monkeypatch, payload):
    class R:
        status_code = 200

        def json(self):
            return payload

    monkeypatch.setattr(cnn.requests, "get", lambda *a, **k: R())


def test_cnn_ts_from_timestamp_key(monkeypatch):
    """Dead-key regression: 'report_date'/'updated_at' never existed — the
    report date lives at fd['timestamp']."""
    _mock_cnn(monkeypatch, _cnn_payload())
    fg = cnn.fetch_fear_greed()
    assert fg["ts"] == datetime.now(UTC).date().isoformat()
    assert fg["score"] == 39.0
    assert fg["prev_1m"] == 64.4


def test_cnn_components_and_raw(monkeypatch):
    _mock_cnn(monkeypatch, _cnn_payload())
    fg = cnn.fetch_fear_greed()
    assert fg["components"]["stock_price_strength"] == {
        "score": 10,
        "rating": "extreme fear",
        "raw": 3.58,
    }
    assert fg["components"]["junk_bond_demand"]["score"] == 74.8
    # raw P/C + VIX histories converted from ms epochs
    assert {"ts": "2026-09-09", "value": 0.7029} in fg["history"]["cnn_put_call_options"]
    assert fg["history"]["cnn_market_volatility_vix"][0]["value"] == 16.2
    assert fg["history"]["cnn_fg"][0]["ts"] == "2025-09-10"  # 1757462400000ms UTC


def test_cnn_landing_report_date_components_history(conn, monkeypatch):
    from arkwatch.qa.f2_harvest import _harvest_cnn_fg

    _mock_cnn(monkeypatch, _cnn_payload())
    monkeypatch.setattr(
        "arkwatch.fetchers.cnn.fetch_fear_greed", cnn.fetch_fear_greed
    )  # f2 imports the symbol lazily; patch at the source module
    _harvest_cnn_fg(conn)
    sc, meta = conn.execute(
        "SELECT value, meta_json FROM flows_periodic WHERE kind='cnn_fg' AND period=?",
        (datetime.now(UTC).date().isoformat(),),
    ).fetchone()
    assert sc == 39.0
    assert json.loads(meta)["prev_1m"] == 64.4
    n_comp = conn.execute(
        "SELECT COUNT(*) FROM flows_periodic WHERE kind LIKE 'cnn_comp_%'"
    ).fetchone()[0]
    assert n_comp == 4  # strength, junk, put/call, vix
    pc = conn.execute(
        "SELECT COUNT(*) FROM flows_periodic WHERE kind='cnn_put_call_options'"
    ).fetchone()[0]
    assert pc == 2  # both history points landed
    status = conn.execute("SELECT status FROM fetch_log WHERE target='CNN:FG'").fetchone()[0]
    assert status == "OK"


def test_cnn_stale_report_gate(conn, monkeypatch):
    from arkwatch.qa.f2_harvest import _harvest_cnn_fg

    _mock_cnn(monkeypatch, _cnn_payload(ts=f"{_day(10)}T23:59:55+00:00"))
    monkeypatch.setattr("arkwatch.fetchers.cnn.fetch_fear_greed", cnn.fetch_fear_greed)
    _harvest_cnn_fg(conn)
    status, err = conn.execute(
        "SELECT status, error FROM fetch_log WHERE target='CNN:FG'"
    ).fetchone()
    assert status == "ERROR"
    assert "stale" in err


# --------------------------------------------------------------------------
# Brief integration
# --------------------------------------------------------------------------


def _brief(conn, tmp_path):
    from arkwatch.signals.compute import generate_brief

    return generate_brief(conn, str(tmp_path / "brief.db"))


def test_brief_etf_divergence_and_stale(conn, tmp_path):
    conn.execute(
        "INSERT INTO flows_daily(date, funding_bps, btc_etf_musd, eth_etf_musd) VALUES (?,?,?,?)",
        (_day(1), 1.5, -46.6, 12.0),
    )
    conn.executemany(
        "INSERT INTO etf_flows_issuer(date, etf, issuer, flow_musd) VALUES (?,?,?,?)",
        [
            (_day(1), "BTC", "GBTC", -65.5),
            (_day(1), "BTC", "IBIT", 10.7),
            (_day(1), "ETH", "ETHE", -5.0),
            (_day(1), "ETH", "ETHA", 17.0),
        ],
    )
    conn.commit()
    text = _brief(conn, tmp_path)
    line = next(ln for ln in text.splitlines() if "ETF flows" in ln)
    assert "GBTC -66·IBIT +11" in line
    assert "ETHE -5·ETHA +17" in line
    assert "⚠stale" not in line  # 1 day old is fresh


def test_brief_etf_stale_flag(conn, tmp_path):
    conn.execute("INSERT INTO flows_daily(date, btc_etf_musd) VALUES (?,?)", (_day(9), -46.6))
    conn.commit()
    text = _brief(conn, tmp_path)
    line = next(ln for ln in text.splitlines() if "ETF flows" in ln)
    assert "⚠stale" in line


def test_brief_physical_and_tic_lines(conn, tmp_path):
    conn.execute("INSERT INTO flows_daily(date, funding_bps) VALUES (?,?)", (_day(1), 1.5))
    for period, kind, value in (
        ("2026-08", "pboc_gold", 2386.6),
        ("2026-08", "pboc_gold_share", 9.08),
        ("2026-08", "lbma_gold", 9632.9),
        ("2026-08", "lbma_silver", 28431.5),
        ("2026-06", "tic_china", 633.4),
        ("2026-06", "tic_belgium", 482.5),
        ("2026-06", "tic_grand_total", 9299.0),
    ):
        conn.execute(
            "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value)"
            " VALUES (?,?,?, 'u',1,?)",
            (period, kind, value, value),
        )
    conn.commit()
    text = _brief(conn, tmp_path)
    phys = next(ln for ln in text.splitlines() if "PBoC Au" in ln)
    assert "Au share 9.1%" in phys
    assert "LBMA Au 9,633t" in phys
    assert "LBMA Ag 28,432t" in phys
    tic = next(ln for ln in text.splitlines() if "TIC CN" in ln)
    assert "633B$+BE 482B$=1,116B$" in tic
    assert "foreign total 9,299B$" in tic
    assert "(26-06)" in tic


def test_brief_fg_momentum_and_split(conn, tmp_path):
    conn.execute("INSERT INTO flows_daily(date, funding_bps) VALUES (?,?)", (_day(1), 1.5))
    conn.execute(
        "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value,meta_json)"
        " VALUES ('2026-09-09','cnn_fg',39.0,'score',1,39.0,?)",
        (json.dumps({"prev_1m": 64.4}),),
    )
    for name, score in (
        ("stock_price_strength", 10.0),
        ("junk_bond_demand", 74.8),
        ("put_call_options", 61.2),
        ("market_volatility_vix", 50.0),
    ):
        conn.execute(
            "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value)"
            " VALUES ('2026-09-09',?,?,'score',1,?)",
            (f"cnn_comp_{name}", score, score),
        )
    conn.commit()
    text = _brief(conn, tmp_path)
    fg = next(ln for ln in text.splitlines() if "Fear&Greed:" in ln)
    assert "1m-ago 64" in fg
    split = next(ln for ln in text.splitlines() if "F&G split" in ln)
    assert "52w strength 10" in split
    assert "junk bonds 75" in split


def test_brief_no_split_when_aligned(conn, tmp_path):
    """Aligned components (spread < 40) must not print a split line."""
    conn.execute("INSERT INTO flows_daily(date, funding_bps) VALUES (?,?)", (_day(1), 1.5))
    conn.execute(
        "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value)"
        " VALUES ('2026-09-09','cnn_fg',50.0,'score',1,50.0)"
    )
    for name, score in (("stock_price_strength", 45.0), ("junk_bond_demand", 55.0)):
        conn.execute(
            "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value)"
            " VALUES ('2026-09-09',?,?,'score',1,?)",
            (f"cnn_comp_{name}", score, score),
        )
    conn.commit()
    text = _brief(conn, tmp_path)
    assert not any("F&G split" in ln for ln in text.splitlines())


# --------------------------------------------------------------------------
# LME off-warrant archive
# --------------------------------------------------------------------------


def test_lme_offwarrant_parse(monkeypatch):
    """Monthly archive: TOTAL {region} rows sum to the headline; the bare
    grand-TOTAL row and '/' (not-held) cells must not corrupt the math."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Off-Warrant Stock"])
    ws.append(["Region", "Location", "AA", "AL", "CU", "NA", "NI", "PB"])
    ws.append(["Asia", "Port Klang", 0, 201546, 445, "/", 79, 6838])
    ws.append(["Asia", "Singapore", 0, 4755, 6644, "/", 11361, 97313])
    ws.append(["TOTAL ASIA", "", 0, 249706, 7089, 0, 34776, 106768])
    ws.append(["TOTAL Europe", "", 0, 120719, 22996, 0, 28626, 1654])
    ws.append(["TOTAL", "", 0, 370425, 30085, 0, 63402, 108422])  # must be skipped
    buf = io.BytesIO()
    wb.save(buf)
    xlsx = buf.getvalue()

    class R:
        status_code = 200
        text = (
            '<a href="/-/media/files/data/reports-and-data/warehouse-and-stock-reports/'
            'off-warrant-stock-reporting/off-warrant-stock-reporting-february-2025.xlsx">f</a>'
            '<a href=".../off-warrant-stock-reporting-march-2025.xlsx">m</a>'  # not yet real
        )

    class R2:
        status_code = 200
        content = xlsx

    class R404:
        status_code = 200
        content = b"<html>not found</html>"  # soft-404 HTML body

    class S:
        def get(self, url, *a, **k):
            if url.endswith(".xlsx"):
                return R2() if "february" in url else R404()
            return R()

    monkeypatch.setattr(flows_extra.creq, "Session", lambda **k: S())
    rows = flows_extra.fetch_lme_offwarrant()
    assert len(rows) == 1  # march file is soft-404 HTML → skipped
    r = rows[0]
    assert r["ts"] == "2025-02"
    assert r["cu_tonnes"] == 7089 + 22996  # region totals only, grand row skipped
    assert r["regions"] == {"Asia": 7089.0, "Europe": 22996.0}


# --------------------------------------------------------------------------
# LME daily OWSR (T+3, cookie-gated)
# --------------------------------------------------------------------------


def _owsr_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "OWSR Reconciled Inventory"
    ws.append(["REGION", "COUNTRY/REGION", "DELIVERY POINT", "AA", "AL", "CU", "NA", "NI"])
    ws.append(["ASIA", "MALAYSIA", "PORT KLANG", 0.0, 38211.0, 767.0, 0.0, 3735.0])
    ws.append(["ASIA", "SINGAPORE", "SINGAPORE", 0.0, 2083.0, 277.0, 0.0, 41300.0])
    ws.append(["EUROPE", "BELGIUM", "ANTWERP", 0.0, 1000.0, 5000.0, 0.0, 0.0])
    ws.append(["GLOBAL TOTAL", "", "", 0.0, 94512.0, 125354.0, 0.0, 101154.0])
    ws.append(["© disclaimer text", "", "", "", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _mock_owsr(monkeypatch, set_cookie=True, listing=None, download_ok=True):
    class RJson:
        status_code = 200
        content = b"{}"

        def __init__(self):
            self._d = (
                listing
                if listing is not None
                else {
                    "Results": [
                        {
                            "ItemId": "id-1",
                            "Name": "Daily_OWSR 04 Sep 2026",
                            "FileExtension": "xlsx",
                            "FileSize": "0MB",
                        },
                        {
                            "ItemId": "id-2",
                            "Name": "Daily_OWSR 03 Sep 2026",
                            "FileExtension": "xlsx",
                            "FileSize": "0MB",
                        },
                    ]
                }
            )

        def json(self):
            return self._d

    class RHtml:
        status_code = 200
        text = "<html>login page</html>"
        content = b"<html>login page</html>"

        def json(self):
            raise ValueError("no json on an html page")

    class RXlsx:
        status_code = 200
        content = _owsr_xlsx()

    class S:
        def get(self, url, *a, **k):
            if "/Get" in url:
                return RHtml() if listing == "expired" else RJson()
            if "/Download" in url:
                return RXlsx() if download_ok else RHtml()
            raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(flows_extra.creq, "Session", lambda **k: S())
    monkeypatch.setenv("LME_COOKIE", "ASP.NET_SessionId=x; .AspNet.Cookies=y" if set_cookie else "")
    if not set_cookie:
        monkeypatch.delenv("LME_COOKIE", raising=False)


def test_owsr_daily_parse(monkeypatch):
    _mock_owsr(monkeypatch)
    rows = flows_extra.fetch_lme_owsr_daily()
    assert [r["ts"] for r in rows] == ["2026-09-04", "2026-09-03"]
    r = rows[0]
    assert r["cu_tonnes"] == 125354.0  # GLOBAL TOTAL row, not the sum
    assert r["regions"]["Asia"] == 767.0 + 277.0
    assert r["regions"]["Europe"] == 5000.0


def test_owsr_missing_cookie_named_error(monkeypatch):
    _mock_owsr(monkeypatch, set_cookie=False)
    with pytest.raises(flows_extra.LmeOwsrError, match="LME_COOKIE not set"):
        flows_extra.fetch_lme_owsr_daily()


def test_owsr_expired_cookie_named_error(monkeypatch):
    """A dead cookie answers the API with an HTML login page — the error
    must name the refresh fix, not a cryptic JSON decode failure."""
    _mock_owsr(monkeypatch, listing="expired")
    with pytest.raises(flows_extra.LmeOwsrError, match="cookie expired"):
        flows_extra.fetch_lme_owsr_daily()


def test_owsr_soft404_download_skipped(monkeypatch):
    _mock_owsr(monkeypatch, download_ok=False)
    assert flows_extra.fetch_lme_owsr_daily() == []


def _seed_cu(conn, today, drift_up=True):
    conn.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit,"
        " value_format, freq, primary_source) VALUES ('LME:CA_STOCKS','Cu','H',0,"
        "'tonne','{:,.0f}','D','LME')"
    )
    for i in range(25):
        v = 200000.0 + (i * 100 if drift_up else -i * 100)
        conn.execute(
            "INSERT INTO raw_observations(series_id, ts, value, vintage_ts, source, fetched_at)"
            " VALUES ('LME:CA_STOCKS', ?, ?, 'realtime', 't', ?)",
            ((today - timedelta(days=i)).isoformat(), v, today.isoformat()),
        )


def test_brief_cu_offwarrant_line(conn, tmp_path):
    today = datetime.now(UTC).date()
    _seed_cu(conn, today)
    conn.execute(
        "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value)"
        " VALUES (?,'lme_owsr_cu', 125354.0, 'tonne',1, 125354.0)",
        ((today - timedelta(days=3)).isoformat(),),
    )
    conn.commit()
    text = _brief(conn, tmp_path)
    line = next(ln for ln in text.splitlines() if "Cu physical" in ln)
    assert "off-warrant 125,354t" in line
    assert "% of LME)" in line


def test_brief_cu_offwarrant_hidden_when_stale(conn, tmp_path):
    """OWSR older than 7 days (cookie dead / retention gap) must not print a
    stale shadow-supply % next to a live stocks number."""
    today = datetime.now(UTC).date()
    _seed_cu(conn, today)
    conn.execute(
        "INSERT INTO flows_periodic(period,kind,value_raw,unit_raw,factor,value)"
        " VALUES (?,'lme_owsr_cu', 125354.0, 'tonne',1, 125354.0)",
        ((today - timedelta(days=20)).isoformat(),),
    )
    conn.commit()
    text = _brief(conn, tmp_path)
    line = next(ln for ln in text.splitlines() if "Cu physical" in ln)
    assert "off-warrant" not in line
