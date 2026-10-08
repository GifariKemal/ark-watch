"""Tests for the vendor-api audit build (2026-09-13).

Covers: the FMP COT parser gate (identity checks + classification bridge —
Σrept is INVALID across legacy/dissagg, OI + nonrep are the exact integers),
the EODHD UST / FMP indicator crossval gates, and the crypto-sentiment
brief line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.qa import cot_gate


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


def _day(n: int = 0) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


# --- EODHD UST branches ------------------------------------------------------


def test_ust_branches(monkeypatch):
    from arkwatch.fetchers import eodhd

    monkeypatch.setenv("EODHD_API_TOKEN", "test")

    class R:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"date": "2026-09-09", "tenor": "10Y", "rate": 2.46},
                    {"date": "2026-09-10", "tenor": "10Y", "rate": 2.55},
                    {"date": "2026-09-10", "tenor": "5Y", "rate": 2.29},
                ]
            }

    monkeypatch.setattr(eodhd.requests, "get", lambda *a, **k: R())
    assert eodhd.fetch_latest("EODHD:USTR10") == {"ts": "2026-09-10", "value": 2.55}
    assert eodhd.fetch_ust_real_yields("10Y")[-1]["value"] == 2.55
    with pytest.raises(eodhd.EodhdError, match="tenor"):
        eodhd.fetch_ust_real_yields("7Y")


def test_sentiment_branch_sparse_and_labeled(monkeypatch):
    from arkwatch.fetchers import eodhd

    monkeypatch.setenv("EODHD_API_TOKEN", "test")

    class R:
        status_code = 200

        def json(self):
            return {
                "BTC-USD.CC": [{"date": "2026-09-08", "normalized": -0.5, "count": 1}],
                "ETH-USD.CC": [{"date": "2026-08-23", "normalized": 0, "count": 1}],
            }

    monkeypatch.setattr(eodhd.requests, "get", lambda *a, **k: R())
    assert eodhd.fetch_latest("EODHD:SENT_BTC") == {"ts": "2026-09-08", "value": -0.5}
    assert eodhd.fetch_latest("EODHD:SENT_ETH") == {"ts": "2026-08-23", "value": 0.0}


# --- Gate-4b/4c crossval helpers ----------------------------------------------


def test_crossval_eodhd_ust_same_date(monkeypatch):
    from arkwatch.qa import verify_sources as vs

    monkeypatch.setattr(
        vs.eodhd, "fetch_ust_real_yields", lambda t, days=10: [{"ts": "2026-09-10", "value": 2.55}]
    )
    assert vs._crossval_eodhd_ust("FRED:DFII10", 2.55, "2026-09-10", 0.02) == "✓"
    # 25bp off → fail with the delta named
    assert "✗ Δ0.250" in vs._crossval_eodhd_ust("FRED:DFII10", 2.30, "2026-09-10", 0.02)
    # date rolled past → degradable ·, never ✗
    assert vs._crossval_eodhd_ust("FRED:DFII10", 2.55, "2026-09-11", 0.02).startswith("·")


def test_crossval_fmp_indicator_month_match(monkeypatch):
    from arkwatch.qa import verify_sources as vs

    class R:
        status_code = 200

        def json(self):
            # monthly indicator observed mid-month (2026-08-15) — the gate
            # must still match it to FRED's month-start ts
            return [{"date": "2026-08-15", "value": 4.1}]

    monkeypatch.setenv("FMP_API_KEY", "test")
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    out = vs._crossval_fmp_indicator("FRED:UNRATE", 4.1, "2026-08-01", 0.1)
    assert out == "✓"


def _rq():
    import requests

    return requests


# --- COT gate ------------------------------------------------------------------


_FMP_DUMP = [
    # gold: OI 500,000; nonreportables 60,000/70,000
    {
        "symbol": "GC",
        "date": "2026-09-08",
        "cftc_contract_market_code": "088691",
        "open_interest_all": 500000,
        "nonrept_positions_long_all": 60000,
        "nonrept_positions_short_all": 70000,
        "tot_rept_positions_long_all": 999999,
    },  # tot_rept deliberately wrong:
    # the Σrept identity was removed as invalid — it must NOT be checked
    # yen keyed only by cftc code + different symbol spelling
    {
        "symbol": "J6",
        "date": "2026-09-08",
        "cftc_contract_market_code": "097741",
        "open_interest_all": 250000,
        "nonrept_positions_long_all": 10000,
        "nonrept_positions_short_all": 9000,
        "tot_rept_positions_long_all": 0,
    },
]


def _seed_cot(conn):
    rows = [
        # gold disagg, matching the dump exactly
        ("2026-09-08", "088691", "disagg", "mm", 145804, 10832, None, 500000),
        ("2026-09-08", "088691", "disagg", "nonrep", 60000, 70000, None, 500000),
        # yen disagg: OI matches, nonrep LONG is off by 1 → must be caught
        ("2026-09-08", "097741", "disagg", "mm", 1, 1, None, 250000),
        ("2026-09-08", "097741", "disagg", "nonrep", 10001, 9000, None, 250000),
    ]
    conn.executemany(
        "INSERT INTO cot_raw(report_date,contract_code,report_type,category,"
        "long,short,spread,open_interest_all,release_ts,source,fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [r + ("2026-09-11", "TEST", "2026-09-13T00:00:00") for r in rows],
    )
    conn.commit()


def test_cot_gate_exact_and_catches_offbyone(conn, monkeypatch):
    _seed_cot(conn)
    monkeypatch.setattr(cot_gate, "fetch_fmp_cot_dump", lambda: _FMP_DUMP)
    checked, mism = cot_gate.run_cot_gate(conn)
    # gold: OI + nonrep L + nonrep S (3 checks, all pass); the wrong
    # tot_rept in the dump proves Σrept is NOT compared
    # yen: OI pass, nonrep L off-by-one caught, nonrep S pass
    assert mism == 1
    assert checked == 6
    gate = conn.execute(
        "SELECT status, error FROM fetch_log WHERE target='FMP:COT-GATE' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert gate[0] == "ERROR" and "1 field mismatch" in gate[1]


def test_cot_gate_clean_pass(conn, monkeypatch):
    _seed_cot(conn)
    conn.execute("UPDATE cot_raw SET long=10000 WHERE contract_code='097741' AND category='nonrep'")
    conn.commit()
    monkeypatch.setattr(cot_gate, "fetch_fmp_cot_dump", lambda: _FMP_DUMP)
    checked, mism = cot_gate.run_cot_gate(conn)
    assert (checked, mism) == (6, 0)
    gate = conn.execute(
        "SELECT status FROM fetch_log WHERE target='FMP:COT-GATE' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert gate[0] == "OK"


def test_cot_gate_symbol_fallback_for_synthetic_handle(conn, monkeypatch):
    conn.execute(
        "INSERT INTO cot_raw(report_date,contract_code,report_type,category,"
        "long,short,spread,open_interest_all,release_ts,source,fetched_at)"
        " VALUES ('2026-09-08','13874+','disagg','nonrep',60000,70000,NULL,"
        "500000,'2026-09-11','TEST','2026-09-13T00:00:00')"
    )
    conn.commit()
    dump = _FMP_DUMP + [
        {
            "symbol": "ES",
            "date": "2026-09-08",
            "cftc_contract_market_code": "138741",
            "open_interest_all": 500000,
            "nonrept_positions_long_all": 60000,
            "nonrept_positions_short_all": 70000,
            "tot_rept_positions_long_all": 0,
        }
    ]
    monkeypatch.setattr(cot_gate, "fetch_fmp_cot_dump", lambda: dump)
    checked, mism = cot_gate.run_cot_gate(conn)
    # the synthetic '13874+' resolves via its stripped code 138741
    assert checked >= 3 and mism == 0


# --- brief sentiment line -------------------------------------------------------


def test_brief_crypto_sentiment_line(conn, tmp_path):
    conn.execute("INSERT INTO flows_daily(date, funding_bps) VALUES (?, 1.5)", (_day(1),))
    for sid, ts, v in (
        ("EODHD:SENT_BTC", _day(2), 0.25),
        ("EODHD:SENT_ETH", _day(40), -0.38),
        ("EODHD:SENT_BTC", _day(60), -0.9),
    ):  # too old → hidden
        conn.execute(
            "INSERT OR IGNORE INTO series_registry(series_id,name,block,tier,"
            "unit,value_format,freq,primary_source) VALUES (?,'x','F',1,"
            "'sent','fraction','D','EODHD')",
            (sid,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO raw_observations(series_id,ts,value,"
            "vintage_ts,source,fetched_at) VALUES (?,?,?,'realtime','t',?)",
            (sid, ts, v, ts),
        )
    conn.commit()
    from arkwatch.signals.compute import generate_brief

    text = generate_brief(conn, str(tmp_path / "b.db"))
    line = next((ln for ln in text.splitlines() if "Crypto sentiment" in ln), None)
    assert line is not None
    assert f"BTC +0.25 ({_day(2)[5:]})" in line
    assert f"ETH -0.38 ({_day(40)[5:]})" in line
