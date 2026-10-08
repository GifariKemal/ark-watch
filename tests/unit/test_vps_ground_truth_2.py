"""VPS ground truth round 2 (2026-10-09): CFTC 403s the datacenter IP (200 via
WARP) while CME/FRED must stay direct, FMP 402 on SLV, energy before the first
harvest, first-boot bootstrap, transient SOCKS errors in the sweep."""

from __future__ import annotations

import json

import pytest

from arkwatch import daemon, db, net
from arkwatch.config import PlanLimited

PROXY = "socks5h://warp:9091"
VIA = {"http": PROXY, "https": PROXY}


@pytest.fixture()
def no_dotenv(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)


@pytest.fixture()
def proxy_env(monkeypatch):
    for k in ("ARKWATCH_PROXY", "ARKWATCH_YAHOO_PROXY", "ARKWATCH_PROXY_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# --- 1. host-allowlisted proxy ------------------------------------------------


@pytest.mark.parametrize(
    ("url", "proxied"),
    [
        ("https://query1.finance.yahoo.com/v8/finance/chart/CL=F", True),
        ("https://finance.yahoo.com/news/rssindex", True),
        ("https://publicreporting.cftc.gov/resource/72hh-3qpy.json", True),
        ("https://www.cmegroup.com/CmeWS/mvc/Settlements", False),
        ("https://api.stlouisfed.org/fred/series/observations", False),
        ("https://evilfinance.yahoo.com.attacker.net/x", False),
    ],
)
def test_proxies_for_host_allowlist(proxy_env, url, proxied):
    proxy_env.setenv("ARKWATCH_PROXY", PROXY)
    assert net.proxies_for(url) == (VIA if proxied else None)


def test_proxies_for_env_unset_is_none(proxy_env):
    assert net.proxies_for("https://publicreporting.cftc.gov/resource/x.json") is None


def test_proxies_for_legacy_env_honoured(proxy_env):
    proxy_env.setenv("ARKWATCH_YAHOO_PROXY", PROXY)
    assert net.proxies_for("https://publicreporting.cftc.gov/x") == VIA


def test_proxies_for_hosts_override(proxy_env):
    proxy_env.setenv("ARKWATCH_PROXY", PROXY)
    proxy_env.setenv("ARKWATCH_PROXY_HOSTS", "cmegroup.com")
    assert net.proxies_for("https://www.cmegroup.com/x") == VIA
    assert net.proxies_for("https://finance.yahoo.com/x") is None


def test_cot_fetch_goes_through_proxy(proxy_env):
    from arkwatch.fetchers import cot

    proxy_env.setenv("ARKWATCH_PROXY", PROXY)
    seen = {}

    class R:
        status_code = 200

        @staticmethod
        def json():
            return []

    def fake_get(url, **kw):
        seen["proxies"] = kw.get("proxies")
        return R()

    proxy_env.setattr(cot.requests, "get", fake_get)
    assert cot.fetch_cot("legacy", "088691") == []
    assert seen["proxies"] == VIA


# --- 2. FMP 402 on SLV ---------------------------------------------------------


def test_slv_402_is_plan_limited(monkeypatch):
    from arkwatch.fetchers import spdr

    class R:
        status_code = 402

    monkeypatch.setattr(spdr.requests, "get", lambda *a, **k: R())
    with pytest.raises(PlanLimited, match="plan-limited: FMP shares-float"):
        spdr.fetch_slv_shares()


# --- 3. energy before the first harvest -----------------------------------------


def _fresh(tmp_path):
    path = tmp_path / "arkwatch.db"
    db.get_conn(path, allow_init=True).close()
    return path


def test_energy_waits_for_fred_spot_on_fresh_volume(tmp_path, no_dotenv, monkeypatch, capsys):
    from arkwatch.qa import energy

    path = _fresh(tmp_path)
    monkeypatch.setattr(energy, "_front_month", lambda: pytest.fail("no network before spot"))
    assert energy.main(["--db", str(path)]) == 0
    assert "waiting for FRED spot (harvest has not run yet)" in capsys.readouterr().out
    c = db.get_conn(path)
    assert c.execute("SELECT fetcher, status FROM fetch_log").fetchall() == [("energy", "SKIPPED")]
    c.close()


def test_energy_real_failure_with_spot_present_still_raises(tmp_path, no_dotenv, monkeypatch):
    from arkwatch.qa import energy

    path = _fresh(tmp_path)
    c = db.get_conn(path)
    c.execute("PRAGMA foreign_keys=OFF")
    for sid in ("FRED:DCOILBRENTEU", "FRED:DCOILWTICO"):
        c.execute(
            "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at) "
            "VALUES (?, '2026-10-08', 70.0, 'FRED', 'x')",
            (sid,),
        )
    c.commit()
    c.close()

    def boom():
        raise energy.EnergyError("no complete same-date curve from EODHD or Yahoo")

    monkeypatch.setattr(energy, "_front_month", boom)
    with pytest.raises(energy.EnergyError):
        energy.main(["--db", str(path)])


# --- 4. first-boot bootstrap ------------------------------------------------------


@pytest.fixture()
def boot(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "DB_PATH", _fresh(tmp_path))
    monkeypatch.setattr(daemon, "STATE_PATH", tmp_path / "daemon_state.json")
    ran: list[str] = []

    def fake_run(cmd, desc):
        ran.append(cmd)
        return cmd != "harvest"  # a failing job must not abort the rest

    monkeypatch.setattr(daemon, "_run_job", fake_run)
    return ran


def test_bootstrap_runs_once_in_order(boot):
    state: dict[str, str] = {}
    daemon._bootstrap(state)
    assert boot == list(daemon.BOOTSTRAP_JOBS)
    assert "send" not in boot
    assert json.loads(daemon.STATE_PATH.read_text())["bootstrapped"] == "1"
    # restart: the marker survives the date-scoped state load
    daemon._bootstrap(daemon._load_state())
    assert boot == list(daemon.BOOTSTRAP_JOBS)


def test_bootstrap_skipped_when_db_has_data(boot):
    c = db.get_conn(daemon.DB_PATH)
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute(
        "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at) "
        "VALUES ('FRED:X', '2026-10-08', 1.0, 'FRED', 'x')"
    )
    c.commit()
    c.close()
    daemon._bootstrap({})
    assert boot == []


# --- 5. sweep retries transient errors once ----------------------------------------


def test_sweep_retries_errored_symbol_once(tmp_path, monkeypatch):
    from arkwatch.fetchers import yahoo
    from arkwatch.qa import instruments

    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)
    monkeypatch.setattr(instruments, "RETRY_PAUSE_S", 0)
    monkeypatch.setattr(
        instruments,
        "instruments",
        lambda: [{"symbol": "BTCUSD", "yahoo": "BTC-USD"}, {"symbol": "DXY", "yahoo": "DX-Y.NYB"}],
    )
    calls: list[str] = []

    def flaky(sym, start_ts):
        calls.append(sym)
        if sym == "BTC-USD" and calls.count(sym) == 1:
            raise yahoo.YahooError("SOCKSHTTPSConnectionPool: Max retries exceeded")
        return [{"ts": "2026-10-08", "close": 1.0}]

    monkeypatch.setattr(yahoo, "fetch_daily", flaky)
    out = instruments.sweep(str(_fresh(tmp_path)))
    assert out == {"BTCUSD|YAHOO": 1, "DXY|YAHOO": 1}
    assert calls == ["BTC-USD", "DX-Y.NYB", "BTC-USD"]
