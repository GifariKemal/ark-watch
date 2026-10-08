"""Offline unit tests for the FRB: charge-off/delinquency family.

Pins the gap-closure wiring (2026-09-19): the fetcher had existed since
commit 906e960 but was never routed — no registry rows, no harvest path.
These tests keep it wired:
  - SDMX zip parse → quarter-start ts + label mapping
  - ROUTES parity: every FRB_CHGDEL_SERIES key has a registry row
  - window floor: a 10-day default still reaches the latest quarter
"""

from __future__ import annotations

import io
import zipfile

from arkwatch.fetchers import fedsurvey

# SDMX-shaped fixture with the LIVE TIME_PERIOD convention: quarter-END ISO
# dates ('2026-06-30'), discovered when the first live ingest parsed zero rows
# (the research notes had assumed '2026-Q2').
_SDMX = """<?xml version="1.0" encoding="UTF-8"?>
<message:StructureSpecificData xmlns:message="http://www.sdmx.org">
  <DataSet>
    <Series LOANTYPE="CONCC" CHGDEL="CHG" COMPONENT="RATIO" SIZE="ALL" SA="SA">
      <Obs TIME_PERIOD="2026-03-31" OBS_VALUE="7.10"/>
      <Obs TIME_PERIOD="2026-06-30" OBS_VALUE="6.97"/>
    </Series>
    <Series LOANTYPE="CI" CHGDEL="DEL" COMPONENT="RATIO" SIZE="ALL" SA="SA">
      <Obs TIME_PERIOD="2026-Q2" OBS_VALUE="1.9"/>
    </Series>
    <Series LOANTYPE="CONCC" CHGDEL="CHG" COMPONENT="NUM" SIZE="ALL" SA="SA">
      <Obs TIME_PERIOD="2026-06-30" OBS_VALUE="1234567890"/>
    </Series>
    <Series LOANTYPE="CONCC" CHGDEL="CHG" COMPONENT="RATIO" SIZE="ALL" SA="NSA">
      <Obs TIME_PERIOD="2026-06-30" OBS_VALUE="99.9"/>
    </Series>
  </DataSet>
</message:StructureSpecificData>
"""


def _fixture_zip() -> bytes:
    """The zip carries the XSD schema FIRST — the parser must find the data
    file by name ('data' in it), not by first-entry position (live trap)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CHGDEL_schema.xsd", "<xs:schema/>")
        zf.writestr("CHGDEL_data.xml", _SDMX)
    return buf.getvalue()


class TestChgdelParse:
    def test_rows_quarter_start_and_filters(self, monkeypatch):
        monkeypatch.setattr(fedsurvey, "_chgdel_cache", None)  # reset process cache
        monkeypatch.setattr(
            "arkwatch.fetchers.fedsurvey.requests.get",
            lambda *a, **k: type("R", (), {"status_code": 200, "content": _fixture_zip()})(),
        )
        rows = fedsurvey._chgdel_rows()
        assert ("chgoff_chg_credit_card", "2026-04-01", 6.97) in rows
        assert ("chgoff_chg_credit_card", "2026-01-01", 7.10) in rows
        assert ("chgoff_del_commercial_industrial", "2026-04-01", 1.9) in rows
        # NUM (dollar) and NSA rows must NOT survive the filters
        assert not any(v > 90 for _, _, v in rows)

    def test_fetch_latest_and_window_floor(self, monkeypatch):
        monkeypatch.setattr(fedsurvey, "_chgdel_cache", None)
        monkeypatch.setattr(
            "arkwatch.fetchers.fedsurvey.requests.get",
            lambda *a, **k: type("R", (), {"status_code": 200, "content": _fixture_zip()})(),
        )
        cur = fedsurvey.fetch_latest("FRB:CHG_CC")
        assert cur == {"ts": "2026-04-01", "value": 6.97}
        # days=10 must not starve a quarterly family (floor 420) — 'today'
        # injected (audit: the wall-clock version was a 2027 time bomb)
        pts = fedsurvey.fetch_window("FRB:CHG_CC", days=10, today="2026-09-19")
        assert [p["ts"] for p in pts] == ["2026-01-01", "2026-04-01"]


class TestStaleOverride:
    def test_frb_override_window(self, tmp_path):
        """AUDIT round-3: the FRB: 300d override had zero coverage — reverting
        it went undetected by the whole suite. Pin BOTH edges of the window:
        262d (between Q-limit 190 and the 300 override) must be NOT stale;
        beyond 300 must be stale."""
        from datetime import UTC, datetime, timedelta

        from arkwatch import db
        from arkwatch.signals.brief import _health_detail

        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        conn.execute(
            "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
            "ts_convention,primary_source,active) VALUES ('FRB:TEST','t','F',0,'pct','pct',"
            "'Q','quarter_start','test',1)"
        )
        now = datetime.now(UTC)
        for age, sid in ((262, "FRB:TEST"),):
            ts = (now - timedelta(days=age)).date().isoformat()
            conn.execute(
                "INSERT INTO raw_observations(series_id,ts,release_ts,value,vintage_ts,"
                "source,fetched_at) VALUES (?,?,'na',1.0,'realtime','FRB',?)",
                (sid, ts, now.isoformat(timespec="seconds")),
            )
        conn.commit()
        # detail carries only FAILING series — absence at 262d IS the 'ok'
        detail = [d for d in _health_detail(conn)[4] if d[0] == "FRB:TEST"]
        assert not detail, f"262d must NOT be stale under the 300d override: {detail}"

        conn.execute(
            "UPDATE raw_observations SET ts=?",
            ((now - timedelta(days=320)).date().isoformat(),),
        )
        conn.commit()
        detail2 = [d for d in _health_detail(conn)[4] if d[0] == "FRB:TEST"]
        assert detail2 and detail2[0][1] == "stale", f"320d must be stale: {detail2}"
        conn.close()


class TestSepDotRouting:
    def test_dot_series_filter_by_year(self, monkeypatch):
        """CAL:FOMC_DOT_2027 must return the 2027 median, not whatever row is
        last across all years (audit 2026-09-20: the bare version mixed
        years, and the family wasn't in ROUTES at all — 3 series erroring
        at every harvest)."""
        from arkwatch.fetchers import sep
        from arkwatch.qa.verify_sources import ROUTES

        fake_rows = [
            {"ts": "2026-06-17", "series_suffix": "2026", "value": 3.9},
            {"ts": "2026-06-17", "series_suffix": "2027", "value": 3.6},
            {"ts": "2026-09-16", "series_suffix": "2026", "value": 3.7},
            {"ts": "2026-09-16", "series_suffix": "2027", "value": 3.4},
            # oldest-first ordering ALSO covered: max-by-ts must not care
            {"ts": "2023-09-20", "series_suffix": "2026", "value": 2.9},
        ]
        monkeypatch.setattr(
            sep, "series_rows", lambda: sorted(fake_rows, key=lambda r: r["ts"], reverse=True)
        )
        assert sep.fetch_latest("CAL:FOMC_DOT_2026") == {"ts": "2026-09-16", "value": 3.7}
        assert sep.fetch_latest("CAL:FOMC_DOT_2027") == {"ts": "2026-09-16", "value": 3.4}
        # routing: the longer prefix must win over CAL: → caldist
        assert ROUTES["CAL:FOMC_DOT"] is sep
        sid = "CAL:FOMC_DOT_2027"
        prefix = next(p for p in ROUTES if sid.startswith(p))
        assert prefix == "CAL:FOMC_DOT"

    def test_source_pinning_kills_dual_label(self, monkeypatch):
        """AUDIT round-2 P1: the harvest derived source from the ROUTES prefix
        ('CAL:FOMC_DOT'.rstrip(':') is a NO-OP — no trailing colon), stamping a
        second source label while sep's backfill writes 'CAL' → every vintage
        stored twice (source is a PK leg). The module-level SOURCE pin must
        win in _window_or_latest."""
        from arkwatch.fetchers import sep
        from arkwatch.qa.harvest import _window_or_latest

        monkeypatch.setattr(
            sep,
            "series_rows",
            lambda: [{"ts": "2026-09-16", "series_suffix": "2026", "value": 4.1}],
        )
        rows, first, err = _window_or_latest(sep, "CAL:FOMC_DOT_2026", "CAL:FOMC_DOT")
        assert rows == [("CAL:FOMC_DOT_2026", "2026-09-16", 4.1, "CAL")]
        assert err is None and first is not None


class TestRegistryParity:
    def test_every_frb_series_is_registered(self):
        """The gap this wiring closed: fetcher keys without registry rows are
        invisible to the harvest (and raw_observations FK-fails)."""
        from arkwatch.qa.verify_sources import ROUTES, load_registry

        assert "FRB:" in ROUTES  # routed to the fedsurvey module
        reg_ids = {e["series_id"] for e in load_registry()}
        missing = [f"FRB:{k}" for k in fedsurvey.FRB_CHGDEL_SERIES if f"FRB:{k}" not in reg_ids]
        assert not missing, f"unregistered FRB series: {missing}"
        # and the harvest contract: the module must expose the entrypoints
        assert callable(fedsurvey.fetch_latest) and callable(fedsurvey.fetch_window)
