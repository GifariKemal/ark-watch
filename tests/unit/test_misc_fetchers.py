"""Tests for the misc fetcher dispatcher (FMP recession probability route)."""

from __future__ import annotations

import pytest

from arkwatch.fetchers.misc import fetch_latest


def test_dispatcher_routes_recession_prob(monkeypatch):
    called = {}

    def fake_fetch(lookback_days=200):
        called["lb"] = lookback_days
        return {"ts": "2026-07-01", "value": 0.76}

    from arkwatch.fetchers import misc

    monkeypatch.setattr(misc, "fetch_recession_prob", fake_fetch)
    out = fetch_latest("FMP:RECESSION_PROB")
    assert out == {"ts": "2026-07-01", "value": 0.76}
    assert called["lb"] == 200


def test_dispatcher_rejects_unknown_series():
    with pytest.raises(RuntimeError, match="unrouted"):
        fetch_latest("FMP:SOMETHING_ELSE")


def test_recession_prob_picks_newest(monkeypatch):
    """The fetcher must take the NEWEST row of the window, not rows[-1]
    (the CBOE lesson: ordering assumptions are how quiet-broken starts)."""
    from arkwatch.fetchers import misc

    monkeypatch.setenv("FMP_API_KEY", "test-key")

    class FakeResp:
        status_code = 200

        def json(self):
            return [
                {"name": "smoothedUSRecessionProbabilities", "date": "2026-05-01", "value": 1.2},
                {"name": "smoothedUSRecessionProbabilities", "date": "2026-07-01", "value": 0.76},
                {"name": "smoothedUSRecessionProbabilities", "date": "2026-06-01", "value": 0.9},
            ]

    monkeypatch.setattr(misc.requests, "get", lambda *a, **k: FakeResp())
    out = misc.fetch_recession_prob()
    assert out["ts"] == "2026-07-01"
    assert out["value"] == pytest.approx(0.76)
    # auth + window params reached the endpoint
    monkeypatch.setattr(
        misc.requests,
        "get",
        lambda url, params=None, timeout=None: setattr(FakeResp, "_params", params) or FakeResp(),
    )
    misc.fetch_recession_prob()
    assert FakeResp._params["name"] == "smoothedUSRecessionProbabilities"
    assert FakeResp._params["apikey"]
    assert "from" in FakeResp._params and "to" in FakeResp._params


def test_fetch_mpt_mortgage_retired():
    from arkwatch.fetchers.misc import fetch_mpt_mortgage

    assert fetch_mpt_mortgage() is None
