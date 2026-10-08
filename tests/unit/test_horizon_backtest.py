from arkwatch.qa import horizon_backtest


def test_hypothesis_matrix_generation_and_fdr():
    hypos = horizon_backtest.generate_100_hypotheses()
    assert len(hypos) >= 100

    # Check diverse coverage
    session_hypos = [h for h in hypos if h["category"] == "SESSION_CLOCK"]
    quarterly_hypos = [h for h in hypos if h["category"] == "QUARTERLY_THEORY"]
    day_hypos = [h for h in hypos if h["category"] == "WEEKLY_PROFILE"]
    month_hypos = [h for h in hypos if h["category"] == "MONTHLY_JOKER"]
    ipda_hypos = [h for h in hypos if h["category"] == "IPDA_RANGE"]

    assert len(session_hypos) >= 20
    assert len(quarterly_hypos) >= 25
    assert len(day_hypos) >= 15
    assert len(month_hypos) >= 15
    assert len(ipda_hypos) >= 20

    # Test FDR adjustment utility
    mock_results = [
        {"id": f"h_{i}", "p_raw": 0.001 * (i + 1), "win_rate": 60.0, "n": 50} for i in range(10)
    ]
    fdr_res = horizon_backtest.apply_fdr_guardrail(mock_results, alpha=0.05)
    assert len(fdr_res) == 10
    assert "significant_after_fdr" in fdr_res[0]
