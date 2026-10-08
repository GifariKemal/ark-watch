"""Unit tests for the transform math that drives the system.

Each test verifies a transform against hand-computed numbers or values
known from the literature/publications."""

from arkwatch.transforms.core import (
    momentum,
    percentile_rank,
    state_direction,
    state_from_z,
    zscore,
)
from arkwatch.transforms.fedwatch import compute, month_code


class TestZscore:
    def test_basic(self):
        # values 1-100: mean ≈ 50.5, std ≈ 28.9 → z of the last value (100) ≈ +1.71
        vals = list(range(1, 101))
        z = zscore(vals, window=100)
        assert z is not None
        assert 1.5 < z < 2.0

    def test_insufficient_data(self):
        vals = [1.0, 2.0, 3.0]
        z = zscore(vals, window=100, min_frac=0.8)
        assert z is None  # 3 obs < 80% of 100

    def test_zero_std(self):
        vals = [5.0] * 100
        z = zscore(vals)
        assert z is None  # std=0 → z undefined

    def test_extreme_positive(self):
        # a very high last value → large positive z
        vals = [10.0] * 99 + [50.0]
        z = zscore(vals, window=100)
        assert z is not None and z > 3


class TestMomentum:
    def test_basic(self):
        vals = [100, 101, 102, 103, 104, 105, 110]
        m = momentum(vals, n=5)
        # vals[-1]=110, vals[-6]=101 → 110-101=9
        assert m == 9

    def test_insufficient(self):
        vals = [1, 2, 3]
        assert momentum(vals, n=10) is None

    def test_negative(self):
        vals = [100, 95, 90, 85, 80, 75, 70]
        m = momentum(vals, n=5)
        assert m < 0


class TestPercentile:
    def test_median(self):
        vals = list(range(1, 101))
        p = percentile_rank(vals, window=100)
        assert 95 < p <= 100  # value 100 = 100th percentile (all values <= 100)

    def test_low(self):
        vals = list(range(1, 101))
        vals[-1] = 1  # last value = minimum
        p = percentile_rank(vals, window=100)
        assert p < 5


class TestState:
    def test_high(self):
        assert state_from_z(1.5) == "HIGH"

    def test_low(self):
        assert state_from_z(-1.5) == "LOW"

    def test_neutral(self):
        assert state_from_z(0.0) == "NEUTRAL"

    def test_insufficient(self):
        assert state_from_z(None) == "INSUFFICIENT"

    def test_direction_rising(self):
        assert state_direction(0.5) == "RISING"

    def test_direction_falling(self):
        assert state_direction(-0.5) == "FALLING"


class TestFedWatch:
    def test_month_code(self):
        assert month_code(2026, 9) == "SEP 26"
        assert month_code(2026, 1) == "JAN 26"
        assert month_code(2027, 12) == "DEC 27"

    def test_implied_rate(self):
        # settle 96.35 → implied rate = 100 - 96.35 = 3.65
        # with no meeting that month, implied = expected average rate
        settlements = {"SEP 26": 96.35, "OCT 26": 96.14}
        effr = 3.63
        from datetime import date

        probs = compute(settlements, effr, meetings=[date(2026, 9, 16)])
        assert len(probs) >= 1
        # implied rate must be in (0, 10), not the raw price level (~96)
        for p in probs:
            assert 0 < p.implied_rate < 10

    def test_probability_bounds(self):
        settlements = {"SEP 26": 96.35}
        from datetime import date

        probs = compute(settlements, 3.63, meetings=[date(2026, 9, 16)])
        for p in probs:
            assert 0 <= p.prob_ease <= 1
            assert 0 <= p.prob_hold <= 1
            assert 0 <= p.prob_hike <= 1
            assert abs(p.prob_ease + p.prob_hold + p.prob_hike - 1.0) < 0.01

    def test_cut_scenario(self):
        # implied below EFFR → signals ease/cut
        settlements = {"SEP 26": 97.0}  # implied = 3.0% < EFFR 3.63%
        from datetime import date

        probs = compute(settlements, 3.63, meetings=[date(2026, 9, 16)])
        assert probs[0].expected_moves < 0  # expected cut

    def test_hike_scenario(self):
        # implied above EFFR → signals hike
        settlements = {"SEP 26": 95.5}  # implied = 4.5% > EFFR 3.63%
        from datetime import date

        probs = compute(settlements, 3.63, meetings=[date(2026, 9, 16)])
        assert probs[0].expected_moves > 0  # expected hike

    def test_running_rate_second_meeting(self):
        """Regression: the second meeting after a fully-priced cut uses the running rate.

        The pre-meeting baseline is the end-of-month rate after meeting-1
        (4.08), not the previous month's time-averaged implied (4.205); a
        market pricing hold must show HOLD, not 'cut 100%'.

        SOAK 2026-09-18: Oct-28 leaves n_post=4 ≤ 7 → the CME last-week
        convention reads the NEXT-month contract for the post rate — the
        strip must carry NOV (hold priced: NOV implied 4.08)."""
        from datetime import date

        # Sep: 15 days @4.33 + 15 days @4.08 → SEP implied = 4.205 (settle 95.795)
        # Oct: hold at 4.08 all month → OCT implied = 4.08 (settle 95.92)
        # Nov: hold at 4.08 → NOV implied = 4.08 (settle 95.92)
        settlements = {"SEP 26": 95.795, "OCT 26": 95.92, "NOV 26": 95.92}
        probs = compute(settlements, 4.33, meetings=[date(2026, 9, 16), date(2026, 10, 28)])
        assert len(probs) == 2
        # meeting-1: 25bp cut fully priced
        assert probs[0].prob_ease > 0.9
        # meeting-2: market prices hold → must report hold (old bug: 'cut 100%' from pre=4.205)
        assert probs[1].prob_hold > 0.9, (
            f"second meeting wrong: {probs[1]} — pre must be 4.08 (the running rate), "
            f"not the prior month's averaged implied"
        )

    def test_stale_anchor_self_heal(self):
        """2026-09-17 incident: DFF prints T+1, so the morning after the
        Sep-16 hike (3.63→3.88) the anchor was still the pre-hike 3.63. The
        D/n_post extraction amplified the 25bp error ×31/4 → Oct-26 implied
        5.53%, Dec-09 3.61%, Jan-27 7.02% (live garbage on the server).
        With anchor_date passed, the just-held meeting re-runs first and the
        running rate bootstraps the post-hike level from the strip itself.

        SOAK 2026-09-18: Oct-28 (n_post=4 ≤ 7) now reads the NEXT-month
        contract per the CME last-week convention — implied = NOV = 3.98,
        not the old day-weighted 4.0775."""
        from datetime import date

        # live strip td=2026-09-15 (server cme_settlements, product 305)
        settlements = {
            "SEP 26": 96.2625,
            "OCT 26": 96.125,
            "NOV 26": 96.02,
            "DEC 26": 95.895,
        }
        probs = compute(settlements, 3.63, anchor_date=date(2026, 9, 15))
        # the passed meeting is bootstrap machinery — never returned
        assert all(p.meeting_date > date(2026, 9, 16) for p in probs)
        oct_row = next(p for p in probs if p.meeting_date == date(2026, 10, 28))
        # bootstrap: (3.7375·30 − 3.63·15)/15 = 3.845 running → Oct post =
        # NOV implied 3.98 — sane, NOT the 5.529 the stale anchor produced
        assert abs(oct_row.implied_rate - 3.98) < 0.01
        assert oct_row.prob_hike > 0.5  # ≈ +13.5bp priced for Oct

    def test_stale_anchor_without_date_now_sane(self):
        """SOAK 2026-09-18: without anchor_date, the Sep bootstrap + the
        next-month convention for late meetings keep every row INSIDE the
        ±100bp tripwire band — the old cascade (5.53 → 3.61 → 7.02) is gone
        at the source, so nothing needs dropping."""
        from datetime import date

        settlements = {
            "SEP 26": 96.2625,
            "OCT 26": 96.125,
            "NOV 26": 96.02,
            "DEC 26": 95.895,
        }
        probs = compute(settlements, 3.63)  # no anchor_date → bootstrap + convention
        assert all(abs(p.expected_moves) <= 4.0 for p in probs)
        # the Oct row is now SANE (post = NOV implied 3.98) and therefore kept
        oct_row = next((p for p in probs if p.meeting_date == date(2026, 10, 28)), None)
        assert oct_row is not None and 3.5 < oct_row.implied_rate < 4.5


class Test3mAnnualized:
    """3-month annualization verified through the production implementation."""

    def test_formula_production(self):
        from arkwatch.transforms.core import annualize_3m

        # 3 months of 0.2% MoM: 1.002^12 - 1 ≈ 2.43%
        assert 2.0 < annualize_3m(0.002, 0.002, 0.002) * 100 < 3.0

    def test_deflation_production(self):
        from arkwatch.transforms.core import annualize_3m

        ann = annualize_3m(0.0, 0.0, -0.004)
        assert ann * 100 < 0.5  # ≈ -1.6%


class TestXccyFormula:
    """compute_xccy exercised against a seeded database (production path, not a re-typed formula)."""

    def _quarterly_month_out(self):
        """Find the next IMM quarterly month >=60 days out (guaranteed quarterly)."""
        import datetime as _dt

        today = _dt.date.today()
        quarters = [3, 6, 9, 12]  # MAR, JUN, SEP, DEC
        names = {3: "MAR", 6: "JUN", 9: "SEP", 12: "DEC"}
        for y in (today.year, today.year + 1):
            for m in quarters:
                imm = _dt.date(y, m, 21)  # ~3rd Monday
                if (imm - today).days >= 60:
                    return f"{names[m]} {str(y)[2:]}"
        return "MAR 30"  # unreachable fallback

    def test_cip_forward_and_basis(self, tmp_path):
        from arkwatch import db
        from arkwatch.transforms.xccy import compute_xccy

        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        td = __import__("datetime").date.today().isoformat()
        month = self._quarterly_month_out()
        rows = []
        for pid, settle in ((8462, 95.91), (10247, 97.36), (58, 1.1628)):
            rows.append((td, pid, month, settle))
        conn.executemany(
            "INSERT INTO cme_settlements(trade_date,product_id,month,settle) VALUES (?,?,?,?)", rows
        )
        conn.execute(
            "INSERT INTO instrument_prices(symbol,ts,source,close) VALUES "
            "('EURUSD',?,'EODHD',1.1600)",
            (td,),
        )
        conn.commit()
        out = compute_xccy(conn)
        assert out, f"expected at least 1 quarterly contract >=30 days out (seeded {month})"
        r = out[0]
        # CIP forward must exceed spot when r_us > r_eu
        assert r.f_cip > r.spot
        assert r.f_cip < r.spot * 1.01
        conn.close()

    def test_basis_negative_when_f_below_cip(self, tmp_path):
        from arkwatch import db
        from arkwatch.transforms.xccy import compute_xccy

        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        td = __import__("datetime").date.today().isoformat()
        month = self._quarterly_month_out()
        rows = [(td, 8462, month, 95.91), (td, 10247, month, 97.36), (td, 58, month, 1.1580)]
        conn.executemany(
            "INSERT INTO cme_settlements(trade_date,product_id,month,settle) VALUES (?,?,?,?)", rows
        )
        conn.execute(
            "INSERT INTO instrument_prices(symbol,ts,source,close) VALUES "
            "('EURUSD',?,'EODHD',1.1600)",
            (td,),
        )
        conn.commit()
        out = compute_xccy(conn)
        assert out and out[0].basis_bps < 0  # F < CIP → negative basis
        conn.close()


class TestSyntheticCross:
    """compute_all_synthetic exercised against a seeded database: XAGUSD ÷ GBPUSD."""

    def test_xaggbp_division_production(self, tmp_path):
        from arkwatch import db
        from arkwatch.transforms.synthetic import compute_all_synthetic

        conn = db.get_conn(tmp_path / "t.db", allow_init=True)
        conn.executemany(
            "INSERT INTO instrument_prices(symbol,ts,source,close) VALUES (?,?,?,?)",
            [
                ("XAGUSD", "2026-09-01", "EODHD", 66.0),
                ("GBPUSD", "2026-09-01", "EODHD", 1.36),
                ("XAUUSD", "2026-09-01", "EODHD", 3260.0),
            ],
        )
        conn.commit()
        out = compute_all_synthetic(conn)
        # division (48.5), not multiplication (89.8) — regression guard
        assert 48 < out["XAGGBP"]["value"] < 49
        assert 2390 < out["XAUGBP"]["value"] < 2410
        conn.close()
