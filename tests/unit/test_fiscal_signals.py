"""Tests for the Treasury fiscaldata signal layer (signals/fiscal.py).

Covers:
- auction_demand: percentile math (hand-computed small history + documented
  40-value distribution), min-hist honesty floor, WEAK/SOFT/NORMAL/STRONG
  buckets, defensive (term, type) matching (10-Year TIPS / 2-Year FRN never
  enter the nominal histories; case/spacing variants still match), headline
  10Y→5Y fallback.
- net_issuance: marketable-only math (nonmarketable must NOT leak in), the
  trailing-7d window edges, defensive transaction_type casing.
- interest_burden: defensive TOTAL-row picking, the ambiguous→month-sum YoY
  fallback, the <13-months honest None, avg-rate 'Total Marketable' pick.
- fiscal_brief_line: full render + per-segment degrade + stale marker.
- store_fiscal_signals: 3 rows, INSERT OR REPLACE dedup.
- watcher auction_demand_weak: fire, permanent per-auction cooldown, 5Y
  fallback, freshness cap, ≤-threshold boundary.

Schema basis: migration v10 (fd_auctions + fd_debt_transactions +
fd_interest_expense + fd_avg_rates, landed by fd-build). Fixture DDL is
CREATE IF NOT EXISTS, so these tests run both before and after the migration
lands in db.py. Money in the tables is raw USD; signals render $B/$T.

Telegram safety: _fire only queues (no fast-path send since 2026-10-10); the conn
fixture still blanks TELEGRAM_* so fired alerts only ever land in alert_deliveries (pending) [nyfed-signals convention].
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch.signals.fiscal import (
    auction_demand,
    auction_demand_weak_alert,
    fiscal_brief_line,
    interest_burden,
    net_issuance,
    store_fiscal_signals,
)

# v10 DDL — VERBATIM from db.py migration (kept in sync by hand; the fixture
# is a no-op once the migration is applied by get_conn(allow_init=True))
SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS fd_auctions (
  auction_date TEXT NOT NULL, cusip TEXT NOT NULL,
  security_type TEXT, security_term TEXT,
  issue_date TEXT, maturity_date TEXT,
  price_per100 REAL, avg_med_yield REAL, bid_to_cover REAL,
  allocation_pct REAL, auction_format TEXT,
  PRIMARY KEY (auction_date, cusip)
);
CREATE TABLE IF NOT EXISTS fd_debt_transactions (
  record_date TEXT NOT NULL, transaction_type TEXT NOT NULL,
  security_market TEXT NOT NULL, security_type TEXT NOT NULL,
  amount_today REAL,
  PRIMARY KEY (record_date, transaction_type, security_market, security_type)
);
CREATE TABLE IF NOT EXISTS fd_interest_expense (
  record_date TEXT NOT NULL, expense_catg_desc TEXT NOT NULL,
  expense_group_desc TEXT, expense_type_desc TEXT,
  month_amt REAL, fytd_amt REAL,
  PRIMARY KEY (record_date, expense_catg_desc, expense_group_desc, expense_type_desc)
);
CREATE TABLE IF NOT EXISTS fd_avg_rates (
  record_date TEXT NOT NULL, security_desc TEXT NOT NULL,
  security_type_desc TEXT, avg_interest_rate REAL,
  PRIMARY KEY (record_date, security_desc)
);
"""


def _day(n: int = 0) -> str:
    """A date `n` days back from today (0 = today) — freshness-gated tests
    must not age out the way fixed literal dates would."""
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Blank the Telegram env even on hosts that carry the production values: belt and
    # braces now that _fire only queues 'pending' rows for the outbox.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    from arkwatch import db

    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    c.executescript(SCHEMA_V10)
    yield c
    c.close()


# --- seeds ----------------------------------------------------------------------


def _seed_auction(conn, d, cusip, sec_type, term, btc):
    conn.execute(
        "INSERT OR REPLACE INTO fd_auctions(auction_date, cusip, security_type, "
        "security_term, bid_to_cover) VALUES (?,?,?,?,?)",
        (d, cusip, sec_type, term, btc),
    )


def _seed_term_history(conn, term, sec_type, bcs, last_age_days=1):
    """Auctions spaced 7d, `bcs` in DATE order (latest = last element). Cusips
    carry the term so two terms seeded on the same dates don't collide on the
    (auction_date, cusip) PK."""
    n = len(bcs)
    for i, btc in enumerate(bcs):
        _seed_auction(
            conn, _day(last_age_days + (n - 1 - i) * 7), f"{term}{i:04d}", sec_type, term, btc
        )
    conn.commit()


# 40 values spanning 2.00..2.39 with the LATEST auction at 2.31 (live-like):
# 32 of 40 at-or-below (2.00..2.30 + itself) → p80.0. The documented
# calibration shape every other percentile seed here builds on.
TEN_YEAR_BCS = (
    [round(2.00 + 0.01 * i, 2) for i in range(31)]
    + [round(2.32 + 0.01 * i, 2) for i in range(8)]
    + [2.31]
)
WEAK_HIST = [2.40 + 0.01 * i for i in range(39)] + [2.31]  # latest lowest → p2.5
SOFT_HIST = (
    [2.00, 2.05, 2.10, 2.15, 2.20, 2.25, 2.30]  # 7 below the latest…
    + [2.40 + 0.01 * i for i in range(32)]
    + [2.31]  # …+ itself → 8 of 40 → p20
)
NORMAL_HIST = TEN_YEAR_BCS[:-1] + [2.20]  # 22 of 40 at-or-below (2.00..2.20 + itself) → p55


def _seed_tx(conn, rows):
    """rows: (record_date, transaction_type, security_market, security_type,
    amount in raw-USD-style units for readability). Stored ×1e-6 — the table's
    real unit is $ MILLIONS (the DTS family; migration-v10 identity proof:
    ΣFYTD(issues−redemptions) matched Δdebt-to-penny to the dollar)."""
    for rd, ttype, mkt, sec, amt in rows:
        conn.execute(
            "INSERT OR REPLACE INTO fd_debt_transactions(record_date, transaction_type, "
            "security_market, security_type, amount_today) VALUES (?,?,?,?,?)",
            (rd, ttype, mkt, sec, amt * 1e-6),
        )
    conn.commit()


def _seed_net_iss_58(conn):
    """+40 (d0) +30 (d2) −12 (d4) = +$58B; the nonmarketable issue and the
    d9 row must not leak in."""
    _seed_tx(
        conn,
        [
            (_day(0), "Issue", "Marketable", "Bill", 60e9),
            (_day(0), "Redemption", "Marketable", "Note", 20e9),
            (_day(2), "Issue", "Marketable", "Note", 30e9),
            (_day(4), "Redemption", "Marketable", "Bond", 12e9),
            (_day(2), "Issue", "Nonmarketable", "Savings", 5e9),  # must NOT count
            (_day(9), "Issue", "Marketable", "Note", 99e9),  # outside the 7d window
        ],
    )


def _seed_interest_full(conn):
    """LIVE SHAPE (verified 2026-09-04): no aggregate row — the headline is
    the SUM over expense_catg_desc='INTEREST EXPENSE ON PUBLIC ISSUES'; the
    GOVT-ACCOUNT (intragov) category must be EXCLUDED. FYTD $881B = 811+70;
    month $90B = 85+5."""
    conn.executemany(
        "INSERT OR REPLACE INTO fd_interest_expense(record_date, expense_catg_desc, "
        "expense_group_desc, expense_type_desc, month_amt, fytd_amt) VALUES (?,?,?,?,?,?)",
        [
            (
                _day(3),
                "INTEREST EXPENSE ON PUBLIC ISSUES",
                "ACCRUED INTEREST EXPENSE",
                "Treasury Notes",
                85e9,
                811e9,
            ),
            (
                _day(3),
                "INTEREST EXPENSE ON PUBLIC ISSUES",
                "AMORTIZED DISCOUNT",
                "Treasury Bills",
                5e9,
                70e9,
            ),
            (
                _day(3),
                "INTEREST EXPENSE ON GOVT ACCOUNT SERIES",
                "CASH BASIS GAS PAYMENTS",
                "Interest Payments",
                40e9,
                400e9,  # intragov — must NOT enter the public headline
            ),
        ],
    )
    conn.commit()


def _seed_interest_ambiguous(conn, months=14):
    """No PUBLIC-ISSUES category (defensive fallback shape), monthly rows.
    Latest month $100B vs $80B at the ~year-ago month (i=12 → 372d back,
    nearest to the −365d target) → +25% y/y."""
    for i in range(months):
        d = (date.fromisoformat(_day(3)) - timedelta(days=31 * i)).isoformat()
        amt = 100e9 if i == 0 else (80e9 if i == 12 else 90e9)
        conn.execute(
            "INSERT OR REPLACE INTO fd_interest_expense(record_date, expense_catg_desc, "
            "expense_group_desc, expense_type_desc, month_amt) VALUES (?,?,?,?,?)",
            (d, "Interest on Treasury Securities", "Treasury Securities", "Competitive", amt),
        )
    conn.commit()


def _seed_rates(conn):
    conn.executemany(
        "INSERT OR REPLACE INTO fd_avg_rates(record_date, security_desc, "
        "security_type_desc, avg_interest_rate) VALUES (?,?,?,?)",
        [
            (_day(3), "Total Marketable", "Total Marketable", 3.34),
            (_day(3), "Treasury Bills", "Bill", 4.09),
            (_day(3), "Treasury Notes/Bonds", "Notes/Bonds", 3.40),
        ],
    )
    conn.commit()


def _seed_debt(conn, v=37.42e12, at=None):
    at = _day(1) if at is None else at
    conn.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit, "
        "value_format, freq, primary_source) VALUES (?,?,'E',0,'?','{:,.1f}','D',?)",
        ("FISCAL:DEBT_TOTAL", "Treasury total public debt", "FISCAL:DEBT_TOTAL"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO raw_observations(series_id, ts, value, vintage_ts, "
        "source, fetched_at) VALUES (?,?,?,?, 'test', ?)",
        ("FISCAL:DEBT_TOTAL", at, v, "realtime", at),
    )
    conn.commit()


# --- auction demand: percentile math ----------------------------------------------


def test_auction_percentile_hand_computed_small_history(conn):
    """Hand-computed: values [2.0, 2.1, 2.2, 2.3, 2.15], latest 2.15 → 3 of 5
    at-or-below → p60 (inclusive convention, min_hist lowered for the test)."""
    _seed_term_history(conn, "10-Year", "Note", [2.0, 2.1, 2.2, 2.3, 2.15])
    ad = auction_demand(conn, min_hist=2)
    t = ad["terms"]["10-Year"]
    assert t["bid_to_cover"] == pytest.approx(2.15)
    assert t["n_history"] == 5
    assert t["percentile"] == pytest.approx(60.0)
    assert ad["headline_term"] == "10-Year"
    assert ad["as_of_date"] == t["auction_date"] == _day(1)


def test_auction_percentile_full_history_documented(conn):
    """40-value ladder 2.00..2.39, latest 2.31 → 32 of 40 at-or-below → p80
    (the documented distribution every other seed here builds on)."""
    _seed_term_history(conn, "10-Year", "Note", TEN_YEAR_BCS)
    t = auction_demand(conn, min_hist=5)["terms"]["10-Year"]
    assert t["n_history"] == 40
    assert t["percentile"] == pytest.approx(80.0)


def test_auction_percentile_min_hist_floor(conn):
    """25 same-term auctions < the 26-obs honesty floor → percentile None
    (never a percentile off a window that mostly measures its own noise)."""
    _seed_term_history(conn, "10-Year", "Note", [2.0 + 0.01 * i for i in range(25)])
    t = auction_demand(conn)["terms"]["10-Year"]
    assert t["n_history"] == 25
    assert t["percentile"] is None


def test_auction_percentile_ties_all_equal(conn):
    """Flat history: every auction at the same b/c → p100 (inclusive), no
    division-by-zero or fabricated mid-percentile."""
    _seed_term_history(conn, "10-Year", "Note", [2.31] * 30)
    t = auction_demand(conn, min_hist=5)["terms"]["10-Year"]
    assert t["percentile"] == pytest.approx(100.0)


# --- auction demand: defensive matching -------------------------------------------


def test_auction_tips_frn_excluded_and_case_variants_match(conn):
    """A bare term match would be wrong twice: the same-date 10-Year TIPS
    auction (different demand animal) must stay out of the nominal 10Y
    history, and the 2-Year FRN out of the 2-Year NOTE history — while
    '10 year'/'note' spacing/case variants still match."""
    _seed_term_history(conn, "10-Year", "Note", TEN_YEAR_BCS)
    _seed_auction(conn, _day(1), "TIP1", "TIPS", "10-Year", 2.45)  # same date, TIPS
    _seed_auction(conn, _day(1), "FRN1", "FRN", "2-Year", 3.10)  # same date, FRN
    _seed_auction(conn, _day(8), "VAR1", "note", "10 Year", 2.50)  # older variant row
    conn.commit()
    ad = auction_demand(conn, min_hist=5)
    t10 = ad["terms"]["10-Year"]
    assert t10["n_history"] == 41  # 40 ladder + the '10 Year' variant; TIPS excluded
    assert t10["bid_to_cover"] == pytest.approx(2.31)  # latest NOMINAL, not the TIPS 2.45
    assert t10["percentile"] == pytest.approx(100.0 * 32 / 41, abs=0.1)
    assert "2-Year" not in ad["terms"]  # only the FRN row existed → excluded, no fabrication


def test_auction_headline_5y_fallback(conn):
    """No 10Y data → headline falls back to 5Y (the named fallback); 30Y stays
    context in terms only."""
    _seed_term_history(conn, "5-Year", "Note", TEN_YEAR_BCS)
    _seed_term_history(conn, "30-Year", "Bond", TEN_YEAR_BCS)
    ad = auction_demand(conn, min_hist=5)
    assert ad["headline_term"] == "5-Year"
    assert set(ad["terms"]) == {"5-Year", "30-Year"}


# --- buckets ----------------------------------------------------------------------


def test_bucket_boundaries():
    from arkwatch.signals.fiscal import _bucket

    assert _bucket(None) == "N/A"
    assert _bucket(9.9) == "WEAK"
    assert _bucket(10.0) == "SOFT"  # WEAK is strictly below p10
    assert _bucket(24.9) == "SOFT"
    assert _bucket(25.0) == "NORMAL"
    assert _bucket(74.9) == "NORMAL"
    assert _bucket(75.0) == "STRONG"  # STRONG is at/above p75


def test_bucket_labels_end_to_end(conn):
    for bcs, expected in (
        (WEAK_HIST, 2.5),
        (SOFT_HIST, 20.0),
        (NORMAL_HIST, 55.0),
        (TEN_YEAR_BCS, 80.0),
    ):
        _seed_term_history(conn, "10-Year", "Note", bcs)
        t = auction_demand(conn, min_hist=5)["terms"]["10-Year"]
        assert t["percentile"] == pytest.approx(expected), bcs[-1]


# --- net issuance ------------------------------------------------------------------


def test_net_issuance_marketable_only_and_window(conn):
    """+$58B = +40 (d0) +30 (d2) −12 (d4); the NONMARKETABLE issue and the
    d9 row (outside the trailing 7d window) must not leak in."""
    _seed_net_iss_58(conn)
    ni = net_issuance(conn)
    assert ni["record_date"] == _day(0)
    assert ni["n_days"] == 3
    assert ni["net_7d_b"] == pytest.approx(58.0)


def test_net_issuance_window_edges(conn):
    """The 7-calendar-day window is anchor−6d..anchor inclusive: a row 6d back
    counts, a row 7d back does not."""
    _seed_tx(
        conn,
        [
            (_day(0), "Issue", "Marketable", "Bill", 1e9),
            (_day(6), "Redemption", "Marketable", "Note", 2e9),  # inside
            (_day(7), "Issue", "Marketable", "Note", 4e9),  # outside
        ],
    )
    ni = net_issuance(conn)
    assert ni["n_days"] == 2
    assert ni["net_7d_b"] == pytest.approx(-1.0)


def test_net_issuance_defensive_type_casing(conn):
    """Live variants 'issue'/'REDEMPTION'/'Issues' all normalize to the two
    legs of the identity."""
    _seed_tx(
        conn,
        [
            (_day(0), "issue", "Marketable", "Bill", 3e9),
            (_day(0), "REDEMPTION", "Marketable", "Note", 1e9),
            (_day(1), "Issues", "Marketable", "Note", 2e9),
        ],
    )
    assert net_issuance(conn)["net_7d_b"] == pytest.approx(4.0)


def test_net_issuance_empty(conn):
    assert net_issuance(conn) == {}


def test_net_issuance_unit_is_dts_millions(conn):
    """REGRESSION (review integrasi 2026-09-04): fd_debt_transactions stores
    $ MILLIONS (the DTS family — fetch-layer proved ΣFYTD = Δdebt-to-penny to
    the dollar). Live anchor: 2026-09-02 all-markets net = 4,581 $M → the
    signal must read $B, i.e. 4.581 — the old raw-USD assumption printed
    1000× too small."""
    _seed_tx(
        conn,
        [
            (_day(0), "Issues", "Marketable", "Bills", 2e9),
            (_day(0), "Redemptions", "Marketable", "Bills", 1e9),
            (_day(1), "Issues", "Marketable", "Notes", 3.581e9),
        ],
    )
    ni = net_issuance(conn)
    assert ni["net_7d_b"] == pytest.approx(4.581)


# --- interest burden ----------------------------------------------------------------


def test_interest_burden_picks_total_row(conn):
    """Exactly one total-prefixed row → its FYTD ($881B), never a sum over the
    hierarchy; the rate pick is 'Total Marketable' (3.34), not Bills 4.09."""
    _seed_interest_full(conn)
    _seed_rates(conn)
    ib = interest_burden(conn)
    assert ib["record_date"] == _day(3)
    assert ib["fytd_b"] == pytest.approx(881.0)
    assert ib["avg_rate_mkt"] == pytest.approx(3.34)
    assert ib["avg_rate_date"] == _day(3)


def test_interest_burden_two_total_rows_ambiguous(conn):
    """Two total-prefixed hits = no single defensible pick → FYTD None; with
    <13 months of history the YoY fallback also stays honest None."""
    conn.executemany(
        "INSERT OR REPLACE INTO fd_interest_expense(record_date, expense_catg_desc, "
        "expense_group_desc, expense_type_desc, month_amt, fytd_amt) VALUES (?,?,?,?,?,?)",
        [
            (_day(3), "Total Interest Expense", "g1", "Total", 90e9, 881e9),
            (_day(3), "Total Intragovernmental", "g2", "Total", 8e9, 80e9),
        ],
    )
    conn.commit()
    ib = interest_burden(conn)
    assert ib["fytd_b"] is None
    assert ib["month_b"] is None
    assert ib["yoy_pct"] is None


def test_interest_burden_ambiguous_falls_back_to_month_yoy(conn):
    """No total row, ≥13 months of history: month-sum $100B, +25% vs the
    same fiscal month last year."""
    _seed_interest_ambiguous(conn)
    ib = interest_burden(conn)
    assert ib["fytd_b"] is None
    assert ib["month_b"] == pytest.approx(100.0)
    assert ib["yoy_pct"] == pytest.approx(25.0)


def test_interest_burden_young_history_honest_none(conn):
    """3 months < FISCAL_MIN_MONTHS → the fallback cannot prove a YoY → None
    (never a growth rate off a window it cannot see through)."""
    _seed_interest_ambiguous(conn, months=3)
    ib = interest_burden(conn)
    assert ib["fytd_b"] is None
    assert ib["month_b"] is None
    assert ib["yoy_pct"] is None


def test_interest_burden_avg_rate_only(conn):
    """Expense table absent (pre-v10 half-migration): the rate leg still
    reports; nothing else is fabricated."""
    conn.execute("DROP TABLE fd_interest_expense")
    _seed_rates(conn)
    ib = interest_burden(conn)
    assert ib["fytd_b"] is None
    assert ib["avg_rate_mkt"] == pytest.approx(3.34)


# --- brief line ----------------------------------------------------------------------


def test_brief_line_full_render(conn):
    _seed_debt(conn)
    _seed_interest_full(conn)
    _seed_rates(conn)
    _seed_term_history(conn, "10-Year", "Note", TEN_YEAR_BCS)
    _seed_net_iss_58(conn)
    out = fiscal_brief_line(conn)
    assert out == (
        "Fiscal: debt $37.4T · int FYTD $881B (avg mkt 3.3%) · 10y b/c 2.31 p80 · net iss 7d +$58B (3d)"
    )


def test_brief_line_int_fallback_render(conn):
    _seed_interest_ambiguous(conn)
    out = fiscal_brief_line(conn)
    assert out == "Fiscal: int mth $100B +25%/yr"


def test_brief_line_avg_rate_only_render(conn):
    conn.execute("DROP TABLE fd_interest_expense")
    _seed_rates(conn)
    assert fiscal_brief_line(conn) == "Fiscal: (avg mkt 3.3%)"


def test_brief_line_degrades_per_segment(conn):
    _seed_term_history(conn, "10-Year", "Note", TEN_YEAR_BCS)
    assert fiscal_brief_line(conn) == "Fiscal: 10y b/c 2.31 p80"
    conn.execute("DELETE FROM fd_auctions")
    conn.commit()
    _seed_debt(conn)
    assert fiscal_brief_line(conn) == "Fiscal: debt $37.4T"


def test_brief_line_bc_without_percentile_when_short_history(conn):
    """<26 obs → no percentile → the b/c renders bare (honest omission, not
    a fabricated p-value)."""
    _seed_term_history(conn, "10-Year", "Note", [2.0, 2.1, 2.2, 2.3, 2.31])
    assert fiscal_brief_line(conn) == "Fiscal: 10y b/c 2.31"


def test_brief_line_net_iss_sign_and_stale_marker(conn):
    """Negative net renders '-$12B'; an anchor older than NET_ISS_STALE_DAYS
    (dead daily harvest) is marked stale, not passed off as current."""
    _seed_tx(conn, [(_day(20), "Redemption", "Marketable", "Note", 12e9)])
    out = fiscal_brief_line(conn)
    assert "net iss 7d -$12B" in out
    assert "(stale" in out


def test_brief_line_none_without_data(conn):
    assert fiscal_brief_line(conn) is None


# --- persistence ----------------------------------------------------------------------


def test_store_fiscal_signals_rows_and_dedup(conn):
    _seed_debt(conn)
    _seed_interest_full(conn)
    _seed_rates(conn)
    _seed_term_history(conn, "10-Year", "Note", TEN_YEAR_BCS)
    _seed_term_history(conn, "4-Week", "Bill", [2.20 + 0.01 * i for i in range(30)])
    _seed_net_iss_58(conn)
    assert store_fiscal_signals(conn) == 3

    r = conn.execute(
        "SELECT ts, value, state, inputs_json FROM computed_signals "
        "WHERE signal_id='fd_auction_demand'"
    ).fetchone()
    assert r[0] == _day(1)  # ts = latest auction date (dedup key)
    assert r[1] == pytest.approx(2.31)  # value = headline 10Y b/c
    assert r[2] == "STRONG"  # p80 bucket
    inputs = json.loads(r[3])
    assert inputs["headline_term"] == "10-Year"
    assert inputs["percentile"] == pytest.approx(80.0)
    assert set(inputs["terms"]) == {"10-Year", "4-Week"}  # all terms carried

    r = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='fd_net_issuance'"
    ).fetchone()
    assert r[0] == _day(0)
    assert r[1] == pytest.approx(58.0)
    assert r[2] == "ISSUING"

    r = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='fd_interest_burden'"
    ).fetchone()
    assert r[0] == _day(3)
    assert r[1] == pytest.approx(881.0)
    assert r[2] == "FYTD"

    store_fiscal_signals(conn)  # same dates → REPLACE, no duplicate history
    n = conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id LIKE 'fd_%'"
    ).fetchone()[0]
    assert n == 3


def test_store_fiscal_signals_empty_is_noop(conn):
    assert store_fiscal_signals(conn) == 0


# --- watcher: auction_demand_weak -------------------------------------------------------


def test_auction_weak_trigger_fires_with_permanent_cooldown(conn):
    _seed_term_history(conn, "10-Year", "Note", WEAK_HIST)  # latest = p2.5
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == ["auction_demand_weak"]
    row = conn.execute(
        "SELECT cooldown_key, priority, message FROM alert_deliveries "
        "WHERE alert_type='auction_demand_weak'"
    ).fetchone()
    assert row[0] == f"auction_demand_weak@10-Year@{_day(1)}"  # per-(term,auction) permanent key
    assert row[1] == "normal"
    assert "10-Year auction b/c 2.31" in row[2]
    assert "p2 of 40 auctions" in row[2]
    # an auction's number never changes → no re-announcement on the next cycle
    assert check_all(conn) == []


def test_auction_weak_trigger_5y_fallback(conn):
    """No 10Y history → the named 5Y fallback carries the trigger."""
    _seed_term_history(conn, "5-Year", "Note", WEAK_HIST)
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == ["auction_demand_weak"]
    msg = conn.execute(
        "SELECT message FROM alert_deliveries WHERE alert_type='auction_demand_weak'"
    ).fetchone()[0]
    assert "5-Year" in msg


def test_auction_weak_trigger_quiet_when_strong(conn):
    _seed_term_history(conn, "10-Year", "Note", TEN_YEAR_BCS)  # p80 — not weak
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []


def test_auction_weak_trigger_stale_auction_never_fires(conn):
    """Freshness cap: an auction 30d old (dead harvest) must not re-alert —
    weak or not."""
    _seed_term_history(conn, "10-Year", "Note", WEAK_HIST, last_age_days=30)
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []


def test_auction_weak_alert_le_boundary_and_history_guard(conn):
    """pct exactly AT the threshold fires (≤); just under the threshold does
    not. Short history (percentile None) never fires even at a loose
    threshold — honest None, no fabricated p."""
    _seed_term_history(
        conn, "10-Year", "Note", [2.40 + 0.01 * i for i in range(36)] + [2.00, 2.10, 2.20, 2.31]
    )  # 4 of 40 ≤ 2.31 → p10.0 exactly
    a = auction_demand_weak_alert(conn, weak_pct=10.0)
    assert a is not None
    assert a["percentile"] == pytest.approx(10.0)
    assert a["n_history"] == 40
    assert auction_demand_weak_alert(conn, weak_pct=9.9) is None  # p10 > 9.9
    conn.execute("DELETE FROM fd_auctions")
    conn.commit()
    _seed_term_history(conn, "10-Year", "Note", [3.0 + 0.1 * i for i in range(4)] + [2.9])
    assert auction_demand(conn)["terms"]["10-Year"]["percentile"] is None
    assert auction_demand_weak_alert(conn, weak_pct=50.0) is None  # would fire if p existed


def test_watcher_check_all_safe_on_empty_v10_tables(conn):
    """The state right after migration v10, before the first harvest: all
    triggers coexist quietly."""
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []


# --- whole-layer degrade -----------------------------------------------------------------


def test_fiscal_layer_degrades_without_v10_tables(conn):
    """Pre-v10 DB (fd-build not landed yet): every public entry is a silent
    no-op — {}, None, 0 — never a crash."""
    for t in ("fd_auctions", "fd_debt_transactions", "fd_interest_expense", "fd_avg_rates"):
        conn.execute(f"DROP TABLE {t}")
    assert auction_demand(conn) == {}
    assert net_issuance(conn) == {}
    assert interest_burden(conn) == {}
    assert auction_demand_weak_alert(conn, weak_pct=10) is None
    assert store_fiscal_signals(conn) == 0
    assert fiscal_brief_line(conn) is None
