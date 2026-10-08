"""Offline unit tests for the fiscaldata expansion (migration v10 + fiscalx
harvester): null-string parsing, page math (page[number]-only API), window
trimming, security_type_desc PK folding, upsert/REPLACE semantics
(announcement→results lifecycle), registry dispatch for FISCAL:DEBT_*, and
schema_fp determinism. No network — HTTP is faked with session stubs; the
fixture DB is created via db.get_conn so the DDL is the real migration v10.

Live anchors used as expected values (verified 2026-09-04):
  - auctions_query: 11,106 rows, earliest auction_date 1979-10-31; latest
    completed 10-Year 2026-08-12 cusip 91282CRF0 btc 2.530 ytm 4.63.
  - debt_to_penny 2026-09-02: public 32,421,027,120,428.18 · intragov
    7,696,018,006,644.39 · total 40,117,045,127,072.57 (RAW USD); 1993-era
    rows carry 'null' splits; first non-null split 1997-09-30.
  - public_debt_transactions amounts are $ MILLIONS (DTS convention): FYTD
    identity Σ(issues−redemptions) = Δdebt_to_penny 2025-09-30→2026-09-02
    matched to the dollar (2,479,492 $M); the 2026-09-02 marketable set nets
    to exactly −20 $M.
"""

from __future__ import annotations

import pytest

from arkwatch import db
from arkwatch.config import load_registry
from arkwatch.fetchers import fiscal
from arkwatch.qa.fetch_log import schema_fp
from arkwatch.qa.fiscalx_harvest import (
    fytd_public_issues,
    net_marketable_today,
    store_auctions,
    store_avg_rates,
    store_debt_transactions,
    store_interest_expense,
)

DEBT_SIDS = ("FISCAL:DEBT_PUBLIC", "FISCAL:DEBT_INTRAGOV", "FISCAL:DEBT_TOTAL")


def _raw_auction(**kw) -> dict:
    """Raw API-shaped auction row (results-filled 2026-08-12 10-Year default)."""
    row = {
        "auction_date": "2026-08-12",
        "cusip": "91282CRF0",
        "security_type": "Note",
        "security_term": "10-Year",
        "issue_date": "2026-08-17",
        "maturity_date": "2036-08-15",
        "price_per100": "99.540696",
        "avg_med_yield": "4.630000",
        "bid_to_cover_ratio": "2.530000",
        "allocation_pctage": "65.270000",
        "auction_format": "Single-Price",
    }
    row.update(kw)
    return row


def _raw_tx(**kw) -> dict:
    row = {
        "record_date": "2026-09-02",
        "transaction_type": "Issues",
        "security_market": "Marketable",
        "security_type": "Bills",
        "security_type_desc": "null",
        "transaction_today_amt": "-9",
    }
    row.update(kw)
    return row


class FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Feeds one JSON page per .get() call; records (url, params) for asserts."""

    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResp(self.pages.pop(0))


def _page(data: list[dict], total: int | None = None) -> dict:
    return {
        "meta": {"count": len(data), "total-count": total if total is not None else len(data)},
        "data": data,
        "links": {},
    }


class TestNullStringParsing:
    def test_num_null_strings_become_none_not_zero(self):
        assert fiscal._num("null") is None
        assert fiscal._num("") is None
        assert fiscal._num(None) is None
        assert fiscal._num("2.530000") == 2.53
        assert fiscal._num("-9") == -9.0

    def test_txt_null_strings(self):
        assert fiscal._txt("null") is None
        assert fiscal._txt("Regular Series") == "Regular Series"
        assert fiscal._txt(None) is None

    def test_announcement_row_numerics_are_none_strings_kept(self):
        """The live announcement row (future auction_date) carries 'null'
        numerics — they must parse to None, never 0.0 (a fake 0.0 btc would
        poison any downstream cover-ratio signal)."""
        p = fiscal.parse_auction_row(
            _raw_auction(
                auction_date="2026-09-10",
                cusip="912810UW6",
                security_term="29-Year 11-Month",
                price_per100="null",
                avg_med_yield="null",
                bid_to_cover_ratio="null",
                allocation_pctage="null",
            )
        )
        assert p["price_per100"] is None
        assert p["avg_med_yield"] is None
        assert p["bid_to_cover"] is None
        assert p["allocation_pct"] is None
        assert p["security_term"] == "29-Year 11-Month"
        assert p["auction_format"] == "Single-Price"

    def test_row_without_keys_dropped(self):
        assert fiscal.parse_auction_row(_raw_auction(cusip="")) is None
        assert fiscal.parse_auction_row(_raw_auction(auction_date="")) is None
        assert fiscal.parse_debt_tx_row(_raw_tx(record_date="")) is None


class TestPagePlan:
    def test_two_pages(self):
        assert fiscal.page_plan(11106, 10000) == [1, 2]  # live total 2026-09-04

    def test_exact_multiple_is_one_page(self):
        assert fiscal.page_plan(10000, 10000) == [1]

    def test_small_and_empty(self):
        assert fiscal.page_plan(96, 500) == [1]
        assert fiscal.page_plan(0, 10000) == []

    def test_three_pages(self):
        assert fiscal.page_plan(22301, 10000) == [1, 2, 3]


class TestFetchPaging:
    def test_fetch_pages_walks_page_number(self):
        """page[number] must advance (the API IGNORES page[offset] — every call
        returns page 1 and a naive offset loop runs forever)."""
        p1 = [_raw_auction(cusip=f"C{i:05d}") for i in range(3)]
        p2 = [_raw_auction(cusip=f"D{i:05d}") for i in range(2)]
        sess = FakeSession([_page(p1, total=5), _page(p2, total=5)])
        rows = fiscal._fetch_pages(
            fiscal.AUCTIONS, {"fields": fiscal.AUCTION_FIELDS}, page_size=3, session=sess
        )
        assert len(rows) == 5
        assert sess.calls[0][1]["page[number]"] == 1
        assert sess.calls[1][1]["page[number]"] == 2

    def test_fetch_recent_full_page_with_extra_dates_stops(self):
        """A FULL page is not truncation when it already holds MORE distinct
        dates than the window needs (500 DTS rows ≈ 20 record_dates > 14) —
        exactly one request, then stop."""
        rows = [_raw_tx(record_date=f"2026-09-{d:02d}") for d in range(2, 12)]  # 10 dates
        rows += [_raw_tx(record_date="2026-08-31")] * 2  # page size = 12, window = 10
        sess = FakeSession([_page(rows)])
        out = fiscal._fetch_recent(
            fiscal.DEBT_TRANSACTIONS, {"sort": "-record_date"}, n_dates=10, size=12, session=sess
        )
        assert len(out) == 12
        assert len(sess.calls) == 1

    def test_fetch_recent_pages_until_boundary_date_in_hand(self):
        """Page boundary splitting a date's rows: keep paging until one OLDER
        date is held, so the newest n are provably complete."""
        p1 = [_raw_tx(record_date="2026-09-02")] * 3 + [_raw_tx(record_date="2026-09-01")] * 2
        p2 = [_raw_tx(record_date="2026-09-01")] * 2 + [_raw_tx(record_date="2026-08-31")]
        sess = FakeSession([_page(p1), _page(p2)])
        out = fiscal._fetch_recent(
            fiscal.DEBT_TRANSACTIONS, {"sort": "-record_date"}, n_dates=2, size=5, session=sess
        )
        assert len(out) == 8
        assert len(sess.calls) == 2
        # the trim then keeps exactly the 2 newest complete dates
        kept = fiscal.latest_dates_rows(out, 2)
        assert {r["record_date"] for r in kept} == {"2026-09-01", "2026-09-02"}
        assert len(kept) == 7  # 3 + 4 (both halves of the split date)

    def test_fetch_recent_short_page_stops(self):
        sess = FakeSession([_page([_raw_tx(), _raw_tx(record_date="2026-09-01")])])
        out = fiscal._fetch_recent(
            fiscal.DEBT_TRANSACTIONS, {"sort": "-record_date"}, n_dates=14, size=500, session=sess
        )
        assert len(out) == 2

    def test_auctions_window_pages_until_cutoff_crossed(self):
        """Future-dated announcement rows stay in; paging continues while the
        oldest held row is still inside the cutoff window."""
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC).date() - timedelta(days=120)).isoformat()
        p1 = [
            _raw_auction(record_date="2026-09-15", cusip="A"),  # announcement (future)
            _raw_auction(record_date=cutoff, cusip="B"),
            _raw_auction(record_date=cutoff, cusip="C"),  # oldest still >= cutoff → page on
        ]
        p2 = [_raw_auction(record_date="2001-01-01", cusip="D")]  # older than cutoff → stop
        sess = FakeSession([_page(p1), _page(p2)])
        out = fiscal.fetch_auctions_window(session=sess, size=3)
        assert sess.calls[0][0] == fiscal.AUCTIONS and len(sess.calls) == 2
        assert {r["cusip"] for r in out} == {"A", "B", "C"}
        assert all(r["record_date"] >= cutoff for r in out)


class TestWindowTrim:
    def test_latest_dates_keeps_newest_n(self):
        rows = (
            [_raw_tx(record_date="2026-09-02")]
            + [_raw_tx(record_date=f"2026-08-{d:02d}") for d in range(25, 31)]
            + [_raw_tx(record_date="2026-09-01")]
        )
        kept = fiscal.latest_dates_rows(rows, 2)
        assert {r["record_date"] for r in kept} == {"2026-09-01", "2026-09-02"}

    def test_n_larger_than_distinct_dates_keeps_all(self):
        rows = [_raw_tx(record_date="2026-09-02"), _raw_tx(record_date="2026-09-01")]
        assert len(fiscal.latest_dates_rows(rows, 6)) == 2


class TestDebtTxParsing:
    def test_security_type_desc_folded_into_pk_key(self):
        """(Issues, Marketable, Bills) legitimately appears TWICE per day —
        Regular vs Cash Management Series. The fold keeps both rows unique
        under the locked v10 PK."""
        reg = fiscal.parse_debt_tx_row(
            _raw_tx(security_type_desc="Regular Series", transaction_today_amt="284326")
        )
        cm = fiscal.parse_debt_tx_row(
            _raw_tx(security_type_desc="Cash Management Series", transaction_today_amt="0")
        )
        assert reg["security_type"] == "Bills (Regular Series)"
        assert cm["security_type"] == "Bills (Cash Management Series)"
        assert reg["security_type"] != cm["security_type"]
        plain = fiscal.parse_debt_tx_row(_raw_tx(security_type="Notes"))
        assert plain["security_type"] == "Notes"

    def test_amount_unit_is_millions_anchored(self):
        # live 2026-09-02: GAS issues 585,955 = $585.955B (DTS $M convention)
        p = fiscal.parse_debt_tx_row(
            _raw_tx(
                security_market="Nonmarketable",
                security_type="Government Account Series",
                transaction_today_amt="585955",
            )
        )
        assert p["amount_today"] == 585955.0

    def test_net_marketable_today_live_anchor(self):
        """The full live 2026-09-02 marketable set nets to exactly −20 $M."""
        rows = [
            _raw_tx(security_type_desc="Regular Series", transaction_today_amt="-9"),
            _raw_tx(security_type_desc="Cash Management Series", transaction_today_amt="0"),
            _raw_tx(security_type="Notes", transaction_today_amt="-2"),
            _raw_tx(security_type="Bonds", transaction_today_amt="0"),
            _raw_tx(
                security_type="Inflation-Protected Securities Increment", transaction_today_amt="-9"
            ),
            _raw_tx(security_type="Federal Financing Bank", transaction_today_amt="0"),
            _raw_tx(
                transaction_type="Redemptions", security_type="Bills", transaction_today_amt="0"
            ),
            _raw_tx(
                transaction_type="Redemptions", security_type="Notes", transaction_today_amt="0"
            ),
            # nonmarketable rows must not enter the marketable net
            _raw_tx(
                security_market="Nonmarketable",
                security_type="Government Account Series",
                transaction_today_amt="585955",
            ),
        ]
        parsed = [p for p in (fiscal.parse_debt_tx_row(r) for r in rows) if p]
        assert net_marketable_today(parsed) == ("2026-09-02", -20.0)

    def test_net_marketable_empty(self):
        assert net_marketable_today([]) is None


class TestFytdHelper:
    def test_gas_category_excluded(self):
        rows = [
            {
                "record_date": "2026-07-31",
                "expense_catg_desc": "INTEREST EXPENSE ON PUBLIC ISSUES",
                "expense_group_desc": "ACCRUED INTEREST EXPENSE",
                "expense_type_desc": "Treasury Notes",
                "month_amt": 1.0,
                "fytd_amt": 410.5,
            },
            {
                "record_date": "2026-07-31",
                "expense_catg_desc": "INTEREST EXPENSE ON PUBLIC ISSUES",
                "expense_group_desc": "AMORTIZED DISCOUNT",
                "expense_type_desc": "Treasury Bills",
                "month_amt": 2.0,
                "fytd_amt": 209.2,
            },
            # intragovernmental GAS cash payments — not debt service
            {
                "record_date": "2026-07-31",
                "expense_catg_desc": "INTEREST EXPENSE ON GOVT ACCOUNT SERIES",
                "expense_group_desc": "CASH BASIS GAS PAYMENTS",
                "expense_type_desc": "Interest Payments",
                "month_amt": 99.0,
                "fytd_amt": 999.0,
            },
            # older month ignored (latest date only)
            {
                "record_date": "2026-06-30",
                "expense_catg_desc": "INTEREST EXPENSE ON PUBLIC ISSUES",
                "expense_group_desc": "ACCRUED INTEREST EXPENSE",
                "expense_type_desc": "Treasury Notes",
                "month_amt": 1.0,
                "fytd_amt": 777.0,
            },
        ]
        assert fytd_public_issues(rows) == ("2026-07-31", pytest.approx(619.7))


class TestUpserts:
    def test_announcement_then_results_replaces(self, tmp_path):
        """REGRESSION (live-verified lifecycle 2026-09-04): the announcement row
        shares (auction_date, cusip) with the results row that lands after the
        auction — INSERT OR IGNORE would freeze the announcement nulls forever."""
        conn = db.get_conn(tmp_path / "fx.db", allow_init=True)
        ann = _raw_auction(
            auction_date="2026-09-10",
            cusip="912810UW6",
            price_per100="null",
            avg_med_yield="null",
            bid_to_cover_ratio="null",
            allocation_pctage="null",
        )
        store_auctions(conn, [ann])
        res = _raw_auction(
            auction_date="2026-09-10",
            cusip="912810UW6",
            bid_to_cover_ratio="2.410000",
            avg_med_yield="4.120000",
        )
        store_auctions(conn, [res])
        row = conn.execute(
            "SELECT bid_to_cover, avg_med_yield, price_per100 FROM fd_auctions"
            " WHERE auction_date='2026-09-10' AND cusip='912810UW6'"
        ).fetchone()
        assert row == (2.41, 4.12, 99.540696)  # results defaults survive the REPLACE
        assert conn.execute("SELECT COUNT(*) FROM fd_auctions").fetchone()[0] == 1
        conn.close()

    def test_same_batch_results_win_regardless_of_input_order(self, tmp_path):
        conn = db.get_conn(tmp_path / "fx.db", allow_init=True)
        res = _raw_auction(bid_to_cover_ratio="2.530000")
        ann = _raw_auction(
            price_per100="null",
            avg_med_yield="null",
            bid_to_cover_ratio="null",
            allocation_pctage="null",
        )
        # announcement LAST in the input — the fullness sort must still put it
        # first so the results row wins the REPLACE
        store_auctions(conn, [res, ann])
        btc = conn.execute("SELECT bid_to_cover FROM fd_auctions").fetchone()[0]
        assert btc == 2.53
        conn.close()

    def test_transactions_round_trip_and_idempotency(self, tmp_path):
        conn = db.get_conn(tmp_path / "fx.db", allow_init=True)
        rows = [
            _raw_tx(security_type_desc="Regular Series", transaction_today_amt="284326"),
            _raw_tx(
                transaction_type="Redemptions", security_type="Notes", transaction_today_amt="0"
            ),
        ]
        assert store_debt_transactions(conn, rows) == 2
        assert store_debt_transactions(conn, rows) == 2  # idempotent re-run
        assert conn.execute("SELECT COUNT(*) FROM fd_debt_transactions").fetchone()[0] == 2
        got = conn.execute(
            "SELECT security_type, amount_today FROM fd_debt_transactions"
            " WHERE transaction_type='Issues'"
        ).fetchone()
        assert got == ("Bills (Regular Series)", 284326.0)
        # DTS restatement replaces the stored value
        store_debt_transactions(
            conn, [_raw_tx(security_type_desc="Regular Series", transaction_today_amt="284327")]
        )
        got = conn.execute(
            "SELECT amount_today FROM fd_debt_transactions WHERE transaction_type='Issues'"
        ).fetchone()
        assert got == (284327.0,)
        conn.close()

    def test_interest_expense_null_pk_legs_are_empty_string(self, tmp_path):
        """SQLite treats NULL values in a composite PK as DISTINCT — storing
        NULL group/type would duplicate rows on every re-run; '' keeps the
        upsert deduplicating. Input = RAW API rows (parsing lives in the store)."""
        conn = db.get_conn(tmp_path / "fx.db", allow_init=True)
        base = {
            "record_date": "2026-07-31",
            "expense_catg_desc": "INTEREST EXPENSE ON PUBLIC ISSUES",
            "expense_group_desc": "null",
            "expense_type_desc": None,
            "month_expense_amt": "1.0",
            "fytd_expense_amt": "2.0",
        }
        rows = [base, dict(base, month_expense_amt="3.0")]  # would-be duplicate if NULLs
        store_interest_expense(conn, rows)
        store_interest_expense(conn, rows)  # re-run
        n = conn.execute("SELECT COUNT(*) FROM fd_interest_expense").fetchone()[0]
        assert n == 1
        stored = conn.execute(
            "SELECT expense_group_desc, expense_type_desc, month_amt FROM fd_interest_expense"
        ).fetchone()
        assert stored == ("", "", 3.0)
        conn.close()

    def test_avg_rates_round_trip(self, tmp_path):
        conn = db.get_conn(tmp_path / "fx.db", allow_init=True)
        rows = [
            {
                "record_date": "2026-07-31",
                "security_desc": "Total Marketable",
                "security_type_desc": "Marketable",
                "avg_interest_rate_amt": "3.443",
            }
        ]
        store_avg_rates(conn, rows)
        store_avg_rates(conn, rows)
        got = conn.execute(
            "SELECT security_type_desc, avg_interest_rate FROM fd_avg_rates"
        ).fetchone()
        assert got == ("Marketable", 3.443)
        assert conn.execute("SELECT COUNT(*) FROM fd_avg_rates").fetchone()[0] == 1
        conn.close()


class TestRegistryDispatch:
    def _penny_session(self) -> FakeSession:
        # newest row carries a 'null' public split (the era pitfall) — the
        # other two fields are live 2026-09-02 values
        data = [
            {
                "record_date": "2026-09-02",
                "debt_held_public_amt": "null",
                "intragov_hold_amt": "7696018006644.39",
                "tot_pub_debt_out_amt": "40117045127072.57",
            },
            {
                "record_date": "2026-09-01",
                "debt_held_public_amt": "32420529452197.11",
                "intragov_hold_amt": "7691935141009.19",
                "tot_pub_debt_out_amt": "40112464593206.30",
            },
        ]
        return FakeSession([_page(data)])

    def test_debt_total_latest(self):
        cur = fiscal.fetch_latest("FISCAL:DEBT_TOTAL", session=self._penny_session())
        assert cur == {"ts": "2026-09-02", "value": 40117045127072.57}

    def test_debt_public_skips_null_split_row(self):
        """Newest row's split can be 'null' (era pitfall) — the dispatcher must
        fall back to the newest NON-null row instead of crashing or storing 0."""
        cur = fiscal.fetch_latest("FISCAL:DEBT_PUBLIC", session=self._penny_session())
        assert cur == {"ts": "2026-09-01", "value": 32420529452197.11}
        cur2 = fiscal.fetch_latest("FISCAL:DEBT_INTRAGOV", session=self._penny_session())
        assert cur2 == {"ts": "2026-09-02", "value": 7696018006644.39}

    def test_debt_public_all_null_raises(self):
        data = [
            {"record_date": "1993-04-01", "debt_held_public_amt": "null"},
            {"record_date": "1993-04-02", "debt_held_public_amt": "null"},
        ]
        sess = FakeSession([_page(data)])
        with pytest.raises(fiscal.FiscalError, match="no recent non-null"):
            fiscal.fetch_latest("FISCAL:DEBT_PUBLIC", session=sess)

    def test_fetch_first_ts_split_starts_1997(self):
        """Depth gate input: split series start at the first NON-null row
        (1997-09-30 live), not the 1993 table start."""
        data = [
            {"record_date": "1993-04-01", "debt_held_public_amt": "null"},
            {"record_date": "1997-09-30", "debt_held_public_amt": "3789667546849.60"},
        ]
        sess = FakeSession([_page(data)])
        assert fiscal.fetch_first_ts("FISCAL:DEBT_PUBLIC", session=sess) == "1997-09-30"

    def test_tga_path_untouched(self, monkeypatch):
        rows = [
            {"record_date": "2026-09-02", "close_today_bal": "null", "open_today_bal": "812345"}
        ]
        sess = FakeSession([_page(rows)])
        monkeypatch.setattr(fiscal, "requests", sess)
        cur = fiscal.fetch_latest("FISCAL:TGA_DAILY")
        assert cur == {"ts": "2026-09-02", "value": 812345.0}
        assert sess.calls[0][0] == fiscal.BASE  # still the operating_cash_balance URL


class TestRegistryYaml:
    def test_three_debt_series_registered(self):
        reg = {e["series_id"]: e for e in load_registry()}
        for sid in DEBT_SIDS:
            assert sid in reg, f"{sid} missing from the registry"
            e = reg[sid]
            assert e["block"] == "D" and e["tier"] == 0
            assert e["freq"] == "D" and e["unit"] == "USD (raw)"
            assert e["sanity_min"] == 0.0 and e["sanity_max"] == 60e12
            assert e.get("active", 1) == 1

    def test_yaml_sids_match_dispatcher_map(self):
        assert set(fiscal.DEBT_PENNY_FIELD) == set(DEBT_SIDS)


class TestSchemaFpDeterminism:
    def test_fp_depends_on_keys_not_values(self):
        """The FED_OPS lesson: the fingerprinted row may be any row of a mixed
        batch (announcement 2026 vs results 1979) — the fp must be a function
        of the FIELD SET only, so batch composition cannot flip it."""
        row_1979 = _raw_auction(
            auction_date="1979-10-31",
            cusip="912827KC5",
            security_type="Note",
            price_per100="null",
            bid_to_cover_ratio="null",
        )
        row_2026 = _raw_auction()
        assert schema_fp(row_1979) == schema_fp(row_2026)
        assert schema_fp(row_2026) is not None

    def test_fp_changes_when_field_set_changes(self):
        renamed = {k: v for k, v in _raw_auction().items() if k != "auction_format"}
        renamed["auction_fmt"] = "Single-Price"
        assert schema_fp(renamed) != schema_fp(_raw_auction())
