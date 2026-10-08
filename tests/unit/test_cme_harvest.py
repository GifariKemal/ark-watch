"""Tests for the CME settlements walkback (qa/cme_harvest.py).

ROUND-11 regression: the round-10 walkback guarded on `if not rows`, but
fetch_settlements never returns [] — it walks trade dates and raises
CmeError when all are empty — so the guard was unreachable dead code while
the real failure mode (a healthy fetch of stale-dated rows, frontier
stalled) fired unhandled. Live trace 2026-09-19: three Saturday runs
(01:16/03:49/04:40 UTC) returned the 09-17 strip for 0 new rows while
Friday 09-18 was unpublished; the walkback never executed. These tests pin
the trigger on the stalled frontier, the skip when the frontier advances
normally, and tolerance of a raised retry. No network: fetchers
monkeypatched.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arkwatch import db
from arkwatch.fetchers import cme
from arkwatch.qa import cme_harvest


class _PinnedDatetime(datetime):
    """datetime subclass pinned to Saturday 2026-09-19 12:00 UTC — the walk
    from now-1day must skip the weekend and land on Friday 09-18."""

    pin: datetime = datetime(2026, 9, 19, 12, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.pin


def _row(td: str, settle: float = 96.25) -> dict:
    return {
        "trade_date": td,
        "product_id": 305,
        "month": "SEP 26",
        "settle": settle,
        "volume": 1,
        "open_interest": 2,
    }


def _seed_frontier(conn) -> None:
    conn.execute(
        "INSERT INTO cme_settlements"
        "(trade_date,product_id,month,settle,volume,open_interest,fetched_at)"
        " VALUES ('2026-09-17',305,'SEP 26',96.25,1,2,'2026-09-18T06:00:00+00:00')"
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


@pytest.fixture()
def pinned(monkeypatch):
    monkeypatch.setattr(cme_harvest, "datetime", _PinnedDatetime)


def test_walkback_fires_when_frontier_stalls(conn, monkeypatch, pinned):
    """Stale-dated fetch (0 new rows, frontier 09-17) → one explicit retry for
    Friday 09-18 must land the missing strip."""
    _seed_frontier(conn)
    calls: list = []

    def fake_fetch(code, trade_date=None):
        calls.append((code, trade_date))
        if trade_date is None:
            return [_row("2026-09-17")]  # the pre-publication walk result
        return [_row("2026-09-18", 96.10)]  # the explicit-date retry

    monkeypatch.setattr(cme, "fetch_settlements", fake_fetch)

    out = cme_harvest.harvest_settlements(conn, products=["ZQ"])

    assert calls[0] == ("ZQ", None)
    assert len(calls) == 2
    assert calls[1][1].date().isoformat() == "2026-09-18"
    assert out["ZQ"] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM cme_settlements WHERE trade_date='2026-09-18' AND product_id=305"
        ).fetchone()[0]
        == 1
    )


def test_no_walkback_when_frontier_advances(conn, monkeypatch, pinned):
    """A normal morning run fetches the new strip directly — no retry."""
    _seed_frontier(conn)
    calls: list = []

    def fake_fetch(code, trade_date=None):
        calls.append((code, trade_date))
        return [_row("2026-09-18", 96.10)]

    monkeypatch.setattr(cme, "fetch_settlements", fake_fetch)

    out = cme_harvest.harvest_settlements(conn, products=["ZQ"])

    assert len(calls) == 1
    assert out["ZQ"] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM cme_settlements WHERE product_id=305").fetchone()[0] == 2
    )


def test_walkback_retry_failure_is_nonfatal(conn, monkeypatch, pinned):
    """The retry may be blocked (403) or still unpublished — the product must
    report 0 new rows, not -1, and nothing bogus may land."""
    _seed_frontier(conn)

    def fake_fetch(code, trade_date=None):
        if trade_date is None:
            return [_row("2026-09-17")]
        raise cme.CmeError("CME ZQ: HTTP 403 (blocked?)")

    monkeypatch.setattr(cme, "fetch_settlements", fake_fetch)

    out = cme_harvest.harvest_settlements(conn, products=["ZQ"])

    assert out["ZQ"] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM cme_settlements WHERE product_id=305").fetchone()[0] == 1
    )
