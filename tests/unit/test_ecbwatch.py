"""Tests for ESTRWatch (D-006) — ECB hike/cut/hold probabilities from ESR.

Hand-computed cases for every convention the transform encodes:
third-WEDNESDAY reference quarters, Wednesday implementation dates, the
day-weighted min-norm solve (structurally rank-deficient: two meetings per
quarter), and the official CME characteristic/mantissa 25bp grid.
"""

from __future__ import annotations

from datetime import date

import pytest

from arkwatch.transforms import ecbwatch as ew

# Production strip 2026-09-09 (cme_settlements product 10247) — regression
# fixture; anchor = €STR fixing 2.189 (2026-09-09), DFR 2.25.
_PROD_STRIP = {
    "AUG 26": 97.6225,
    "DEC 26": 97.2875,
    "DEC 27": 96.9175,
    "DEC 28": 96.9375,
    "JUL 26": 97.715,
    "JUN 26": 97.8075,
    "JUN 27": 96.9575,
    "JUN 28": 96.94,
    "JUN 29": 96.9175,
    "MAR 27": 97.0775,
    "MAR 28": 96.9275,
    "MAR 29": 96.9125,
    "NOV 26": 98.585,
    "OCT 26": 98.055,
    "SEP 26": 97.525,
    "SEP 27": 96.91,
    "SEP 28": 96.93,
}


# --- calendar --------------------------------------------------------------


def test_third_wednesday():
    assert ew._third_wednesday(2026, 9) == date(2026, 9, 16)
    assert ew._third_wednesday(2026, 12) == date(2026, 12, 16)
    assert ew._third_wednesday(2027, 3) == date(2027, 3, 17)
    assert ew._third_wednesday(2027, 12) == date(2027, 12, 15)
    assert ew._third_wednesday(2029, 6) == date(2029, 6, 20)
    # REVIEW-CAUGHT (P1): months starting on a THURSDAY — day 14 is itself
    # a Wednesday, so a walk starting at day 14 returns the SECOND one
    assert ew._third_wednesday(2028, 6) == date(2028, 6, 21)  # Jun-1-2028 = Thu
    assert ew._third_wednesday(2029, 3) == date(2029, 3, 21)  # Mar-1-2029 = Thu
    assert ew._third_wednesday(2033, 9) == date(2033, 9, 21)  # Sep-1-2033 = Thu


def test_reference_quarter():
    # 'SEP 26' (cme_settlements.month has a space) → [3rd Wed Sep, 3rd Wed Dec)
    assert ew.reference_quarter("SEP 26") == (date(2026, 9, 16), date(2026, 12, 16))
    assert ew.reference_quarter("JUN29") == (date(2029, 6, 20), date(2029, 9, 19))
    # the Thursday-start months resolve correctly end-to-end too
    assert ew.reference_quarter("MAR 28") == (date(2028, 3, 15), date(2028, 6, 21))
    assert ew.reference_quarter("JUN 28") == (date(2028, 6, 21), date(2028, 9, 20))
    # serial months are excluded outright
    assert ew.reference_quarter("OCT 26") is None
    assert ew.reference_quarter("garbage") is None


def test_implementation_date_first_wednesday_after():
    assert ew.implementation_date(date(2026, 9, 10)) == date(2026, 9, 16)
    assert ew.implementation_date(date(2026, 10, 29)) == date(2026, 11, 4)
    assert ew.implementation_date(date(2026, 12, 17)) == date(2026, 12, 23)
    # 2027-04-29 (Thu) → Wednesday May 5, NOT May 6 (the design draft's typo)
    assert ew.implementation_date(date(2027, 4, 29)) == date(2027, 5, 5)


# --- the solve -------------------------------------------------------------


def test_single_meeting_full_weight_reduces_to_fedwatch_algebra():
    """One meeting implemented BEFORE the front window start → coefficient
    1.0 → plain delta = F − anchor (the fedwatch running-rate reduction)."""
    rows, diag = ew.compute(
        {"MAR 27": 97.90, "JUN 27": 97.90},  # F = 2.10 both
        estr=2.00,
        estr_asof=date(2026, 10, 1),
        fixings=None,
        dfr=2.25,
        decisions=[date(2026, 12, 17)],  # impl 12-23 < MAR27 start 03-17
    )
    assert diag["rms_bp"] < 0.1
    r = rows[0]
    assert r.exact is True
    assert r.delta_bp == pytest.approx(10.0, abs=0.05)  # 2.10 − 2.00
    assert r.prob_hike == pytest.approx(10 / 25, abs=1e-3)
    assert r.prob_hold == pytest.approx(1 - 10 / 25, abs=1e-3)
    assert r.implied_rate == pytest.approx(2.35, abs=1e-3)  # dfr 2.25 + 0.10


def test_meeting_inside_window_day_weighted():
    """impl 2026-11-04 inside SEP26 [09-16, 12-16): weight = 42/91.
    Consistent two-equation system recovers the weighted delta exactly."""
    delta = 0.045 * 91 / 42  # the delta that makes SEP26 F = 2.045
    rows, diag = ew.compute(
        {"SEP 26": 100 - 2.045, "DEC 26": 100 - (2.00 + delta)},
        estr=2.00,
        estr_asof=date(2026, 10, 1),
        decisions=[date(2026, 10, 29)],  # impl 11-04
    )
    assert diag["rms_bp"] < 0.1
    r = rows[0]
    assert r.delta_bp == pytest.approx(delta * 100, abs=0.05)
    assert r.prob_hike == pytest.approx(delta / 0.25, abs=1e-3)
    assert r.exact is True  # sole unknown in its window


def test_two_meetings_one_window_joint_recover():
    """DEC26 window [12-16, 03-17) N=91 holds impl 12-23 (w=84/91) and
    02-10 (w=35/91). A consistent 2×2 system must recover both jumps
    exactly — the joint solve is what a greedy per-month split cannot do."""
    d_a, d_b = 0.25, 0.10
    f1 = 2.00 + d_a * 84 / 91 + d_b * 35 / 91
    f2 = 2.00 + d_a + d_b
    rows, diag = ew.compute(
        {"DEC 26": 100 - f1, "MAR 27": 100 - f2},
        estr=2.00,
        estr_asof=date(2026, 10, 1),
        decisions=[date(2026, 12, 17), date(2027, 2, 4)],
    )
    assert diag["rms_bp"] < 0.1
    assert rows[0].delta_bp == pytest.approx(25.0, abs=0.05)
    assert rows[1].delta_bp == pytest.approx(10.0, abs=0.05)
    # both share the window → not exact, noise amp = 1/|w1−w2| = 91/49
    assert rows[0].exact is False and rows[1].exact is False
    assert rows[0].noise_amp == pytest.approx(91 / 49, abs=0.01)
    # probabilities: 25bp → hike 100%; 10bp → hike 40%
    assert rows[0].prob_hike == pytest.approx(1.0, abs=1e-3)
    assert rows[1].prob_hike == pytest.approx(0.4, abs=1e-3)


def test_greater_than_25bp_characteristic_mantissa():
    """Official CME convention: E=1.6 → P(25bp)=0.4, P(50bp)=0.6 → the DB
    hike scalar is 1.0 with the split in sizes; E=2.9 → P(75)=0.9/P(50)=0.1."""
    rows, _ = ew.compute(
        {"MAR 27": 100 - 2.40, "JUN 27": 100 - 2.40},  # δ = 40bp
        estr=2.00,
        estr_asof=date(2026, 10, 1),
        decisions=[date(2026, 12, 17)],
    )
    r = rows[0]
    assert r.prob_hike == pytest.approx(1.0, abs=1e-6)
    assert r.sizes == {"25": pytest.approx(0.4, abs=1e-3), "50": pytest.approx(0.6, abs=1e-3)}


def test_front_window_running_accrual():
    """Anchor inside the front window: realized days enter via c_k; the
    remaining-days weight is the running-rate logic on the tail."""
    fixings = {date(2026, 9, d): 2.00 for d in range(16, 24)}  # 8 realized days
    delta = 0.25
    f_sep = 2.00 + delta * 42 / 91  # impl 11-04, remaining 42/91 of window
    rows, diag = ew.compute(
        {"SEP 26": 100 - f_sep, "DEC 26": 100 - (2.00 + delta)},
        estr=2.00,
        estr_asof=date(2026, 9, 23),
        fixings=fixings,
        decisions=[date(2026, 10, 29)],
    )
    assert diag["rms_bp"] < 0.1
    assert rows[0].delta_bp == pytest.approx(25.0, abs=0.05)
    assert rows[0].prob_hike == pytest.approx(1.0, abs=1e-6)


# --- degenerate ------------------------------------------------------------


def test_empty_and_serial_only_and_expired():
    assert ew.compute({}, 2.0, date(2026, 9, 9))[0] == []
    assert ew.compute({"OCT 26": 98.055, "NOV 26": 98.585}, 2.0, date(2026, 9, 9))[0] == []
    # everything expired (asof beyond all windows)
    rows, diag = ew.compute({"SEP 26": 97.5}, 2.0, date(2027, 1, 1))
    assert rows == [] and diag["n_contracts"] == 0


def test_meeting_beyond_last_contract_flagged():
    rows, diag = ew.compute(
        {"SEP 26": 97.525, "DEC 26": 97.2875},
        estr=2.189,
        estr_asof=date(2026, 9, 9),
        decisions=[date(2026, 10, 29), date(2027, 12, 16)],
    )
    assert "2027-12-16" in diag["beyond_horizon"]
    meet = [r.meeting_date.isoformat() for r in rows]
    assert "2027-12-16" not in meet


# --- normalization + production regression ----------------------------------


def test_probabilities_sum_to_one_everywhere():
    for strip, estr, asof, decs in (
        (_PROD_STRIP, 2.189, date(2026, 9, 9), None),
        (
            {"DEC 26": 97.73, "MAR 27": 97.65},
            2.00,
            date(2026, 10, 1),
            [date(2026, 12, 17), date(2027, 2, 4)],
        ),
    ):
        rows, _ = ew.compute(strip, estr=estr, estr_asof=asof, decisions=decs)
        for r in rows:
            assert r.prob_ease + r.prob_hold + r.prob_hike == pytest.approx(1.0, abs=1e-6)


def test_production_strip_regression():
    """Fixture = the live 2026-09-09 strip. Gates: fit ≤3bp, fitted SEP26/
    DEC26 match the ecb_path implied levels, front probabilities in the
    observed range (Sep-10 ≈85% hike; Oct-29 ≈64% under min-norm)."""
    rows, diag = ew.compute(_PROD_STRIP, estr=2.189, estr_asof=date(2026, 9, 9), dfr=2.25)
    assert diag["rms_bp"] <= 3.0
    assert "null_space_split" in diag["flags"]  # 8 meetings vs 4 quarters
    assert diag["fitted"]["SEP 26"] == pytest.approx(2.475, abs=0.005)
    assert diag["fitted"]["DEC 26"] == pytest.approx(2.7125, abs=0.005)
    assert diag["basis_bp"] == pytest.approx(-6.1, abs=0.1)
    assert rows[0].meeting_date == date(2026, 9, 10)
    assert 0.75 <= rows[0].prob_hike <= 0.95
    assert 0.50 <= rows[1].prob_hike <= 0.75  # Oct-29
    assert rows[0].implied_rate == pytest.approx(2.46, abs=0.02)


def test_format_brief_next_meeting_only():
    rows, diag = ew.compute(_PROD_STRIP, estr=2.189, estr_asof=date(2026, 9, 9), dfr=2.25)
    txt = ew.format_brief(rows, diag, asof="2026-09-09", today=date(2026, 9, 9))
    # next meeting (Sep-10), never the far-future 'exact' row (Dec-2027)
    assert txt.startswith("ECBWatch hike")
    assert "Sep-10" in txt and "DFR 2.46%" in txt
    # review round-1 honesty markers: Sep-10 shares its quarter → '≈';
    # the signed delta is always shown; the ESR strip date rides along
    assert "≈85%" in txt and "+21bp" in txt and "ESR 09-09" in txt
    assert "Dec-16" not in txt


def test_format_brief_exact_has_no_approx_marker():
    rows, diag = ew.compute(
        {"MAR 27": 97.90, "JUN 27": 97.90},
        estr=2.00,
        estr_asof=date(2026, 10, 1),
        decisions=[date(2026, 12, 17)],
    )
    txt = ew.format_brief(rows, diag, today=date(2026, 10, 1))
    assert "≈" not in txt and "+10bp" in txt


def test_format_brief_degraded_tail():
    rows, diag = ew.compute(_PROD_STRIP, estr=2.189, estr_asof=date(2026, 9, 9), dfr=2.25)
    diag = dict(diag, flags=diag["flags"] + ["degraded"], rms_bp=4.1)
    assert "⚠ fit 4.1bp" in ew.format_brief(rows, diag, today=date(2026, 9, 9))


def test_format_brief_skips_decided_meeting():
    """2026-09-17 incident: during the €STR fixing lag (decision Thu →
    implementation Wed → first reflecting fixing) a just-decided meeting is
    still an unknown in the solve, and rows[0] displayed the DECIDED Sep-10
    hike as a forward 'hike ≈93%' call. Display must show the next UNDECIDED
    meeting; the decided one still anchors the cumulative DFR level."""
    rows, diag = ew.compute(_PROD_STRIP, estr=2.189, estr_asof=date(2026, 9, 15), dfr=2.25)
    # solve-side sanity: Sep-10 IS in the rows (impl 09-16 > anchor 09-15)…
    assert rows[0].meeting_date == date(2026, 9, 10)
    # …but the brief (clock = 09-17) must skip it
    txt = ew.format_brief(rows, diag, asof="2026-09-15", today=date(2026, 9, 17))
    assert txt is not None
    assert "Sep-10" not in txt
    assert "Oct-29" in txt


def test_outlier_settlement_dropped():
    """A corrupt settle (implied outside sanity bounds) must not enter the
    solve — the RMS gate cannot catch self-consistent garbage (review P2)."""
    strip = dict(_PROD_STRIP)
    strip["SEP 27"] = 150.0  # implied −50% → impossible
    rows, diag = ew.compute(strip, estr=2.189, estr_asof=date(2026, 9, 9), dfr=2.25)
    assert "outlier_dropped" in diag["flags"]
    assert "SEP 27" not in diag["fitted"]
    # the solvable system still runs
    assert rows and diag["rms_bp"] <= 3.0
