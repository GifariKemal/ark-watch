"""Tests for the NY Fed v9 signal layer (PLAN-NYFED-FULL §2 signals).

Covers:
- Net Liq v2 (signals/soma.py): the soma_agency_summary split of the old
  MBS/other residual into exact ΔMBS + small 'other', with the pre-v9
  fallback when the agency table is missing/misaligned.
- Ops explainer (signals/soma.py): fed_operations Results legs vs the SOMA
  week's gross purchases + the 'via N ops' header suffix.
- Dealer positioning (signals/dealers.py): within-seriesbreak z, min-obs
  guard, 4-week Δ, DLR brief line, computed_signals persistence.
- Watcher triggers: fx_swap_draw (urgent), dealer_stress, fed_ops_resume —
  fire + quiet paths + cooldown keys.

Schema basis: migration v9 (soma_agency_summary + fed_operations +
pd_positions) on top of the v7 SOMA tables. The fixture DDL is CREATE IF
NOT EXISTS, so these tests run both before and after the migration lands in
db.py. All money in the SOMA/agency tables is raw USD, pd_positions is
$millions; every signal/output is $B (boundary conversion).

Telegram safety: _fire attempts a real send when the host env carries
TELEGRAM_* vars — the conn fixture blanks them so fired alerts only ever
land in alert_deliveries (pending).
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.signals.dealers import (
    PD_KEYIDS,
    UST_KEYID,
    dealers_brief_line,
    dealers_snapshot,
    store_dealer_signals,
)
from arkwatch.signals.soma import (
    soma_brief_line,
    soma_net_liquidity,
    soma_ops_explainer,
    store_soma_signals,
)

# Locked v7 DDL (soma-build) — no-op once the migration is in db.py
SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS soma_holdings (
  as_of_date TEXT NOT NULL, cusip TEXT NOT NULL,
  security_type TEXT, maturity_date TEXT,
  par_value REAL, pct_outstanding REAL, change_week REAL,
  PRIMARY KEY (as_of_date, cusip)
);
CREATE INDEX IF NOT EXISTS idx_soma_cusip ON soma_holdings(cusip, as_of_date);
CREATE TABLE IF NOT EXISTS soma_summary (
  as_of_date TEXT PRIMARY KEY, total_par REAL,
  bills REAL, notes_bonds REAL, tips REAL, frn REAL,
  weekly_change REAL, rolling_off_7d REAL, rolling_off_30d REAL,
  rolling_off_90d REAL, n_cusips INTEGER, avg_maturity_years REAL
);
"""

# Locked v9 DDL (ny-build) — no-op once the migration is in db.py
SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS soma_agency_summary (
  as_of_date TEXT PRIMARY KEY,
  mbs REAL, cmbs REAL, agency_debts REAL, total REAL
);
CREATE TABLE IF NOT EXISTS fed_operations (
  operation_id TEXT NOT NULL, family TEXT NOT NULL,
  operation_date TEXT NOT NULL, settlement_date TEXT,
  operation_type TEXT, direction TEXT,
  maturity_start TEXT, maturity_end TEXT, status TEXT,
  amount REAL, details_json TEXT,
  PRIMARY KEY (operation_id, family)
);
CREATE TABLE IF NOT EXISTS pd_positions (
  asofdate TEXT NOT NULL, keyid TEXT NOT NULL,
  seriesbreak TEXT, value_musd REAL,
  PRIMARY KEY (asofdate, keyid)
);
"""


def _wednesday(offset_weeks: int = 0) -> str:
    """A real Wednesday date `offset_weeks` back from today (0 = most recent).

    Fixed literal dates would age past SOMA_ALERT_MAX_AGE_DAYS and silently
    break every freshness-gated test two weeks later (ronde-3 lesson)."""
    today = datetime.now(UTC).date()
    wed = today - timedelta(days=(today.weekday() - 2) % 7)
    return (wed - timedelta(weeks=offset_weeks)).isoformat()


def _day(n: int = 0) -> str:
    """A date `n` days back from today (0 = today)."""
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


CUR = _wednesday()
PREV = _wednesday(1)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Blank the Telegram env even on hosts that carry the production values:
    # _fire's fast-path send must raise (caught → stays 'pending') instead of
    # delivering test alerts to the real channel.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    c.executescript(SCHEMA_V7)
    c.executescript(SCHEMA_V9)
    yield c
    c.close()


# --- seeds ----------------------------------------------------------------------


def _seed_summary_pair(
    conn,
    cur_total=4439.9e9,
    prev_total=4435.7e9,  # ΔSOMA = +4.2B
    weekly_change=42e9,  # gross purchases +42B → matured 37.8B
    rolling_off_7d=61e9,
):
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, weekly_change, "
        "rolling_off_7d, n_cusips) VALUES (?,?,?,?,5)",
        (CUR, cur_total, weekly_change, rolling_off_7d),
    )
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, n_cusips) VALUES (?,?,5)",
        (PREV, prev_total),
    )
    conn.commit()


def _seed_obs(conn, sid, at_cur, at_prev):
    """Realtime rows pinned to the Wednesday anchors (native units; None =
    leave that anchor unseeded)."""
    conn.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit, "
        "value_format, freq, primary_source) VALUES (?,?,'E',0,'?','{:,.1f}','D',?)",
        (sid, sid, sid),
    )
    for ts, v in ((CUR, at_cur), (PREV, at_prev)):
        if v is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO raw_observations(series_id, ts, value, vintage_ts, "
            "source, fetched_at) VALUES (?,?,?,?, 'test', ?)",
            (sid, ts, v, "realtime", ts),
        )
    conn.commit()


def _seed_liq_live_shape(conn):
    """The live 2026-08-26 'silent tightening' shape: ΔWALCL −14.8B,
    ΔSOMA +4.2B, RRP flat, ΔTGA +23B (contribution −23) → net −37.8B."""
    _seed_obs(conn, "FRED:WALCL", 6730912.0, 6745712.0)  # −14,800 $M
    _seed_obs(conn, "FRED:RRPONTSYD", 33.0, 33.0)  # Δ0 → contribution +$0B
    _seed_obs(conn, "FISCAL:TGA_DAILY", 959435.0, 936435.0)  # +23,000 $M


def _seed_agency(conn, cur_total=2200e9, prev_total=2219e9):
    """Agency summary at both anchors: Δ total = −19.0B (mbs ≈ 2.2T live)."""
    for as_of, total in ((CUR, cur_total), (PREV, prev_total)):
        conn.execute(
            "INSERT OR REPLACE INTO soma_agency_summary(as_of_date, mbs, cmbs, "
            "agency_debts, total) VALUES (?,?,?,?,?)",
            (as_of, total * 0.985, total * 0.015, 0.0, total),
        )
    conn.commit()


def _seed_ops(conn, rows):
    """rows: (op_id, family, op_date, settlement, direction, status, amount)
    — raw USD amounts."""
    for op_id, family, od, sd, dirn, status, amount in rows:
        conn.execute(
            "INSERT OR REPLACE INTO fed_operations(operation_id, family, "
            "operation_date, settlement_date, direction, status, amount) "
            "VALUES (?,?,?,?,?,?,?)",
            (op_id, family, od, sd, dirn, status, amount),
        )
    conn.commit()


def _seed_week_ops(conn):
    """Three tsy purchases (bill ops) settled inside (PREV, CUR] totaling
    $30B — 5/7 share of the +$42B gross — plus noise legs that must NOT
    qualify: an ambs sale, an op settled ON PREV (last week's window) and an
    Announced (unresults) purchase."""
    inside = (date.fromisoformat(CUR) - timedelta(days=2)).isoformat()
    _seed_ops(
        conn,
        [
            ("OP-1", "tsy", inside, inside, "P", "Results", 12e9),
            ("OP-2", "tsy", inside, inside, "P", "Results", 10e9),
            ("OP-3", "tsy", inside, inside, "P", "Results", 8e9),
            ("OP-4", "ambs", inside, inside, "S", "Results", 6e9),  # drain leg, not a purchase
            ("OP-5", "tsy", PREV, PREV, "P", "Results", 11e9),  # settled ON PREV → outside
            ("OP-6", "tsy", inside, inside, "P", "Announced", 9e9),  # no results yet
        ],
    )


def _seed_pd(conn, keyid, values_b, break_after=None, new_break="SBN2024", old_break="SBP2013"):
    """Weekly Wednesday pd_positions for one keyid, ending at CUR.

    `values_b` ascending $B (latest LAST, stored ×1000 as $M). `break_after`:
    ISO date — observations strictly after it carry `new_break` (a seeded
    methodology restatement); None = one homogeneous break."""
    end = date.fromisoformat(CUR)
    n = len(values_b)
    for i, v in enumerate(values_b):
        d = (end - timedelta(weeks=n - 1 - i)).isoformat()
        brk = new_break if (break_after is None or d > break_after) else old_break
        conn.execute(
            "INSERT OR REPLACE INTO pd_positions(asofdate, keyid, seriesbreak, value_musd) "
            "VALUES (?,?,?,?)",
            (d, keyid, brk, v * 1000.0),
        )
    conn.commit()


# UST live-shape: 25 stable weeks around $450B (±5-8B wiggle), then the
# declining tail 468→464→460→444→436 — the last four weeks print −$32B/−6.8%
# (460→444→436 was the live 3-week slide; 468 is the 4w-ago base).
UST_VALUES = [
    458,
    442,
    455,
    445,
    458,
    442,
    456,
    444,
    457,
    443,
    458,
    442,
    455,
    445,
    456,
    444,
    458,
    442,
    457,
    443,
    455,
    445,
    458,
    442,
    456,
    468,
    464,
    460,
    444,
    436,
]

# Small-wiggle series for the non-headline keyids ($B).
_MBS_VALUES = [
    113,
    119,
    114,
    118,
    115,
    117,
    114,
    118,
    116,
    115,
    117,
    113,
    118,
    114,
    116,
    117,
    115,
    119,
    113,
    116,
    118,
    114,
    117,
    115,
    116,
    116,
    115,
    117,
    116,
    116,
]
_CORP_VALUES = [
    86,
    90,
    85,
    89,
    87,
    88,
    86,
    90,
    88,
    87,
    89,
    85,
    90,
    86,
    88,
    87,
    89,
    85,
    88,
    86,
    90,
    87,
    88,
    86,
    89,
    88,
    87,
    89,
    88,
    88,
]
_AGENCY_VALUES = [
    54,
    58,
    55,
    57,
    56,
    56,
    54,
    58,
    56,
    55,
    57,
    55,
    58,
    54,
    56,
    57,
    55,
    58,
    54,
    56,
    57,
    55,
    56,
    58,
    54,
    56,
    55,
    57,
    56,
    56,
]
_MUNI_VALUES = [
    8,
    10,
    8,
    9,
    9,
    10,
    8,
    9,
    9,
    10,
    8,
    9,
    10,
    8,
    9,
    9,
    10,
    8,
    9,
    9,
    10,
    8,
    9,
    9,
    10,
    9,
    8,
    10,
    9,
    9,
]


def _seed_pd_all(conn):
    for keyid, vals in (
        (UST_KEYID, UST_VALUES),
        ("PDPOSMBS-TOT", _MBS_VALUES),
        ("PDPOSCS-TOT", _CORP_VALUES),
        ("PDPOSFGS-TOT", _AGENCY_VALUES),
        ("PDPOSSMGO-TOT", _MUNI_VALUES),
    ):
        _seed_pd(conn, keyid, vals)


# --- Net Liq v2: the agency split -----------------------------------------------


def test_net_liq_v2_split_exact_and_sums(conn):
    """v9 agency summary at both anchors: ΔMBS = Δ total agency (exact) and
    other = ΔWALCL − ΔSOMA − ΔMBS; the split components still sum to the
    headline (the brief relies on that invariant)."""
    _seed_summary_pair(conn)
    _seed_liq_live_shape(conn)
    _seed_agency(conn)
    liq = soma_net_liquidity(conn)
    assert liq["soma_change_b"] == pytest.approx(4.2)
    assert liq["mbs_b"] == pytest.approx(-19.0)  # 2200 − 2219
    assert liq["other_b"] == pytest.approx(0.0, abs=1e-9)  # −14.8 − 4.2 + 19.0
    assert liq["mbs_other_b"] == pytest.approx(-19.0)  # unchanged residual def
    assert liq["net_change_b"] == pytest.approx(-37.8)
    comp = (
        liq["soma_change_b"]
        + liq["mbs_b"]
        + liq["other_b"]
        + liq["rrp_contribution_b"]
        + liq["tga_contribution_b"]
    )
    assert comp == pytest.approx(liq["net_change_b"])


def test_net_liq_v2_fallback_when_agency_table_missing(conn):
    """Pre-v9 DB (no soma_agency_summary): the split legs are None — the
    combined MBS/other residual stays and nothing is fabricated."""
    conn.execute("DROP TABLE soma_agency_summary")
    _seed_summary_pair(conn)
    _seed_liq_live_shape(conn)
    liq = soma_net_liquidity(conn)
    assert liq["mbs_b"] is None
    assert liq["other_b"] is None
    assert liq["mbs_other_b"] == pytest.approx(-19.0)  # −14.8 − 4.2


def test_net_liq_v2_misaligned_agency_grid_no_split(conn):
    """The split requires agency rows at BOTH soma anchors exactly — a lagged
    agency harvest must degrade to the combined residual instead of
    differencing mismatched windows."""
    conn.execute("DELETE FROM soma_agency_summary WHERE as_of_date=?", (CUR,))
    conn.execute(
        "INSERT OR REPLACE INTO soma_agency_summary(as_of_date, total) VALUES (?,?)",
        (_wednesday(2), 2230e9),  # stale grid: newest agency row is 2 weeks old
    )
    conn.commit()
    _seed_summary_pair(conn)
    _seed_liq_live_shape(conn)
    liq = soma_net_liquidity(conn)
    assert liq["mbs_b"] is None
    assert liq["other_b"] is None
    assert liq["mbs_other_b"] == pytest.approx(-19.0)


def test_net_liq_v2_null_agency_total_no_split(conn):
    """A row exists at CUR but its total is NULL (partial harvest): no split —
    honest None over a half-fabricated Δ."""
    _seed_agency(conn)
    conn.execute(
        "INSERT OR REPLACE INTO soma_agency_summary(as_of_date, mbs, total) "
        "VALUES (?, 2200e9, NULL)",
        (CUR,),
    )
    conn.commit()
    _seed_summary_pair(conn)
    _seed_liq_live_shape(conn)
    liq = soma_net_liquidity(conn)
    assert liq["mbs_b"] is None
    assert liq["other_b"] is None


def test_brief_line_v2_render(conn):
    """Live-shape render: split MBS/other legs (other at 1dp), ops suffix in
    the buy/matured parenthetical."""
    _seed_summary_pair(conn)
    _seed_liq_live_shape(conn)
    _seed_agency(conn)
    _seed_week_ops(conn)
    out = soma_brief_line(conn)
    assert "ΔSOMA +$4B/wk (buy +$42B · matured $38B · via 3 tsy-P ops)" in out
    assert "roll 7d $61B" in out
    assert "Net Liq: -$38B/wk (SOMA +$4B, MBS -$19B, other +$0.0B, RRP +$0B, TGA -$23B)" in out


def test_brief_line_v2_absent_keeps_combined_residual(conn):
    """No agency table → the pre-v9 'MBS/other' single leg renders and the
    components still sum to the headline."""
    conn.execute("DROP TABLE soma_agency_summary")
    _seed_summary_pair(conn)
    _seed_liq_live_shape(conn)
    out = soma_brief_line(conn)
    assert "Net Liq: -$38B/wk (SOMA +$4B, MBS/other -$19B, RRP +$0B, TGA -$23B)" in out


def test_store_soma_signals_carries_split_and_dedups(conn):
    _seed_summary_pair(conn)
    _seed_liq_live_shape(conn)
    _seed_agency(conn)
    assert store_soma_signals(conn) >= 1
    store_soma_signals(conn)  # same snapshot → REPLACE, no duplicate history
    n = conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id LIKE 'soma_%'"
    ).fetchone()[0]
    assert n == 2  # soma_net_liquidity + soma_walcl_gap (no holdings/pct seeded)
    row = conn.execute(
        "SELECT inputs_json FROM computed_signals WHERE signal_id='soma_net_liquidity'"
    ).fetchone()
    inputs = json.loads(row[0])
    assert inputs["mbs_b"] == pytest.approx(-19.0)
    assert inputs["other_b"] == pytest.approx(0.0, abs=1e-3)


# --- ops explainer ----------------------------------------------------------------


def test_ops_explainer_share_logic(conn):
    """Only P legs count; the qualifying leg is the largest share of |gross|."""
    _seed_week_ops(conn)
    leg = soma_ops_explainer(conn, PREV, CUR, 42.0)
    assert leg is not None
    assert leg["family"] == "tsy"
    assert leg["direction"] == "P"
    assert leg["n_ops"] == 3
    assert leg["amount_b"] == pytest.approx(30.0)
    assert leg["share"] == pytest.approx(30.0 / 42.0)
    # threshold above the achieved share → nothing explains the buys
    assert soma_ops_explainer(conn, PREV, CUR, 42.0, min_share=0.8) is None


def test_ops_explainer_window_edges(conn):
    """The effective date is COALESCE(settlement, operation_date) over the
    half-open window (PREV, CUR]: settled ON PREV belongs to last week's Δ,
    a NULL settlement falls back to the op date, settled after CUR belongs
    to next week's."""
    inside = (date.fromisoformat(CUR) - timedelta(days=1)).isoformat()
    after = (date.fromisoformat(CUR) + timedelta(days=1)).isoformat()
    _seed_ops(
        conn,
        [
            ("A", "tsy", inside, None, "P", "Results", 25e9),  # no settlement → op date
            ("B", "tsy", inside, PREV, "P", "Results", 25e9),  # settled ON PREV → out
            ("C", "tsy", inside, after, "P", "Results", 25e9),  # settles after CUR → out
        ],
    )
    leg = soma_ops_explainer(conn, PREV, CUR, 30.0)
    assert leg is not None
    assert leg["n_ops"] == 1  # only the settlement-NULL fallback row
    assert leg["amount_b"] == pytest.approx(25.0)


def test_ops_explainer_degrades(conn):
    _seed_summary_pair(conn)
    # no fed_operations table at all (pre-v9)
    conn.execute("DROP TABLE fed_operations")
    assert soma_ops_explainer(conn, PREV, CUR, 42.0) is None
    # table exists but empty
    conn.execute(
        "CREATE TABLE fed_operations(operation_id TEXT, family TEXT, "
        "operation_date TEXT, settlement_date TEXT, direction TEXT, status TEXT, "
        "amount REAL)"
    )
    assert soma_ops_explainer(conn, PREV, CUR, 42.0) is None
    # gross purchases unknown / zero → nothing to explain against
    inside = (date.fromisoformat(CUR) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO fed_operations(operation_id, family, operation_date, direction, "
        "status, amount) VALUES ('X','tsy',?, 'P','Results',?)",
        (inside, 30e9),
    )
    conn.commit()
    assert soma_ops_explainer(conn, PREV, CUR, None) is None
    assert soma_ops_explainer(conn, PREV, CUR, 0.0) is None


def test_brief_line_ops_suffix_without_buy_detail(conn):
    """Quiet week (both buy/matured legs under the display floor): the ops
    suffix still renders as its own parenthetical."""
    _seed_summary_pair(conn, cur_total=158e9, prev_total=160e9, weekly_change=3e9)
    _seed_liq_live_shape(conn)
    inside = (date.fromisoformat(CUR) - timedelta(days=2)).isoformat()
    _seed_ops(
        conn,
        [
            ("OP-1", "tsy", inside, inside, "P", "Results", 1.4e9),
            ("OP-2", "tsy", inside, inside, "P", "Results", 0.8e9),
        ],
    )
    out = soma_brief_line(conn)
    assert "ΔSOMA -$2B/wk (via 2 tsy-P ops)" in out
    assert "buy" not in out  # the one-week decomposition stayed suppressed


def test_ops_explainer_ambs_never_explains_tsy_gross(conn):
    """REGRESSION (review ronde-1): gross_b is the TREASURY weekly_change — an
    ambs purchase grows the AGENCY portfolio and must never be told as the
    source of the tsy buys, however large its share."""
    inside = (date.fromisoformat(CUR) - timedelta(days=1)).isoformat()
    _seed_ops(
        conn,
        [
            ("M1", "ambs", inside, inside, "P", "Results", 25e9),
            ("M2", "ambs", inside, inside, "P", "Results", 10e9),
        ],
    )
    assert soma_ops_explainer(conn, PREV, CUR, 30.0) is None  # no tsy leg at all


def test_brief_buy_detail_renders_on_holiday_span(conn):
    """REGRESSION (review ronde-1): a 6/8-day holiday-week span is still ONE
    interval — the buy/matured decomposition stays exact and must render;
    only a MISSED-harvest span (>9d) suppresses it."""
    # live shape: prev 158 → cur 162 (Δ +4) with gross buys +42, matured 38
    _seed_summary_pair(conn, cur_total=162e9, prev_total=158e9, weekly_change=42e9)
    _seed_liq_live_shape(conn)
    holiday_prev = (date.fromisoformat(CUR) - timedelta(days=8)).isoformat()
    conn.execute("DELETE FROM soma_summary WHERE as_of_date=?", (PREV,))
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, n_cusips) "
        "VALUES (?,?,?,5)",
        (holiday_prev, 158e9, 22e9),
    )
    conn.commit()
    out = soma_brief_line(conn)
    assert (
        "buy +$42B" in out and "matured $38B" in out and "/wk" in out
    )  # span 8 → normal week label


# --- dealer positioning ------------------------------------------------------------


def test_dealer_z_within_break_and_exact(conn):
    """z = population z of the latest value vs ALL obs of the current break
    (cross-checked against statistics.pstdev as an independent oracle)."""
    _seed_pd(conn, UST_KEYID, UST_VALUES)
    snap = dealers_snapshot(conn)
    ust = snap[UST_KEYID]
    assert ust["asofdate"] == CUR
    assert ust["value_musd"] == pytest.approx(436_000.0)
    assert ust["n_obs"] == 30
    vals_musd = [v * 1000.0 for v in UST_VALUES]
    exp_z = (vals_musd[-1] - statistics.mean(vals_musd)) / statistics.pstdev(vals_musd)
    assert ust["z"] == pytest.approx(exp_z)
    assert ust["z"] < -1.5  # deep drawdown → the ⚠ / RISK_OFF shape


def test_dealer_delta_4w(conn):
    """4w-ago base = the obs closest to 28 days back (index −5 on a weekly
    grid): 436 vs 468 → −$32B = −6.84%."""
    _seed_pd(conn, UST_KEYID, UST_VALUES)
    ust = dealers_snapshot(conn)[UST_KEYID]
    assert ust["base_4w_musd"] == pytest.approx(468_000.0)
    assert ust["delta_4w_musd"] == pytest.approx(-32_000.0)
    assert ust["delta_4w_pct"] == pytest.approx(-32 / 468 * 100)


def test_dealer_break_does_not_mix(conn):
    """A methodology restatement (SBP2013 → SBN2024, level shift 450→380):
    the z window is ONLY the current break's obs. Cross-checked against an
    independent oracle (statistics.pstdev on the post-break values alone) —
    and the blend's z would read the restatement as a drawdown."""
    old = [450 + (i % 7) for i in range(30)]
    new = [380 + (i % 4) for i in range(30)]
    _seed_pd(conn, UST_KEYID, old + new, break_after=_wednesday(30))
    ust = dealers_snapshot(conn)[UST_KEYID]
    assert ust["seriesbreak"] == "SBN2024"
    assert ust["n_obs"] == 30  # only the post-break obs feed the z
    new_musd = [v * 1000.0 for v in new]
    exp_z = (new_musd[-1] - statistics.mean(new_musd)) / statistics.pstdev(new_musd)
    assert ust["z"] == pytest.approx(exp_z)
    all_musd = [v * 1000.0 for v in old + new]
    z_mixed = (all_musd[-1] - statistics.mean(all_musd)) / statistics.pstdev(all_musd)
    assert z_mixed < ust["z"]  # blending the breaks fabricates a drawdown


def test_dealer_min_obs_guard(conn):
    """A young current break (< 26 obs) → z=None (honest), value/Δ still
    reported; a young single-break series also stays z=None."""
    _seed_pd(
        conn, UST_KEYID, [450 + (i % 5) for i in range(30)] + [420] * 5, break_after=_wednesday(5)
    )
    ust = dealers_snapshot(conn)[UST_KEYID]
    assert ust["n_obs"] == 5
    assert ust["z"] is None
    assert ust["value_musd"] == pytest.approx(420_000.0)
    _seed_pd(conn, "PDPOSMBS-TOT", [100.0] * 10)  # single young break
    assert dealers_snapshot(conn)["PDPOSMBS-TOT"]["z"] is None


def test_dealer_null_break_is_its_own_group(conn):
    """seriesbreak NULL everywhere → one homogeneous window (SQL '= ?' would
    have dropped every row; the Python-side grouping must not)."""
    _seed_pd(conn, UST_KEYID, UST_VALUES, break_after=None)
    conn.execute("UPDATE pd_positions SET seriesbreak=NULL")
    conn.commit()
    ust = dealers_snapshot(conn)[UST_KEYID]
    assert ust["n_obs"] == 30
    assert ust["z"] is not None


def test_dealers_snapshot_degrades(conn):
    conn.execute("DROP TABLE pd_positions")
    assert dealers_snapshot(conn) == {}
    assert dealers_brief_line(conn) is None
    assert store_dealer_signals(conn) == 0


def test_dealers_brief_line_render(conn):
    _seed_pd_all(conn)
    line = dealers_brief_line(conn)
    assert line is not None
    assert line.startswith("DLR: ")
    # curated display order, value in $B ($M → $B at the boundary)
    assert (
        line.index("UST")
        < line.index("MBS")
        < line.index("Corp")
        < line.index("Agency")
        < line.index("Muni")
    )
    assert "UST $436B z-" in line
    assert "⚠" in line  # |z| ≥ dealer_stress_z on the UST leg
    assert "MBS $116B z+0." in line
    assert "Muni $9B" in line
    # z renders 1dp signed
    ust = dealers_snapshot(conn)[UST_KEYID]
    assert f"z{ust['z']:+.1f}" in line
    # as-of date disclosed (weekly survey — the reader must see which week)
    assert line.endswith(f"(as of {CUR[5:]})")


def test_dealers_brief_line_marks_lagging_keyid_stale(conn):
    """REGRESSION (review ronde-1): a partially failed Thursday harvest leaves
    some keyids a week behind — their number must be marked (stale), not
    passed off as current beside this week's UST print."""
    _seed_pd_all(conn)
    # freeze MBS two weeks back (delete its last two weekly prints)
    conn.execute(
        "DELETE FROM pd_positions WHERE keyid='PDPOSMBS-TOT' AND asofdate > "
        f"date('{CUR}', '-14 day')"
    )
    conn.commit()
    line = dealers_brief_line(conn)
    assert "MBS" in line and "(stale)" in line
    assert "UST $436B" in line  # the fresh keyid renders unmarked


def test_delta_4w_pct_sign_on_negative_base(conn):
    """REGRESSION (review ronde-1): a net-SHORT class crossing toward zero is
    a POSITIVE move — pct must use abs(base), never flip sign with it."""
    vals = [-40, -38, -36, -34, -32, -30, -28, -26, -24, -22] + [-18, -14, -12, -10]
    _seed_pd(conn, UST_KEYID, vals)
    ust = dealers_snapshot(conn)[UST_KEYID]
    # latest −10 vs base(4w ago) −22: the short SHRINKS → delta +12 $B
    assert ust["delta_4w_musd"] == pytest.approx(12e3)
    # abs(base): +54.5% — the OLD delta/base form printed −54.5% (sign flip)
    assert ust["delta_4w_pct"] == pytest.approx(12.0 / 22.0 * 100.0)


def test_dealer_stress_silent_on_stale_snapshot(conn):
    """REGRESSION (review ronde-1): the trigger is freshness-capped — a
    restored/old DB whose latest survey week is >10d old must not fire even
    when its Δ4w clears the threshold."""
    stale_end = (date.fromisoformat(CUR) - timedelta(days=21)).isoformat()
    conn.execute("DELETE FROM pd_positions")
    for i, v in enumerate(UST_VALUES):
        d = (date.fromisoformat(stale_end) - timedelta(weeks=len(UST_VALUES) - 1 - i)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO pd_positions(asofdate, keyid, seriesbreak, value_musd) "
            "VALUES (?,?,?,?)",
            (d, UST_KEYID, "SBN2024", v * 1000.0),
        )
    conn.commit()
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []


def test_store_dealer_signals_replace_dedup(conn):
    _seed_pd_all(conn)
    assert store_dealer_signals(conn) == 1
    ust = dealers_snapshot(conn)[UST_KEYID]
    row = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='dealer_positions'"
    ).fetchone()
    assert row[0] == CUR  # weekly dedup key
    assert row[1] == pytest.approx(ust["z"], abs=1e-3)  # stored rounded to 3dp
    assert row[2] == "RISK_OFF"  # UST z < −1
    store_dealer_signals(conn)  # same survey week → REPLACE
    n = conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id='dealer_positions'"
    ).fetchone()[0]
    assert n == 1
    inputs = json.loads(
        conn.execute(
            "SELECT inputs_json FROM computed_signals WHERE signal_id='dealer_positions'"
        ).fetchone()[0]
    )
    assert set(inputs["keyids"]) == set(PD_KEYIDS.values())
    assert inputs["keyids"]["UST"]["value_b"] == pytest.approx(436.0)


def test_store_dealer_signals_state_neutral(conn):
    """Flat inventory → z ≈ 0 → NEUTRAL (not every week is a stress week)."""
    _seed_pd(conn, UST_KEYID, [450 + (i % 2) for i in range(30)])
    assert store_dealer_signals(conn) == 1
    state = conn.execute(
        "SELECT state FROM computed_signals WHERE signal_id='dealer_positions'"
    ).fetchone()[0]
    assert state in {"NEUTRAL", "N/A"}


# --- watcher: fx_swap_draw -----------------------------------------------------------


def test_fx_swap_draw_via_fed_operations_fires_urgent(conn):
    _seed_ops(conn, [("FXS-1", "fxs", _day(2), _day(2), None, "Results", 5e9)])
    from arkwatch.qa.watcher import check_all

    fired = check_all(conn)
    assert "fx_swap_draw" in fired
    row = conn.execute(
        "SELECT cooldown_key, priority, message FROM alert_deliveries "
        "WHERE alert_type='fx_swap_draw'"
    ).fetchone()
    assert row[0] == f"fx_swap_draw@{_day(2)}"  # per-operation-day key
    assert row[1] == "urgent"  # URGENT set member
    assert "$5.0B" in row[2]
    # permanent per-day dedup: the same draw never re-announces
    assert check_all(conn) == []


def test_fx_swap_draw_via_dedicated_table(conn):
    """The fetch layer may store fxs ops in their own table — the trigger
    probes it when fed_operations has no fxs rows."""
    conn.execute("DROP TABLE fed_operations")
    conn.execute("CREATE TABLE fxs_operations(operation_date TEXT, amount REAL)")
    conn.execute(
        "INSERT INTO fxs_operations(operation_date, amount) VALUES (?,?)",
        (_day(1), 3.5e9),
    )
    conn.commit()
    from arkwatch.qa.watcher import _fxs_recent_ops, check_all

    ops = _fxs_recent_ops(conn, days=7)
    assert ops == [(_day(1), pytest.approx(3.5))]
    assert "fx_swap_draw" in check_all(conn)


def test_fx_swap_draw_stale_and_missing_noop(conn):
    from arkwatch.qa.watcher import _fxs_recent_ops, check_all

    # stale draw (10d ago) is outside the 7d window
    _seed_ops(conn, [("FXS-1", "fxs", _day(10), _day(10), None, "Results", 5e9)])
    assert _fxs_recent_ops(conn, days=7) == []
    assert check_all(conn) == []
    # neither storage exists (pre-v9) → silent no-op, no crash
    conn.execute("DELETE FROM fed_operations WHERE family='fxs'")
    conn.execute("DROP TABLE fed_operations")
    assert _fxs_recent_ops(conn, days=7) == []
    assert check_all(conn) == []


def test_fx_swap_draw_unrecognized_schema_noop(conn):
    """A fxs_operations table whose date column we don't recognize = silent
    no-op (never a crash on a schema this layer never defined)."""
    conn.execute("DROP TABLE fed_operations")
    conn.execute("CREATE TABLE fxs_operations(some_col TEXT)")
    conn.execute("INSERT INTO fxs_operations(some_col) VALUES ('x')")
    conn.commit()
    from arkwatch.qa.watcher import _fxs_recent_ops

    assert _fxs_recent_ops(conn, days=7) == []


# --- watcher: dealer_stress -----------------------------------------------------------


def test_dealer_stress_fires_and_cooldowns_per_snapshot(conn):
    _seed_pd_all(conn)  # UST Δ4w = −6.8% ≤ −3.0 placeholder
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == ["dealer_stress"]
    row = conn.execute(
        "SELECT cooldown_key, priority FROM alert_deliveries WHERE alert_type='dealer_stress'"
    ).fetchone()
    assert row[0] == f"dealer_stress@{CUR}"  # permanent per-survey-week key
    assert row[1] == "normal"
    # weekly number never changes → no re-announcement on the next cycle
    assert check_all(conn) == []


def test_dealer_stress_quiet_when_flat(conn):
    _seed_pd(conn, UST_KEYID, [450 + (i % 2) for i in range(30)])  # Δ4w ≈ 0
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []


def test_dealer_stress_missing_delta_noop(conn):
    """< 5 obs → no 4w base → delta_4w_pct None → the trigger stays silent
    (honest None, no fabricated 0%)."""
    _seed_pd(conn, UST_KEYID, [450.0, 440.0, 430.0])
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []


# --- watcher: fed_ops_resume -----------------------------------------------------------


def test_fed_ops_resume_first_sale_after_purchases(conn):
    # history ≥ quiet_days+14d deep, else the depth gate stays quiet by design
    _seed_ops(
        conn,
        [
            ("P0", "tsy", _day(60), _day(60), "P", "Results", 10e9),
            ("P1", "tsy", _day(40), _day(40), "P", "Results", 10e9),
            ("P2", "tsy", _day(27), _day(27), "P", "Results", 10e9),
            ("P3", "tsy", _day(20), _day(20), "P", "Results", 10e9),
            ("S1", "tsy", _day(2), _day(2), "S", "Results", 8e9),  # first S in 30d+
        ],
    )
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == ["fed_ops_resume"]
    row = conn.execute(
        "SELECT cooldown_key, message FROM alert_deliveries WHERE alert_type='fed_ops_resume'"
    ).fetchone()
    assert row[0] == f"fed_ops_resume@{_day(2)}"
    assert "resumes sales" in row[1]
    assert check_all(conn) == []  # permanent per-op-date dedup


def test_fed_ops_resume_first_ambs_purchases(conn):
    """The other spec case: the ambs desk's first P after tsy-only history."""
    _seed_ops(
        conn,
        [
            ("P0", "tsy", _day(60), _day(60), "P", "Results", 10e9),
            ("P1", "tsy", _day(20), _day(20), "P", "Results", 10e9),
            ("A1", "ambs", _day(3), _day(3), "P", "Results", 1.5e9),
        ],
    )
    from arkwatch.qa.watcher import _fed_ops_resume, check_all

    assert _fed_ops_resume(conn, quiet_days=30) == ("A1", "ambs", "P", _day(3))
    assert "fed_ops_resume" in check_all(conn)


def test_fed_ops_resume_quiet_on_shallow_history(conn):
    """REGRESSION (review ronde-1): proving 'absent 30d' needs 44d of stored
    Results — a younger table (fresh v9 migration) must stay quiet instead of
    announcing a phantom resume off a gap it cannot see through."""
    _seed_ops(
        conn,
        [
            ("P1", "tsy", _day(20), _day(20), "P", "Results", 10e9),
            ("S1", "tsy", _day(2), _day(2), "S", "Results", 8e9),
        ],
    )
    from arkwatch.qa.watcher import _fed_ops_resume

    assert _fed_ops_resume(conn, quiet_days=30) is None


def test_fed_ops_resume_quiet_when_mix_running(conn):
    """Sales that started 25d ago are an OLD resume — outside the 14d recent
    window; the ops inside it all have priors within the quiet window."""
    _seed_ops(
        conn,
        [
            ("P1", "tsy", _day(40), _day(40), "P", "Results", 10e9),
            ("S0", "tsy", _day(25), _day(25), "S", "Results", 8e9),
            ("S1", "tsy", _day(10), _day(10), "S", "Results", 8e9),
            ("S2", "tsy", _day(2), _day(2), "S", "Results", 8e9),
        ],
    )
    from arkwatch.qa.watcher import _fed_ops_resume, check_all

    assert _fed_ops_resume(conn, quiet_days=30) is None
    assert check_all(conn) == []


def test_fed_ops_resume_ignores_fxs_and_missing_table(conn):
    from arkwatch.qa.watcher import _fed_ops_resume, check_all

    # fxs rows are fx_swap_draw's domain, not a pacing signal
    _seed_ops(conn, [("FXS-1", "fxs", _day(2), _day(2), None, "Results", 5e9)])
    assert _fed_ops_resume(conn, quiet_days=30) is None
    conn.execute("DROP TABLE fed_operations")
    assert _fed_ops_resume(conn, quiet_days=30) is None
    assert check_all(conn) == []


def test_watcher_check_all_safe_on_empty_v9_tables(conn):
    """All three new triggers coexist quietly on an empty v9 DB (the
    state right after the migration, before the first harvests)."""
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []
