"""Regression tests for message splitting and IMM date approximation.

Guards against: an IndexError when the split boundary falls outside <pre>
blocks, unbalanced <pre> tags across split chunks, and the IMM off-by-one
that produced the second Monday instead of the third."""

from __future__ import annotations

from datetime import date

from arkwatch.senders.telegram import _split_message
from arkwatch.transforms.xccy import _imm_approx


class TestSplitMessage:
    def test_long_text_without_pre_no_crash(self):
        """Long text without any <pre> block: previously an IndexError at the split boundary."""
        text = "\n".join(f"line {i} " + "x" * 60 for i in range(120))
        assert len(text) > 4000
        parts = _split_message(text)
        assert len(parts) >= 2
        for p in parts:
            assert len(p) < 4000
        # content preserved (no lines lost)
        assert sum(p.count("line") for p in parts) == 120

    def test_short_passthrough(self):
        assert _split_message("short") == ["short"]

    def test_pre_block_reopened(self):
        """A <pre> block cut by the split is closed and reopened in the next chunk."""
        pre = "<pre>\n" + "\n".join(f"r{i} " + "y" * 60 for i in range(90)) + "\n</pre>"
        text = "header\n\n" + pre
        parts = _split_message(text)
        assert len(parts) >= 2
        for p in parts:
            assert p.count("<pre>") == p.count("</pre>")  # balanced in every chunk


class TestImmThirdWednesday:
    def test_sep26_is_21(self):
        # the 6E delivery / SR3-ESR reference date (was the third Monday, 2 days early)
        assert _imm_approx("SEP 26") == date(2026, 9, 16)

    def test_always_15_to_21(self):
        for _m, name in ((3, "MAR"), (6, "JUN"), (9, "SEP"), (12, "DEC")):
            d = _imm_approx(f"{name} 27")
            assert 15 <= d.day <= 21 and d.weekday() == 2, (name, d)

    def test_reject_4digit_year(self):
        assert _imm_approx("SEP 2026") is None
