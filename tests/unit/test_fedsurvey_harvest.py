"""Offline unit tests for the fedsurvey harvest resilience contract.

The 2026-09-19 live lesson this file pins down: a missing server-side NLP key
made ALL 6 sources error while the job exited 0 (marked healthy, no alert).
The contract now under test:
  - NLP outage must NOT lose the data point: structural rows (SLOOS bias,
    minutes dissent) still land; the tone row does NOT — so the next run
    re-detects the source as pending and retries the analysis.
  - any failing source (fetch or NLP) → main() exits 1 → the daemon pages.

No network: all fetchers + the NLP call are monkeypatched.
"""

from __future__ import annotations

from arkwatch import db
from arkwatch.fetchers import fedsurvey, minutes, pressconf
from arkwatch.qa import fedsurvey_harvest as fh

TONE_OK = {"score": 12.0, "summary": "modestly hawkish", "key_concerns": ["inflation"]}


def _seed(monkeypatch, nlp_script: list) -> list:
    """Patch the 6 sources + analyze_tone with offline fixtures.

    nlp_script: per-call results — an Exception instance is raised, a dict is
    returned. Returns the list of (source_type) call labels made."""
    calls: list[str] = []

    monkeypatch.setattr(
        fedsurvey, "fetch_sloos", lambda: {"text": "banks tightened standards", "ts": "2026-08-01"}
    )
    monkeypatch.setattr(
        fedsurvey,
        "fetch_beige_book",
        lambda: {"text": "economic activity increased slightly", "ts": "2026-09-01"},
    )
    monkeypatch.setattr(
        fedsurvey,
        "fetch_scoos",
        lambda: {"text": "credit terms roughly unchanged", "ts": "2026-06-01"},
    )
    monkeypatch.setattr(
        fedsurvey,
        "fetch_fsr",
        lambda: {"text": "financial system remained resilient", "ts": "2026-04-11"},
    )
    monkeypatch.setattr(minutes, "minutes_dates", lambda: ["2026-07-29"])
    monkeypatch.setattr(
        minutes,
        "parse_minutes",
        lambda d: {
            "full_text": "participants discussed risks",
            "dissent_count": 1,
            "dissent_direction": "hawkish",
        },
    )
    monkeypatch.setattr(pressconf, "available_dates", lambda: ["2026-07-29"])
    monkeypatch.setattr(pressconf, "fetch_transcript_text", lambda d: "chair opening remarks")

    import arkwatch.fetchers.nlp as nlp_mod

    def fake_tone(text: str, source_type: str = "minutes") -> dict:
        calls.append(source_type)
        result = nlp_script.pop(0) if nlp_script else dict(TONE_OK)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(nlp_mod, "analyze_tone", fake_tone)
    return calls


def _row(conn, signal_id: str):
    return conn.execute(
        "SELECT value FROM computed_signals WHERE signal_id = ?", (signal_id,)
    ).fetchone()


class TestNlpOutageResilience:
    def test_outage_keeps_structural_rows_and_retries_next_run(self, monkeypatch, tmp_path):
        """Run 1 (NLP down): SLOOS bias + minutes dissent land, no tone rows.
        Run 2 (NLP up): tone rows land for all six — nothing was lost."""
        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        _seed(monkeypatch, [RuntimeError("NLP_API_KEY not set")] * 6)

        out = fh.harvest_all(conn)
        assert out["sloos"].startswith("nlp-failed")
        assert out["minutes"].startswith("nlp-failed")
        assert out["pressconf"].startswith("nlp-failed")
        # structural survived the outage
        assert _row(conn, "fedsurvey_sloos_bias")[0] == 1  # tightened > eased
        assert _row(conn, "fedsurvey_minutes_bias")[0] == 1  # hawkish dissent = +1, not 0
        # tone rows must NOT exist — they are the completion marker
        assert _row(conn, "fedsurvey_sloos_tone") is None
        assert _row(conn, "fedsurvey_minutes_tone") is None

        out2 = fh.harvest_all(conn)  # NLP recovered
        assert out2["sloos"].startswith("new")
        assert out2["minutes"].startswith("new")
        assert _row(conn, "fedsurvey_sloos_tone")[0] == 12.0
        assert _row(conn, "fedsurvey_minutes_tone") is not None
        conn.close()

    def test_unchanged_run_makes_no_nlp_calls(self, monkeypatch, tmp_path):
        """After a successful harvest, a same-ts rerun is 'unchanged' and must
        not burn NLP tokens (the ~95%-of-days path)."""
        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        calls = _seed(monkeypatch, [dict(TONE_OK)] * 6)

        fh.harvest_all(conn)
        assert len(calls) == 6
        out = fh.harvest_all(conn)
        assert len(calls) == 6  # no new calls
        assert all(v.startswith("unchanged") for v in out.values())
        conn.close()


class TestExitCodeContract:
    def test_any_failure_exits_nonzero(self, monkeypatch, tmp_path, capsys):
        """A sick source must fail the job (daemon alert) — exit 0 on 6/6
        errors was the silent-rot bug."""
        conn_path = tmp_path / "t.db"
        _seed(monkeypatch, [])
        monkeypatch.setattr(
            fedsurvey, "fetch_fsr", lambda: (_ for _ in ()).throw(RuntimeError("HTTP 404"))
        )

        rc = fh.main(["--db", str(conn_path)])
        assert rc == 1
        assert "error" in capsys.readouterr().out

    def test_all_unchanged_exits_zero(self, monkeypatch, tmp_path):
        """The normal no-news day must stay exit 0 (no page)."""
        _seed(monkeypatch, [])
        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        fh.harvest_all(conn)
        conn.close()
        rc = fh.main(["--db", str(tmp_path / "t.db")])
        assert rc == 0
