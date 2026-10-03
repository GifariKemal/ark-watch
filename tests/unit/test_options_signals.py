"""Tests for the CME options signal layer (signals/options.py).

Schema basis: migration v8 (cme_options_settlements + cme_option_underlyings,
locked in PLAN-CME-OPTIONS §2.3). The fixture DDL is CREATE IF NOT EXISTS, so
these tests run both before and after the migration lands in db.py.

Units: OI in contracts, PCR dimensionless, strikes/underlyings in native
price units — no money conversion anywhere.

Dates: the PCR trigger freshness-caps the CURRENT trade_date at 4 days (CME
keeps ~5 days of settlements server-side), so seeds pin dates relative to
today — a fixed literal would age past the cap and silently break the
trigger tests.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.signals import options
from arkwatch.signals.options import (
    OPTIONS_WALL_MIN_OI_PCT,
    options_brief_line,
    options_pcr_extreme_alert,
    options_snapshot,
    store_options_signals,
)

# Locked v8 DDL (options-build) — no-op once the migration is in db.py
SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS cme_options_settlements (
  trade_date TEXT NOT NULL, product_id INTEGER NOT NULL, product_code TEXT NOT NULL,
  contract_id TEXT NOT NULL, option_type TEXT NOT NULL, strike REAL NOT NULL,
  settle REAL, volume INTEGER, open_interest INTEGER,
  PRIMARY KEY (trade_date, product_id, contract_id, option_type, strike));
CREATE TABLE IF NOT EXISTS cme_option_underlyings (
  trade_date TEXT NOT NULL, product_id INTEGER NOT NULL, contract_id TEXT NOT NULL,
  settle REAL NOT NULL, PRIMARY KEY (trade_date, contract_id));
"""

# Option product ids (PLAN-CME-OPTIONS §2.1 product-slate, live-verified)
PID = {"OG": 192, "SO": 193, "HXE": 797, "PO": 2910, "ES": 138, "NQ": 148, "BTC": 8875}


def _day(n: int = 0) -> str:
    """A trade date `n` days back from today (0 = freshest snapshot)."""
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    c.executescript(SCHEMA_V8)
    yield c
    c.close()


def _seed_chain(conn, trade_date, code, contract, rows, underlying=None):
    """Seed one option chain (front or back contract) at one trade_date.

    rows: (option_type, strike, oi) — settle/volume are irrelevant to the
    signals and seeded as constants.
    """
    pid = PID[code]
    for otype, strike, oi in rows:
        conn.execute(
            "INSERT OR REPLACE INTO cme_options_settlements"
            "(trade_date, product_id, product_code, contract_id, option_type, strike,"
            " settle, volume, open_interest) VALUES (?,?,?,?,?,?,?,?,?)",
            (trade_date, pid, code, contract, otype, float(strike), 1.0, 0, oi),
        )
    if underlying is not None:
        conn.execute(
            "INSERT OR REPLACE INTO cme_option_underlyings"
            "(trade_date, product_id, contract_id, settle) VALUES (?,?,?,?)",
            (trade_date, pid, contract, underlying),
        )
    conn.commit()


def _seed_gold_live(conn, trade_date=None, underlying=2730.5, with_back_month=False):
    """Live-shape gold front chain (PLAN §2.1 sample: fat 2700-put wall, OI
    1205) around a 2730.5 underlying anchor.

    call OI 1600 · put OI 1835 → PCR 1.146875; total 3435 → wall share 35.1%.
    max-pain hand-check — payoff(K) = Σ_calls OI·max(0,K−s) + Σ_puts OI·max(0,s−K):
        payoff(2600) = 0 + 50·300 + 100·1205 + 150·120 + 200·60 = 165,500
        payoff(2650) = 50·100 + 50·1205 + 100·120 + 150·60 = 86,250
        payoff(2700) = 100·100 + 50·200 + 50·120 + 100·60 = 32,000  ← min
        payoff(2750) = 150·100 + 100·200 + 50·400 + 50·60 = 58,000
        payoff(2800) = 200·100 + 150·200 + 100·400 + 50·600 = 120,000
    → max pain = 2700 (the put wall pins it).
    """
    td = trade_date or _day(0)
    _seed_chain(
        conn,
        td,
        "OG",
        "OGV26",
        [
            ("Call", 2600, 100),
            ("Call", 2650, 200),
            ("Call", 2700, 400),
            ("Call", 2750, 600),
            ("Call", 2800, 300),
            ("Put", 2600, 150),
            ("Put", 2650, 300),
            ("Put", 2700, 1205),
            ("Put", 2750, 120),
            ("Put", 2800, 60),
        ],
        underlying=underlying,
    )
    if with_back_month:
        # back expiry (Z26 = Dec > V26 = Oct) — must NEVER leak into the front
        _seed_chain(conn, td, "OG", "OGZ26", [("Call", 2600, 50), ("Put", 2600, 60)])


def _seed_btc(conn, trade_date=None):
    """Minimal BTC book: one strike, pcr 10/25 = 0.40, call-side wall."""
    _seed_chain(
        conn,
        trade_date or _day(0),
        "BTC",
        "BTCZ26",
        [("Call", 65000, 25), ("Put", 65000, 10)],
        underlying=65000.0,
    )


def _seed_pcr_day(conn, n_days_ago, put_oi, call_oi):
    """One gold PCR history day: 2-strike ladder splitting the given totals."""
    _seed_chain(
        conn,
        _day(n_days_ago),
        "OG",
        "OGV26",
        [
            ("Call", 2600, call_oi // 2),
            ("Call", 2650, call_oi - call_oi // 2),
            ("Put", 2600, put_oi // 2),
            ("Put", 2650, put_oi - put_oi // 2),
        ],
    )


# --- PCR + front-contract selection -------------------------------------------------


def test_pcr_math_uses_front_contract_only(conn):
    """Front = nearest expiry parsed from the month code (OGV26 < OGZ26);
    the back-month chain must not leak into the front PCR or walls."""
    _seed_chain(
        conn,
        _day(0),
        "OG",
        "OGV26",
        [
            ("Call", 2600, 100),
            ("Call", 2650, 200),
            ("Call", 2700, 100),
            ("Put", 2600, 100),
            ("Put", 2650, 300),
            ("Put", 2700, 1205),
        ],
        underlying=2730.5,
    )
    _seed_chain(conn, _day(0), "OG", "OGZ26", [("Call", 2600, 50), ("Put", 2600, 60)])
    snap = options_snapshot(conn, "OG")
    assert snap["contract_id"] == "OGV26"
    assert snap["trade_date"] == _day(0)
    assert snap["call_oi"] == 400
    assert snap["put_oi"] == 1605
    assert snap["pcr"] == pytest.approx(1605 / 400)  # 4.0125 — back month excluded


def test_pcr_none_when_no_call_oi(conn):
    """A call-less book must degrade to pcr=None, not a fabricated 0/0."""
    _seed_chain(conn, _day(0), "OG", "OGV26", [("Put", 2600, 100)])
    snap = options_snapshot(conn, "OG")
    assert snap["pcr"] is None


# --- walls ---------------------------------------------------------------------------


def test_wall_detection_top_wall_with_distance(conn):
    _seed_gold_live(conn)
    snap = options_snapshot(conn, "OG")
    assert snap["total_oi"] == 3435
    top = snap["top_wall"]
    assert top["side"] == "Put"
    assert top["strike"] == 2700
    assert top["oi"] == 1205
    assert top["share_pct"] == pytest.approx(100 * 1205 / 3435)  # 35.08%
    assert top["distance_pct"] == pytest.approx((2700 - 2730.5) / 2730.5 * 100)  # −1.1%
    # definitional: every listed wall clears the configured floor
    assert all(w["oi"] >= OPTIONS_WALL_MIN_OI_PCT / 100 * 3435 for w in snap["walls"])
    assert len(snap["walls"]) >= 1
    # walls are ordered largest first
    ois = [w["oi"] for w in snap["walls"]]
    assert ois == sorted(ois, reverse=True)


def test_no_wall_when_oi_spread_flat(conn):
    """20 strikes/side × OI 5 → each side-strike = 2.5% of total, below the
    3% floor → no wall at all (the threshold IS the display floor)."""
    strikes = [2500 + 5 * i for i in range(20)]
    rows = [("Call", k, 5) for k in strikes] + [("Put", k, 5) for k in strikes]
    _seed_chain(conn, _day(0), "OG", "OGV26", rows, underlying=2600.0)
    snap = options_snapshot(conn, "OG")
    assert snap["walls"] == []
    assert snap["top_wall"] is None


# --- max pain ------------------------------------------------------------------------


def test_max_pain_hand_computed(conn):
    """3-strike hand computation (payoffs at K over candidates):
        strikes 2600/2650/2700 · calls 100/400/100 · puts 100/200/100
        payoff(2600) = calls 0 + puts 50·200 + 100·100 = 20,000
        payoff(2650) = calls 50·100 + puts 50·100 = 10,000  ← minimum
        payoff(2700) = calls 100·100 + 50·400 + puts 0 = 30,000
    max pain = 2650.
    """
    _seed_chain(
        conn,
        _day(0),
        "OG",
        "OGV26",
        [
            ("Call", 2600, 100),
            ("Call", 2650, 400),
            ("Call", 2700, 100),
            ("Put", 2600, 100),
            ("Put", 2650, 200),
            ("Put", 2700, 100),
        ],
    )
    assert options_snapshot(conn, "OG")["max_pain"] == 2650


def test_max_pain_tie_resolves_to_lower_strike(conn):
    """Symmetric book → payoff ties across the two middle strikes; the pick
    must be deterministic (the lower one)."""
    strikes = [2500 + 5 * i for i in range(20)]
    rows = [("Call", k, 5) for k in strikes] + [("Put", k, 5) for k in strikes]
    _seed_chain(conn, _day(0), "OG", "OGV26", rows)
    assert options_snapshot(conn, "OG")["max_pain"] == 2545  # s_9 (s_10 ties)


# --- degradation ---------------------------------------------------------------------


def test_degrade_missing_tables(conn):
    conn.execute("DROP TABLE cme_options_settlements")
    conn.execute("DROP TABLE cme_option_underlyings")
    assert options_snapshot(conn, "OG") is None
    assert options_brief_line(conn) is None
    assert options_pcr_extreme_alert(conn) is None
    assert store_options_signals(conn) == 0


def test_degrade_empty_tables(conn):
    assert options_snapshot(conn, "OG") is None
    assert options_brief_line(conn) is None
    assert options_pcr_extreme_alert(conn) is None
    assert store_options_signals(conn) == 0


# --- brief line ----------------------------------------------------------------------


def test_brief_line_render(conn):
    _seed_gold_live(conn)
    assert options_brief_line(conn) == "Opt: Au PCR 1.15 (wall 2700P -1.1%) · pain Au 2700"


def test_brief_line_multi_product_order_and_btc_wall(conn):
    """Fixed product order (metals first, BTC last) regardless of insert
    order; a product without data (nothing seeded for Ag/Cu/Pt/ES/NQ) simply
    does not render."""
    _seed_btc(conn)
    _seed_gold_live(conn)
    assert options_brief_line(conn) == (
        "Opt: Au PCR 1.15 (wall 2700P -1.1%) · BTC PCR 0.40 (wall 65000C +0.0%)"
        " · pain Au 2700 · pain BTC 65000"
    )


def test_brief_line_drops_wall_below_floor(conn):
    strikes = [2500 + 5 * i for i in range(20)]
    rows = [("Call", k, 5) for k in strikes] + [("Put", k, 5) for k in strikes]
    _seed_chain(conn, _day(0), "OG", "OGV26", rows, underlying=2600.0)
    assert options_brief_line(conn) == "Opt: Au PCR 1.00 · pain Au 2545"


def test_brief_line_wall_without_underlying_anchor(conn):
    """No futures anchor row → the wall renders without a distance (no
    fabricated %, no crash)."""
    _seed_gold_live(conn, underlying=None)
    line = options_brief_line(conn)
    assert "(wall 2700P)" in line
    assert "wall 2700P " not in line  # no dangling distance segment


# --- trigger (options_pcr_extreme) ---------------------------------------------------


def _seed_history(conn, n_days, cur_put, cur_call):
    """n_days prior days alternating PCR 0.95/1.05 (range 0.10) + today's
    (cur_put / cur_call) as the current PCR."""
    for n in range(1, n_days + 1):
        put, call = (950, 1000) if n % 2 else (1050, 1000)
        _seed_pcr_day(conn, n, put, call)
    _seed_pcr_day(conn, 0, cur_put, cur_call)


def test_pcr_extreme_quiet_below_min_obs(conn):
    """10 history days (< min_obs 20) + an absurd PCR today → still quiet by
    construction (no uncalibratable range may drive an alert)."""
    _seed_history(conn, 10, 3000, 1000)  # PCR 3.0 today
    assert options_pcr_extreme_alert(conn, min_obs=20, ratio=0.30) is None


def test_pcr_extreme_fires_put_heavy(conn):
    """20 prior days alternating 0.95/1.05 (range 0.10); today 1.50 >
    1.05 + 0.30·0.10 = 1.08 → PUT_HEAVY."""
    _seed_history(conn, 20, 1500, 1000)  # PCR 1.50
    a = options_pcr_extreme_alert(conn, min_obs=20, ratio=0.30)
    assert a is not None
    assert a["direction"] == "PUT_HEAVY"
    assert a["pcr"] == pytest.approx(1.5)
    assert a["hist_min"] == pytest.approx(0.95)
    assert a["hist_max"] == pytest.approx(1.05)
    assert a["n_obs"] == 20  # history EXCLUDES today
    assert a["trade_date"] == _day(0)


def test_pcr_extreme_quiet_inside_ratio_buffer(conn):
    """Today 1.06 is beyond the historical max (1.05) but inside the 0.30×0.10
    buffer (< 1.08) → a marginal new record must NOT fire."""
    _seed_history(conn, 20, 1060, 1000)  # PCR 1.06
    assert options_pcr_extreme_alert(conn, min_obs=20, ratio=0.30) is None


def test_pcr_extreme_fires_call_heavy(conn):
    """Today 0.60 < 0.95 − 0.30·0.10 = 0.92 → CALL_HEAVY."""
    _seed_history(conn, 20, 600, 1000)  # PCR 0.60
    a = options_pcr_extreme_alert(conn, min_obs=20, ratio=0.30)
    assert a is not None
    assert a["direction"] == "CALL_HEAVY"


def test_pcr_extreme_stale_snapshot_never_fires(conn):
    """Latest trade_date 10d old (daily harvest broken, data unrecoverable
    past CME's ~5-day retention) → never fires."""
    for n in range(11, 31):  # 20 prior days…
        put, call = (950, 1000) if n % 2 else (1050, 1000)
        _seed_pcr_day(conn, n, put, call)
    _seed_pcr_day(conn, 10, 1500, 1000)  # …but the latest day is stale
    assert options_pcr_extreme_alert(conn, min_obs=20, ratio=0.30) is None


def test_pcr_extreme_flat_history_never_fires(conn):
    """Identical PCRs every day → range 0 → nothing to be extreme against."""
    for n in range(0, 21):
        _seed_pcr_day(conn, n, 1000, 1000)  # PCR exactly 1.0 every day
    assert options_pcr_extreme_alert(conn, min_obs=20, ratio=0.30) is None


# --- watcher integration ---------------------------------------------------------------


def test_watcher_fires_with_per_day_cooldown(conn):
    """check_all fires options_pcr_extreme once; the per-day cooldown key
    (settlements are final — a day's PCR never changes) blocks the
    re-announcement on the next 60s watch cycle, exactly like the SOMA
    per-snapshot pattern."""
    from arkwatch.qa.watcher import check_all

    _seed_history(conn, 20, 1500, 1000)
    assert check_all(conn) == ["options_pcr_extreme"]
    assert "options_pcr_extreme" not in check_all(conn)  # same trade_date → dedup
    key = conn.execute(
        "SELECT cooldown_key FROM alert_deliveries WHERE alert_type='options_pcr_extreme'"
    ).fetchone()[0]
    assert key == f"options_pcr_extreme@{_day(0)}"


def test_watcher_quiet_below_min_obs(conn):
    from arkwatch.qa.watcher import check_all

    _seed_history(conn, 10, 3000, 1000)  # < 20 prior days → not armed yet
    assert check_all(conn) == []


# --- persistence ------------------------------------------------------------------------


def test_store_two_rows_and_replace_dedup(conn):
    _seed_gold_live(conn)
    _seed_btc(conn)
    assert store_options_signals(conn) == 2
    r = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='options_pcr'"
    ).fetchone()
    assert r[0] == _day(0)  # ts = trade_date (daily dedup key)
    assert r[1] == pytest.approx(1.146875, abs=1e-3)
    assert r[2] == "PUTS_DOMINANT"
    r = conn.execute(
        "SELECT value, state FROM computed_signals WHERE signal_id='options_walls'"
    ).fetchone()
    assert r[0] == pytest.approx(100 * 1205 / 3435, abs=0.01)  # 35.08
    assert r[1] == "WALL"
    # inputs_json carries the multi-product pcr + wall + max-pain context
    ctx = json.loads(
        conn.execute(
            "SELECT inputs_json FROM computed_signals WHERE signal_id='options_pcr'"
        ).fetchone()[0]
    )
    assert set(ctx["products"]) == {"OG", "BTC"}
    assert ctx["products"]["OG"]["wall"]["strike"] == 2700
    assert ctx["products"]["OG"]["max_pain"] == 2700
    assert ctx["products"]["BTC"]["pcr"] == pytest.approx(0.4)
    # re-run same trade_date → REPLACE, no duplicate history
    store_options_signals(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id LIKE 'options_%'"
    ).fetchone()[0]
    assert n == 2


def test_store_gold_missing_uses_max_date_and_na(conn):
    """Gold absent but BTC present → ts = the freshest product date, gold
    value None / state N/A (honest absence, not a fabricated number)."""
    _seed_btc(conn, trade_date=_day(1))
    assert store_options_signals(conn) == 2
    r = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='options_pcr'"
    ).fetchone()
    assert r[0] == _day(1)
    assert r[1] is None
    assert r[2] == "N/A"


def test_store_no_wall_state_none(conn):
    strikes = [2500 + 5 * i for i in range(20)]
    rows = [("Call", k, 5) for k in strikes] + [("Put", k, 5) for k in strikes]
    _seed_chain(conn, _day(0), "OG", "OGV26", rows, underlying=2600.0)
    assert store_options_signals(conn) == 2
    r = conn.execute(
        "SELECT value, state FROM computed_signals WHERE signal_id='options_walls'"
    ).fetchone()
    assert r[0] is None
    assert r[1] == "NONE"


class TestOpexCalendar:
    def test_third_friday_calculation(self):
        tf_oct = options.third_friday_of_month(2026, 10, calendar_shift=False)
        assert tf_oct == date(2026, 10, 16)
        assert tf_oct.weekday() == 4

        tf_mar = options.third_friday_of_month(2026, 3, calendar_shift=False)
        assert tf_mar == date(2026, 3, 20)

    def test_next_opex_detection(self):
        snap = options.next_opex("2026-10-04")
        assert snap["opex_date"] == "2026-10-16"
        assert snap["days_to_opex"] == 12
        assert snap["is_opex_week"] is False
        assert snap["is_quad_witching"] is False

        snap_week = options.next_opex("2026-10-12")
        assert snap_week["is_opex_week"] is True
        assert snap_week["days_to_opex"] == 4

        snap_dec = options.next_opex("2026-12-01")
        assert snap_dec["is_quad_witching"] is True
        assert snap_dec["cycle"] == "Q4"

    def test_opex_calendar_annual_schedule(self):
        cal = options.opex_calendar(2026)
        assert len(cal) == 12
        quads = [c["month"] for c in cal if c["is_quad_witching"]]
        assert quads == [3, 6, 9, 12]
