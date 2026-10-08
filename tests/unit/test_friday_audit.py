from arkwatch import db
from arkwatch.qa import friday_audit


def test_friday_rerange_empirical_audit(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)
    prices = [
        ("2026-09-07", 100.0, 102.0, 99.0, 101.0),
        ("2026-09-08", 101.0, 105.0, 100.0, 104.0),
        ("2026-09-09", 104.0, 108.0, 103.0, 107.0),
        ("2026-09-10", 107.0, 110.0, 106.0, 109.0),
        ("2026-09-11", 109.0, 109.5, 102.0, 103.0),
        ("2026-09-14", 103.0, 105.0, 102.0, 104.0),
        ("2026-09-15", 104.0, 106.0, 103.0, 105.0),
        ("2026-09-16", 105.0, 107.0, 104.0, 106.0),
        ("2026-09-17", 106.0, 108.0, 105.0, 107.0),
        ("2026-09-18", 107.0, 125.0, 107.0, 122.0),
    ]
    for d_str, o, h, low_val, c in prices:
        conn.execute(
            "INSERT INTO instrument_prices (symbol, source, ts, open, high, low, close) VALUES ('NQ1', 'TEST', ?, ?, ?, ?, ?)",
            (d_str, o, h, low_val, c),
        )
    conn.commit()

    res = friday_audit.audit_friday_rerange(
        conn, "NQ1", lookback_weeks=3, source="TEST"
    )
    assert res["weeks_evaluated"] == 2
    assert res["revert_inside_range_count"] == 1
    assert res["continuation_breakout_count"] == 1
    assert res["mean_reversion_rate_pct"] == 50.0
    assert "amt_reconciliation" in res
    conn.close()
