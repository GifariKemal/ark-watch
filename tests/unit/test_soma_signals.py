"""Tests for the SOMA signal layer (signals/soma.py).

Schema basis: migration v7 (soma_holdings + soma_summary, locked in
PLAN-SOMA §3). The fixture DDL is CREATE IF NOT EXISTS, so these tests run
both before and after the migration lands in db.py. All money in the tables
is raw USD; every signal/output is $B (PAR_TO_B boundary conversion).

Liquidity anchors: WALCL ($M) and TGA ($M) and RRP ($B) are read AT the SOMA
Wednesday dates, so the seeds below pin observations to CUR/PREV instead of
"now" — a now-relative seed would drift off the anchored lookup.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.signals.soma import (
    soma_brief_line,
    soma_float_scarcity,
    soma_maturity_profile,
    soma_net_liquidity,
    soma_roll_off_alert,
    soma_specials_new_alert,
    soma_walcl_gap_alert,
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


def _wednesday(offset_weeks: int = 0) -> str:
    """A real Wednesday date `offset_weeks` back from today (0 = most recent).

    Fixed literal dates (e.g. '2026-09-02') age past SOMA_ALERT_MAX_AGE_DAYS
    and silently break every freshness-gated test two weeks later."""
    today = datetime.now(UTC).date()
    wed = today - timedelta(days=(today.weekday() - 2) % 7)
    return (wed - timedelta(weeks=offset_weeks)).isoformat()


CUR = _wednesday()  # Wednesday snapshot
PREV = _wednesday(1)  # prior Wednesday


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    c.executescript(SCHEMA_V7)
    yield c
    c.close()


def _seed_holdings(conn, with_change=True):
    """Five CUSIPs across maturity buckets at the CUR snapshot. Raw USD.

    Per-CUSIP changeFromPriorWeek: Bill 0 · Note-2y −8B (deepest cut) ·
    Note-5y 0 · Bond-10y +1B (purchase) · TIPS −2B → the 1-3y bucket
    drains −10B/wk.
    """
    rows = [
        # (cusip, security_type, maturity, par, change_week)
        ("912828AA1", "Bill", "2026-09-08", 12e9, 0.0),
        ("912828BB2", "Note", "2028-09-15", 40e9, -8e9),
        ("912828CC3", "Note", "2031-06-15", 60e9, 0.0),
        ("912828DD4", "Bond", "2036-03-15", 30e9, 1e9),
        ("912828EE5", "TIPS", "2029-01-15", 20e9, -2e9),
    ]
    for cusip, sec, mat, par, chg in rows:
        conn.execute(
            "INSERT OR REPLACE INTO soma_holdings"
            "(as_of_date, cusip, security_type, maturity_date, par_value, change_week) "
            "VALUES (?,?,?,?,?,?)",
            (CUR, cusip, sec, mat, par, chg if with_change else None),
        )
    conn.commit()


def _seed_summary(
    conn,
    as_of=CUR,
    total_par=162e9,
    tips=20e9,
    weekly_change=-18e9,
    rolling_off_7d=12e9,
    rolling_off_30d=46e9,
    prev_total_par=180e9,
):
    # prev_total_par 180 makes Δtotal = −18 == weekly_change → matured 0
    # (the "clean" shape). The portfolio-change regression test overrides it.
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, weekly_change, "
        "rolling_off_7d, rolling_off_30d, n_cusips) VALUES (?,?,?,?,?,?,5)",
        (as_of, total_par, tips, weekly_change, rolling_off_7d, rolling_off_30d),
    )
    # prior-week snapshot (22B TIPS) — needed for tips change + portfolio Δ
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, n_cusips) "
        "VALUES (?,?,?,5)",
        (PREV, prev_total_par, 22e9),
    )
    conn.commit()


def _seed_obs(conn, sid, at_cur, at_prev):
    """Realtime rows pinned to the Wednesday anchors (unit = native; pass
    None to leave an anchor unseeded — e.g. this week's WALCL release
    missing). raw_observations has an FK into series_registry, so the series
    must be registered before the observations land."""
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


def _seed_rrp(conn, latest=400.0, week_ago=525.0):
    """ON-RRP rows ($B) at the anchors: Δ = latest − week_ago."""
    _seed_obs(conn, "FRED:RRPONTSYD", latest, week_ago)


def _seed_walcl_tga(
    conn,
    walcl_cur=6730912.0,
    walcl_prev=6745699.0,
    tga_cur=959435.0,
    tga_prev=936406.0,
):
    """WALCL + TGA rows ($M) at the anchors (live 2026-08-19→26 shape)."""
    _seed_obs(conn, "FRED:WALCL", walcl_cur, walcl_prev)
    _seed_obs(conn, "FISCAL:TGA_DAILY", tga_cur, tga_prev)


def _seed_pct(conn, prev_window=True):
    """Float-scarcity rows: two issues at the 70% cap at CUR; at PREV one was
    already capped (AAA…) and one was not (BBB… → the NEW entrant)."""
    rows = [
        # (as_of, cusip, pct, par)
        (CUR, "912828AA9", 70.0, 30e9),
        (CUR, "912828BB8", 69.9, 12e9),  # NEW entrant (55% at PREV)
        (CUR, "912828CC7", 45.0, 10e9),  # high but not capped
        (CUR, "912828DD6", 12.0, 60e9),  # ordinary
    ]
    if prev_window:
        rows += [
            (PREV, "912828AA9", 69.9, 30e9),  # already at cap before (69.9 ≥ 69.5)
            (PREV, "912828BB8", 55.0, 12e9),  # → NEW entrant at CUR
        ]
    for as_of, cusip, pct, par in rows:
        conn.execute(
            "INSERT OR REPLACE INTO soma_holdings(as_of_date, cusip, security_type, "
            "maturity_date, par_value, pct_outstanding) VALUES (?,?,?,?,?,?)",
            (as_of, cusip, "Note", "2039-05-15", par, pct),
        )
    conn.commit()


# --- maturity profile -------------------------------------------------------


def test_maturity_profile_buckets_and_steepest(conn):
    _seed_holdings(conn)
    _seed_summary(conn)
    prof = soma_maturity_profile(conn)
    assert prof["as_of_date"] == CUR
    by_name = {b["name"]: b for b in prof["buckets"]}
    # $B conversion at the boundary: 40e9 USD → 40.0 $B (a 1000× slip would
    # read 40000.0 here)
    assert by_name["0-1y"]["par"] == pytest.approx(12.0)
    assert by_name["1-3y"]["par"] == pytest.approx(60.0)  # Note 40 + TIPS 20
    assert by_name["1-3y"]["n_cusips"] == 2
    assert by_name["1-3y"]["change_week"] == pytest.approx(-10.0)  # −8 −2
    assert by_name["7-10y"]["change_week"] == pytest.approx(1.0)
    assert prof["steepest_bucket"] == "1-3y"
    # bucket pars sum back to the snapshot total (162B)
    assert sum(b["par"] for b in prof["buckets"]) == pytest.approx(162.0)
    assert prof["tips"]["par"] == pytest.approx(20.0)
    assert prof["tips"]["change"] == pytest.approx(-2.0)


def test_maturity_profile_no_change_info_no_steepest(conn):
    """Holdings without changeFromPriorWeek (pre-2013 history) → all bucket
    changes 0 → steepest None (a zero-drain week states nothing)."""
    _seed_holdings(conn, with_change=False)
    _seed_summary(conn)
    prof = soma_maturity_profile(conn)
    assert all(b["change_week"] == 0.0 for b in prof["buckets"])
    assert prof["steepest_bucket"] is None


def test_maturity_profile_empty_db(conn):
    assert soma_maturity_profile(conn) == {}


# --- roll-off alert ----------------------------------------------------------


def test_roll_off_alert_fires_above_threshold(conn):
    _seed_summary(conn, rolling_off_7d=25e9)
    a = soma_roll_off_alert(conn, threshold_b=20.0)
    assert a is not None
    assert a["rolling_off_7d_b"] == pytest.approx(25.0)
    assert a["rolling_off_30d_b"] == pytest.approx(46.0)
    assert a["threshold_b"] == 20.0


def test_roll_off_alert_quiet_below_threshold(conn):
    _seed_summary(conn, rolling_off_7d=12e9)
    assert soma_roll_off_alert(conn, threshold_b=65.0) is None


def test_roll_off_alert_stale_snapshot_never_fires(conn):
    stale = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    _seed_summary(conn, as_of=stale, rolling_off_7d=25e9)
    assert soma_roll_off_alert(conn, threshold_b=20.0) is None


def test_roll_off_alert_missing_tables(conn):
    conn.execute("DROP TABLE soma_holdings")
    conn.execute("DROP TABLE soma_summary")
    assert soma_roll_off_alert(conn, threshold_b=20.0) is None


# --- net liquidity (FR-34 + portfolio-change semantics) -----------------------


def test_net_liquidity_rrp_fall_is_injection(conn):
    """Facility −125B = cash returning to markets → contribution +125B.

    Δnet = ΔSOMA − ΔRRP = −18 − (−125) = +107 → INJECTING (FR-34:
    net-liq = WALCL − RRP − TGA; no WALCL/TGA seeded → SOMA-anchored).
    """
    _seed_summary(conn)
    _seed_rrp(conn, latest=400.0, week_ago=525.0)
    liq = soma_net_liquidity(conn)
    assert liq["soma_change_b"] == pytest.approx(-18.0)
    assert liq["rrp_change_b"] == pytest.approx(-125.0)
    assert liq["rrp_contribution_b"] == pytest.approx(125.0)
    assert liq["net_change_b"] == pytest.approx(107.0)
    assert liq["state"] == "INJECTING"


def test_net_liquidity_uses_total_par_not_gross(conn):
    """REGRESSION (live 2026-08-26 shape): weekly_change is GROSS purchases of
    surviving issues — par maturing during the week silently leaves the
    snapshot. Portfolio Δ = Δtotal_par; matured = gross − net."""
    _seed_summary(
        conn,
        total_par=162e9,
        prev_total_par=158e9,  # Δtotal = +4B …
        weekly_change=42e9,  # … while gross purchases = +42B
        rolling_off_7d=12e9,
    )
    liq = soma_net_liquidity(conn)
    assert liq["soma_change_b"] == pytest.approx(4.0)  # NOT 42
    assert liq["gross_change_b"] == pytest.approx(42.0)
    assert liq["matured_b"] == pytest.approx(38.0)
    assert liq["net_change_b"] == pytest.approx(4.0)  # no RRP/WALCL/TGA seeded


def test_net_liquidity_fr34_full_decomposition(conn):
    """With WALCL + TGA + RRP: Δnet = ΔWALCL − ΔRRP − ΔTGA, decomposed into
    SOMA + MBS/other + RRP + TGA contributions that sum to the headline."""
    _seed_summary(conn)  # ΔSOMA = −18
    _seed_rrp(conn, latest=400.0, week_ago=525.0)  # contrib +125
    _seed_walcl_tga(conn)  # ΔWALCL −14.787, ΔTGA +23.029
    liq = soma_net_liquidity(conn)
    assert liq["dwalcl_b"] == pytest.approx(-14.787)
    assert liq["mbs_other_b"] == pytest.approx(-14.787 - (-18.0))  # +3.213
    assert liq["tga_change_b"] == pytest.approx(23.029)
    assert liq["tga_contribution_b"] == pytest.approx(-23.029)
    assert liq["net_change_b"] == pytest.approx(-14.787 + 125.0 - 23.029)
    assert liq["state"] == "INJECTING"
    # components sum to the headline (the brief relies on this)
    comp = (
        liq["soma_change_b"]
        + liq["mbs_other_b"]
        + liq["rrp_contribution_b"]
        + liq["tga_contribution_b"]
    )
    assert comp == pytest.approx(liq["net_change_b"])


def test_net_liquidity_walcl_missing_falls_back_to_soma(conn):
    """No WALCL/TGA rows → ΔWALCL=None, MBS/other=None, headline anchored on
    ΔSOMA (the brief shows only the components it actually has)."""
    _seed_summary(conn)
    _seed_rrp(conn)
    liq = soma_net_liquidity(conn)
    assert liq["dwalcl_b"] is None
    assert liq["mbs_other_b"] is None
    assert liq["net_change_b"] == pytest.approx(-18.0 + 125.0)


def test_net_liquidity_walcl_same_row_both_anchors_no_fake_zero(conn):
    """REGRESSION: this week's WALCL Wednesday row missing (FRED release
    late / harvest failed) → BOTH anchors resolve to the SAME prior row.
    That must degrade to the SOMA-anchored fallback — a naive same-row diff
    fabricates ΔWALCL=0.0 and a fictional 'MBS/other = −ΔSOMA' component,
    and can false-fire the WALCL-gap integrity tripwire."""
    _seed_summary(conn)  # ΔSOMA = −18
    _seed_obs(conn, "FRED:WALCL", None, 6745699.0)  # only the PREV anchor row exists
    _seed_rrp(conn, latest=400.0, week_ago=525.0)
    liq = soma_net_liquidity(conn)
    assert liq["dwalcl_b"] is None  # NOT 0.0
    assert liq["mbs_other_b"] is None
    assert liq["net_change_b"] == pytest.approx(-18.0 + 125.0)  # SOMA-anchored
    # same guard protects the integrity tripwire from the misalignment
    assert soma_walcl_gap_alert(conn) is None
    assert liq["span_days"] == 7  # normal week for the /wk label


def test_net_liquidity_without_rrp_uses_soma_only(conn):
    _seed_summary(conn)
    liq = soma_net_liquidity(conn)
    assert liq["rrp_contribution_b"] is None
    assert liq["net_change_b"] == pytest.approx(-18.0)
    assert liq["state"] == "DRAINING"


def test_net_liquidity_empty_db(conn):
    assert soma_net_liquidity(conn) == {}


# --- WALCL identity tripwire (BUILD-PLAN §6.3) ---------------------------------


def test_walcl_gap_alert_quiet_inside_envelope(conn):
    _seed_summary(conn)
    _seed_walcl_tga(conn)  # gap = +3.2B — well inside ±80
    assert soma_walcl_gap_alert(conn, threshold_b=80.0) is None


def test_walcl_gap_alert_fires_beyond_envelope(conn):
    _seed_summary(conn)  # ΔSOMA = −18
    _seed_walcl_tga(conn, walcl_cur=6830912.0)  # ΔWALCL = +85.2 → gap +103.2
    a = soma_walcl_gap_alert(conn, threshold_b=80.0)
    assert a is not None
    assert a["gap_b"] == pytest.approx(85.213 - (-18.0), abs=0.1)
    assert a["dsoma_b"] == pytest.approx(-18.0)
    assert a["dwalcl_b"] == pytest.approx(85.213, abs=0.1)


def test_walcl_gap_alert_needs_both_legs(conn):
    _seed_summary(conn)  # no WALCL rows at all
    assert soma_walcl_gap_alert(conn) is None


# --- float scarcity / repo specials (§6c) ---------------------------------------


def test_float_scarcity_counts_and_entrants(conn):
    _seed_pct(conn)
    sc = soma_float_scarcity(conn)
    assert sc["as_of_date"] == CUR
    assert sc["n_at_cap"] == 2
    assert sc["par_at_cap_b"] == pytest.approx(42.0)
    # only BBB… (55% → 69.9%) is NEW; AAA… was already capped at PREV
    assert [e["cusip"] for e in sc["new_entrants"]] == ["912828BB8"]
    assert sc["new_entrants"][0]["par_b"] == pytest.approx(12.0)


def test_float_scarcity_left_edge_is_not_entrant(conn):
    """A fresh backfill's left edge (no prior window at all) must not flag
    every capped issue as NEW (19 false events at 2024-08-07 otherwise)."""
    _seed_pct(conn, prev_window=False)
    sc = soma_float_scarcity(conn)
    assert sc["n_at_cap"] == 2
    assert sc["new_entrants"] == []


def test_float_scarcity_no_pct_data(conn):
    _seed_holdings(conn)  # pct NULL everywhere
    assert soma_float_scarcity(conn) == {}


def test_float_scarcity_six_day_gap_no_false_entrant(conn):
    """REGRESSION (live-verified calendar): the NY Fed as-of list has real
    6-day weeks (Tuesday as-of when Wednesday is a holiday — Christmas,
    Juneteenth, Veterans Day). A 7d-lookback right edge missed those snapshots
    and mass-flagged every already-capped issue as NEW (~20 issues/$500B)."""
    gap6 = (date.fromisoformat(CUR) - timedelta(days=6)).isoformat()  # holiday-week snapshot
    for as_of, pct in ((gap6, 69.9), (CUR, 70.0)):
        conn.execute(
            "INSERT OR REPLACE INTO soma_holdings(as_of_date, cusip, security_type, "
            "maturity_date, par_value, pct_outstanding) VALUES (?,?,?,?,?,?)",
            (as_of, "912828AA9", "Note", "2039-05-15", 30e9, pct),
        )
    conn.commit()
    sc = soma_float_scarcity(conn)
    assert sc["n_at_cap"] == 1
    assert sc["new_entrants"] == []  # capped 6d ago → NOT new


def test_specials_new_alert_min_par_filter(conn):
    _seed_pct(conn)
    a = soma_specials_new_alert(conn, min_par_b=10.0)
    assert a is not None
    assert a["n"] == 1
    assert a["par_b"] == pytest.approx(12.0)
    assert soma_specials_new_alert(conn, min_par_b=20.0) is None


def test_specials_new_alert_quiet_without_entrants(conn):
    _seed_holdings(conn)
    _seed_summary(conn)
    assert soma_specials_new_alert(conn) is None


# --- persistence ---------------------------------------------------------------


def test_store_soma_signals_persists_three_rows(conn):
    _seed_holdings(conn)
    _seed_summary(conn)
    _seed_rrp(conn, latest=400.0, week_ago=525.0)
    assert store_soma_signals(conn) == 3
    r = conn.execute(
        "SELECT ts, value, state FROM computed_signals WHERE signal_id='soma_buckets'"
    ).fetchone()
    assert r[0] == CUR  # ts = SOMA as_of_date (weekly dedup key)
    assert r[1] == pytest.approx(162.0)
    assert r[2] == "1-3y"
    r = conn.execute(
        "SELECT value, state FROM computed_signals WHERE signal_id='soma_tips_split'"
    ).fetchone()
    assert r[0] == pytest.approx(20.0)
    assert r[1] == "FALLING"
    r = conn.execute(
        "SELECT value, state FROM computed_signals WHERE signal_id='soma_net_liquidity'"
    ).fetchone()
    assert r[0] == pytest.approx(107.0)
    assert r[1] == "INJECTING"


def test_store_soma_signals_persists_five_rows_full(conn):
    """With pct + WALCL + TGA: + soma_specials (NEW_ENTRANT) + soma_walcl_gap."""
    _seed_holdings(conn)
    _seed_summary(conn)
    _seed_rrp(conn)
    _seed_walcl_tga(conn)
    _seed_pct(conn)
    assert store_soma_signals(conn) == 5
    r = conn.execute(
        "SELECT value, state FROM computed_signals WHERE signal_id='soma_specials'"
    ).fetchone()
    assert r[0] == 2
    assert r[1] == "NEW_ENTRANT"
    r = conn.execute(
        "SELECT value, state FROM computed_signals WHERE signal_id='soma_walcl_gap'"
    ).fetchone()
    assert r[0] == pytest.approx(3.213, abs=0.01)
    assert r[1] == "OK"


def test_store_soma_signals_rerun_replaces_not_duplicates(conn):
    _seed_holdings(conn)
    _seed_summary(conn)
    _seed_rrp(conn)
    store_soma_signals(conn)
    store_soma_signals(conn)  # same snapshot → REPLACE, no duplicate history
    n = conn.execute(
        "SELECT COUNT(*) FROM computed_signals WHERE signal_id LIKE 'soma_%'"
    ).fetchone()[0]
    assert n == 3


def test_store_soma_signals_empty_db_is_noop(conn):
    assert store_soma_signals(conn) == 0


# --- brief line -----------------------------------------------------------------


def test_brief_line_renders_full_block(conn):
    _seed_holdings(conn)
    _seed_summary(conn)
    _seed_rrp(conn, latest=400.0, week_ago=525.0)
    out = soma_brief_line(conn)
    lines = out.split("\n")
    assert len(lines) == 3  # no pct data → no Float line
    assert lines[0].startswith("Fed SOMA:")
    assert "ΔSOMA -$18B/wk" in lines[0]
    assert "roll 7d $12B" in lines[0]
    assert "30d $46B" in lines[0]
    assert f"(as of {CUR[5:]})" in lines[0]
    assert "Kurva: 1-3y -$10B/wk (steepest)" in lines[1]
    assert "TIPS -$2B/wk" in lines[1]
    # components are liquidity CONTRIBUTIONS → they sum to the headline
    assert "Net Liq: +$107B/wk (SOMA -$18B, RRP +$125B)" in lines[2]


def test_brief_line_buy_matured_detail(conn):
    """Live 2026-08-26 shape: portfolio +$4B but gross buys +$42B with $38B
    maturing — both legs ≥ the display floor → detail renders."""
    _seed_holdings(conn)
    _seed_summary(conn, prev_total_par=158e9, weekly_change=42e9, rolling_off_7d=12e9)
    out = soma_brief_line(conn)
    assert "ΔSOMA +$4B/wk (buy +$42B · matured $38B)" in out


def test_brief_line_fr34_components_and_float(conn):
    _seed_holdings(conn)
    _seed_summary(conn)
    _seed_rrp(conn, latest=400.0, week_ago=525.0)
    _seed_walcl_tga(conn)
    _seed_pct(conn)
    out = soma_brief_line(conn)
    lines = out.split("\n")
    assert len(lines) == 4
    assert "Net Liq: +$87B/wk (SOMA -$18B, MBS/other +$3B, RRP +$125B, TGA -$23B)" in lines[2]
    assert "Float: 2 issues Fed-capped ($42B par) · NEW 1 ($12B)" in lines[3]


def test_brief_net_liq_single_snapshot_is_silent(conn):
    """First-ever harvest (one soma_summary row, no prior week): no ΔSOMA and
    no ΔWALCL (it needs both snapshot anchors) → the whole Net Liq line is
    absent rather than partially rendered."""
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, n_cusips) "
        "VALUES (?,?,?,5)",
        (CUR, 162e9, 20e9),
    )
    conn.commit()
    _seed_rrp(conn, latest=400.0, week_ago=525.0)
    _seed_walcl_tga(conn)
    out = soma_brief_line(conn)
    assert out == f"Fed SOMA: ΔSOMA n/a (as of {CUR[5:]})"
    assert "Net Liq" not in out


def test_brief_net_liq_fedbs_anchor_when_prior_total_null(conn):
    """Prior snapshot row exists but its total_par is NULL (restated/corrupt
    row): ΔSOMA=None while ΔWALCL is computable → the anchor renders as one
    FedBS piece so the displayed components still sum to the headline."""
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, n_cusips) "
        "VALUES (?,?,?,5)",
        (CUR, 162e9, 20e9),
    )
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, n_cusips) "
        "VALUES (?,?,?,5)",
        (PREV, None, 22e9),
    )
    conn.commit()
    _seed_rrp(conn, latest=400.0, week_ago=525.0)
    _seed_walcl_tga(conn)
    out = soma_brief_line(conn)
    # Δnet = ΔWALCL − ΔRRP − ΔTGA = −14.787 + 125 − 23.029 ≈ +87.2
    assert "Net Liq: +$87B/wk (FedBS -$15B, RRP +$125B, TGA -$23B)" in out


def test_brief_span_label_on_missed_week(conn):
    """A missed harvest (14d between stored snapshots) must not present a
    two-week Δ as a weekly rate — the label reads /2wk AND the one-week
    buy/matured decomposition stays suppressed (the fixture's gross/matured
    both clear the $10B display floor, so only the span rule can suppress)."""
    _seed_holdings(conn)
    _seed_summary(conn)
    conn.execute("DELETE FROM soma_summary WHERE as_of_date=?", (PREV,))
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, weekly_change, n_cusips) "
        "VALUES (?,?,?,?,5)",
        (_wednesday(2), 180e9, 22e9, 42e9),  # 14d before CUR; gross +42 → matured 60 ≥ floor
    )
    conn.commit()
    out = soma_brief_line(conn)
    assert "ΔSOMA -$18B/2wk" in out
    assert "Net Liq: -$18B/2wk" in out
    assert "buy" not in out  # probative: legs clear the floor, span rule is the suppressor


def test_brief_stale_suffix_after_a_week(conn):
    """A snapshot older than a week (missed Friday harvests) is flagged on
    the header instead of silently aging."""
    stale_d = datetime.now(UTC).date() - timedelta(days=9)
    _seed_summary(conn, as_of=stale_d.isoformat())
    # drop any seeded row newer than the stale snapshot (PREV is fixed-date)
    conn.execute("DELETE FROM soma_summary WHERE as_of_date > ?", (stale_d.isoformat(),))
    conn.commit()
    out = soma_brief_line(conn)
    assert f"(stale {(datetime.now(UTC).date() - stale_d).days}d)" in out


def test_brief_line_skips_noise_curve_segment(conn):
    """Live 2026-08-26 shape: no draining bucket, TIPS change exactly 0 —
    sub-display-resolution segments must not render as '+$0B/wk' noise."""
    _seed_holdings(conn, with_change=False)  # all bucket changes 0 → no steepest
    _seed_summary(conn)  # tips 20 vs 22 → −$2B passes the floor… so pin both
    conn.execute(
        "INSERT OR REPLACE INTO soma_summary(as_of_date, total_par, tips, n_cusips) "
        "VALUES (?,?,?,5)",
        (PREV, 162e9, 20e9),
    )
    conn.commit()
    out = soma_brief_line(conn)
    lines = out.split("\n")
    assert len(lines) == 2  # header + Net Liq only — no Kurva line
    assert not any("Kurva" in ln for ln in lines)


def test_brief_line_none_without_data(conn):
    assert soma_brief_line(conn) is None


def test_brief_line_none_without_tables(conn):
    conn.execute("DROP TABLE soma_holdings")
    conn.execute("DROP TABLE soma_summary")
    assert soma_brief_line(conn) is None


# --- watcher smoke ----------------------------------------------------------------


def test_watcher_check_all_safe_on_empty_db(conn):
    """check_all must not crash when the soma tables exist but are empty
    (the pre-backfill state right after migration v7)."""
    from arkwatch.qa.watcher import check_all

    assert check_all(conn) == []


def test_soma_alert_cooldown_keyed_per_snapshot(conn):
    """The roll-off alert dedups per WEEKLY snapshot: the same as_of_date is
    cooling down, but the next week's snapshot is a different key and alerts.
    (Without the per-snapshot key the 6h cooldown would re-announce one
    weekly number ~4x/day for its whole freshness window.)"""
    from arkwatch.qa.watcher import _cooldown_active

    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO alert_deliveries(alert_type, triggered_at, cooldown_key, "
        "priority, status, message) VALUES ('soma_roll_off', ?, "
        "'soma_roll_off@2026-08-26', 'normal', 'sent', 'x')",
        (now,),
    )
    conn.commit()
    assert _cooldown_active(conn, "soma_roll_off@2026-08-26")
    assert not _cooldown_active(conn, f"soma_roll_off@{CUR}")


def test_fire_per_snapshot_key_never_reannounces(conn):
    """REGRESSION: per-snapshot keys dedup PERMANENTLY. A time-windowed check
    alone let the same weekly figure re-fire every cooldown window — ~28
    duplicate alerts per snapshot at the 6h default (and ~24/day under the
    1h VIX-stress cooldown)."""
    from arkwatch.qa.watcher import _cooldown_active, _fire

    old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO alert_deliveries(alert_type, triggered_at, cooldown_key, "
        "priority, status, message) VALUES ('soma_roll_off', ?, "
        "'soma_roll_off@2026-08-26', 'normal', 'sent', 'x')",
        (old,),
    )
    conn.commit()
    # permanent mode (weekly triggers): age is irrelevant
    assert _cooldown_active(conn, "soma_roll_off@2026-08-26", permanent=True)
    # windowed mode (every non-snapshot trigger) is unchanged by this
    assert not _cooldown_active(conn, "vix_backwardation")
    # and _fire refuses to re-announce that snapshot
    assert not _fire(conn, "soma_roll_off", "c", "x", "y", cooldown_key="soma_roll_off@2026-08-26")


def test_permanent_dedup_counts_pending_and_ignores_failed(conn):
    """A 'pending' row still awaiting delivery blocks the snapshot (no
    re-fire while a delivery attempt is in flight) — but a TERMINAL 'failed'
    row (outbox marks it at the attempt cap) must not block, or a weekly
    alert lost to a Telegram outage could never re-announce after recovery."""
    from arkwatch.qa.watcher import _cooldown_active

    old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO alert_deliveries(alert_type, triggered_at, cooldown_key, "
        "priority, status, message) VALUES ('soma_roll_off', ?, "
        "'soma_roll_off@2026-08-26', 'normal', 'pending', 'x')",
        (old,),
    )
    conn.commit()
    assert _cooldown_active(conn, "soma_roll_off@2026-08-26", permanent=True)
    conn.execute(
        "UPDATE alert_deliveries SET status='failed', last_error='send failed after 5 attempts'"
    )
    conn.commit()
    assert not _cooldown_active(conn, "soma_roll_off@2026-08-26", permanent=True)
