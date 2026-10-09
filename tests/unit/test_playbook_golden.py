"""Golden regression: scenario geometry and core AMT levels pinned to pre-merge main (54e47af..b008d72).

The upstream AMT merge (2026-10-09) only adds context fields; trigger/stop/target of every
scenario and the T-1 / overnight / weekly / CVA / naked-POC levels must not move.
"""

from __future__ import annotations

import random
import shutil
from datetime import UTC, date, datetime, timedelta

import pytest

from arkwatch import api, db

LEVEL_KEYS = ("PDH", "PDL", "PDC", "VAH", "VAL", "POC", "ONH", "ONL", "OR15_HIGH", "OR15_LOW")
LEVEL_KEYS += ("WEEKLY_VWAP", "WEEKLY_VAH", "WEEKLY_VAL", "DYNAMIC_CVA_VAH", "DYNAMIC_CVA_VAL")
LEVEL_KEYS += ("NAKED_POC_ABOVE", "NAKED_POC_BELOW")
NONE_BELOW = "NONE_IN_25D_LOOKBACK (All-Time Low)"

GOLDEN = {
    ("NQ1", "2026-10-07T15:07:00+00:00"): (
        "2026-10-07",
        [("SCENARIO_SWING_NAKED_POC_TARGET_LONG", "LONG", 19380.67, 19205.89, 19889.11)],
        (19568.0663, 19205.8915, 19285.2578, 19474.1415, 19314.6415, 19390.7665, 19459.6815)
        + (19238.9475, 19365.342, 19336.1488, 19380.6747, 19452.1703, 19314.5439, 19401.1959)
        + (19050.7681, 19889.1102, NONE_BELOW),
    ),
    ("NQ1", "2026-10-07T23:30:00+00:00"): (
        "2026-10-08",
        [],
        (19459.6815, 19238.9475, 19285.5842, 19436.9475, 19342.4475, 19376.1975, 19347.1863)
        + (19247.9969, "FORMING_IN_RTH", "FORMING_IN_RTH", 19376.2407, 19452.1703, 19307.3004)
        + (19451.257, 19100.8292, 19374.1471, NONE_BELOW),
    ),
    ("BTCUSD", "2026-10-07T15:07:00+00:00"): (
        "2026-10-07",
        [
            ("SCENARIO_INTRADAY_SWEEP_LONG", "LONG", 62043.24, 61857.64, 62753.3),
            ("SCENARIO_SWING_CVA_EXPANSION_SHORT", "SHORT", 62209.32, 62326.65, 61866.35),
            ("SCENARIO_SWING_NAKED_POC_TARGET_SHORT", "SHORT", 62535.58, 62714.74, 61534.28),
        ],
        (62714.7423, 62043.2354, 62483.5728, 62524.2354, 62212.2354, 62322.7354, 62714.7423)
        + (62043.2354, 62537.4022, 62472.3216, 62535.5759, 62770.8814, 62171.7021, 62552.2837)
        + (62209.3155, 62753.2989, 61534.281),
    ),
    ("BTCUSD", "2026-10-07T23:30:00+00:00"): (
        "2026-10-08",
        [("SCENARIO_SWING_CVA_EXPANSION_SHORT", "SHORT", 62140.5, 62327.75, 61691.09)],
        (62702.3536, 61466.3849, 61932.0641, 62541.3849, 61966.3849, 62378.8849, 62702.3536)
        + (61466.3849, 61972.8382, 61850.2217, 62454.1879, 62793.8906, 61982.6371, 62589.9065)
        + (62140.4979, 62408.811, 60608.6811),
    ),
}


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    """Deterministic 5m bars (CME hours for NQ1, 24/7 for BTCUSD) incl. bars after as_of,
    plus duplicated YAHOO/EODHD daily history."""
    path = tmp_path_factory.mktemp("golden") / "seed.db"
    conn = db.get_conn(path, allow_init=True)
    rng = random.Random(42)
    for sym, base, crypto in (("NQ1", 20000.0, False), ("BTCUSD", 60000.0, True)):
        px, t, rows = base, datetime(2026, 9, 14, tzinfo=UTC), []
        while t < datetime(2026, 10, 9, tzinfo=UTC):
            et = t - timedelta(hours=4)
            closed = not crypto and (
                et.weekday() == 5
                or (et.weekday() == 4 and et.hour >= 17)
                or (et.weekday() == 6 and et.hour < 18)
            )
            if not closed:
                o, c = px, px + rng.gauss(0, base * 0.0008)
                h = max(o, c) + abs(rng.gauss(0, base * 0.0003))
                lo = min(o, c) - abs(rng.gauss(0, base * 0.0003))
                rows.append(
                    (sym, t.isoformat(timespec="seconds"), o, h, lo, c, rng.randint(50, 500))
                )
                px = c
            t += timedelta(minutes=5)
        conn.executemany(
            "INSERT INTO intraday_bars VALUES (?, ?, '5m', 'YAHOO', ?, ?, ?, ?, ?, 'now')", rows
        )
        d, p, drows = date(2025, 1, 2), base * 0.8, []
        while d < date(2026, 10, 9):
            if crypto or d.weekday() < 5:
                o, c = p, p * (1 + rng.gauss(0, 0.01))
                for src in ("YAHOO", "EODHD"):
                    drows.append(
                        (sym, d.isoformat(), src, o, max(o, c) * 1.005, min(o, c) * 0.995, c)
                    )
                p = c
            d += timedelta(days=1)
        conn.executemany("INSERT INTO instrument_prices VALUES (?,?,?,?,?,?,?,1e6,0)", drows)
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize(("symbol", "as_of"), list(GOLDEN))
def test_playbook_golden_scenarios_and_levels(seeded_db, tmp_path, symbol, as_of):
    session_id, scenarios, level_values = GOLDEN[(symbol, as_of)]
    f = tmp_path / "g.db"
    shutil.copy(seeded_db, f)  # generation records scenarios: one fresh DB per case
    pb = api.get_trading_playbook(symbol, db_path=f, as_of=as_of)
    assert pb["session_id"] == session_id
    got = [
        (s["id"], s["direction"], s["trigger_price"], s["invalidation_level"], s["target_profit"])
        for s in pb["scenarios"]
    ]
    assert got == scenarios
    assert pb["rejected_scenarios"] == []
    assert {k: pb["reference_levels"][k] for k in LEVEL_KEYS} == dict(
        zip(LEVEL_KEYS, level_values, strict=True)
    )
