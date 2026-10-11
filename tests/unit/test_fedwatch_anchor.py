"""ROUND-8 regression tests: the FedWatch decided-anchor snap and the
DIY-vs-official gate breach observability.

Live incident reconstructed (all numbers from production 2026-09-18):
  FOMC hiked on 2026-09-16 (upper 4.00 → midpoint 3.875). DFF@09-16 still
  prints the OLD 3.63 (new target effective T+1) and FRED had not published
  DFF@09-17 even by 09-18 17:45 UTC. With the old strict `>` the snap only
  fired for a few hours on decision evening; from D+1 the anchor reverted to
  3.63 and the replay-bootstrap (market-expected 3.865) drove the brief
  ("hike 62%" vs official 57.6%). The >= predicate keeps the DECIDED rate
  anchored through the whole publication-lag window.
"""

from __future__ import annotations

from datetime import datetime as _REAL_DT

import pytest

import arkwatch.qa.f2_harvest as f2
from arkwatch import db as adb
from arkwatch.transforms import fedwatch as fw

FROZEN = _REAL_DT(2026, 9, 18, 12, 0, 0)


class _FrozenDateTime:
    """Patches the module-level `datetime` name (now() is frozen; strptime
    passes through — compute_fedwatch parses official meeting dates with it)."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN.replace(tzinfo=tz)

    @staticmethod
    def strptime(s, fmt):
        return _REAL_DT.strptime(s, fmt)


def _build_db(tmp_path):
    conn = adb.get_conn(tmp_path / "t.db", allow_init=True)
    conn.execute(
        "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
        "primary_source) VALUES('FRED:DFF','Effective Fed Funds','A',0,'pct','pct','D','FRED:DFF')"
    )
    # The anchor: observation dated ON the decision day, still the OLD rate
    conn.execute(
        "INSERT INTO raw_observations(series_id,ts,value,vintage_ts,source,fetched_at)"
        " VALUES('FRED:DFF','2026-09-16',3.63,'realtime','FRED','2026-09-17T23:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO events(event_uid,ts_utc,country,name,normalized_name,actual,actual_source)"
        " VALUES('fed-sep26','2026-09-16T18:00:00+00:00','US','FOMC',"
        "'FED INTEREST RATE DECISION',4.0,'FMP')"
    )
    for month, settle in (("SEP 26", 96.2525), ("OCT 26", 96.105), ("NOV 26", 95.98)):
        conn.execute(
            "INSERT INTO cme_settlements(trade_date,product_id,month,settle)"
            " VALUES('2026-09-17',305,?,?)",
            (month, settle),
        )
    return conn


@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(f2, "datetime", _FrozenDateTime)
    monkeypatch.setattr(fw, "datetime", _FrozenDateTime)


def test_decided_anchor_snap_survives_dff_publication_lag(tmp_path, frozen, monkeypatch):
    import arkwatch.fetchers.quikstrike as qs

    monkeypatch.setattr(qs, "fetch_fedwatch_official", lambda: None)
    conn = _build_db(tmp_path)
    out = f2.compute_fedwatch(conn)
    conn.close()

    oct_row = next(r for r in out if r["meeting"] == "2026-10-28")
    # post = implied(NOV 26) = 100 - 95.98 = 4.02; pre = decided 3.875
    assert oct_row["implied"] == pytest.approx(4.02, abs=1e-9)
    # (4.02 - 3.875) / 0.25 = 0.58 — with the pre-round-8 `>` the anchor
    # reverted to 3.63 and the replay-bootstrap produced 0.62
    assert oct_row["hike"] == pytest.approx(0.58, abs=1e-9)
    # the decided meeting itself is never a "probability statement"
    assert min(r["meeting"] for r in out) == "2026-10-28"


def test_gate_breach_writes_fetch_log_row(tmp_path, frozen, monkeypatch):
    import arkwatch.fetchers.quikstrike as qs

    ok_official = [
        {
            "meeting": "Oct 2026",
            "meeting_date": "28 Oct 2026",
            "contract": "ZQ",
            "mid": 96.10,
            "ease": 0.0,
            "hold": 42.0,
            "hike": 58.0,
        }
    ]
    breach_official = [
        {
            "meeting": "Oct 2026",
            "meeting_date": "28 Oct 2026",
            "contract": "ZQ",
            "mid": 96.10,
            "ease": 0.0,
            "hold": 90.0,
            "hike": 10.0,
        }
    ]
    holder = {"v": ok_official}
    monkeypatch.setattr(qs, "fetch_fedwatch_official", lambda: holder["v"])

    conn = _build_db(tmp_path)
    f2.compute_fedwatch(conn)  # DIY 58% vs official 58% → no breach
    n_ok = conn.execute("SELECT COUNT(*) FROM fetch_log WHERE target='FEDWATCH:XVAL'").fetchone()[0]
    holder["v"] = breach_official
    f2.compute_fedwatch(conn)  # DIY 58% vs official 10% → 48pp breach
    rows = conn.execute(
        "SELECT status, error FROM fetch_log WHERE target='FEDWATCH:XVAL'"
    ).fetchall()
    conn.close()

    assert n_ok == 0, "an in-tolerance gate must not write a row"
    assert len(rows) == 1 and rows[0][0] == "ERROR"
    assert "Δ48.0pp" in rows[0][1] and "official 10%" in rows[0][1]


def test_official_fetch_runs_outside_the_write_transaction(tmp_path, frozen, monkeypatch):
    """The QuikStrike GET+POST can take ~80s; holding BEGIN IMMEDIATE across it stalls every
    other writer (watcher alerts, the market lane) for that long."""
    import arkwatch.fetchers.quikstrike as qs

    conn = _build_db(tmp_path)
    seen = []
    monkeypatch.setattr(qs, "fetch_fedwatch_official", lambda: seen.append(conn.in_transaction))
    f2.compute_fedwatch(conn)
    conn.close()
    assert seen == [False]
