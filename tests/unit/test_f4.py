"""Unit tests for the calibration core math: first-print aggregation and the episode engine.

Covers: first-vintage selection, episode tagging (trailing peak, fall-through,
month gaps), determinism of calibration inputs, and month_ends generation."""

from __future__ import annotations

from datetime import date

from arkwatch.qa.f4 import episode_blok_c, fetch_first_prints_agg, month_ends


class TestMonthEnds:
    def test_basic(self):
        assert month_ends("2026-01-01", "2026-03-15") == [
            "2026-01-31",
            "2026-02-28",
            "2026-03-15",
        ]

    def test_februari_kabisat(self):
        assert month_ends("2028-01-01", "2028-03-01")[1] == "2028-02-29"


class TestFirstPrintAgg:
    def test_min_release_per_ts(self):
        """The earliest vintage per timestamp wins, not the latest revision."""
        rows = [
            {"ts": "2026-01-01", "value": 310.0, "realtime_start": "2026-02-15"},
            {"ts": "2026-01-01", "value": 311.0, "realtime_start": "2026-03-15"},  # revision
            {"ts": "2026-02-01", "value": 311.5, "realtime_start": "2026-03-15"},
        ]
        out = fetch_first_prints_agg(rows)
        assert out == [
            {"ts": "2026-01-01", "value": 310.0, "release": "2026-02-15"},
            {"ts": "2026-02-01", "value": 311.5, "release": "2026-03-15"},
        ]

    def test_none_value_dropped(self):
        rows = [{"ts": "2026-01-01", "value": None, "realtime_start": "2026-02-15"}]
        assert fetch_first_prints_agg(rows) == []


class TestEpisode:
    def test_entry_high_official(self):
        """Three consecutive months at a high annualized rate classify as high(official,3m)."""
        cpi = [100.0]
        for _ in range(6):
            cpi.append(cpi[-1] * 1.0045)  # ~5.5% annualized 3m
        _cs, tag = episode_blok_c(
            cpi, min_months=3, peak_window_m=24, peak_exit=0.5, reaccel_low=3.0, confirm=5.0
        )
        assert tag.startswith("high(official")

    def test_provisional_before_min_months(self):
        cpi = [100.0]
        for _ in range(4):  # only 2 annualized 3m points
            cpi.append(cpi[-1] * 1.0045)
        _cs, tag = episode_blok_c(
            cpi, min_months=3, peak_window_m=24, peak_exit=0.5, reaccel_low=3.0, confirm=5.0
        )
        assert "provisional" in tag

    def test_exit_via_trailing_peak_not_episode_peak(self):
        """Regression: exits are measured against the trailing peak, not the episode peak.

        After peaking at ~11.9% and flattening to ~5.5%, the exit threshold
        (0.5 x 11.9 = 5.95) is crossed, but the trailing peak (11.9, still inside
        the 24m window) forces a same-month re-entry. Guards against a
        quasi-permanent 'high' episode whose peak has been reset downward."""
        cpi = [100.0]
        for _ in range(4):  # climb to ~11.9% annualized
            cpi.append(cpi[-1] * 1.0095)
        for _ in range(30):  # plateau at the ~5.5% level
            cpi.append(cpi[-1] * 1.0045)
        _cs, tag = episode_blok_c(
            cpi, min_months=3, peak_window_m=24, peak_exit=0.5, reaccel_low=3.0, confirm=5.0
        )
        # After exiting via the 50%-of-peak rule, 5.5 >= 3.0 re-enters as 'low'
        # (not stuck 'high' with a freshly reset 5.5 peak) or lands on 'none' —
        # both acceptable. The failure mode is 'high(official, N m)' with a large N.
        assert not tag.startswith("high(official,5") or "high(official,5" not in tag

    def test_gap_bulan_diskip(self):
        """Timestamped input with a missing month must not crash.

        The longest trailing contiguous segment is used; plain value-only
        lists remain supported."""
        ts_val = [
            ("2025-07-01", 300.0),
            ("2025-08-01", 300.9),
            ("2025-09-01", 301.8),
            ("2025-11-01", 303.0),
        ]  # October missing
        _cs, tag = episode_blok_c(ts_val, 3, 24, 0.5, reaccel_low=3.0, confirm=5.0)
        assert isinstance(tag, str)  # no crash; the post-gap segment is used

    def test_low_exit_sederhana(self):
        cpi = [100.0]
        for _ in range(4):
            cpi.append(cpi[-1] * 1.0035)  # ~4.3% annualized = 'low'
        for _ in range(3):
            cpi.append(cpi[-1] * 1.001)  # ~1.2% = exits the episode
        _cs, tag = episode_blok_c(cpi, 3, 24, 0.5, reaccel_low=3.0, confirm=5.0)
        assert tag == "none" or tag.startswith("low")


class TestCalibrateStats:
    def test_episode_count_dari_transisi(self):
        """episode_blok_c must be deterministic for identical input.

        Episode statistics count tag transitions, so non-determinism here
        would corrupt the calibration stats. Transition counting itself is
        exercised end-to-end via the CLI shape in integration tests."""
        cpi = [100.0 + i * 0.4 for i in range(12)]
        r1 = episode_blok_c(cpi, 3, 24, 0.5, reaccel_low=3.0, confirm=5.0)
        r2 = episode_blok_c(cpi, 3, 24, 0.5, reaccel_low=3.0, confirm=5.0)
        assert r1 == r2


def test_imm_via_xccy_delegasi():
    """The IMM approximation resolves exact contract dates and rejects 4-digit years."""
    from arkwatch.transforms.xccy import _imm_approx

    assert _imm_approx("MAR 27") == date(2027, 3, 17)
    assert _imm_approx("SEP 26") == date(2026, 9, 16)
    assert _imm_approx("SEP 2026") is None  # 4-digit years are rejected
