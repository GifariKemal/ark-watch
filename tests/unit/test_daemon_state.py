"""daemon day-state persistence — ROUND-11 regression tests.

Live defect (2026-09-19 05:26 UTC): a deploy-restart landed 64 seconds
into verify's 10-minute retry. The retry schedule is in-memory and died
with the process, while the persisted already-ran key (written
unconditionally, failure or not) cancelled the day's only automatic
recovery. Contract under test: only SUCCEEDED jobs persist/restore — a
failed job re-runs exactly once per restart."""

import json
from datetime import datetime

from arkwatch import daemon
from arkwatch.daemon import WIB, _due_jobs, _load_state, _save_state

TODAY_KEY = "0715-verify"


def _dated(key: str) -> str:
    return f"{key}@{datetime.now(WIB).date().isoformat()}"


def test_save_persists_successes_only(tmp_path):
    p = tmp_path / "daemon_state.json"
    state = {_dated(TODAY_KEY): "1", _dated("0815-cme"): "0"}
    _save_state(state, p)
    on_disk = json.loads(p.read_text())
    assert on_disk == {_dated(TODAY_KEY): "1"}


def test_load_restores_today_successes_only(tmp_path):
    p = tmp_path / "daemon_state.json"
    p.write_text(
        json.dumps(
            {
                _dated(TODAY_KEY): "1",
                _dated("0815-cme"): "0",  # failed → must NOT restore
                "0715-verify@2026-09-18": "1",  # yesterday → pruned
            }
        )
    )
    restored = _load_state(p)
    assert restored == {_dated(TODAY_KEY): "1"}


def test_failed_job_replays_after_simulated_restart(tmp_path):
    """The exact live sequence: run → fail (in-memory '0', nothing on disk
    for it) → restart → the key is absent → _due_jobs will re-run it."""
    p = tmp_path / "daemon_state.json"
    state = {}
    # scheduled run failed
    state[_dated(TODAY_KEY)] = "0"
    _save_state(state, p)
    # restart
    restored = _load_state(p)
    assert _dated(TODAY_KEY) not in restored  # → due again


def test_healed_retry_upgrades_the_day_state(tmp_path):
    p = tmp_path / "daemon_state.json"
    state = {_dated(TODAY_KEY): "0", _dated("0600-harvest"): "1"}
    # retry succeeded → run_loop upgrades then persists
    state[_dated(TODAY_KEY)] = "1"
    _save_state(state, p)
    assert _load_state(p) == {
        _dated(TODAY_KEY): "1",
        _dated("0600-harvest"): "1",
    }


def test_missing_or_corrupt_file_restores_nothing(tmp_path):
    assert _load_state(tmp_path / "absent.json") == {}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert _load_state(corrupt) == {}


def test_weekly_gdelt_retention_runs_after_daily_backup():
    now = datetime(2026, 9, 27, 23, 45, tzinfo=WIB)
    commands = [cmd for cmd, _desc, _key in _due_jobs(now, {})]

    assert commands.index("backup") < commands.index("gdelt-retention --apply")


def test_gdelt_retention_logs_complete_structured_result(monkeypatch):
    result = '{"deleted":{"gdelt_gkg":123456},"db_bytes_before":123456789}'
    logs = []
    monkeypatch.setattr(daemon, "_heartbeat", lambda: None)
    monkeypatch.setattr(daemon.logger, "info", logs.append)
    monkeypatch.setattr(daemon, "_spawn", lambda argv, timeout_s: (0, result, ""))

    assert daemon._run_job("gdelt-retention --apply", "weekly cleanup") is True
    assert f"gdelt-retention-result {result}" in logs


def test_save_state_drops_previous_days(tmp_path):
    p = tmp_path / "state.json"
    today = datetime.now(WIB).date().isoformat()
    _save_state(
        {f"0600-harvest@{today}": "1", "0600-harvest@2000-01-01": "1", "bootstrapped": "1"}, p
    )
    assert set(json.loads(p.read_text())) == {f"0600-harvest@{today}", "bootstrapped"}


# --- scheduler calendar edges (2026-10-12 audit: none had a test) ---------------------------


def test_news_cadence_follows_both_dst_calendars():
    from datetime import UTC

    fast, slow = daemon.MARKET_ACTIVE_INTERVAL_MINUTES, daemon.MARKET_NEWS_INTERVAL_MINUTES
    # 2026-10-26: London already on GMT, New York still on EDT -> NY closes 20:00 UTC
    assert daemon._market_news_interval(datetime(2026, 10, 26, 19, 30, tzinfo=UTC)) == fast
    assert daemon._market_news_interval(datetime(2026, 10, 26, 20, 30, tzinfo=UTC)) == slow
    # 2026-11-02: both on winter time -> NY closes 21:00 UTC
    assert daemon._market_news_interval(datetime(2026, 11, 2, 20, 30, tzinfo=UTC)) == fast
    # 2026-03-09: NY on EDT (since 03-08), London still GMT -> London opens 08:00 UTC
    assert daemon._market_news_interval(datetime(2026, 3, 9, 7, 30, tzinfo=UTC)) == slow
    assert daemon._market_news_interval(datetime(2026, 3, 9, 8, 0, tzinfo=UTC)) == fast
    # weekend: never the fast cadence
    assert daemon._market_news_interval(datetime(2026, 10, 24, 14, 0, tzinfo=UTC)) == slow


def test_due_jobs_catch_up_once_and_never_replay_yesterday():
    late = datetime(2026, 10, 14, 11, 0, tzinfo=WIB)  # Wednesday, daemon started late
    due = _due_jobs(late, {})
    keys = [k for _c, _d, k in due]
    assert len(keys) == len(set(keys)) and any(k.startswith("0600-harvest@") for k in keys)
    assert not _due_jobs(late, {k: "1" for k in keys})  # everything ran: nothing left
    # 00:01 the next day: yesterday's 23:30 backup is not replayed (a fresh WIB day)
    early = datetime(2026, 10, 15, 0, 1, tzinfo=WIB)
    assert not any("backup" in c for c, _d, _k in _due_jobs(early, {}))


def test_hourly_jobs_never_replay_a_past_hour():
    now = datetime(2026, 10, 14, 15, 40, tzinfo=WIB)
    hourly = [k for c, _d, k in _due_jobs(now, {}) if c == "polymarket"]
    assert hourly == ["1520-polymarket@2026-10-14"]  # this hour only, not 00:20..14:20
