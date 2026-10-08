from __future__ import annotations

from arkwatch import db, queries


def test_cot_category_window_returns_latest_n_ascending():
    conn = db.get_conn(":memory:", allow_init=True)
    for d in range(1, 6):
        conn.execute(
            "INSERT INTO cot_raw(report_date, contract_code, report_type, release_ts, category,"
            " long, short, source, fetched_at) VALUES (?, '088691', 'legacy_fut', 'x', 'MM',"
            " ?, 1, 'CFTC', 'x')",
            (f"2026-01-0{d}", d),
        )
    rows = queries.cot_category_window(conn, "088691", "MM", limit=3)
    assert [r[0] for r in rows] == ["2026-01-03", "2026-01-04", "2026-01-05"]
