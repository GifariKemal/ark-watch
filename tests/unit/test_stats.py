"""Known-value tests for arkwatch.qa.stats."""

from __future__ import annotations

import subprocess
import sys

import pytest

from arkwatch.qa import stats


def test_wilson_ci_known_values():
    lo, hi = stats.wilson_ci(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4)
    assert hi == pytest.approx(0.7634, abs=1e-4)
    lo, hi = stats.wilson_ci(0, 10)
    assert lo == 0.0
    assert hi == pytest.approx(0.2775, abs=1e-4)


def test_binom_pvalue_vs_base_rate():
    assert stats.binom_pvalue_vs_base_rate(10, 10, 0.5) == pytest.approx(0.5**10)
    assert stats.binom_pvalue_vs_base_rate(10, 10, 0.9) == pytest.approx(0.9**10)
    # at the base rate there is no evidence of an edge
    assert stats.binom_pvalue_vs_base_rate(5, 10, 0.5) > 0.5


def test_fdr_by_and_bh_known_values():
    rej, q = stats.fdr_by([0.01, 0.04])
    # BH q = [0.02, 0.04]; BY multiplies by c(2) = 1 + 1/2
    assert q == pytest.approx([0.03, 0.06])
    assert list(rej) == [True, False]
    _, q_bh = stats.fdr_by([0.01, 0.04], method="fdr_bh")
    assert q_bh == pytest.approx([0.02, 0.04])


def test_effective_n_design_effect():
    # perfectly correlated within session -> one obs per session
    assert stats.effective_n([1, 1, 0, 0], ["a", "a", "b", "b"]) == pytest.approx(2.0)
    # no within-session correlation -> no deflation (clipped to n)
    assert stats.effective_n([1, 0, 1, 0], ["a", "a", "b", "b"]) == pytest.approx(4.0)
    # singleton clusters are independent
    assert stats.effective_n([1, 0, 1], ["a", "b", "c"]) == pytest.approx(3.0)


def test_cluster_bootstrap_ci():
    assert stats.cluster_bootstrap_ci([2.0, 2.0, 2.0], ["a", "b", "b"]) == (2.0, 2.0)
    vals = [1.0, -1.0, 2.0, -1.0, 1.5, -1.0]
    cl = ["a", "a", "b", "c", "c", "d"]
    ci = stats.cluster_bootstrap_ci(vals, cl, seed=7)
    assert ci == stats.cluster_bootstrap_ci(vals, cl, seed=7)  # deterministic
    assert ci[0] < sum(vals) / len(vals) < ci[1]


def test_api_server_import_skips_scipy_statsmodels():
    # the API reaches qa.stats via the scorecard; the heavy libs load lazily
    code = (
        "import sys, arkwatch.server.app; "
        "assert not {'scipy', 'statsmodels'} & set(sys.modules), 'heavy import at startup'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
