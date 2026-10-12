"""watcher credit_equity_divergence: spreads widen while ES1 holds up."""

from arkwatch import db
from arkwatch.qa import watcher

DAYS = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]


def _conn(hy, es, ig=None):
    conn = db.get_conn(":memory:", allow_init=True)
    for sid, vals in (("FRED:BAMLH0A0HYM2", hy), ("FRED:BAMLC0A0CM", ig or [1.0] * len(DAYS))):
        conn.execute(
            "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit,"
            " value_format, freq, primary_source) VALUES (?, ?, 'D', 0, 'pct', 'x', 'D', 'FRED')",
            (sid, sid),
        )
        conn.executemany(
            "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at)"
            " VALUES (?, ?, ?, 'FRED', 'x')",
            [(sid, d, v) for d, v in zip(DAYS, vals, strict=True)],
        )
    conn.executemany(
        "INSERT INTO instrument_prices(symbol, ts, source, close) VALUES ('ES1', ?, 'YAHOO', ?)",
        [(d, c) for d, c in zip(DAYS, es, strict=False)],
    )
    return conn


def test_hy_widening_while_es_rises_fires_once_per_week():
    conn = _conn(hy=[2.80, 2.90, 3.00, 3.10, 3.20, 3.40], es=[6000, 6010, 6020, 6030, 6040, 6060])
    key, cond, ctx, _ = watcher.credit_equity_divergence(conn)
    assert key == "credit_equity_divergence:HY@2026-W41"  # 2026-10-08 is in ISO week 41
    assert cond == "HY OAS +60bp in 5 prints (2.80% to 3.40%) while ES1 +1.0%"
    assert ctx.startswith("2026-10-01 to 2026-10-08")
    assert watcher._fire(conn, "credit_equity_divergence", cond, ctx, "a", cooldown_key=key)
    assert not watcher._fire(conn, "credit_equity_divergence", cond, ctx, "a", cooldown_key=key)


def test_equities_falling_with_credit_is_plain_risk_off():
    conn = _conn(hy=[2.80, 2.90, 3.00, 3.10, 3.20, 3.40], es=[6000, 5950, 5900, 5850, 5800, 5700])
    assert watcher.credit_equity_divergence(conn) is None


def test_widening_below_threshold_and_ig_branch():
    quiet = _conn(hy=[2.80, 2.85, 2.90, 2.95, 3.00, 3.05], es=[6000] * 6)
    assert watcher.credit_equity_divergence(quiet) is None  # +25bp < 50bp
    ig = _conn(hy=[2.80] * 6, es=[6000] * 6, ig=[0.80, 0.85, 0.90, 0.95, 1.00, 1.05])
    assert watcher.credit_equity_divergence(ig)[1].startswith("IG OAS +25bp")


def test_no_equity_print_at_window_start_stays_quiet():
    conn = _conn(hy=[2.80, 2.90, 3.00, 3.10, 3.20, 3.40], es=[])
    assert watcher.credit_equity_divergence(conn) is None


def test_next_print_of_the_same_episode_does_not_refire():
    conn = _conn(hy=[2.80, 2.90, 3.00, 3.10, 3.20, 3.40], es=[6000, 6010, 6020, 6030, 6040, 6060])
    key, cond, ctx, _ = watcher.credit_equity_divergence(conn)
    assert watcher._fire(conn, "credit_equity_divergence", cond, ctx, "a", cooldown_key=key)
    conn.execute(
        "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at)"
        " VALUES ('FRED:BAMLH0A0HYM2', '2026-10-09', 3.45, 'FRED', 'x')"
    )
    conn.execute(
        "INSERT INTO instrument_prices(symbol, ts, source, close) VALUES ('ES1', '2026-10-09', 'YAHOO', 6070)"
    )
    key2 = watcher.credit_equity_divergence(conn)[0]
    assert key2 == key  # the window slid one print, the episode key did not


def test_todays_partial_es1_close_is_ignored():
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    conn = _conn(hy=[2.80, 2.90, 3.00, 3.10, 3.20, 3.40], es=[6000, 6010, 6020, 6030, 6040, 6060])
    conn.execute(
        "INSERT INTO raw_observations(series_id, ts, value, source, fetched_at)"
        " VALUES ('FRED:BAMLH0A0HYM2', ?, 3.50, 'FRED', 'x')",
        (today,),
    )
    # a mid-session daily row with a crash close must not decide "ES1 did not fall"
    conn.execute(
        "INSERT INTO instrument_prices(symbol, ts, source, close) VALUES ('ES1', ?, 'YAHOO', 1)",
        (today,),
    )
    assert "while ES1 +" in watcher.credit_equity_divergence(conn)[1]
