"""Forecast calibration: pure Brier/reliability math + Polymarket/FedWatch IO (mocked HTTP)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import pytest
import requests

from arkwatch import __main__ as cli
from arkwatch import db
from arkwatch.signals import calibration as cal


def test_brier_and_skill_hand_computed():
    assert cal.brier([0.8, 0.3], [1, 0]) == pytest.approx(0.065)  # (0.04 + 0.09) / 2
    assert cal.brier_skill(0.065, 0.5) == pytest.approx(0.74)  # 1 - 0.065 / 0.25
    assert cal.brier_skill(0.1, 0.0) is None  # climatology is perfect, skill undefined


def test_reliability_bins():
    bins = cal.reliability_bins([0.05, 0.15, 0.95, 1.0], [0, 1, 1, 1])
    assert len(bins) == 10 and sum(b["count"] for b in bins) == 4
    assert bins[0] == {
        "lo": 0.0,
        "hi": 0.1,
        "mean_forecast": 0.05,
        "observed_freq": 0.0,
        "count": 1,
    }
    assert bins[9]["count"] == 2 and bins[9]["mean_forecast"] == 0.975  # p=1.0 lands in the top bin
    assert bins[5] == {
        "lo": 0.5,
        "hi": 0.6,
        "mean_forecast": None,
        "observed_freq": None,
        "count": 0,
    }


def test_calibration_slope():
    # x mean .4, y mean 1/3: sxy = .2, sxx = .08 -> 2.5
    assert cal.calibration_slope([0.2, 0.4, 0.6], [0, 0, 1]) == pytest.approx(2.5)
    assert cal.calibration_slope([0.5, 0.5], [0, 1]) is None
    assert cal.calibration_slope([0.5], [1]) is None


def test_multiclass_brier():
    # (.04 + .09 + .01) and (.25 + .25 + 0) -> mean .32
    assert cal.multiclass_brier([[0.2, 0.7, 0.1], [0.5, 0.5, 0.0]], [1, 0]) == pytest.approx(0.32)


def test_summarize_flags_insufficient():
    s = cal.summarize([0.9, 0.2], [1, 0])
    assert s["n"] == 2 and s["insufficient"] and s["brier"] == 0.025 and s["brier_skill"] == 0.9
    assert cal.summarize([], [])["brier"] is None
    assert not cal.summarize([0.5] * 30, [1] * 30)["insufficient"]


def _t(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_at_horizon_latest_before_cut_and_staleness():
    snaps = [(_t("2026-01-01"), 0.1), (_t("2026-01-22"), 0.2), (_t("2026-01-29"), 0.3)]
    end = _t("2026-01-31")
    assert cal.at_horizon(snaps, end, 1) == 0.3
    assert cal.at_horizon(snaps, end, 7) == 0.2  # cut 01-24, 2d stale is fine
    assert cal.at_horizon(snaps, end, 30) == 0.1  # cut 01-01, exact
    assert cal.at_horizon(snaps[:1], end, 7) is None  # 23d stale counts as absent
    assert cal.at_horizon([], end, 1) is None


# ---- IO --------------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "arkwatch.db", allow_init=True)
    yield c
    c.close()


def _pm(c, slug, computed, p, end, topic="fed"):
    c.execute(
        "INSERT INTO computed_signals VALUES (?,?,?,?,?,?,?)",
        (
            f"polymarket:{slug}",
            computed[:10],
            computed,
            computed,
            p,
            topic,
            json.dumps(
                {"end_date": end, "topic": topic, "url": f"https://polymarket.com/market/{slug}"}
            ),
        ),
    )


def _closed(slug, prices, end, closed=True, closed_time=None):
    return {
        "slug": slug,
        "closed": closed,
        "outcomePrices": prices,
        "endDate": end,
        "closedTime": closed_time,
    }


@pytest.fixture()
def http(monkeypatch):
    """Mocked HTTP layer: state['reply'](url, params) -> json; calls are recorded."""
    state = {"calls": [], "reply": lambda url, params: []}

    def fake(url, params, stats):
        stats["http"] += 1
        state["calls"].append((url, params))
        return state["reply"](url, params)

    monkeypatch.setattr(cal, "_get", fake)
    return state


def _seed_markets(c):
    _pm(c, "mkt-a", "2026-01-01T00:00:00+00:00", 0.3, "2026-01-31T00:00:00Z")
    _pm(c, "mkt-a", "2026-01-24T00:00:00+00:00", 0.6, "2026-01-31T00:00:00Z")
    _pm(c, "mkt-a", "2026-01-29T20:00:00+00:00", 0.9, "2026-01-31T00:00:00Z")
    _pm(c, "mkt-b", "2026-02-27T00:00:00+00:00", 0.2, "2026-02-28T00:00:00Z", topic="oil")
    _pm(c, "mkt-c", "2026-02-27T00:00:00+00:00", 0.5, "2099-01-01T00:00:00Z")  # not ended
    _pm(c, "mkt-d", "2026-02-27T00:00:00+00:00", 0.5, "2026-02-28T00:00:00Z")  # unresolved


GAMMA_CLOSED = [
    _closed("mkt-a", '["1", "0"]', "2026-01-31T00:00:00Z", closed_time="2026-01-31 01:00:00+00"),
    _closed("mkt-b", '["0", "1"]', "2026-02-28T00:00:00Z"),
    _closed("mkt-d", '["0.5", "0.5"]', "2026-02-28T00:00:00Z", closed=False),
]


def _sig(c, sid):
    row = c.execute(
        "SELECT value, state, inputs_json FROM computed_signals WHERE signal_id=?", (sid,)
    ).fetchone()
    return row[0], row[1], json.loads(row[2])


def test_polymarket_scores_and_caches_resolutions(conn, http):
    _seed_markets(conn)
    http["reply"] = lambda url, params: GAMMA_CLOSED
    results, stats = cal.run(conn)
    assert http["calls"][0][1] == {"slug": ["mkt-a", "mkt-b", "mkt-d"], "closed": "true"}
    assert stats == {"http": 1, "degraded": False}
    # 1d: a 0.9 -> 1, b 0.2 -> 0
    v, state, s = _sig(conn, "calibration:polymarket:brier_1d")
    assert v == 0.025 and state == "insufficient" and s["n"] == 2 and s["base_rate"] == 0.5
    assert s["brier_skill"] == 0.9 and len(s["reliability"]) == 10
    assert s["by_topic"]["fed"]["n"] == 1 and s["by_topic"]["oil"]["brier"] == 0.04
    assert _sig(conn, "calibration:polymarket:brier_7d")[0] == 0.16  # a only: (0.6 - 1)^2
    assert _sig(conn, "calibration:polymarket:brier_30d")[0] == 0.49  # (0.3 - 1)^2
    assert _sig(conn, "calibration:pm_resolution:mkt-a")[:2] == (1.0, "resolved")
    assert _sig(conn, "calibration:pm_resolution:mkt-b")[0] == 0.0
    assert conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id LIKE 'calibration:pm_resolution:%'"
    ).fetchone() == (2,)  # mkt-d not closed yet: retried next run, mkt-c not ended
    # second run fetches only what is still unresolved
    http["calls"].clear()
    cal.run(conn)
    assert [p["slug"] for _, p in http["calls"]] == [["mkt-d"]]


def test_early_resolution_ignores_post_close_prices(conn, http):
    _pm(conn, "mkt-e", "2026-03-09T00:00:00+00:00", 0.4, "2026-03-31T00:00:00Z")
    _pm(conn, "mkt-e", "2026-03-20T00:00:00+00:00", 0.999, "2026-03-31T00:00:00Z")
    http["reply"] = lambda url, params: [
        _closed("mkt-e", '["1", "0"]', "2026-03-31T00:00:00Z", closed_time="2026-03-10T00:00:00Z")
    ]
    results, _ = cal.run(conn)
    assert results["calibration:polymarket:brier_1d"]["brier"] == 0.36  # (0.4 - 1)^2


def test_gamma_outage_degrades_and_cli_exits_0(tmp_path, http, capsys):
    path = tmp_path / "a.db"
    c = db.get_conn(path, allow_init=True)
    _seed_markets(c)
    c.close()

    def boom(url, params):
        raise requests.ConnectionError("down")

    http["reply"] = boom
    assert cal.main(["--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "calibration:polymarket:brier_1d: n=0 brier=None (insufficient)" in out
    assert "DEGRADED" in out
    c = db.get_conn(path)
    assert c.execute(
        "SELECT status FROM fetch_log WHERE target='polymarket:resolutions'"
    ).fetchone() == ("ERROR",)
    assert _sig(c, "calibration:fedwatch:brier_7d")[1] == "insufficient"
    c.close()


def test_fedwatch_multiclass_brier_against_dff(conn):
    conn.execute(
        "INSERT INTO series_registry(series_id,name,block,tier,unit,value_format,freq,"
        "primary_source) VALUES('FRED:DFF','EFFR','A',0,'pct','pct','D','FRED:DFF')"
    )
    for d, v in [
        ("09-10", 3.63),
        ("09-15", 3.63),
        ("09-16", 3.63),
        ("09-18", 3.88),
        ("09-21", 3.88),
    ]:
        conn.execute(
            "INSERT INTO raw_observations(series_id,ts,value,vintage_ts,source,fetched_at)"
            " VALUES('FRED:DFF',?,?,'realtime','FRED','x')",
            (f"2026-{d}", v),
        )
    for d, src, p in [
        ("2026-09-15", "diy", (0.1, 0.3, 0.6)),
        ("2026-09-09", "diy", (0.2, 0.6, 0.2)),
        ("2026-09-15", "official", (0.0, 0.4, 0.6)),
    ]:
        conn.execute(
            "INSERT INTO fedwatch_snapshots(date,meeting_date,source,prob_ease,prob_hold,prob_hike)"
            " VALUES (?,?,?,?,?,?)",
            (d, "2026-09-16", src, *p),
        )
    out = cal.score_fedwatch(conn, datetime(2026, 10, 1).date())
    h1 = out["1d"]
    assert h1["n"] == 1 and h1["insufficient"] and h1["realized_meetings"] == 1
    assert h1["brier"] == 0.26 and h1["mean_p_realized"] == 0.6  # .01 + .09 + .16
    assert h1["meetings"][0]["realized"] == "hike"
    assert h1["by_source"]["official"]["brier"] == 0.32  # 0 + .16 + .16
    assert out["7d"]["brier"] == 1.04  # .04 + .36 + .64
    assert out["30d"]["n"] == 0 and out["30d"]["brier"] is None


def test_history_benchmark(conn, http):
    fed = {
        "slug": "fed-cut-june",
        "question": "Will the Fed cut rates in June?",
        "volumeNum": 1e6,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0", "1"]',
        "endDate": "2026-06-17T00:00:00Z",
        "clobTokenIds": '["tok-yes", "tok-no"]',
    }
    noise = fed | {"slug": "celebrity", "question": "Will a celebrity marry?"}
    day = 86400
    end = int(_t("2026-06-17").timestamp())

    def reply(url, params):
        if url == cal.CLOB_HISTORY:
            assert params["market"] == "tok-yes"
            return {"history": [{"t": end - 30 * day, "p": 0.5}, {"t": end - day, "p": 0.1}]}
        return [fed, noise]

    http["reply"] = reply
    results, stats = cal.run(conn, history=5)
    assert stats["http"] == 2
    h = results["calibration:polymarket_hist:brier_1d"]
    assert h["n"] == 1 and h["brier"] == 0.01 and h["by_topic"]["fed"]["n"] == 1
    assert results["calibration:polymarket_hist:brier_30d"]["brier"] == 0.25
    assert results["calibration:polymarket_hist:brier_7d"]["n"] == 0  # 1d snapshot is after cut


def test_cli_command_registered(monkeypatch, tmp_path, http):
    monkeypatch.setattr(sys, "argv", ["arkwatch", "calibration", "--db", str(tmp_path / "x.db")])
    assert cli.main() == 0


def test_history_outage_keeps_well_formed_rows(conn, http):
    def boom(url, params):
        raise requests.HTTPError("422")

    http["reply"] = boom
    results, stats = cal.run(conn, history=5)
    assert stats["degraded"] and results["calibration:polymarket_hist:brier_1d"]["n"] == 0
