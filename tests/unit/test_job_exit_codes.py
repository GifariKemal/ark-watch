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
