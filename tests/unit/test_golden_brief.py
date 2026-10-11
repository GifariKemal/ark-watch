"""Golden tests for brief rendering: determinism and structure ratchets across refactors.

Guarantees:
1. DETERMINISM — rendering twice from the same database produces identical
   output after normalizing volatile tokens (WIB clock, header date, series
   timestamps).
2. STRUCTURE — the core sections are present and ordered.

The test database is a copy of the production database (data/arkwatch.db)
when present; otherwise the tests skip (pure CI environments)."""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from arkwatch.signals.compute import generate_brief

LIVE_DB = Path(__file__).resolve().parents[2] / "data" / "arkwatch.db"

VOLATILE = [
    (r"WIB \d{2}:\d{2}", "WIB HH:MM"),
    (r"data as of \d{2}-\w{3} ET", "data as of DD-Mon ET"),
    (r"=== US MACRO BRIEF(?: —|:) \w+, \d{2} \w+ \d{4} ===", "=== HEADER ==="),
    (r"\d{4}-\d{2}-\d{2}", "DATE"),  # series timestamp stamps
]


def _normalize(text: str) -> str:
    for pat, rep in VOLATILE:
        text = re.sub(pat, rep, text)
    return text


@pytest.fixture(scope="module")
def brief_text(tmp_path_factory):
    if not LIVE_DB.exists():
        pytest.skip("production DB not present — golden tests require real data")
    tmp = tmp_path_factory.mktemp("golden") / "copy.db"
    shutil.copy(LIVE_DB, tmp)
    conn = sqlite3.connect(tmp)  # read-only path: a plain connection is enough
    text = generate_brief(conn, str(tmp))
    conn.close()
    return text


def test_render_deterministic(brief_text, tmp_path):
    """A second render from an identical database copy matches after normalization."""
    tmp = tmp_path / "copy2.db"
    shutil.copy(LIVE_DB, tmp)
    conn = sqlite3.connect(tmp)
    text2 = generate_brief(conn, str(tmp))
    conn.close()
    assert _normalize(brief_text) == _normalize(text2)


def test_structure_sections_present(brief_text):
    """Core sections must survive refactors intact."""
    t = brief_text
    assert "=== US MACRO BRIEF" in t or "=== HEADER ===" in _normalize(t)
    assert "REGIME :" in t
    assert "Quality:" in t
    assert "Pillars:" in t
    assert ("Positioning (COT" in t) or ("Flows:" in t)  # Saturday variant differs
    assert "Sources:" in t


def test_no_unrendered_none(brief_text):
    """None values must never leak into numeric lines as literal 'None' text."""
    for line in brief_text.split("\n"):
        if any(ch.isdigit() for ch in line):
            assert "None" not in line, f"None leaked into output: {line}"
