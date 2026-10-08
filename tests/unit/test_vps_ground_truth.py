"""VPS ground truth (2026-10-09, datacenter IP): Yahoo 429 unless proxied,
NLP keys unset, LME FK on a fresh DB, FMP 402 outside the free plan."""

from __future__ import annotations

import sqlite3

import pytest

from arkwatch import api, db
from arkwatch.config import PlanLimited, nlp_missing
from arkwatch.fetchers import yahoo


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code, self._payload, self.text = status, payload, ""
        self.content = b"<rss><channel></channel></rss>"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


@pytest.fixture()
def no_dotenv(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)


# --- 1. Yahoo proxy ---------------------------------------------------------


def test_yahoo_proxies_unset_and_set(monkeypatch):
    monkeypatch.delenv("ARKWATCH_YAHOO_PROXY", raising=False)
    assert yahoo._proxies() is None
    monkeypatch.setenv("ARKWATCH_YAHOO_PROXY", "socks5h://warp:9091")
    assert yahoo._proxies() == {"http": "socks5h://warp:9091", "https": "socks5h://warp:9091"}


def test_every_yahoo_call_passes_proxies(monkeypatch):
    monkeypatch.setenv("ARKWATCH_YAHOO_PROXY", "socks5h://warp:9091")
    monkeypatch.setattr(yahoo, "THROTTLE_S", 0)
    seen = []

    def fake_get(url, **kw):
        seen.append(kw.get("proxies"))
        return _Resp(200, {"chart": {"result": [{"meta": {"x": 1}, "timestamp": []}]}})

    monkeypatch.setattr(yahoo.SESSION, "get", fake_get)
    yahoo.fetch_meta("CL=F")
    yahoo.fetch_daily("CL=F")
    yahoo.fetch_intraday("CL=F")
    assert seen == [yahoo._proxies()] * 3


def test_rss_proxies_only_the_yahoo_feed(monkeypatch):
    import curl_cffi.requests as creq

    from arkwatch.fetchers import rss_news

    monkeypatch.setenv("ARKWATCH_YAHOO_PROXY", "socks5h://warp:9091")
    seen = {}

    class FakeSession:
        def __init__(self, **kw):
            pass

        def get(self, url, **kw):
            seen[url] = kw.get("proxies")
            return _Resp(200)

    monkeypatch.setattr(creq, "Session", FakeSession)
    for name in ("YAHOO", "FED"):
        rss_news.fetch_rss_feed(name, rss_news.FEEDS[name])
    assert seen == {rss_news.FEEDS["YAHOO"]: yahoo._proxies(), rss_news.FEEDS["FED"]: None}


# --- 2. NLP unconfigured ----------------------------------------------------

NLP_KEYS = ("NLP_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture()
def no_nlp(monkeypatch, no_dotenv):
    for k in NLP_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_nlp_missing_only_when_no_key(monkeypatch, no_nlp):
    assert nlp_missing() == "unconfigured: NLP_API_KEY"
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert nlp_missing() is None


def test_fedsurvey_unconfigured_nlp_is_skipped_exit_0(tmp_path, monkeypatch, no_nlp):
    from arkwatch.fetchers import fedsurvey, minutes, pressconf
    from arkwatch.qa import fedsurvey_harvest as fh

    page = lambda: {"ts": "2026-10-01", "text": "net tightening"}  # noqa: E731
    for fn in ("fetch_sloos", "fetch_beige_book", "fetch_scoos", "fetch_fsr"):
        monkeypatch.setattr(fedsurvey, fn, page)
    monkeypatch.setattr(minutes, "minutes_dates", lambda: ["2026-09-17"])
    monkeypatch.setattr(
        minutes,
        "parse_minutes",
        lambda d: {"full_text": "x", "dissent_direction": None, "dissent_count": 0},
    )
    monkeypatch.setattr(pressconf, "available_dates", lambda: ["2026-09-17"])
    monkeypatch.setattr(pressconf, "fetch_transcript_text", lambda d: "x")
    assert fh.main(["--db", str(tmp_path / "a.db")]) == 0
    c = db.get_conn(tmp_path / "a.db")
    status = c.execute("SELECT status FROM fetch_log WHERE target='FEDSURVEY:ALL'").fetchone()
    c.close()
    assert status == ("OK",)


def test_sentiment_skips_without_nlp_key(tmp_path, monkeypatch, no_nlp):
    from arkwatch.signals import sentiment

    monkeypatch.setattr(sentiment, "_call", lambda *a, **k: pytest.fail("NLP called"))
    c = db.get_conn(tmp_path / "a.db", allow_init=True)
    c.execute(
        "INSERT INTO market_news VALUES ('n1','2026-10-09T00:00:00+00:00','RSS_FED','t',"
        "NULL,NULL,'[]','c',1.0,1.0,'t')"
    )
    assert sentiment.extract_news_intelligence(c) == 0
    c.close()


# --- 3. LME FK on a fresh DB ------------------------------------------------


def test_lme_series_needs_registry_sync(tmp_path):
    from arkwatch.qa.backfill import sync_registry

    c = db.get_conn(tmp_path / "a.db", allow_init=True)
    row = [("LME:CA_STOCKS", "2026-09-30", 150000.0, "LME:XLSX")]
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.insert_observations(c, row)
    sync_registry(c)
    assert db.insert_observations(c, row) == 1
    c.close()


def test_f2_syncs_registry_before_any_insert(tmp_path, monkeypatch):
    from arkwatch.qa import f2_harvest

    class Synced(Exception):
        pass

    def fake_sync(conn):
        raise Synced

    monkeypatch.setattr("arkwatch.qa.backfill.sync_registry", fake_sync)
    with pytest.raises(Synced):
        f2_harvest.main(["--db", str(tmp_path / "a.db"), "--skip-cot", "--skip-flows"])


# --- 4. FMP 402 = plan-limited ----------------------------------------------


def test_breadth_plan_limited_exits_0(tmp_path, monkeypatch, capsys, no_dotenv):
    from arkwatch.qa import equity_breadth as eb

    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setattr(eb.requests, "get", lambda *a, **k: _Resp(402, {}))
    assert eb.main(["--db", str(tmp_path / "a.db")]) == 0
    assert "skipped: FMP plan does not include constituents" in capsys.readouterr().out
    c = db.get_conn(tmp_path / "a.db")
    log = c.execute("SELECT target, status, error FROM fetch_log").fetchall()
    c.close()
    assert log == [("FMP:SP500", "SKIPPED", "plan-limited: FMP sp500-constituent")]


def test_market_news_fmp_402_is_skipped(tmp_path, monkeypatch):
    from arkwatch.qa import market_news as mn

    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setattr(mn.SESSION, "get", lambda *a, **k: _Resp(402, {}))
    with pytest.raises(PlanLimited, match="plan-limited: FMP news/general-latest"):
        mn._fmp()
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    monkeypatch.delenv("CRYPTOPANIC_API_KEY", raising=False)
    monkeypatch.setattr("arkwatch.fetchers.tree_news.fetch_tree_news", lambda: [])
    monkeypatch.setattr("arkwatch.fetchers.rss_news.fetch_all_rss_feeds", lambda: [])
    monkeypatch.setattr(mn, "_gdelt_updates", lambda: {})
    out = mn.run(str(tmp_path / "a.db"))
    c = db.get_conn(tmp_path / "a.db")
    log = dict(c.execute("SELECT target, status FROM fetch_log").fetchall())
    c.close()
    assert log["FMP"] == "SKIPPED" and out["FMP"] == 0


def test_market_fallback_402_is_not_a_failed_asset(tmp_path, monkeypatch):
    from arkwatch.qa import market_timeline as mt

    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setattr(mt.requests, "get", lambda *a, **k: _Resp(402, {}))
    monkeypatch.setattr(mt.yahoo, "fetch_intraday", lambda *a, **k: [])
    monkeypatch.setattr(mt, "_eodhd_bars", lambda _s: [])
    monkeypatch.setattr(mt, "collect_okx_market", lambda _p: {})
    result = mt.run(str(tmp_path / "a.db"), only="EURUSD")
    assert result["EURUSD"] == 0
    c = db.get_conn(tmp_path / "a.db")
    log = dict(c.execute("SELECT target, status FROM fetch_log WHERE target LIKE 'EURUSD:%'"))
    c.close()
    assert log["EURUSD:FMP:5m"] == "SKIPPED"


def test_freshness_reports_plan_limited(tmp_path):
    c = db.get_conn(tmp_path / "a.db", allow_init=True)
    c.execute(
        "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
        "primary_source,active) VALUES ('FMP:X','x','A',1,'pct','level','D','T',1)"
    )
    c.execute(
        "INSERT INTO fetch_log(ts,fetcher,target,status,error)"
        " VALUES ('t','f','FMP:X','SKIPPED','plan-limited: FMP x')"
    )
    status = {r["series_id"]: r["status"] for r in api.data_freshness(c)}
    c.close()
    assert status == {"FMP:X": "plan_limited"}


def test_harvest_fmp_402_series_is_skipped_plan_limited(tmp_path, monkeypatch, no_dotenv):
    from arkwatch.fetchers import misc
    from arkwatch.qa import harvest as hv

    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setattr(misc.requests, "get", lambda *a, **k: _Resp(402, {}))
    row = {"series_id": "FMP:RECESSION_PROB", "block": "A", "freq": "M"}
    monkeypatch.setattr(hv, "load_registry", lambda: [row])
    monkeypatch.setattr("arkwatch.qa.backfill.sync_registry", lambda conn: None)
    ok, fail, _ = hv.harvest(str(tmp_path / "a.db"))
    c = db.get_conn(tmp_path / "a.db")
    log = c.execute("SELECT target, status, error FROM fetch_log").fetchall()
    c.close()
    assert fail == 0
    assert log == [("FMP:RECESSION_PROB", "SKIPPED", "plan-limited: FMP economic-indicators")]
