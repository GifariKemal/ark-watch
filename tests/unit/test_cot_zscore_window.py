"""_cot_zscore reads only the newest window; the result must equal the old full-history read."""

import random
from datetime import date, timedelta

from arkwatch import db
from arkwatch.signals import cot_signals as cs


def _full_history_zscore(conn, code):
    """The pre-bound implementation, verbatim apart from the name."""
    for cat in ("mm", "lev"):
        rows = conn.execute(
            "SELECT report_date, long, short FROM cot_raw "
            "WHERE contract_code=? AND category=? AND long IS NOT NULL AND long > 0 "
            "AND report_type NOT LIKE '%_c' ORDER BY report_date",
            (code, cat),
        ).fetchall()
        if len(rows) >= cs.COT_Z_MIN_WEEKS:
            break
    else:
        return None
    if len(rows) < cs.COT_Z_MIN_WEEKS:
        return None
    nets = [(r[1] or 0) - (r[2] or 0) for r in rows]
    window = nets[-cs.COT_Z_WINDOW_WEEKS :]
    if len(window) < cs.COT_Z_MIN_WEEKS:
        return None
    mean = sum(window) / len(window)
    std = (sum((v - mean) ** 2 for v in window) / len(window)) ** 0.5
    return None if std == 0 else (window[-1] - mean) / std


def test_bounded_read_matches_full_history(tmp_path):
    conn = db.get_conn(tmp_path / "c.db", allow_init=True)
    rnd = random.Random(7)
    start = date(2015, 1, 6)
    seeds = {
        "GOLD": ("disagg", "mm", 400),
        "SPX": ("tff", "lev", 300),
        "THIN": ("disagg", "mm", 40),
    }
    for code, (rtype, cat, weeks) in seeds.items():
        for i in range(weeks):
            d = (start + timedelta(weeks=i)).isoformat()
            conn.execute(
                "INSERT INTO cot_raw (report_date, contract_code, report_type, release_ts,"
                " category, long, short, source, fetched_at) VALUES (?,?,?,?,?,?,?,'t','t')",
                (d, code, rtype, d, cat, rnd.randint(1, 90_000), rnd.randint(0, 90_000)),
            )
            # combined-report rows must stay excluded
            conn.execute(
                "INSERT INTO cot_raw (report_date, contract_code, report_type, release_ts,"
                " category, long, short, source, fetched_at) VALUES (?,?,?,?,?,?,?,'t','t')",
                (d, code, rtype + "_c", d, cat, 10**9, 0),
            )
    for code in (*seeds, "NONE"):
        assert cs._cot_zscore(conn, code) == _full_history_zscore(conn, code), code
    assert cs._cot_zscore(conn, "GOLD") is not None
    assert cs._cot_zscore(conn, "THIN") is None
