from datetime import UTC, datetime, timedelta

from arkwatch import db
from arkwatch.signals import amt_horizons


def test_compute_horizon_amt_profile(tmp_path):
    db_file = tmp_path / "arkwatch.db"
    conn = db.get_conn(db_file, allow_init=True)

    t0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    bars = []
    # Insert 12 bars (1 hour)
    for i in range(12):
        ts = (t0 + timedelta(minutes=5 * i)).isoformat(timespec="seconds")
        # Price ramping from 100 to 112
        p = 100.0 + i
        bars.append(("NQ1", ts, "5m", "TEST", p, p + 1.0, p - 0.5, p + 0.5, 1000.0, ts))

    conn.executemany(
        """
        INSERT INTO intraday_bars (symbol, bar_ts_utc, interval, source, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bars,
    )
    conn.commit()

    # Slice first 30 mins (6 bars: 12:00 to 12:30)
    t_end = t0 + timedelta(minutes=30)
    res = amt_horizons.compute_horizon_amt(conn, "NQ1", t0, t_end)
    assert res is not None
    assert res["bars_count"] == 6
    assert res["high"] == 106.0
    assert res["low"] == 99.5
    assert "vah" in res
    assert "val" in res
    assert "poc" in res
    assert "vwap" in res
    assert res["total_volume"] == 6000.0
    conn.close()
