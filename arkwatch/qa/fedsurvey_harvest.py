"""fedsurvey_harvest.py — scheduled harvest of ALL federalreserve.gov surveys + reports.

One job covers: SLOOS, Beige Book, SCOOS, FSR, FOMC minutes (NLP),
FOMC press conference (NLP). Each source checks for new data vs the stored
latest — 404s and "no new data" are INFO (not ERROR): a quarterly source
will 404 or return unchanged data on ~95% of days, by design.

Release calendar (for reference):
  SLOOS: quarterly (Feb/May/Aug/Nov, ~2wk after quarter end)
  Beige Book: 8x/year (~2wk before each FOMC)
  SCOOS: quarterly (similar to SLOOS)
  FSR: semi-annual (Apr/Nov)
  Minutes: 8x/year (+3 weeks after FOMC)
  Press conf: 8x/year (same day as FOMC, PDF appears ~2hr later)

NLP-outage resilience (2026-09-19 live lesson: a missing server-side key made
ALL 6 sources "error" while exit-0 marked the job healthy): the tone row is the
completion marker. If the NLP call fails, structural metrics (SLOOS bias,
minutes dissent) still land, the tone row does NOT — so _latest_stored still
sees the source as pending and the next day's run retries the analysis
automatically. Any error → exit 1 → the daemon's dated alert pages the owner.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import db
from ..fetchers import fedsurvey

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"


def _latest_stored(conn, kind: str) -> str:
    """Latest ts whose NLP tone row was STORED (value non-NULL) for a kind.

    Tone rows are the completion marker: a bias-only row (structural metrics
    stored, NLP failed) must NOT advance this — the source stays "pending"
    and tomorrow's run retries the tone analysis."""
    row = conn.execute(
        "SELECT MAX(ts) FROM computed_signals WHERE signal_id = ? AND value IS NOT NULL",
        (f"fedsurvey_{kind}_tone",),
    ).fetchone()
    return row[0] if row and row[0] else ""


def _store_row(conn, signal_id: str, ts: str, value, note: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO computed_signals(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (signal_id, ts, "fedsurvey", now, value, note[:200] if note else None, None),
    )
    conn.commit()


def _bias_value(metrics: dict) -> int:
    """Structural bias → +1/0/-1. The two vocabularies are the same axis:
    SLOOS says tightening/easing, minutes dissent says hawkish/dovish —
    without the synonym map a 'hawkish' dissent stored as 0 (live 2026-09-19)."""
    bias = (metrics.get("net_bias") or "").lower()
    if bias in ("tightening", "hawkish"):
        return 1
    if bias in ("easing", "dovish"):
        return -1
    return 0


def _check_new(available_ts: str, stored_ts: str) -> bool:
    """True if the available data is newer than what we've stored."""
    if not stored_ts:
        return True  # never harvested
    return available_ts > stored_ts


def _nlp_tone(text: str, source_type: str) -> dict:
    """analyze_tone, but an NLP outage degrades to {'score': None, ...} instead
    of raising — the caller keeps the structural data and retries tomorrow."""
    from ..config import nlp_missing

    if skip := nlp_missing():  # no key at all = SKIPPED, not an outage
        return {"score": None, "summary": skip}
    try:
        from ..fetchers.nlp import analyze_tone

        return analyze_tone(text, source_type=source_type)
    except Exception as ex:
        return {"score": None, "summary": f"nlp unavailable: {str(ex)[:80]}"}


def _nlp_failed(ts: str, tone: dict, stored: str = "") -> str:
    """Status line for a tone that did not land: 'skipped' when NLP is
    unconfigured (exit 0), 'nlp-failed' otherwise (pages, retried next run)."""
    why = tone.get("summary", "")
    if why.startswith("unconfigured"):
        return f"skipped @ {ts} ({why}{stored})"
    return f"nlp-failed @ {ts} ({why[:60]}{stored}) — retry next run"


def _harvest_text_source(conn, kind: str, fetch_fn, source_type: str) -> tuple[str, bool]:
    """Shared path for the 4 fetch-a-page sources (SLOOS/BeigeBook/SCOOS/FSR).

    Returns (status_line, is_new). On NLP failure: structural metrics (SLOOS
    only) still stored, tone row not — retry happens next run."""
    data = fetch_fn()
    stored = _latest_stored(conn, kind)
    if not _check_new(data["ts"], stored):
        return f"unchanged (stored {stored})", False

    metrics = fedsurvey.sloos_tone_metrics(data["text"]) if kind == "sloos" else {}
    tone = _nlp_tone(data["text"], source_type)
    if tone.get("score") is None:
        if metrics:
            _store_row(
                conn,
                f"fedsurvey_{kind}_bias",
                data["ts"],
                _bias_value(metrics),
                metrics.get("net_bias", ""),
            )
        return _nlp_failed(data["ts"], tone), False

    _store_row(
        conn, f"fedsurvey_{kind}_tone", data["ts"], tone.get("score"), tone.get("summary", "")
    )
    if metrics:
        _store_row(
            conn,
            f"fedsurvey_{kind}_bias",
            data["ts"],
            _bias_value(metrics),
            metrics.get("net_bias", ""),
        )
    bias = f", bias={metrics.get('net_bias')}" if metrics else ""
    return f"new @ {data['ts']} (tone={tone.get('score')}{bias})", True


def harvest_all(conn) -> dict[str, str]:
    """Check every Fed survey/report for new data; fetch + NLP + store if new.

    Returns {source: status} — statuses starting with 'new' are harvests,
    'unchanged'/'nlp-failed'/'error: ...' are logged per-source.
    """
    from .fetch_log import log_collection

    out: dict[str, str] = {}
    n_new = 0

    # --- 4 page-fetch sources (shared path) ---
    text_sources = [
        ("sloos", fedsurvey.fetch_sloos, "sloos"),
        ("beige_book", fedsurvey.fetch_beige_book, "beige book"),
        ("scoos", fedsurvey.fetch_scoos, "scoos"),
        ("fsr", fedsurvey.fetch_fsr, "financial stability report"),
    ]
    for kind, fetch_fn, source_type in text_sources:
        try:
            status, is_new = _harvest_text_source(conn, kind, fetch_fn, source_type)
            out[kind] = status
            n_new += int(is_new)
        except Exception as ex:
            out[kind] = f"error: {str(ex)[:80]}"

    # --- FOMC Minutes (NLP) ---
    try:
        from ..fetchers.minutes import minutes_dates, parse_minutes

        latest = minutes_dates()[0]  # newest available
        stored = _latest_stored(conn, "minutes")
        # minutes ts = meeting date (release is +3wk but URL uses meeting date)
        if _check_new(latest, stored):
            p = parse_minutes(latest)
            tone = _nlp_tone(p["full_text"], "minutes")
            dissent_metrics = {"net_bias": p["dissent_direction"] or "none"}
            if tone.get("score") is None:
                _store_row(
                    conn,
                    "fedsurvey_minutes_bias",
                    latest,
                    _bias_value(dissent_metrics),
                    f"dissent={p['dissent_count']} {p['dissent_direction'] or 'none'}",
                )
                out["minutes"] = _nlp_failed(latest, tone, f"; dissent={p['dissent_count']} stored")
            else:
                _store_row(
                    conn,
                    "fedsurvey_minutes_tone",
                    latest,
                    tone.get("score"),
                    tone.get("summary", ""),
                )
                _store_row(
                    conn,
                    "fedsurvey_minutes_bias",
                    latest,
                    _bias_value(dissent_metrics),
                    f"dissent={p['dissent_count']} {p['dissent_direction'] or 'none'}",
                )
                out["minutes"] = (
                    f"new @ {latest} (tone={tone.get('score')}, dissent={p['dissent_count']})"
                )
                n_new += 1
        else:
            out["minutes"] = f"unchanged (stored {stored})"
    except Exception as ex:
        out["minutes"] = f"error: {str(ex)[:80]}"

    # --- FOMC Press Conference (NLP) ---
    try:
        from ..fetchers.pressconf import available_dates, fetch_transcript_text

        latest_pc = available_dates()[0]
        stored = _latest_stored(conn, "pressconf")
        if _check_new(latest_pc, stored):
            text = fetch_transcript_text(latest_pc)
            tone = _nlp_tone(text, "press conference")
            if tone.get("score") is None:
                out["pressconf"] = _nlp_failed(latest_pc, tone)
            else:
                _store_row(
                    conn,
                    "fedsurvey_pressconf_tone",
                    latest_pc,
                    tone.get("score"),
                    tone.get("summary", ""),
                )
                out["pressconf"] = f"new @ {latest_pc} (tone={tone.get('score')})"
                n_new += 1
        else:
            out["pressconf"] = f"unchanged (stored {stored})"
    except Exception as ex:
        out["pressconf"] = f"error: {str(ex)[:80]}"

    n_err = sum(1 for v in out.values() if v.startswith(("error:", "nlp-failed")))
    log_collection(
        conn,
        "fedsurvey",
        "FEDSURVEY:ALL",
        None,
        n_new,
        err=None if n_err == 0 else f"{n_err}/{len(out)} sources failed",
        # a checked-and-unchanged day is HEALTHY, not EMPTY (audit round-2:
        # the EMPTY badge belongs to fetches that returned zero observations)
        status="OK" if n_err == 0 else None,
    )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch fedsurvey")
    p.add_argument("--db", default=str(DEFAULT_DB))
    a = p.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    conn = db.get_conn(a.db, allow_init=True)
    print("=== Fed Surveys harvest (SLOOS + Beige Book + SCOOS + FSR + Minutes + Press Conf) ===")
    results = harvest_all(conn)
    # errors LAST: the daemon's success summary keeps the tail lines, so a sick
    # source must not be scrolled away by healthy ones
    for source, status in sorted(
        results.items(), key=lambda kv: kv[1].startswith(("error:", "nlp-failed"))
    ):
        print(f"  {source:12s} {status}")
    n_new = sum(1 for v in results.values() if v.startswith("new"))
    n_err = sum(1 for v in results.values() if v.startswith(("error:", "nlp-failed")))
    print(f"=== done: {n_new} new, {len(results) - n_new - n_err} unchanged, {n_err} errors ===")
    conn.close()
    # any failure (fetch or NLP) must page via the daemon's exit-code contract —
    # 2026-09-19: 6/6 errors exited 0 and the missing key rotted silently
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
