"""Unconfigured optional providers record SKIPPED (once per run), never ERROR.

VPS first deploy (2026-10-09): EODHD/LME/CryptoPanic/Telegram unset produced
an ERROR per series and paged as failures. A configured-but-failing provider
must stay ERROR.
"""

from __future__ import annotations

import pytest

from arkwatch import api, db
from arkwatch.config import missing_env
from arkwatch.fetchers import eodhd
from arkwatch.qa import harvest as hv
from arkwatch.qa import verify_sources as vs

EODHD_ROW = {"series_id": "EODHD:GSPC.INDX", "block": "A", "freq": "D"}


@pytest.fixture()
def no_eodhd(monkeypatch):
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)


def test_missing_env(monkeypatch):
    monkeypatch.setenv("A_SET", "x")
    monkeypatch.setenv("A_BLANK", "  ")
    monkeypatch.delenv("A_UNSET", raising=False)
    assert missing_env("A_SET") is None
    assert missing_env("A_SET", "A_BLANK", "A_UNSET") == "unconfigured: A_BLANK, A_UNSET"


def _harvest(tmp_path, monkeypatch, rows):
    monkeypatch.setattr(hv, "load_registry", lambda: rows)
    monkeypatch.setattr("arkwatch.qa.backfill.sync_registry", lambda conn: None)
    path = tmp_path / "a.db"
    res = hv.harvest(str(path))
    c = db.get_conn(path)
    log = c.execute("SELECT target, status, error FROM fetch_log").fetchall()
    c.close()
    return res, log


def test_harvest_skips_unconfigured_provider_once(tmp_path, monkeypatch, no_eodhd):
    rows = [EODHD_ROW, {**EODHD_ROW, "series_id": "EODHD:NDX.INDX"}]
    (ok, fail, _), log = _harvest(tmp_path, monkeypatch, rows)
    assert (ok, fail) == (0, 0)
    assert log == [("EODHD:", "SKIPPED", "unconfigured: EODHD_API_TOKEN")]


def test_harvest_configured_failure_stays_error(tmp_path, monkeypatch, no_eodhd):
    monkeypatch.setenv("EODHD_API_TOKEN", "t")

    def boom(*a, **k):
        raise eodhd.EodhdError("HTTP 401")

    monkeypatch.setattr(eodhd, "fetch_latest", boom)
    monkeypatch.setattr(eodhd, "fetch_window", boom, raising=False)
    (ok, fail, _), log = _harvest(tmp_path, monkeypatch, [EODHD_ROW])
    assert fail == 1 and log[0][:2] == ("EODHD:GSPC.INDX", "ERROR")


def test_verify_reports_unconfigured_once_without_violation(no_eodhd):
    rows = [EODHD_ROW, {**EODHD_ROW, "series_id": "EODHD:NDX.INDX"}]
    rep = vs.verify(registry=rows, anchors=[])
    assert rep.violations == []
    assert [(r.series_id, r.note) for r in rep.rows] == [
        ("EODHD:*", "SKIPPED unconfigured: EODHD_API_TOKEN")
    ]


def test_freshness_marks_skipped_provider_unconfigured(tmp_path):
    c = db.get_conn(tmp_path / "a.db", allow_init=True)
    for sid in ("EODHD:GSPC.INDX", "EODHD:NDX.INDX", "FRED:X"):
        c.execute(
            "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
            "primary_source,active) VALUES (?,?,'A',1,'pct','level','D','T',1)",
            (sid, sid),
        )
    log = "INSERT INTO fetch_log(ts,fetcher,target,status) VALUES ('t','f',?,?)"
    c.execute(log, ("EODHD:NDX.INDX", "OK"))
    c.execute(log, ("EODHD:", "SKIPPED"))
    c.execute(log, ("FRED:X", "ERROR"))
    c.execute(log, ("EODHD:GSPC.INDX", "ERROR"))  # configured again later -> not skipped
    status = {r["series_id"]: r["status"] for r in api.data_freshness(c)}
    c.close()
    assert status == {
        "EODHD:GSPC.INDX": "never",
        "EODHD:NDX.INDX": "unconfigured",
        "FRED:X": "never",
    }


def test_market_news_unconfigured_sources_skipped(tmp_path, monkeypatch):
    from arkwatch.qa import market_news as mn

    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    monkeypatch.delenv("CRYPTOPANIC_API_KEY", raising=False)
    monkeypatch.setattr(mn, "_fmp", lambda: [])
    monkeypatch.setattr("arkwatch.fetchers.tree_news.fetch_tree_news", lambda: [])
    monkeypatch.setattr("arkwatch.fetchers.rss_news.fetch_all_rss_feeds", lambda: [])
    monkeypatch.setattr(mn, "_gdelt_updates", lambda: {})
    path = tmp_path / "a.db"
    mn.run(str(path))
    c = db.get_conn(path)
    log = dict(c.execute("SELECT target, status FROM fetch_log").fetchall())
    c.close()
    assert log["EODHD"] == log["CRYPTOPANIC"] == "SKIPPED"
    assert log["FMP"] == "OK"


def test_outbox_unconfigured_channel_is_skipped_not_failed(tmp_path, monkeypatch):
    from arkwatch.senders import outbox

    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / "a.db"
    c = db.get_conn(path, allow_init=True)
    c.execute(
        "INSERT INTO brief_deliveries(brief_date,channel,status,created_at)"
        " VALUES ('2026-10-09','telegram','pending','x')"
    )
    c.close()
    res = outbox.send_pending(str(path))
    c = db.get_conn(path)
    row = c.execute("SELECT status, last_error FROM brief_deliveries").fetchone()
    c.close()
    assert res["failed"] == 0
    assert row == ("skipped", "unconfigured: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")
