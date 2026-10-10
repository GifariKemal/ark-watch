"""The LLM tone score is validated before it can reach computed_signals."""

import pytest

import arkwatch.config as config
import arkwatch.fetchers.nlp as nlp_mod
from arkwatch.qa import fedsurvey_harvest as fh


@pytest.mark.parametrize(
    ("score", "want"),
    [
        (12, 12.0),
        ("-30.5", -30.5),
        (250, 100.0),
        (-1e9, -100.0),
        ("high", None),
        (None, None),
        (True, None),
        (float("nan"), None),
        ([1], None),
    ],
)
def test_tone_score_is_numeric_and_clamped(monkeypatch, score, want):
    monkeypatch.setattr(config, "nlp_missing", lambda: None)
    monkeypatch.setattr(
        nlp_mod, "analyze_tone", lambda *_a, **_kw: {"score": score, "summary": "s"}
    )
    tone = fh._nlp_tone("text", "minutes")
    assert tone["score"] == want
    if want is None:  # treated as nlp-failed by the callers: nothing stored, retried next run
        assert tone["summary"].startswith("nlp invalid score")
