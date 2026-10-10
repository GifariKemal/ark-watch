from arkwatch import db
from arkwatch.qa import equity_breadth
from arkwatch.qa.fetch_log import log_collection


def test_plan_limit_is_rechecked_daily_not_hourly(tmp_path, monkeypatch, capsys):
    path = tmp_path / "b.db"
    conn = db.get_conn(path, allow_init=True)
    log_collection(
        conn, "equity_breadth", "FMP:SP500", None, 0, err="plan-limited: FMP", status="SKIPPED"
    )
    conn.close()
    monkeypatch.setattr(
        equity_breadth, "run", lambda _p: (_ for _ in ()).throw(AssertionError("called FMP"))
    )
    assert equity_breadth.main(["--db", str(path)]) == 0
    assert "rechecked daily" in capsys.readouterr().out
