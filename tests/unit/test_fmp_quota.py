"""FMP free plan: 402 = endpoint outside the plan, 429 'Limit Reach' = daily quota spent.
Both are SKIPPED (PlanLimited), never ERROR noise; a retry adapter turning 429 into
RetryError must not leak out either."""

from __future__ import annotations

import pytest
import requests

from arkwatch.config import PlanLimited
from arkwatch.fetchers import misc, spdr
from arkwatch.qa import equity_breadth, market_news, market_timeline


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.text = "Limit Reach"

    def json(self):
        return {"Error Message": "Limit Reach"}

    def raise_for_status(self):
        raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")


@pytest.mark.parametrize("status", [402, 429])
@pytest.mark.parametrize(
    ("mod", "attr", "call"),
    [
        (misc, "requests", lambda: misc.fetch_recession_prob()),
        (spdr, "requests", lambda: spdr.fetch_slv_shares()),
        (market_timeline, "requests", lambda: market_timeline._fmp_bars("SPY")),
    ],
)
def test_plan_limit_statuses_are_skipped_not_errors(monkeypatch, status, mod, attr, call):
    monkeypatch.setattr(getattr(mod, attr), "get", lambda *a, **k: _Resp(status))
    with pytest.raises(PlanLimited):
        call()


@pytest.mark.parametrize("status", [402, 429])
def test_market_news_fmp(monkeypatch, status):
    monkeypatch.setattr(market_news.SESSION, "get", lambda *a, **k: _Resp(status))
    with pytest.raises(PlanLimited):
        market_news._fmp()


def test_market_news_fmp_retry_error_means_quota(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.RetryError("too many 429 error responses")

    monkeypatch.setattr(market_news.SESSION, "get", boom)
    with pytest.raises(PlanLimited):
        market_news._fmp()


def test_breadth_constituents_quota(monkeypatch):
    monkeypatch.setattr(equity_breadth.requests, "get", lambda *a, **k: _Resp(429))
    with pytest.raises(PlanLimited):
        equity_breadth._constituents("k")
