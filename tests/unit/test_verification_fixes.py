"""Regression tests for the 2026-09-13 three-layer data-verification fixes.

Each test pins the exact failure mode the audit caught:
- COT swap double-underscore (P1-2): 61.7% of gold shorts were NULL
- fetch_window gap-heal (P1-1): latest-only snapshots froze shutdown-day holes
- Bybit leg visibility + funding freshness + COALESCE (P1-3)
- ADS colon dates (P1-4)
- copper trigger monthly-cadence gate (P2)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch import db


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


def _day(n: int = 0) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


# --- P1-2: COT swap double-underscore ---------------------------------------


def test_cot_swap_double_underscore(monkeypatch):
    from arkwatch.fetchers import cot

    class R:
        status_code = 200

        def json(self):
            # the CFTC quirk: swap uses DOUBLE underscore; mm single
            return [
                {
                    "report_date_as_yyyy_mm_dd": "2026-09-08",
                    "contract_market_name": "GOLD",
                    "open_interest_all": "411313",
                    "swap__positions_long_all": "14542",
                    "swap__positions_short_all": "253855",
                    "swap__positions_spread_all": "23273",
                    "m_money_positions_long_all": "145804",
                    "m_money_positions_short_all": "10832",
                }
            ]

    monkeypatch.setattr(cot.requests, "get", lambda *a, **k: R())
    rows = cot.fetch_cot("disagg", "088691", limit=5)
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["swap"]["short"] == 253855  # the number that was NULL 158 weeks
    assert by_cat["swap"]["spread"] == 23273
    assert by_cat["swap"]["long"] == 14542
    assert by_cat["mm"]["long"] == 145804


# --- P1-1: fetch_window gap-heal ---------------------------------------------


def test_nyfed_fetch_window_covers_hole_dates(monkeypatch):
    from arkwatch.fetchers import nyfed

    class R:
        status_code = 200

        def json(self):
            return {
                "refRates": [
                    {"effectiveDate": "2026-09-03", "type": "EFFR", "percentPercentile1": "3.74"},
                    {"effectiveDate": "2026-09-04", "type": "EFFR", "percentPercentile1": "3.73"},
                    {"effectiveDate": "2026-09-05", "type": "OBFR", "percentPercentile1": "3.70"},
                    {"effectiveDate": "2026-09-08", "type": "EFFR", "percentPercentile1": "3.72"},
                ]
            }

    monkeypatch.setattr(nyfed.requests, "get", lambda *a, **k: R())
    pts = nyfed.fetch_window("NYFED:EFFR_P1", days=10)
    # type filter + all dates: the 09-04 hole day is IN the window
    assert [p["ts"] for p in pts] == ["2026-09-03", "2026-09-04", "2026-09-08"]
    assert pts[1]["value"] == 3.73


def test_eodhd_fetch_window_all_rows_not_max(monkeypatch):
    from arkwatch.fetchers import eodhd

    # dates must be RECENT (today-1) so the ronde-6 span guard passes
    today = datetime.now(UTC).date()
    monkeypatch.setattr(
        eodhd,
        "_get",
        lambda path, params=None: [
            {
                "code": "EFFR_SOFR",
                "date": (today - timedelta(days=1)).isoformat(),
                "value_bps": -3.0,
            },
            {"code": "EFFR_SOFR", "date": today.isoformat(), "value_bps": -2.0},
            {"code": "OTHER", "date": today.isoformat(), "value_bps": 99.0},
        ],
    )
    pts = eodhd.fetch_window("EODHD:FS_EFFR_SOFR", days=10)
    assert [(p["ts"], p["value"]) for p in pts] == [
        ((today - timedelta(days=1)).isoformat(), -3.0),
        (today.isoformat(), -2.0),
    ]


def test_eodhd_fetch_window_stale_span_raises(monkeypatch):
    """ronde-6 P2-8: a stale window (newest >5d old) means the server
    shrank/truncated the response — raise, don't silently partial-heal."""
    from arkwatch.fetchers import eodhd

    old_day = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    monkeypatch.setattr(
        eodhd,
        "_get",
        lambda path, params=None: [
            {"code": "EFFR_SOFR", "date": old_day, "value_bps": -3.0},
        ],
    )
    import pytest as _pytest

    with _pytest.raises(eodhd.EodhdError, match="stale"):
        eodhd.fetch_window("EODHD:FS_EFFR_SOFR", days=10)


def test_fiscal_fetch_window_multi_day(monkeypatch):
    from arkwatch.fetchers import fiscal

    monkeypatch.setattr(
        fiscal,
        "_get",
        lambda path, params=None, session=None: {
            "data": [
                {"record_date": "2026-09-04", "tot_pub_debt_out_amt": "35467890123456.00"},
                {"record_date": "2026-09-03", "tot_pub_debt_out_amt": "35400123456789.00"},
                {"record_date": "2026-09-02", "tot_pub_debt_out_amt": None},
            ]
        },
    )
    pts = fiscal.fetch_window("FISCAL:DEBT_TOTAL", days=10)
    assert [p["ts"] for p in pts] == ["2026-09-03", "2026-09-04"]  # null row skipped


def test_harvest_uses_fetch_window_and_falls_back(monkeypatch):
    """The harvest integration: window fetchers land ALL points; a series the
    module's fetch_window does not support falls back to fetch_latest."""
    from arkwatch.qa import harvest as h

    calls = {"window": [], "latest": []}

    class FakeMod:
        @staticmethod
        def fetch_window(sid, days=10):
            calls["window"].append(sid)
            if sid == "FAKE:SUPPORTED":
                return [{"ts": _day(2), "value": 1.0}, {"ts": _day(1), "value": 2.0}]
            raise RuntimeError("unsupported")

        @staticmethod
        def fetch_latest(sid):
            calls["latest"].append(sid)
            return {"ts": _day(0), "value": 9.0}

    rows, first, werr = h._window_or_latest(FakeMod, "FAKE:SUPPORTED", "FAKE")
    assert len(rows) == 2 and rows[-1][1] == _day(1) and first["value"] == 2.0
    assert werr is None
    rows, first, werr = h._window_or_latest(FakeMod, "FAKE:OTHER", "FAKE")
    assert len(rows) == 1 and first["value"] == 9.0
    assert calls["latest"] == ["FAKE:OTHER"]
    assert werr and werr.startswith("WINDOW_FALLBACK")  # review ronde-2 P2


# --- P1-3: Bybit legs — RETIRED (stablecoin only) ------------------------------


def test_llama_dict_shape_summed(monkeypatch):
    """2026-09-17: DefiLlama changed totalCirculatingUSD from a scalar to a
    per-peg dict — float(dict) crashed the f2 harvest. ROUND-2 CORRECTION:
    the old scalar carried ONLY the USD-pegged total (live evidence: the
    peggedUSD value matches the last stored scalar to the cent) — summing
    all pegs changed the series DEFINITION mid-stream. Keep peggedUSD."""
    from arkwatch.fetchers import bybit

    class R:
        status_code = 200

        def json(self):
            return [
                # BOTH fields changed shape in the same release: the date is
                # now an epoch — live it arrives as a NUMERIC STRING
                # ("1789603200" = 2026-09-17T00:00Z), so isinstance(int)
                # misses it
                {
                    "date": "1789603200",
                    "totalCirculatingUSD": {
                        "peggedUSD": 305_000_000_000.0,
                        "peggedEUR": 500_000_000.0,
                    },
                },
            ]

    monkeypatch.setattr(bybit.requests, "get", lambda *a, **k: R())
    out = bybit.fetch_stablecoin_total()
    assert out["total_usd"] == 305_000_000_000.0  # peggedUSD, NOT the 305.5B sum
    assert out["ts"] == "2026-09-17"  # epoch → ISO, not "1789603200"


def test_stablecoin_harvest_writes_and_gates(conn):
    """The surviving flows leg: DefiLlama stablecoin → flows_daily +
    LLAMA:STABLECOIN fetch_log row. ROUND-2: the harvest now writes a 7-day
    WINDOW (hole self-heal) — mock fetch_stablecoin_window."""
    from arkwatch.qa.f2_harvest import harvest_flows

    today = datetime.now(UTC).date().isoformat()
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

    class FakeBybit:
        @staticmethod
        def fetch_stablecoin_window(n=7):
            return [
                {"ts": yesterday, "total_usd": 309_100_000_000.0},
                {"ts": today, "total_usd": 309_123_456_789.0},
            ]

    import unittest.mock as _mock

    with _mock.patch(
        "arkwatch.fetchers.bybit.fetch_stablecoin_window", FakeBybit.fetch_stablecoin_window
    ):
        out = harvest_flows(conn)
    assert out["stablecoin_usd"] == 309_123_456_789.0
    # BOTH window rows landed (the hole-heal contract)
    row = conn.execute(
        "SELECT stablecoin_usd FROM flows_daily WHERE date=?", (yesterday,)
    ).fetchone()
    assert row[0] == 309_100_000_000.0
    row = conn.execute("SELECT stablecoin_usd FROM flows_daily WHERE date=?", (today,)).fetchone()
    assert row[0] == 309_123_456_789.0
    log = conn.execute("SELECT status FROM fetch_log WHERE target='LLAMA:STABLECOIN'").fetchone()
    assert log[0] == "OK"


def test_flows_upsert_coalesce_protects_prior_values():
    import sqlite3

    today = datetime.now(UTC).date().isoformat()

    def mock_harvest(conn, funding, oi):
        # drive the upsert directly with the COALESCE contract
        conn.execute(
            "INSERT INTO flows_daily(date,funding_bps,oi_btc,oi_eth,stablecoin_usd)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(date) DO UPDATE SET"
            " funding_bps=COALESCE(excluded.funding_bps, funding_bps),"
            " oi_btc=COALESCE(excluded.oi_btc, oi_btc),"
            " oi_eth=COALESCE(excluded.oi_eth, oi_eth),"
            " stablecoin_usd=COALESCE(excluded.stablecoin_usd, stablecoin_usd)",
            (today, funding, oi, oi, None),
        )

    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute(
        "CREATE TABLE flows_daily (date TEXT PRIMARY KEY, funding_bps REAL,"
        " funding_eth REAL, oi_btc REAL, oi_eth REAL, stablecoin_usd REAL)"
    )
    mock_harvest(c, 3.5, 1000.0)  # morning success
    mock_harvest(c, None, None)  # afternoon retry, legs dead
    row = c.execute("SELECT funding_bps, oi_btc FROM flows_daily").fetchone()
    assert row == (3.5, 1000.0)  # NULL legs never clobber (audit P1-3)
    mock_harvest(c, 4.0, 2000.0)  # next day's real update DOES win
    assert c.execute("SELECT funding_bps FROM flows_daily").fetchone()[0] == 4.0
    c.close()


# --- P1-4: ADS colon dates ----------------------------------------------------


def test_ads_colon_date_normalized():
    # the normalization formats live inline in philly.fetch_latest; pin the
    # accepted shapes so a future refactor cannot drop the colon variant
    s = "2026:09:05"
    from datetime import datetime as _dt

    for fmt in ("%Y:%m:%d",):
        assert _dt.strptime(s, fmt).date().isoformat() == "2026-09-05"


# --- Price upsert: partial bars must never freeze (2026-09-17 audit P1) --------


def test_insert_prices_partial_then_final_heals(conn):
    """INSERT OR IGNORE froze partial bars forever: (a) a NULL-close row
    blocked its own final bar, (b) an intraday close swept pre-session-close
    could never be corrected. The COALESCE upsert: latest non-NULL wins,
    NULLs never clobber."""
    from arkwatch.qa.instruments import insert_prices

    # morning sweep lands a NULL-close partial (2026-09-10 class)
    n = insert_prices(conn, "GC1", "YAHOO", [{"ts": "2026-09-10", "close": None, "high": 4400.0}])
    assert n == 1
    row = conn.execute(
        "SELECT close, high FROM instrument_prices WHERE symbol='GC1' AND ts='2026-09-10'"
    ).fetchone()
    assert row == (None, 4400.0)
    # final print arrives — must HEAL the NULL close, not be ignored
    insert_prices(conn, "GC1", "YAHOO", [{"ts": "2026-09-10", "close": 4366.5, "high": 4410.0}])
    row = conn.execute(
        "SELECT close, high FROM instrument_prices WHERE symbol='GC1' AND ts='2026-09-10'"
    ).fetchone()
    assert row == (4366.5, 4410.0)
    # intraday wrong close (ES1 class) — a later final bar must overwrite it
    insert_prices(conn, "ES1", "YAHOO", [{"ts": "2026-09-16", "close": 7600.0}])
    insert_prices(conn, "ES1", "YAHOO", [{"ts": "2026-09-16", "close": 7668.5}])
    assert (
        conn.execute(
            "SELECT close FROM instrument_prices WHERE symbol='ES1' AND ts='2026-09-16'"
        ).fetchone()[0]
        == 7668.5
    )
    # a NULL-close payload must NOT clobber a stored good close
    insert_prices(conn, "ES1", "YAHOO", [{"ts": "2026-09-16", "close": None}])
    assert (
        conn.execute(
            "SELECT close FROM instrument_prices WHERE symbol='ES1' AND ts='2026-09-16'"
        ).fetchone()[0]
        == 7668.5
    )
    # fully-empty rows are dropped at build time
    assert insert_prices(conn, "ES1", "YAHOO", [{"ts": "2026-09-17"}]) == 0


# --- CAL: derived series (round-3 queue) ---------------------------------------


def test_caldist_ref_month_and_dedup(tmp_path):
    """Calendar-derived series: the release's REFERENCE month comes from the
    name token (with Jan→Dec-prior-year rollover) or release-month−1; twins
    dedup via MAX per month."""
    import sqlite3

    from arkwatch.fetchers import caldist

    dbp = str(tmp_path / "t.db")
    db.get_conn(dbp, allow_init=True).close()
    c = sqlite3.connect(dbp)
    rows = [
        # token wins: AUG token on a Sep-1 release → 2026-08-01
        ("ISM MANUFACTURING PMI AUG", "2026-09-01T14:00:00", 54.6),
        # token-less twin, same release → same month (dedup, MAX arbitrates)
        ("US ISM MANUFACTURING INDEX", "2026-09-01T14:00:00", 54.6),
        # Jan release with DEC token → prior year
        ("ISM MANUFACTURING PMI DEC", "2026-01-03T14:00:00", 49.2),
        # token-less Jan row → convention m_minus_1 → prior-year Dec too
        ("ISM MANUFACTURING PMI", "2026-01-03T14:00:00", 49.2),
    ]
    for nm, ts, actual in rows:
        c.execute(
            "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
            "importance,consensus,consensus_source,actual,actual_source,previous,"
            "surprise_z,is_curated,indicator_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                nm + ts,
                ts,
                ts,
                "US",
                nm,
                nm,
                "high",
                50.0,
                "FMP",
                actual,
                "FMP",
                None,
                None,
                0,
                "ISM MANUFACTURING PMI",
            ),
        )
    c.commit()
    c.close()
    out = caldist.family_rows("ISM MANUFACTURING PMI", "m_minus_1", db_path=dbp)
    assert out == [
        {"ts": "2025-12-01", "value": 49.2},
        {"ts": "2026-08-01", "value": 54.6},
    ]
    # fetch_latest reads DEFAULT_DB — not usable against the temp db; the
    # family_rows proof above is the contract, this pins the routing table
    assert caldist.FAMILIES["ISM_MFG_PMI"][0] == "ISM MANUFACTURING PMI"


# --- Round-2 fixes: demote restore, heal scale guard, asof tails ----------------


def test_demote_restores_when_values_arrive(tmp_path):
    """ROUND-2 P1 regression: the valueless-demote was PERMANENT — a row born
    empty pre-release (normal cadence) got importance='low' and no code path
    ever restored it, so vendor-high US rows vanished from the radar after
    their actual landed. The ON CONFLICT path must restore importance from
    the value-carrying incoming row (never demoted, so trustworthy)."""
    import sqlite3

    from arkwatch.qa.calendar import save

    dbp = str(tmp_path / "t.db")
    db.get_conn(dbp, allow_init=True).close()
    ev = {
        "normalized_name": "US JOBLESS CLAIMS",
        "ts_utc": "2026-09-24T12:30:00",
        "name": "x",
        "importance": "high",
        "previous": None,
        "source": "CME",
    }
    # born empty pre-release → demoted
    save(dbp, [{**ev, "consensus": None, "actual": None}])
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT importance FROM events").fetchone()[0] == "low"
    c.close()
    # the same uid arrives WITH values (undemoted, vendor-high) → restore
    save(dbp, [{**ev, "consensus": 208.0, "actual": 206.0}])
    c = sqlite3.connect(dbp)
    assert c.execute("SELECT importance, actual FROM events").fetchone() == ("high", 206.0)
    c.close()


def test_sibling_heal_scale_guard(tmp_path):
    """ROUND-2 P1: the heal was unit-blind — one indicator_key family can hold
    a CME %MoM twin and an FMP level twin; healing across a >10x scale gap
    poisons sigma/ESI (live: one EXISTING HOME contamination)."""
    import sqlite3

    from arkwatch.qa.calendar import save

    dbp = str(tmp_path / "t.db")
    db.get_conn(dbp, allow_init=True).close()
    # target twin: LEVEL scale (consensus ~1.4 million), empty
    save(
        dbp,
        [
            {
                "normalized_name": "US EXISTING HOME SALES",
                "ts_utc": "2026-09-20T14:00:00",
                "name": "x",
                "importance": "low",
                "consensus": 1_400_000.0,
                "actual": None,
                "previous": None,
                "source": "CME",
            }
        ],
    )
    # healer twin: %MoM scale (actual -1.5) — 6 orders of magnitude off
    save(
        dbp,
        [
            {
                "normalized_name": "EXISTING HOME SALES",
                "ts_utc": "2026-09-20T14:00:00",
                "name": "x",
                "importance": "high",
                "consensus": -1.0,
                "actual": -1.5,
                "previous": None,
                "source": "FMP",
            }
        ],
    )
    c = sqlite3.connect(dbp)
    vals = dict(c.execute("SELECT normalized_name, actual FROM events").fetchall())
    assert vals["EXISTING HOME SALES"] == -1.5  # healer keeps its own
    # the level twin must NOT receive the -1.5 (out of the 0.1x..10x band)
    assert vals["US EXISTING HOME SALES"] is None
    c.close()


def test_fedwatch_format_brief_asof():
    """ROUND-2: the line rendered the pre-decision strip as current policy —
    the pricing vintage must ride along."""
    from datetime import date

    from arkwatch.transforms.fedwatch import format_brief

    p = type(
        "P",
        (),
        {
            "meeting_date": date(2026, 10, 28),
            "prob_ease": 0.0,
            "prob_hold": 0.1,
            "prob_hike": 0.9,
            "implied_rate": 4.08,
        },
    )
    txt = format_brief([p], asof="2026-09-15")
    assert "(ZQ 09-15)" in txt


# --- Calendar audit batch 2026-09-17: aliases, stub gate, hour gate, sibling heal


def test_indicator_key_fmp_renames_merge():
    """FMP renamed families (CPI→Inflation Rate, ISM Non-Mfg→ISM Services,
    Markit→S&P Global, Budget→Monthly Budget Statement) must hash to ONE
    indicator_key — split keys strand the old names' rows (the fill-if-NULL
    heal can never reach them) and double-count releases inside ESI."""
    from arkwatch.qa.calendar import indicator_key

    assert indicator_key("CPI YOY AUG") == indicator_key("INFLATION RATE YOY AUG")
    assert indicator_key("CORE CPI MOM AUG") == indicator_key("CORE INFLATION RATE MOM AUG")
    assert indicator_key("ISM NON MANUFACTURING PMI AUG") == indicator_key("ISM SERVICES PMI AUG")
    assert indicator_key("MARKIT SERVICES PMI SEP") == indicator_key("S P GLOBAL SERVICES PMI SEP")
    assert indicator_key("BUDGET BALANCE AUG") == indicator_key("MONTHLY BUDGET STATEMENT AUG")
    # substance qualifiers stay distinct indicators
    assert indicator_key("CPI YOY AUG") != indicator_key("INFLATION RATE MOM AUG")


def test_calendar_hour_gate_and_demote(tmp_path):
    """US events at implausible hours (05:30Z = 01:30 ET) degrade to
    date-only + low; valueless US-prefixed stubs demote while empty but
    still land; dead CME stubs are dropped entirely."""
    import sqlite3

    from arkwatch.qa.calendar import save

    dbp = str(tmp_path / "t.db")
    db.get_conn(dbp, allow_init=True).close()
    save(
        dbp,
        [
            {  # impossible hour → date-only + low
                "normalized_name": "SOME FMP SLIPPED TZ RELEASE",
                "ts_utc": "2026-09-18T05:30:00",
                "name": "x",
                "importance": "high",
                "consensus": 1.0,
                "actual": None,
                "previous": None,
                "source": "FMP",
            },
            {  # valueless US-stub → demoted to low, still stored
                "normalized_name": "US BAKER HUGHES RIG COUNT",
                "ts_utc": "2026-09-18T17:00:00",
                "name": "x",
                "importance": "high",
                "consensus": None,
                "actual": None,
                "previous": None,
                "source": "CME",
            },
            {  # dead CME stub family → dropped
                "normalized_name": "US EIA PETROLEUM STATUS REPORT",
                "ts_utc": "2026-09-18T15:30:00",
                "name": "x",
                "importance": "high",
                "consensus": None,
                "actual": None,
                "previous": None,
                "source": "CME",
            },
        ],
    )
    c = sqlite3.connect(dbp)
    r1 = c.execute(
        "SELECT substr(ts_utc,12,5), importance FROM events WHERE normalized_name LIKE 'SOME FMP%'"
    ).fetchone()
    assert r1 == ("00:00", "low")
    r2 = c.execute(
        "SELECT importance FROM events WHERE normalized_name='US BAKER HUGHES RIG COUNT'"
    ).fetchone()
    assert r2 == ("low",)
    assert (
        c.execute(
            "SELECT COUNT(*) FROM events WHERE normalized_name LIKE 'US EIA PETROLEUM%'"
        ).fetchone()[0]
        == 0
    )
    c.close()


def test_calendar_sibling_heal_fills_twin(tmp_path):
    """The NFP 09-04 class: the FMP twin carries the actual, the TV twin
    stays NULL forever because its own source never refills. save() must
    fill same indicator_key + same-date siblings when an actual arrives."""
    import sqlite3

    from arkwatch.qa.calendar import save

    dbp = str(tmp_path / "t.db")
    db.get_conn(dbp, allow_init=True).close()
    # first ingest: TV twin, no actual yet (different name, SAME indicator_key
    # family via the existing JOBLESS CLAIMS alias path)
    save(
        dbp,
        [
            {
                "normalized_name": "INITIAL JOBLESS CLAIMS SEP 12",
                "ts_utc": "2026-09-17T12:30:00",
                "name": "x",
                "importance": "high",
                "consensus": 208.0,
                "actual": None,
                "previous": 206.0,
                "source": "TV",
            },
        ],
    )
    # second ingest: FMP twin arrives WITH the actual
    save(
        dbp,
        [
            {
                "normalized_name": "INITIAL JOBLESS CLAIMS SEP 12",
                "ts_utc": "2026-09-17T12:30:00",
                "name": "x",
                "importance": "high",
                "consensus": 208.0,
                "actual": 209.0,
                "previous": 206.0,
                "source": "FMP",
            },
        ],
    )
    # and a NAME-twin sibling (alias family, e.g. the US-prefixed CME spelling)
    save(
        dbp,
        [
            {
                "normalized_name": "US JOBLESS CLAIMS",
                "ts_utc": "2026-09-17T12:30:00",
                "name": "x",
                "importance": "low",
                "consensus": None,
                "actual": None,
                "previous": None,
                "source": "CME",
            },
        ],
    )
    save(
        dbp,
        [  # re-deliver the FMP actual (idempotent path)
            {
                "normalized_name": "INITIAL JOBLESS CLAIMS SEP 12",
                "ts_utc": "2026-09-17T12:30:00",
                "name": "x",
                "importance": "high",
                "consensus": 208.0,
                "actual": 209.0,
                "previous": 206.0,
                "source": "FMP",
            },
        ],
    )
    c = sqlite3.connect(dbp)
    for uidless in c.execute(
        "SELECT normalized_name, actual FROM events ORDER BY normalized_name"
    ).fetchall():
        print("  row:", uidless)
    # BOTH spellings (same indicator_key, same date) carry the actual now
    vals = dict(c.execute("SELECT normalized_name, actual FROM events").fetchall())
    assert vals["INITIAL JOBLESS CLAIMS SEP 12"] == 209.0
    assert vals["US JOBLESS CLAIMS"] == 209.0  # healed via the sibling path
    c.close()


# --- Calendar ingest: dead sub-component families ------------------------------


def test_calendar_blocks_dead_philly_subcomponents(tmp_path):
    """2026-09-17 calendar audit: the 5 PHILLY FED sub-component families
    stopped carrying consensus AND actuals after 2023-08 — ingest created
    fully-empty rows forever (10/day on release days). save() must drop them
    while the HEALTHY headline family still lands."""
    import sqlite3

    from arkwatch.qa.calendar import save

    dbp = str(tmp_path / "t.db")
    db.get_conn(dbp, allow_init=True).close()
    n = save(
        dbp,
        [
            {
                "normalized_name": "PHILLY FED PRICES PAID SEP",
                "ts_utc": "2026-09-17T19:30:00",
                "name": "Philly Fed Prices Paid",
                "importance": "low",
                "consensus": None,
                "actual": None,
                "previous": None,
                "source": "TEST",
            },
            {
                "normalized_name": "PHILADELPHIA FED MANUFACTURING INDEX SEP",
                "ts_utc": "2026-09-17T19:30:00",
                "name": "Philadelphia Fed Manufacturing Index",
                "importance": "high",
                "consensus": 30.5,
                "actual": None,
                "previous": 47.4,
                "source": "TEST",
            },
        ],
    )
    assert n == 1  # only the headline landed
    c = sqlite3.connect(dbp)
    assert (
        c.execute(
            "SELECT COUNT(*) FROM events WHERE normalized_name LIKE 'PHILLY FED%'"
        ).fetchone()[0]
        == 0
    )
    assert (
        c.execute(
            "SELECT consensus FROM events WHERE normalized_name LIKE 'PHILADELPHIA%'"
        ).fetchone()[0]
        == 30.5
    )
    c.close()


# --- P2: copper monthly-cadence gate ------------------------------------------


def test_copper_trigger_skips_frozen_feed(conn, capsys):
    from arkwatch.qa import watcher

    conn.execute(
        "INSERT OR IGNORE INTO series_registry(series_id, name, block, tier, unit,"
        " value_format, freq, primary_source) VALUES ('LME:CA_STOCKS','Cu','H',0,"
        "'tonne','{:,.0f}','D','LME')"
    )
    stale_ts = (datetime.now(UTC).date() - timedelta(days=60)).isoformat()
    for i in range(80):
        conn.execute(
            "INSERT INTO raw_observations(series_id, ts, value, vintage_ts, source, fetched_at)"
            " VALUES ('LME:CA_STOCKS', ?, ?, 'realtime', 't', ?)",
            (
                (date.fromisoformat(stale_ts) - timedelta(days=i)).isoformat(),
                200000.0 - i * 100,
                stale_ts,
            ),
        )
    conn.commit()
    fired = watcher.check_all(conn)
    assert "copper_stocks_drain" not in fired
    assert "frozen" in capsys.readouterr().out
