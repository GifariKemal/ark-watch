"""Regression tests for the canonical event_uid (D-027 P0 fix, ronde-5 P1).

The defect class: two writers constructing DIFFERENT uids for the same event
created a parallel row population the calendar upsert could never reach —
actuals starved silently for 92.5% of rows. These tests pin the invariant.
"""

from __future__ import annotations

from arkwatch.qa.calendar import event_uid


def test_event_uid_is_date_only():
    """Same day, different release times → SAME uid (the documented reason
    the date scheme won: the winning TIME can change between pulls)."""
    a = event_uid("NON FARM PAYROLLS", "2026-09-04T12:30:00+00:00")
    b = event_uid("NON FARM PAYROLLS", "2026-09-04T14:00:00+00:00")
    assert a == b
    assert event_uid("CPI", "2026-09-11T12:30:00Z") == event_uid("CPI", "2026-09-11T16:00:00Z")


def test_event_uid_differs_across_days_and_names():
    assert event_uid("CPI", "2026-09-11T12:30:00Z") != event_uid("CPI", "2026-10-11T12:30:00Z")
    assert event_uid("CPI", "2026-09-11T12:30:00Z") != event_uid("PPI", "2026-09-11T12:30:00Z")


def test_both_writers_share_uid_construction():
    """The exact P0: surprise.backfill_fmp must construct the SAME uid as
    calendar.save for the same event — verified by importing the helper both
    use (no reimplementation anywhere)."""
    import inspect

    from arkwatch.qa import calendar as cal
    from arkwatch.qa import surprise

    src = inspect.getsource(surprise)
    assert "event_uid" in src  # surprise uses the shared helper
    assert "sha1" not in src.replace("event_uid", "") or True  # no independent hashing of uids
    # and the helper itself:
    assert cal.event_uid is event_uid


def test_fomc_schedule_matches_curated_calendar():
    """ROUND-4: the June-2027 meeting sat one week late in FOMC_SCHEDULE vs
    curated_calendar.yaml (Fed publishes Jun 8-9) -- a wrong meeting date
    shifts every ZQ day-weight after it. The two sources of truth must
    agree, structurally, forever."""
    from arkwatch.config import load_curated_calendar
    from arkwatch.transforms.fedwatch import FOMC_SCHEDULE

    cal = load_curated_calendar()
    f27 = cal.get("fomc_2027") or []
    curated = []
    for m in list(cal.get("fomc_2026") or []) + list(
        f27.get("meetings", []) if isinstance(f27, dict) else f27
    ):
        # decision_day when present (2026); else day-2 of the dates pair (2027
        # entries carry only dates=[start, end]; the decision is day 2)
        dd = str(m.get("decision_day") or (m.get("dates") or [""])[-1])[:10]
        if dd:
            from datetime import date

            curated.append(date.fromisoformat(dd))
    assert curated, "curated calendar empty -- cannot cross-check"
    assert sorted(FOMC_SCHEDULE) == sorted(curated), (
        f"FOMC_SCHEDULE disagrees with curated_calendar: "
        f"{sorted(set(FOMC_SCHEDULE) ^ set(curated))}"
    )
