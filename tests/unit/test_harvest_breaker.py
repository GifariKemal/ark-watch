"""harvest: a tarpitting source trips a per-prefix breaker instead of eating the job's budget."""

from __future__ import annotations

import requests

from arkwatch import db
from arkwatch.qa import backfill, harvest


class SlowFred:
    calls = 0

    @classmethod
    def fetch_observations(cls, *_a, **_k):
        cls.calls += 1
        raise requests.Timeout("read timed out")


def test_breaker_skips_the_rest_of_a_dead_source(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    db.get_conn(path, allow_init=True).close()
    reg = [{"series_id": f"FRED:S{i}", "block": "A", "freq": "D"} for i in range(8)]
    monkeypatch.setattr(harvest, "load_registry", lambda: reg)
    monkeypatch.setattr(backfill, "sync_registry", lambda _c: None)
    monkeypatch.setitem(harvest.ROUTES, "FRED:", SlowFred)
    ok, fail, _rows = harvest.harvest(str(path))
    assert SlowFred.calls == harvest.BREAKER_AFTER  # not 8 x (timeout + retries)
    assert (ok, fail) == (0, 8)  # skipped series still count: a dead source pages
    c = db.get_conn(path)
    row = c.execute("SELECT status, error FROM fetch_log WHERE target='FRED:'").fetchone()
    c.close()
    assert row[0] == "SKIPPED" and row[1].startswith("breaker")
