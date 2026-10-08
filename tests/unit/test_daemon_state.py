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
