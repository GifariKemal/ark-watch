"""REST server (arkwatch/server): auth, pagination, read-only, writes+audit, jobs, freshness."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from arkwatch import db
from arkwatch.qa import jobs_runner
from arkwatch.server.app import app

KEY = "k" * 40
TODAY = datetime.now(UTC).date()


def _days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def _series(c, sid, freq, active=1):
    c.execute(
        "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
        "primary_source,active) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, sid, "A", 1, "pct", "level", freq, "TEST", active),
    )


def _scenario(c, uid, state, direction="LONG", entry=None, created="2026-10-01T00:00:00"):
    c.execute(
        "INSERT INTO playbook_scenarios(scenario_uid,symbol,horizon,direction,scenario_id,title,"
        "trigger_condition,trigger_price,target_profit,invalidation_level,risk_reward_ratio,"
        "created_at_utc,session_id,state,entry_price,payload_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, "NQ1", "INTRADAY", direction, "s1", "t", "cond", 100.0, 110.0, 95.0, 2.0,
         created, "2026-10-01", state, entry, json.dumps({"k": 1})),
    )  # fmt: skip


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    path = tmp_path / "arkwatch.db"
    c = db.get_conn(path, allow_init=True)
    _series(c, "T:DAILY", "D")
    _series(c, "T:MONTHLY", "M")
    _series(c, "T:STALE", "D")
    _series(c, "T:NEVER", "W")
    _series(c, "T:OFF", "D", active=0)
    db.insert_observations(
        c,
        [
            ("T:DAILY", _days_ago(2), 1.0, "S"),
            ("T:DAILY", _days_ago(1), 2.0, "S", "2026-10-07T12:00:00"),
            ("T:DAILY", _days_ago(0), 3.0, "S"),
            ("T:MONTHLY", _days_ago(100), 4.0, "S"),
            ("T:STALE", _days_ago(30), 5.0, "S"),
        ],
    )
    for i in range(3):
        c.execute(
            "INSERT INTO computed_signals(signal_id,ts,run_id,computed_at,value,state)"
            " VALUES (?,?,?,?,?,?)",
            ("regime_x", _days_ago(i), "r", "now", float(i), "ON"),
        )
    c.execute(
        "INSERT INTO computed_signals(signal_id,ts,run_id,computed_at,value,state)"
        " VALUES ('rx_other','2026-01-01','r','now',1,NULL)"
    )
    _scenario(c, "PB-PENDING", "PENDING_TRIGGER", created="2026-10-01T00:00:00")
    _scenario(c, "PB-ACTIVE", "ACTIVE", entry=100.0, created="2026-10-02T00:00:00")
    _scenario(c, "PB-WIN", "HIT_TARGET_WIN", created="2026-10-03T00:00:00")
    c.execute(
        "INSERT INTO brief_deliveries(id,brief_date,channel,status,attempts,last_error,created_at)"
        " VALUES (1,'2026-10-01','tg','failed',3,'boom','x'), (2,'2026-10-02','tg','sent',1,NULL,'x')"
    )
    for i, sym in enumerate(['["GC1"]', '["NQ1"]', '["A%B"]']):
        c.execute(
            "INSERT INTO market_news(news_id,published_at_utc,source,title,symbols_json,"
            "cluster_id,relevance,novelty,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"n{i}", f"2026-10-0{i + 1}T00:00:00", "FMP", "t", sym, "c", 0.5, 0.5, "x"),
        )
    c.execute(
        "INSERT INTO events(event_uid,ts_utc,country,name,normalized_name,actual)"
        " VALUES ('e1',?,'US','CPI','cpi',3.1), ('e2','2000-01-01T00:00:00','US','old','old',1)",
        (f"{TODAY.isoformat()}T12:30:00",),
    )
    c.close()
    monkeypatch.setenv("ARKWATCH_DB", str(path))
    monkeypatch.setenv("ARKWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARKWATCH_API_KEY", KEY)
    monkeypatch.delenv("ARKWATCH_ALLOW_NO_AUTH", raising=False)
    return path


@pytest.fixture()
def client(seeded):
    with TestClient(app) as cl:
        cl.headers["x-arkwatch-key"] = KEY
        yield cl


def _audit_count(path) -> int:
    c = db.get_conn(path, read_only=True)
    try:
        return c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        c.close()


# --- auth / startup -----------------------------------------------------------


AUTH_PATHS = (
    "/v1/series",
    "/v1/alerts",
    "/v1/alerts/summary",
    "/v1/briefs",
    "/v1/briefs/latest",
    "/v1/briefs/2026-10-01",
    "/v1/graph",
    "/v1/playbook/scorecard",
    "/v1/risk/book",
)


@pytest.mark.parametrize("path", AUTH_PATHS)
def test_auth_required_and_rejected(client, path):
    for headers in ({"x-arkwatch-key": ""}, {"x-arkwatch-key": "x" * 40}):
        r = client.get(path, headers=headers)
        assert r.status_code == 401
        assert r.json() == {
            "error": "missing or invalid API key",
            "code": "unauthorized",
            "detail": None,
        }
    assert client.get("/v1/series").status_code == 200


def test_health_unauthenticated(client, seeded):
    (seeded.parent / "daemon_heartbeat").write_text(datetime.now(UTC).isoformat())
    (seeded.parent / "daemon_state.json").write_text(
        json.dumps({f"0600-harvest@{TODAY}": "1", f"0620-instruments sweep@{TODAY}": "1"})
    )
    r = client.get("/v1/health", headers={"x-arkwatch-key": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db_readable"] is True
    assert body["schema_version"] == db.SCHEMA_VERSION
    assert body["heartbeat_age_s"] < 60
    assert body["daemon_succeeded_today"] == ["harvest", "instruments sweep"]
    assert body["freshness"] == {"fresh": 1, "late": 1, "stale": 1, "never": 1}


def test_health_db_missing_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("ARKWATCH_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("ARKWATCH_API_KEY", KEY)
    with TestClient(app) as cl:
        r = cl.get("/v1/health")
        assert r.status_code == 503 and r.json()["status"] == "down"
        r = cl.get("/v1/series", headers={"x-arkwatch-key": KEY})
        assert r.status_code == 503 and r.json()["code"] == "db_unavailable"
    assert not (tmp_path / "nope.db").exists()  # never created by the API


def test_startup_refuses_short_or_missing_key(seeded, monkeypatch):
    monkeypatch.setenv("ARKWATCH_API_KEY", "short")
    with pytest.raises(RuntimeError, match="ARKWATCH_API_KEY"), TestClient(app):
        pass
    monkeypatch.delenv("ARKWATCH_API_KEY")
    monkeypatch.setenv("ARKWATCH_ALLOW_NO_AUTH", "1")
    with TestClient(app) as cl:
        assert cl.get("/v1/series").status_code == 200


def test_no_auth_switch_ignored_outside_pytest(seeded, monkeypatch):
    monkeypatch.delenv("ARKWATCH_API_KEY")
    monkeypatch.setenv("ARKWATCH_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST")  # what a production process looks like
    with pytest.raises(RuntimeError, match="ARKWATCH_API_KEY"), TestClient(app):
        pass
    monkeypatch.setenv("ARKWATCH_API_KEY", " " * 40)  # whitespace is not a key
    with pytest.raises(RuntimeError, match="ARKWATCH_API_KEY"), TestClient(app):
        pass


def test_docs_and_openapi_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json", "/v1/openapi.json"):
        assert client.get(path, headers={"x-arkwatch-key": ""}).status_code in (401, 404)


def test_audit_log_is_append_only(seeded):
    c = db.get_conn(seeded, allow_init=True)
    c.execute("INSERT INTO audit_log(ts,actor,action) VALUES ('t','a','x')")
    for sql in ("UPDATE audit_log SET actor='z'", "DELETE FROM audit_log"):
        with pytest.raises(Exception, match="append-only"):
            c.execute(sql)
    c.close()


# --- reads ----------------------------------------------------------------------


def test_series_pagination_walks_all_rows(client):
    seen, cursor = [], None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = client.get("/v1/series", params=params).json()
        seen += [s["series_id"] for s in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted(seen) and len(seen) == len(set(seen)) == 5
    assert [s["series_id"] for s in client.get("/v1/series?active=false").json()["items"]] == [
        "T:OFF"
    ]
    r = client.get("/v1/series", params={"cursor": "garbage"})
    assert r.status_code == 400 and r.json()["code"] == "bad_request"


def test_series_detail_and_observations(client):
    d = client.get("/v1/series/T:DAILY").json()
    assert d["n_obs"] == 3 and d["freshness"]["status"] == "fresh"
    assert client.get("/v1/series/NOPE").json()["code"] == "not_found"
    obs = client.get("/v1/series/T:DAILY/observations", params={"from": _days_ago(1)}).json()
    assert [o["value"] for o in obs["items"]] == [2.0, 3.0]
    # the 'na' release_ts sentinel is surfaced as null, a real one passes through
    assert [o["release_ts"] for o in obs["items"]] == ["2026-10-07T12:00:00", None]


def test_signals(client):
    items = client.get("/v1/signals", params={"prefix": "regime_"}).json()["items"]
    # '_' is escaped: 'rx_other' must not match the prefix 'regime_' / 'r_'
    assert [(s["signal_id"], s["last_ts"], s["value"]) for s in items] == [
        ("regime_x", _days_ago(0), 0.0)
    ]
    assert client.get("/v1/signals", params={"prefix": "r_"}).json()["items"] == []
    hist = client.get("/v1/signals/regime_x/history", params={"limit": 2}).json()
    assert [p["ts"] for p in hist["items"]] == [_days_ago(2), _days_ago(1)]
    assert hist["next_cursor"]


def test_playbooks_list_detail_and_news_calendar(client):
    page = client.get("/v1/playbooks").json()
    assert [p["scenario_uid"] for p in page["items"]] == ["PB-WIN", "PB-ACTIVE", "PB-PENDING"]
    assert client.get("/v1/playbooks?state=active").json()["items"][0]["scenario_uid"] == (
        "PB-ACTIVE"
    )
    assert client.get("/v1/playbooks/PB-WIN").json()["payload"] == {"k": 1}
    assert client.get("/v1/playbook/performance").json()["total_scenarios"] == 3
    # LIKE wildcards in the symbol filter are literal
    assert [n["news_id"] for n in client.get("/v1/news?symbol=%25").json()["items"]] == ["n2"]
    assert [n["news_id"] for n in client.get("/v1/news?symbol=GC").json()["items"]] == ["n0"]
    cal = client.get("/v1/calendar").json()["items"]
    assert [e["event_uid"] for e in cal] == ["e1"]


def test_scorecard_and_book_risk_endpoints(client, seeded):
    c = db.get_conn(seeded)
    c.execute(
        "UPDATE playbook_scenarios SET entry_price=100, r_multiple=2, resolved_at_utc="
        "'2026-10-03T20:00:00+00:00' WHERE scenario_uid='PB-WIN'"
    )
    c.close()
    body = client.get("/v1/playbook/scorecard").json()
    assert set(body) >= {"generated_at", "groups", "overall", "disclaimer"}
    (g,) = body["groups"]
    assert (g["scenario_type"], g["asset_class"], g["n"], g["tier"]) == (
        "s1",
        "equity_index",
        1,
        "unvalidated",
    )
    assert client.get("/v1/playbook/scorecard?asset_class=crypto").json()["groups"] == []
    assert client.get("/v1/playbook/scorecard?direction=long").json()["overall"]["n"] == 1
    assert client.get("/v1/playbook/scorecard?horizon=bogus").status_code == 422
    # detail: own-type scorecard, or null with a reason
    assert client.get("/v1/playbooks/PB-ACTIVE").json()["scorecard"]["n"] == 1
    c = db.get_conn(seeded)
    c.execute("UPDATE playbook_scenarios SET scenario_id='s2' WHERE scenario_uid='PB-PENDING'")
    c.close()
    d = client.get("/v1/playbooks/PB-PENDING").json()
    assert d["scorecard"] is None and "no resolved trades" in d["scorecard_reason"]

    risk = client.get("/v1/risk/book").json()
    assert risk["open_count"] == 2 and risk["r_at_stake"] == 1
    assert {s["scenario_uid"] for s in risk["scenarios"]} == {"PB-PENDING", "PB-ACTIVE"}
    assert any("expiry" in f for f in risk["flags"]) and risk["veto_hints"]


def test_levels_mixed_fields_become_null_plus_status(client, monkeypatch):
    from arkwatch import api

    monkeypatch.setattr(
        api,
        "get_session_levels",
        lambda symbol, conn: {
            "symbol": "NQ1",
            "as_of": "x",
            "last_price": 1.5,
            "levels": {"PDH": 2.0, "OR15_HIGH": "FORMING_IN_RTH", "TPO_SINGLE_PRINTS": [1]},
            "extra": 1,
        },
    )
    body = client.get("/v1/sessions/NQ1/levels").json()
    assert body["levels"] == {"PDH": 2.0, "OR15_HIGH": None}
    assert body["level_status"] == {"OR15_HIGH": "FORMING_IN_RTH"}
    assert body["context"] == {"extra": 1, "level_context": {"TPO_SINGLE_PRINTS": [1]}}


def test_regime_cache_invalidated_by_data_version(client, seeded, monkeypatch):
    from arkwatch import api

    calls = []
    real = api.get_regime_snapshot
    monkeypatch.setattr(api, "get_regime_snapshot", lambda c: calls.append(1) or real(c))
    assert client.get("/v1/regime").json()["label"] in ("RISK-ON", "RISK-OFF", "NEUTRAL", "INSUFFICIENT DATA")
    client.get("/v1/regime")
    assert len(calls) == 1  # served from cache
    w = db.get_conn(seeded)
    db.insert_observations(w, [("T:DAILY", "1999-01-01", 1.0, "S")])
    w.close()
    client.get("/v1/regime")
    assert len(calls) == 2  # a commit elsewhere moved data_version


def test_freshness_statuses(client):
    body = client.get("/v1/freshness").json()
    status = {i["series_id"]: i["status"] for i in body["items"]}
    assert status == {
        "T:DAILY": "fresh",
        "T:MONTHLY": "late",
        "T:STALE": "stale",
        "T:NEVER": "never",
    }
    assert body["summary"] == {"fresh": 1, "late": 1, "stale": 1, "never": 1}


def _write(path, sql, rows):
    w = db.get_conn(path)
    w.executemany(sql, rows)
    w.commit()
    w.close()


def test_alerts_list_filters_and_summary(client, seeded):
    now = datetime.now(UTC)
    ts = [(now - timedelta(hours=h)).isoformat(timespec="seconds") for h in (1, 2, 30, 24 * 10)]
    _write(
        seeded,
        "INSERT INTO alert_deliveries(alert_type,triggered_at,cooldown_key,priority,status,"
        "message,last_error) VALUES (?,?,?,?,?,?,?)",
        [
            ("vix", ts[0], "k1", "urgent", "sent", "VIX > 30 & <rising>", None),
            ("hy", ts[1], "k2", "normal", "skipped", "HY wide", "no channel"),
            ("vix", ts[2], "k3", "normal", "failed", "old", "boom"),
            ("dxy", ts[3], "k4", "normal", "sent", "ancient", None),
        ],
    )
    page = client.get("/v1/alerts", params={"limit": 2}).json()
    assert [a["triggered_at"] for a in page["items"]] == ts[:2]  # newest first
    assert page["items"][0]["message"] == "VIX > 30 & <rising>"  # raw, JSON-escaped only
    rest = client.get("/v1/alerts", params={"cursor": page["next_cursor"]}).json()
    assert [a["status"] for a in rest["items"]] == ["failed", "sent"]
    assert rest["next_cursor"] is None
    one = client.get("/v1/alerts", params={"status": "skipped"}).json()["items"]
    assert [(a["alert_type"], a["last_error"]) for a in one] == [("hy", "no channel")]
    assert len(client.get("/v1/alerts", params={"alert_type": "vix"}).json()["items"]) == 2
    assert len(client.get("/v1/alerts", params={"since": ts[2]}).json()["items"]) == 3
    assert client.get("/v1/alerts", params={"status": "bogus"}).status_code == 422
    assert client.get("/v1/alerts", params={"limit": 201}).status_code == 422

    s = client.get("/v1/alerts/summary").json()
    assert s["newest_triggered_at"] == ts[0]
    assert s["last_24h"] == {
        "total": 2,
        "by_status": {"sent": 1, "skipped": 1},
        "by_priority": {"urgent": 1, "normal": 1},
    }
    assert s["last_7d"]["total"] == 3
    assert s["last_7d"]["by_status"] == {"sent": 1, "skipped": 1, "failed": 1}


def test_alerts_summary_empty(client):
    s = client.get("/v1/alerts/summary").json()
    assert s["newest_triggered_at"] is None
    assert s["last_24h"] == {"total": 0, "by_status": {}, "by_priority": {}}


def test_briefs_list_latest_and_by_date(client, seeded):
    assert client.get("/v1/briefs/latest").json()["code"] == "not_found"
    _write(
        seeded,
        "INSERT INTO brief_log(date,markdown,regime_score,generated_at) VALUES (?,?,?,?)",
        [
            ("2026-10-07", "# old", 0.1, "2026-10-07T00:00:00"),
            ("2026-10-08", "# Brief\n*x* & <y>", -0.42, "2026-10-08T00:00:00"),
        ],
    )
    items = client.get("/v1/briefs").json()["items"]
    assert [(b["date"], b["chars"]) for b in items] == [("2026-10-08", 17), ("2026-10-07", 5)]
    assert "markdown" not in items[0]
    assert client.get("/v1/briefs/latest").json() == {
        "date": "2026-10-08",
        "regime_score": -0.42,
        "generated_at": "2026-10-08T00:00:00",
        "markdown": "# Brief\n*x* & <y>",
    }
    assert client.get("/v1/briefs/2026-10-07").json()["markdown"] == "# old"
    r = client.get("/v1/briefs/2026-01-01")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert client.get("/v1/briefs/not-a-date").status_code == 422
    assert len(client.get("/v1/briefs", params={"limit": 1}).json()["items"]) == 1


def test_graph_built_from_stored_data_only(client, seeded):
    w = db.get_conn(seeded)
    for sid, block, active in (("FRED:DFF", "A", 0), ("FRED:IORB", "A", 1), ("X:H", "H", 1)):
        w.execute(
            "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
            "primary_source,active) VALUES (?,?,?,1,'pct','pct','D','T',?)",
            (sid, sid, block, active),
        )
    db.insert_observations(w, [("FRED:DFF", _days_ago(1), 4.33, "S")])
    soon = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    far = (datetime.now(UTC) + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%S")
    w.executemany(
        "INSERT INTO events(event_uid,ts_utc,country,name,normalized_name,importance,consensus)"
        " VALUES (?,?,'US',?,?,?,?)",
        [("hi", soon, "NFP", "nfp", "high", 150.0), ("lo", soon, "x", "x", "low", None),
         ("far", far, "CPI", "cpi", "high", None)],
    )  # fmt: skip
    w.execute(
        "INSERT INTO instrument_prices(symbol,ts,source,open,high,low,close,volume)"
        " VALUES ('NQ1',?,'YAHOO',1,2,0.5,1.5,10)",
        (_days_ago(0),),
    )
    w.commit()
    w.close()

    g = client.get("/v1/graph").json()
    nodes = {n["id"]: n for n in g["nodes"]}
    links = {(lk["source"], lk["target"], lk["kind"]) for lk in g["links"]}
    assert nodes["regime"]["kind"] == "regime" and g["generated_at"]
    assert {f"pillar:{k}" for k in "ABCDEF"} <= set(nodes)
    assert ("pillar:B", "regime", "pillar_of") in links
    # a pillar part that is a registry series (even inactive) feeds its pillar
    dff = nodes["series:FRED:DFF"]
    assert (dff["value"], dff["status"], dff["weight"]) == (4.33, "fresh", 1.0)
    assert ("series:FRED:DFF", "pillar:A", "series_in") in links
    # active block members hang off the same pillar with a lower weight
    assert nodes["series:FRED:IORB"]["weight"] < 1.0
    assert ("series:T:DAILY", "pillar:A", "series_in") in links
    # parts that are not registry series, inactive non-parts and block H are skipped
    assert "series:FRED:DFII10" not in nodes and "series:T:OFF" not in nodes
    assert "series:X:H" not in nodes
    nq = nodes["asset:NQ1"]
    assert (nq["value"], nq["status"]) == (1.5, "fresh")
    assert nodes["asset:XAUUSD"]["status"] == "never"
    assert ("asset:NQ1", "regime", "trades") in links
    # open playbooks only, linked to their asset
    assert nodes["scenario:PB-ACTIVE"]["status"] == "live"
    assert nodes["scenario:PB-PENDING"]["status"] == "pending"
    assert "scenario:PB-WIN" not in nodes
    assert ("scenario:PB-ACTIVE", "asset:NQ1", "trades") in links
    # next 7 days, high/medium only
    assert nodes["event:hi"]["value"] == 150.0
    assert ("event:hi", "regime", "scheduled") in links
    assert "event:lo" not in nodes and "event:far" not in nodes
    assert g["counts"]["nodes"] == len(g["nodes"]) <= 450
    assert g["counts"]["links"] == len(g["links"])
    assert g["counts"]["asset"] == sum(n["kind"] == "asset" for n in g["nodes"])
    assert client.get("/v1/graph").json()["nodes"] == g["nodes"]  # deterministic


# --- read-only connection / migration -------------------------------------------


def test_read_only_connection_cannot_write_or_create(seeded, tmp_path):
    c = db.get_conn(seeded, read_only=True)
    assert c.execute("PRAGMA query_only").fetchone()[0] == 1
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    with pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO jobs(kind, created_at) VALUES ('x', 'y')")
    c.close()
    with pytest.raises(FileNotFoundError):
        db.get_conn(tmp_path / "missing.db", read_only=True)
    assert not (tmp_path / "missing.db").exists()


def test_v33_migration_from_v32_keeps_playbooks(tmp_path, monkeypatch):
    path = tmp_path / "v32.db"
    monkeypatch.setattr(db, "MIGRATIONS", {k: v for k, v in db.MIGRATIONS.items() if k <= 32})
    monkeypatch.setattr(db, "SCHEMA_VERSION", 32)
    c = db.get_conn(path, allow_init=True)
    _scenario(c, "OLD", "PENDING_TRIGGER")
    c.close()
    monkeypatch.undo()
    c = db.get_conn(path, allow_init=True)
    assert (
        c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == db.SCHEMA_VERSION
    )
    assert c.execute("SELECT state, note FROM playbook_scenarios").fetchall() == [
        ("PENDING_TRIGGER", None)
    ]
    c.execute("UPDATE playbook_scenarios SET state='CANCELLED_MANUAL'")  # CHECK widened
    idx = {
        r[0]: r[1] for r in c.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")
    }
    for name, table in {
        "idx_events_ts": "events",
        "idx_playbook_state_created": "playbook_scenarios",
        "idx_playbook_symbol_state": "playbook_scenarios",
        "idx_brief_deliveries_status": "brief_deliveries",
        "idx_alert_deliveries_status": "alert_deliveries",
    }.items():
        assert idx.get(name) == table
    cols = {r[1] for r in c.execute("PRAGMA table_info(series_registry)")}
    assert "locked_by_ui" in cols
    assert c.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864
    c.close()


# --- writes ---------------------------------------------------------------------


def test_cancel_rules_audit_and_idempotency(client, seeded):
    url = "/v1/playbooks/PB-PENDING/cancel"
    r = client.post(url, json={"note": "news risk"}, headers={"x-arkwatch-actor": "gifari"})
    assert r.status_code == 200 and r.json()["changed"] is True
    assert r.json()["scenario"]["state"] == "CANCELLED_MANUAL"
    assert r.json()["scenario"]["note"] == "news risk"
    assert client.post(url, json={"note": "again"}).json()["changed"] is False
    assert _audit_count(seeded) == 1

    act = "/v1/playbooks/PB-ACTIVE/cancel"
    assert client.post(act, json={"note": "x"}).status_code == 409
    sc = client.post(act, json={"note": "x", "exit_price": 102.5}).json()["scenario"]
    assert (sc["exit_price"], sc["pnl_points"], sc["r_multiple"]) == (102.5, 2.5, 0.5)
    assert client.post("/v1/playbooks/PB-WIN/cancel", json={"note": "x"}).json()["code"] == (
        "conflict"
    )
    assert client.post("/v1/playbooks/NOPE/cancel", json={"note": "x"}).status_code == 404
    r = client.post(url, json={"note": "x", "sql": "DROP TABLE x"})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    assert _audit_count(seeded) == 2


def test_outbox_retry(client, seeded):
    r = client.post("/v1/outbox/1/retry").json()
    assert r["changed"] is True and r["item"]["status"] == "pending"
    assert r["item"]["attempts"] == 0
    assert client.post("/v1/outbox/1/retry").json()["changed"] is False
    assert client.post("/v1/outbox/2/retry").status_code == 409
    assert client.post("/v1/outbox/99/retry").status_code == 404
    assert _audit_count(seeded) == 1
    assert [o["id"] for o in client.get("/v1/outbox?status=pending").json()["items"]] == [1]


def test_oversized_row_ids_are_422_not_500(client):
    big = 2**63  # one past SQLite INTEGER max: would overflow at bind time
    assert client.get(f"/v1/jobs/{big}").status_code == 422
    assert client.post(f"/v1/outbox/{big}/retry").status_code == 422
    assert client.get(f"/v1/jobs/{big - 1}").status_code == 404


def test_series_patch_locks_against_yaml_sync(client, seeded):
    from arkwatch.qa.backfill import sync_registry

    w = db.get_conn(seeded)
    sync_registry(w)
    sid = w.execute("SELECT series_id FROM series_registry WHERE active=1 AND series_id NOT LIKE 'T:%'").fetchone()[0]  # fmt: skip
    r = client.patch(f"/v1/series/{sid}", json={"active": False}).json()
    assert r["changed"] is True and r["series"]["active"] is False
    assert r["series"]["locked_by_ui"] is True
    assert client.patch(f"/v1/series/{sid}", json={"active": False}).json()["changed"] is False
    sync_registry(w)  # YAML says active=1; the UI lock wins
    assert w.execute("SELECT active, locked_by_ui FROM series_registry WHERE series_id=?", (sid,)).fetchone() == (0, 1)  # fmt: skip
    w.close()
    assert _audit_count(seeded) == 1
    assert client.patch("/v1/series/T:DAILY", json={"active": "maybe"}).status_code == 422


def test_jobs_enqueue_allowlist_idempotent_and_runner(client, seeded, monkeypatch):
    r = client.post("/v1/jobs", json={"kind": "rm -rf"})
    assert r.status_code == 422
    r = client.post("/v1/jobs", json={"kind": "market"})
    assert r.status_code == 202 and r.json()["changed"] is True
    job = r.json()["job"]
    assert job["status"] == "queued" and job["requested_by"] == "api"
    again = client.post("/v1/jobs", json={"kind": "market"}).json()
    assert again["changed"] is False and again["job"]["id"] == job["id"]
    assert _audit_count(seeded) == 1

    seen = []

    class Done:
        returncode, stdout, stderr = 0, "a\nb\nok\n", ""

    def fake_run(argv, **kw):
        seen.append(argv[1:])
        return Done()

    monkeypatch.setattr(jobs_runner.subprocess, "run", fake_run)
    w = db.get_conn(seeded)
    assert jobs_runner.run_pending_jobs(w) == 1
    assert jobs_runner.run_pending_jobs(w) == 0
    w.close()
    assert seen == [["-m", "arkwatch", "market"]]
    got = client.get(f"/v1/jobs/{job['id']}").json()
    assert got["status"] == "done" and got["result"]["tail"] == ["a", "b", "ok"]
    assert client.get("/v1/health").json()["jobs_last_status"] == {"market": "done"}
    assert [j["id"] for j in client.get("/v1/jobs").json()["items"]] == [job["id"]]


def test_jobs_runner_failure_and_unknown_kind(seeded, monkeypatch):
    class Fail:
        returncode, stdout, stderr = 2, "", "Traceback\nboom"

    monkeypatch.setattr(jobs_runner.subprocess, "run", lambda *a, **k: Fail())
    w = db.get_conn(seeded)
    w.execute("INSERT INTO jobs(kind, created_at) VALUES ('energy','x'), ('evil','x')")
    assert jobs_runner.run_pending_jobs(w) == 2
    rows = w.execute("SELECT kind, status, error FROM jobs ORDER BY id").fetchall()
    w.close()
    assert rows == [
        ("energy", "failed", "boom"),
        ("evil", "failed", "kind 'evil' is not allowlisted"),
    ]


def test_job_allowlist_maps_to_daemon_commands():
    from arkwatch.daemon import SCHEDULE

    scheduled = {cmd for *_, cmd, _desc in SCHEDULE}
    for kind, argv in jobs_runner.JOB_KINDS.items():
        assert kind in ("market", "market-news") or " ".join(argv) in scheduled, kind
    assert not {"brief", "send", "backup", "daemon"} & set(jobs_runner.JOB_KINDS)


# --- openapi export ---------------------------------------------------------------


def test_export_openapi(tmp_path):
    from arkwatch.server.export_openapi import main

    out = tmp_path / "openapi.json"
    assert main(["--out", str(out)]) == 0
    spec = json.loads(out.read_text(encoding="utf-8"))
    assert {"/v1/health", "/v1/series/{series_id}/observations", "/v1/jobs"} <= set(spec["paths"])
    new = {"/v1/alerts", "/v1/alerts/summary", "/v1/briefs/{date}", "/v1/graph"}
    assert new <= set(spec["paths"])
    assert {"/v1/playbook/scorecard", "/v1/risk/book"} <= set(spec["paths"])
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]
    kinds = spec["components"]["schemas"]["JobIn"]["properties"]["kind"]["enum"]
    assert set(kinds) == set(jobs_runner.JOB_KINDS)


# --- concurrency ------------------------------------------------------------------


def test_wal_stress_writer_and_8_readers(client, seeded):
    """One writer inserting + 8 readers polling observations for ~10s: zero
    'database is locked', and every reader only ever sees its high-water mark grow."""
    stop = time.monotonic() + 10
    errors: list[str] = []
    done = {"writes": 0, "reads": 0}
    base = datetime(2030, 1, 1)

    def writer():
        w = db.get_conn(seeded)
        i = 0
        try:
            while time.monotonic() < stop:
                ts = (base + timedelta(seconds=i)).isoformat()
                db.insert_observations(w, [("T:DAILY", ts, float(i), "S")])
                i += 1
            done["writes"] = i
        except Exception as ex:  # noqa: BLE001 - surfaced via the assertion below
            errors.append(f"writer: {ex}")
        finally:
            w.close()

    def reader():
        high = base.isoformat()
        try:
            while time.monotonic() < stop:
                r = client.get(
                    "/v1/series/T:DAILY/observations", params={"from": high, "limit": 50}
                )
                if r.status_code != 200:
                    errors.append(f"{r.status_code} {r.text[:200]}")
                    return
                ts = [o["ts"] for o in r.json()["items"]]
                if ts and (ts != sorted(ts) or ts[0] < high):
                    errors.append(f"non-monotone {high} {ts[:3]}")
                    return
                if ts:
                    high = ts[-1]
                done["reads"] += 1
        except Exception as ex:  # noqa: BLE001
            errors.append(f"reader: {ex}")

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not [e for e in errors if "locked" in e], errors
    assert errors == []
    assert done["writes"] > 100 and done["reads"] > 100


def test_odds_serves_question_volume_and_end_date(client, seeded):
    """Polymarket rows live in computed_signals; /v1/odds exposes the fields the dashboard needs
    (question, volume, end date) that the generic signals listing deliberately does not."""
    import json as _json

    c = db.get_conn(seeded, allow_init=True)
    rows = [
        (
            "polymarket:fed-hold",
            _days_ago(0),
            0.845,
            "fed",
            8_277_000.0,
            "Will the Fed hold in October?",
        ),
        (
            "polymarket:recession-2026",
            _days_ago(0),
            0.065,
            "recession",
            2_283_000.0,
            "US recession by end of 2026?",
        ),
        ("polymarket:old-market", _days_ago(10), 0.5, "oil", 9_999_999.0, "Stale market"),
    ]
    for sid, ts, p, topic, vol, q in rows:
        c.execute(
            "INSERT INTO computed_signals(signal_id,ts,run_id,computed_at,value,state,inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                sid,
                ts,
                "r",
                ts + "T00:00:00",
                p,
                topic,
                _json.dumps(
                    {
                        "question": q,
                        "outcomes": ["Yes", "No"],
                        "volume": vol,
                        "end_date": "2026-12-31",
                        "slug": sid[11:],
                    }
                ),
            ),
        )
    c.commit()
    c.close()
    body = client.get("/v1/odds").json()
    got = [
        (m["slug"], m["probability"], m["topic"], m["question"], m["volume"], m["end_date"])
        for m in body["items"]
    ]
    assert got == [
        ("fed-hold", 0.845, "fed", "Will the Fed hold in October?", 8_277_000.0, "2026-12-31"),
        (
            "recession-2026",
            0.065,
            "recession",
            "US recession by end of 2026?",
            2_283_000.0,
            "2026-12-31",
        ),
    ]  # volume desc; a market not refreshed for days is dropped
    assert body["items"][0]["outcomes"] == ["Yes", "No"]
    assert client.get("/v1/odds", headers={"x-arkwatch-key": ""}).status_code == 401
