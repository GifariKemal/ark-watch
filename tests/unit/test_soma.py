"""Offline unit tests for the SOMA per-CUSIP layer: parsing scale, maturity
buckets, TIPS/nominal split, roll-off windows, summary computation, and the
DB round-trip (migration v7 tables). No network — all fixtures are seeded rows.

Live anchors used as expected values (verified 2026-09-03):
  - /api/soma/tsy/get/asof/2026-08-26.json: 431 CUSIPs, totals match the
    official /api/soma/summary.json to the dollar (Bills 541,994,926,700 ...).
  - percentOutstanding arrives as a FRACTION (0.6999 = 70% Fed ownership cap).
  - Official WAM (all Treasury) at 2026-08-26 = 8.26 years.
"""

from __future__ import annotations

from datetime import date

import pytest

from arkwatch import db
from arkwatch.fetchers import soma
from arkwatch.qa.soma_harvest import store_soma_week

AS_OF = "2026-08-26"


def _hold(**kw) -> dict:
    """Seeded holding row with test defaults (par in raw USD)."""
    row = {
        "as_of_date": AS_OF,
        "cusip": "912810QT8",
        "security_type": "NotesBonds",
        "maturity_date": "2041-11-15",
        "par_value": 1_000.0,
        "pct_outstanding": 70.0,
        "change_week": -100.0,
    }
    row.update(kw)
    return row


class TestParsingScale:
    def test_pct_outstanding_fraction_becomes_percent(self):
        """0.6999 (API fraction, the 70% ownership cap) must land as ~70 percent —
        a 100x display bug here would poison the repo-specials signal."""

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "soma": {
                        "holdings": [
                            {
                                "asOfDate": AS_OF,
                                "cusip": "912810QT8",
                                "securityType": "NotesBonds",
                                "maturityDate": "2041-11-15",
                                "parValue": "31240000000",
                                "percentOutstanding": "0.6999990782665313",
                                "changeFromPriorWeek": "-1500000",
                            }
                        ]
                    }
                }

        class FakeSession:
            def get(self, url, timeout=None):
                assert url.endswith(f"/tsy/get/asof/{AS_OF}.json")
                return FakeResp()

            def close(self):
                pass

        rows = soma.fetch_soma_holdings(AS_OF, session=FakeSession())
        assert rows[0]["par_value"] == 31_240_000_000.0
        assert rows[0]["change_week"] == -1_500_000.0
        assert rows[0]["pct_outstanding"] == pytest.approx(69.99990783, abs=1e-6)

    def test_missing_numeric_fields_become_none_not_zero(self):
        """Empty-string numerics (older history rows) must be None — 0.0 would
        fabricate a fake 'no change' week in the QT pace."""

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "soma": {
                        "holdings": [
                            {
                                "asOfDate": "2003-07-09",
                                "cusip": "912810QT8",
                                "securityType": "Bills",
                                "maturityDate": "2003-08-07",
                                "parValue": "1000000",
                                "percentOutstanding": "",
                                "changeFromPriorWeek": "",
                            }
                        ]
                    }
                }

        class FakeSession:
            def get(self, url, timeout=None):
                return FakeResp()

            def close(self):
                pass

        rows = soma.fetch_soma_holdings("2003-07-09", session=FakeSession())
        assert rows[0]["pct_outstanding"] is None
        assert rows[0]["change_week"] is None
        assert rows[0]["par_value"] == 1_000_000.0


class TestMaturityBuckets:
    def test_bucket_boundaries_half_open(self):
        # [0,1) [1,3) [3,5) [5,7) [7,10) [10,inf)
        assert soma.maturity_bucket(0.0) == "0-1y"
        assert soma.maturity_bucket(0.999) == "0-1y"
        assert soma.maturity_bucket(1.0) == "1-3y"
        assert soma.maturity_bucket(2.999) == "1-3y"
        assert soma.maturity_bucket(3.0) == "3-5y"
        assert soma.maturity_bucket(5.0) == "5-7y"
        assert soma.maturity_bucket(7.0) == "7-10y"
        assert soma.maturity_bucket(10.0) == "10y+"
        assert soma.maturity_bucket(99.0) == "10y+"

    def test_bucket_aggregation_and_counts(self):
        # ~2y + ~4y + ~15y from AS_OF 2026-08-26 (day counts verified via stdlib
        # below — one calendar year is 0.999y in the 365.25-day convention, so
        # exact-calendar-year maturities would land in the SHORTER bucket)
        rows = [
            _hold(cusip="A", maturity_date="2028-08-26", par_value=100.0, change_week=-10.0),
            _hold(cusip="B", maturity_date="2030-08-26", par_value=200.0, change_week=-20.0),
            _hold(cusip="C", maturity_date="2041-11-15", par_value=400.0, change_week=-40.0),
            # no maturity -> skipped entirely, not silently bucketed
            _hold(cusip="D", maturity_date=None, par_value=999.0),
        ]
        buckets = soma.compute_maturity_buckets(rows, AS_OF)
        assert buckets["1-3y"] == {"par": 100.0, "change_week": -10.0, "n_cusips": 1}
        assert buckets["3-5y"] == {"par": 200.0, "change_week": -20.0, "n_cusips": 1}
        assert buckets["10y+"] == {"par": 400.0, "change_week": -40.0, "n_cusips": 1}
        assert buckets["0-1y"]["n_cusips"] == 0
        total_n = sum(b["n_cusips"] for b in buckets.values())
        assert total_n == 3  # D skipped

    def test_buckets_pure_function_of_date(self):
        """Same holding ages into a shorter bucket as the as-of date advances —
        the diff-across-dates use case must work."""
        rows = [_hold(cusip="A", maturity_date="2031-08-26")]  # ~5.0y from 2026-08-26
        assert soma.compute_maturity_buckets(rows, "2026-08-26")["3-5y"]["n_cusips"] == 1
        assert soma.compute_maturity_buckets(rows, "2029-08-26")["1-3y"]["n_cusips"] == 1
        assert soma.compute_maturity_buckets(rows, "2030-08-26")["0-1y"]["n_cusips"] == 1


class TestTipsNominalSplit:
    def test_split_by_security_type(self):
        rows = [
            _hold(cusip="A", security_type="TIPS", par_value=100.0, change_week=5.0),
            _hold(cusip="B", security_type="Bills", par_value=50.0, change_week=-5.0),
            _hold(cusip="C", security_type="NotesBonds", par_value=200.0, change_week=-10.0),
            _hold(cusip="D", security_type="FRNs", par_value=10.0, change_week=0.0),
            # missing change_week (older history) counts as 0, not None-propagated
            _hold(cusip="E", security_type="TIPS", par_value=100.0, change_week=None),
        ]
        split = soma.compute_tips_nominal_split(rows)
        assert split["tips"] == {"par": 200.0, "change": 5.0}
        assert split["nominal"] == {"par": 260.0, "change": -15.0}

    def test_split_sums_to_total(self):
        rows = [
            _hold(cusip="A", security_type="TIPS", par_value=100.0),
            _hold(cusip="B", security_type="NotesBonds", par_value=200.0),
        ]
        split = soma.compute_tips_nominal_split(rows)
        assert split["tips"]["par"] + split["nominal"]["par"] == 300.0


class TestRollOff:
    def test_horizon_inclusive(self):
        # maturity exactly at as_of + 7d counts in 7d; +8d does not
        rows = [
            _hold(cusip="IN7", maturity_date="2026-09-02"),  # as_of + 7d
            _hold(cusip="OUT", maturity_date="2026-09-03"),  # as_of + 8d
            _hold(cusip="IN0", maturity_date=AS_OF),  # already matured on the spot
            _hold(cusip="NO Mat", maturity_date=None, par_value=500.0),
        ]
        assert soma.rolling_off(rows, AS_OF, 7) == 2000.0  # IN7 + IN0
        assert soma.rolling_off(rows, AS_OF, 30) == 3000.0  # + OUT
        assert soma.rolling_off(rows, AS_OF, 90) == 3000.0

    def test_rolling_off_windows_distinct(self):
        rows = [
            _hold(cusip="A", maturity_date="2026-08-28", par_value=10.0),  # +2d
            _hold(cusip="B", maturity_date="2026-10-25", par_value=20.0),  # +60d
            _hold(cusip="C", maturity_date="2026-11-24", par_value=40.0),  # +90d exact
        ]
        assert soma.rolling_off(rows, AS_OF, 7) == 10.0
        assert soma.rolling_off(rows, AS_OF, 30) == 10.0
        assert soma.rolling_off(rows, AS_OF, 90) == 70.0  # inclusive boundary


class TestSummary:
    def test_summary_from_seeded_holdings(self):
        rows = [
            _hold(
                cusip="B1",
                security_type="Bills",
                maturity_date="2026-08-27",
                par_value=12_000_000_000.0,
                change_week=0.0,
            ),
            _hold(
                cusip="N1",
                security_type="NotesBonds",
                maturity_date="2036-08-26",
                par_value=20_000_000_000.0,
                change_week=-2_000_000_000.0,
            ),
            _hold(
                cusip="T1",
                security_type="TIPS",
                maturity_date="2046-08-26",
                par_value=8_000_000_000.0,
                change_week=1_000_000_000.0,
            ),
            _hold(
                cusip="F1",
                security_type="FRNs",
                maturity_date="2027-02-15",
                par_value=2_000_000_000.0,
                change_week=None,
            ),
        ]
        s = soma.compute_soma_summary(rows, AS_OF)
        assert s["as_of_date"] == AS_OF
        assert s["bills"] == 12e9
        assert s["notes_bonds"] == 20e9
        assert s["tips"] == 8e9
        assert s["frn"] == 2e9
        assert s["total_par"] == 42e9
        assert s["weekly_change"] == -1e9  # None treated as 0, not propagated
        assert s["rolling_off_7d"] == 12e9  # only the overnight bill
        assert s["rolling_off_30d"] == 12e9
        assert s["rolling_off_90d"] == 12e9
        assert s["n_cusips"] == 4

        # par-weighted maturity; day counts via stdlib (leap years included —
        # hand-typed day constants were wrong and hid nothing the stdlib hides)
        def _y(m: str) -> float:
            return (date.fromisoformat(m) - date.fromisoformat(AS_OF)).days / 365.25

        expected_wam = (
            12e9 * _y("2026-08-27")
            + 20e9 * _y("2036-08-26")
            + 8e9 * _y("2046-08-26")
            + 2e9 * _y("2027-02-15")
        ) / 42e9
        assert s["avg_maturity_years"] == pytest.approx(expected_wam, rel=1e-9)

    def test_summary_empty_holdings(self):
        # compute_soma_summary itself tolerates empty input (the hard refusal of
        # an empty payload lives in store_soma_week / the fetcher)
        s = soma.compute_soma_summary([], AS_OF)
        assert s["total_par"] == 0.0
        assert s["avg_maturity_years"] is None

    def test_type_totals_match_official_summary_identity(self):
        """Regression of the 2026-09-03 live identity check: per-CUSIP sums must
        equal the official /api/soma/summary.json row for 2026-08-26."""

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "soma": {
                        "holdings": [
                            {
                                "asOfDate": AS_OF,
                                "cusip": "912797TY3",
                                "securityType": "Bills",
                                "maturityDate": "2026-08-27",
                                "parValue": "11980824200",
                                "percentOutstanding": "0.0441831083149250",
                                "changeFromPriorWeek": "0",
                            },
                            {
                                "asOfDate": AS_OF,
                                "cusip": "912810QT8",
                                "securityType": "NotesBonds",
                                "maturityDate": "2041-11-15",
                                "parValue": "31240000000",
                                "percentOutstanding": "0.6999990782665313",
                                "changeFromPriorWeek": "-1500000",
                            },
                        ]
                    }
                }

        class FakeSession:
            def get(self, url, timeout=None):
                return FakeResp()

            def close(self):
                pass

        rows = soma.fetch_soma_holdings(AS_OF, session=FakeSession())
        s = soma.compute_soma_summary(rows, AS_OF)
        assert s["bills"] == 11_980_824_200.0  # official: 541,994,926,700 (subset here)
        assert s["notes_bonds"] == 31_240_000_000.0
        assert s["weekly_change"] == -1_500_000.0


class TestDbRoundTrip:
    def test_store_soma_week_writes_both_tables(self, tmp_path):
        conn = db.get_conn(tmp_path / "soma.db", allow_init=True)
        rows = [
            _hold(
                cusip="A",
                security_type="Bills",
                maturity_date="2026-08-27",
                par_value=100.0,
                change_week=-5.0,
            ),
            _hold(
                cusip="B",
                security_type="TIPS",
                maturity_date="2046-08-26",
                par_value=300.0,
                change_week=7.0,
            ),
        ]
        n_new, summary = store_soma_week(conn, rows)
        assert n_new == 2
        hold = conn.execute(
            "SELECT as_of_date,cusip,security_type,maturity_date,par_value,pct_outstanding,change_week"
            " FROM soma_holdings ORDER BY cusip"
        ).fetchall()
        assert hold == [
            (AS_OF, "A", "Bills", "2026-08-27", 100.0, 70.0, -5.0),
            (AS_OF, "B", "TIPS", "2046-08-26", 300.0, 70.0, 7.0),
        ]
        row = conn.execute(
            "SELECT total_par,bills,notes_bonds,tips,frn,weekly_change,"
            "rolling_off_7d,rolling_off_30d,rolling_off_90d,n_cusips,avg_maturity_years"
            " FROM soma_summary WHERE as_of_date=?",
            (AS_OF,),
        ).fetchone()
        assert row[0] == 400.0  # total_par
        assert row[1] == 100.0  # bills
        assert row[2] == 0.0  # notes_bonds
        assert row[3] == 300.0  # tips
        assert row[4] == 0.0  # frn
        assert row[5] == 2.0  # weekly_change
        assert row[6] == 100.0  # rolling_off_7d (the overnight bill only)
        assert row[9] == 2  # n_cusips
        # summary persisted matches the returned dict
        assert summary["total_par"] == 400.0

        # idempotent re-run: same payload → 0 new, tables unchanged
        n_again, _ = store_soma_week(conn, rows)
        assert n_again == 0
        n_rows = conn.execute("SELECT COUNT(*) FROM soma_holdings").fetchone()[0]
        assert n_rows == 2
        n_summ = conn.execute("SELECT COUNT(*) FROM soma_summary").fetchone()[0]
        assert n_summ == 1
        conn.close()

    def test_store_restatement_replaces_week_atomically(self, tmp_path):
        """REGRESSION (verified against the old behavior 2026-09-03): a Fed
        restatement of an already-stored week must refresh holdings AND
        summary together. INSERT OR IGNORE kept stale par and ghost CUSIPs
        while the summary moved on — the tables silently drifted apart."""
        conn = db.get_conn(tmp_path / "soma.db", allow_init=True)
        store_soma_week(
            conn,
            [
                _hold(cusip="A", par_value=10e9),
                _hold(cusip="B", par_value=5e9),
            ],
        )
        # restated week: A's par corrected, B dropped by the Fed, C added
        n_new, s2 = store_soma_week(
            conn,
            [
                _hold(cusip="A", par_value=11e9),
                _hold(cusip="C", par_value=2e9),
            ],
        )
        assert n_new == 1  # only C is new; A was replaced
        hold = dict(
            conn.execute(
                "SELECT cusip, par_value FROM soma_holdings WHERE as_of_date=?", (AS_OF,)
            ).fetchall()
        )
        assert hold == {"A": 11e9, "C": 2e9}  # ghost B gone, A corrected
        total = conn.execute(
            "SELECT total_par FROM soma_summary WHERE as_of_date=?", (AS_OF,)
        ).fetchone()[0]
        assert total == 13e9  # summary == Σ holdings — no drift
        assert s2["total_par"] == 13e9
        conn.close()

    def test_store_rejects_mixed_as_of_dates(self, tmp_path):
        conn = db.get_conn(tmp_path / "soma.db", allow_init=True)
        rows = [_hold(), _hold(cusip="X", as_of_date="2026-08-19")]
        with pytest.raises(soma.SomaError, match="mixed"):
            store_soma_week(conn, rows)
        conn.close()

    def test_store_refuses_empty_holdings(self, tmp_path):
        conn = db.get_conn(tmp_path / "soma.db", allow_init=True)
        with pytest.raises(soma.SomaError, match="empty"):
            store_soma_week(conn, [])
        conn.close()
