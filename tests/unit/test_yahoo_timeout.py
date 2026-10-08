"""Yahoo calls must fail fast: a tarpitted proxy egress cannot stall a whole sweep."""

from __future__ import annotations

import contextlib

from arkwatch.fetchers import yahoo


def test_all_yahoo_calls_use_short_timeout(monkeypatch):
    seen: list[tuple] = []

    class Resp:
        status_code = 500
        text = ""

    class Sess:
        def get(self, url, **kw):
            seen.append(kw.get("timeout"))
            return Resp()

    monkeypatch.setattr(yahoo, "SESSION", Sess())
    monkeypatch.setattr(yahoo, "_throttle", lambda: None, raising=False)
    for fn, args in ((yahoo.fetch_meta, ("CL=F",)), (yahoo.fetch_daily, ("CL=F",))):
        with contextlib.suppress(Exception):  # the 500 is expected; only the timeout matters
            fn(*args)
    assert seen and all(t == yahoo.YAHOO_TIMEOUT for t in seen)
    assert yahoo.YAHOO_TIMEOUT[1] <= 30
