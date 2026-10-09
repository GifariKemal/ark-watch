"""A daemon job that exits 1 pages the owner (job_failed alert, now pushed to the phone).
Plan-limited providers and partial after-hours degradation must not do that."""

from __future__ import annotations

from arkwatch.config import PlanLimited
from arkwatch.qa import market_timeline, verify_sources


def test_verify_plan_limited_series_is_skipped_not_a_violation(monkeypatch):
    class Mod:
        @staticmethod
        def fetch_latest(_sid):
            raise PlanLimited("plan-limited: FMP economic-indicators")

    monkeypatch.setitem(verify_sources.ROUTES, "FMP:", Mod)
    rep = verify_sources.verify(
        registry=[{"series_id": "FMP:RECESSION_PROB", "block": "A"}], anchors=[]
    )
    assert rep.violations == []
    assert "plan-limited" in rep.rows[0].note


def _run_main(monkeypatch, result):
    monkeypatch.setattr(market_timeline, "run", lambda *a, **k: result)
    return market_timeline.main(["--no-1m"])


def test_market_partial_degradation_is_not_a_failure(monkeypatch, capsys):
    # 4 of 40 ETFs degraded outside US hours (stale/future after-hours bars)
    result = {f"S{i}": 1 for i in range(36)} | {f"E{i}": -1 for i in range(4)} | {"breadth": 0}
    assert _run_main(monkeypatch, result) == 0
    assert "4 degraded" in capsys.readouterr().out


def test_market_majority_degraded_is_an_outage(monkeypatch):
    result = {f"S{i}": 1 for i in range(10)} | {f"E{i}": -1 for i in range(30)}
    assert _run_main(monkeypatch, result) == 1


def test_market_okx_collector_failure_still_fails(monkeypatch):
    assert _run_main(monkeypatch, {"S1": 1, "okx:collector": -1}) == 1


class _R:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


def test_fmp_crossval_quota_error_degrades_not_a_violation(monkeypatch):
    """FMP answers {'Error Message': 'Limit Reach'} (a dict) once the daily quota is spent."""
    import requests

    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R(429, {"Error Message": "Limit Reach"}))
    out = verify_sources._crossval_fmp("FRED:DGS2", 3.9, "2026-10-08", None)
    assert out.startswith("·"), out


def test_fmp_crossval_transport_error_degrades(monkeypatch):
    import requests

    monkeypatch.setenv("FMP_API_KEY", "k")

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    assert verify_sources._crossval_fmp("FRED:DGS2", 3.9, "2026-10-08", None).startswith("·")


def test_fmp_crossval_real_mismatch_is_still_a_violation(monkeypatch):
    import requests

    monkeypatch.setenv("FMP_API_KEY", "k")
    rows = [{"date": "2026-10-08", "year2": 4.5}]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R(200, rows))
    monkeypatch.setitem(verify_sources.CROSSVAL_MAP, "FRED:DGS2", "year2")
    assert verify_sources._crossval_fmp("FRED:DGS2", 3.9, "2026-10-08", None).startswith("✗")


def test_verify_source_timeout_is_not_a_violation(monkeypatch):
    """A slow upstream (ECB read timeout) says nothing about the data: degrade, do not page."""
    import requests

    class Mod:
        @staticmethod
        def fetch_latest(_sid):
            raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setitem(verify_sources.ROUTES, "ECB:", Mod)
    rep = verify_sources.verify(registry=[{"series_id": "ECB:ESTR", "block": "A"}], anchors=[])
    assert rep.violations == []
    assert "timeout" in rep.rows[0].note.lower() or "unreachable" in rep.rows[0].note.lower()
