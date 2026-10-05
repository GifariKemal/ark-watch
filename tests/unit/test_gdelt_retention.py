from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.qa import gdelt_retention
from arkwatch.qa.gdelt_retention import _cutoff, clean


def test_cutoff_is_8_days_lookback():
    saturday = datetime(2026, 9, 26, 7, 12, tzinfo=UTC)
    sunday = datetime(2026, 9, 27, 7, 12, tzinfo=UTC)

    assert _cutoff(saturday) == "2026-09-18T00:00:00+00:00"
    assert _cutoff(sunday) == "2026-09-19T00:00:00+00:00"


def _seed(path, fetched_at):
    conn = db.get_conn(path, allow_init=True)
    conn.executemany(
        "INSERT INTO gdelt_events "
        "(event_id,event_date,added_at_utc,fetched_at,raw_record_json) VALUES (?,?,?,?,?)",
        [
            ("old", "20260901", fetched_at[0], fetched_at[0], "{}"),
            ("old-unlinked", "20260901", fetched_at[0], fetched_at[0], "{}"),
            ("recent", "20260920", fetched_at[1], fetched_at[1], "{}"),
        ],
    )
    conn.executemany(
        "INSERT INTO gdelt_mentions "
        "(observation_id,event_id,fetched_at,raw_record_json) VALUES (?,?,?,?)",
        [
            ("old-mention", "old", fetched_at[0], "{}"),
            ("recent-mention", "old", fetched_at[1], "{}"),
        ],
    )
    conn.executemany(
        "INSERT INTO gdelt_gkg "
        "(record_id,record_time,themes_json,entities_json,locations_json,tone_json,"
        "fetched_at,raw_record_json) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("old-gkg", fetched_at[0], "[]", "{}", "[]", "[]", fetched_at[0], "{}"),
            ("new-gkg", fetched_at[1], "[]", "{}", "[]", "[]", fetched_at[1], "{}"),
        ],
    )
    conn.commit()
    conn.close()


def test_gdelt_retention_preview_is_read_only_and_protects_referenced_events(tmp_path):
    path = tmp_path / "arkwatch.db"
    now = datetime(2026, 9, 27, 7, tzinfo=UTC)
    _seed(path, ["2026-09-18T23:59:59+00:00", "2026-09-27T00:00:00+00:00"])

    result = clean(path, now=now)

    assert result["applied"] is False
    assert result["cutoff_utc"] == "2026-09-19T00:00:00+00:00"
    assert result["tables"]["gdelt_mentions"]["rows"] == 1
    assert result["tables"]["gdelt_gkg"]["rows"] == 1
    assert result["tables"]["gdelt_events"]["rows"] == 1
    assert result["protected_events"] == 1
    conn = db.get_conn(path)
    assert conn.execute("SELECT COUNT(*) FROM gdelt_events").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM gdelt_mentions").fetchone()[0] == 2
    conn.close()


def test_gdelt_retention_apply_removes_temporary_backup_after_verified_cleanup(
    tmp_path, monkeypatch
):
    path = tmp_path / "arkwatch.db"
    monkeypatch.setattr(gdelt_retention, "BACKUP_DIR", tmp_path / "backups")
    now = datetime.now(UTC) + timedelta(seconds=2)
    cutoff = _cutoff(now)
    old = (datetime.fromisoformat(cutoff) - timedelta(seconds=1)).isoformat()
    recent = cutoff
    _seed(path, [old, recent])

    result = clean(path, now=now, apply=True)

    assert result["deleted"] == {
        "gdelt_mentions": 1,
        "gdelt_gkg": 1,
        "gdelt_events": 1,
        "market_news": 0,
        "market_news_payloads": 0,
    }
    assert result["remaining"] == {table: 0 for table in gdelt_retention.TABLES}
    assert result["quick_check"] == "ok"
    assert result["db_bytes_before"] > 0
    assert result["db_bytes_after"] > 0
    assert result["wal_bytes_before"] >= 0
    assert result["wal_bytes_after"] >= 0
    assert result["temporary_backup_removed"] is True
    assert list((tmp_path / "backups").iterdir()) == []
    conn = db.get_conn(path)
    assert conn.execute("SELECT event_id FROM gdelt_events ORDER BY event_id").fetchall() == [
        ("old",),
        ("recent",),
    ]
    assert conn.execute("SELECT observation_id FROM gdelt_mentions").fetchall() == [
        ("recent-mention",)
    ]
    assert conn.execute("SELECT record_id FROM gdelt_gkg").fetchall() == [("new-gkg",)]
    conn.close()


def test_gdelt_retention_skips_snapshot_when_there_is_nothing_to_delete(tmp_path, monkeypatch):
    path = tmp_path / "arkwatch.db"
    _seed(path, ["2026-09-27T00:00:00+00:00", "2026-09-27T00:01:00+00:00"])

    def fail_snapshot(*_args):
        raise AssertionError("unnecessary snapshot")

    monkeypatch.setattr(gdelt_retention, "_create_temporary_backup", fail_snapshot)
    result = clean(path, now=datetime(2026, 9, 27, 1, tzinfo=UTC), apply=True)

    assert result["temporary_backup_bytes"] == 0
    assert result["temporary_backup_removed"] is True
    assert result["deleted"] == {table: 0 for table in gdelt_retention.TABLES}


def test_gdelt_retention_aborts_without_temporary_backup(tmp_path, monkeypatch):
    path = tmp_path / "arkwatch.db"
    now = datetime.now(UTC)
    _seed(path, ["2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00"])

    def fail_snapshot(*_args):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(gdelt_retention, "_create_temporary_backup", fail_snapshot)

    with pytest.raises(OSError, match="simulated backup failure"):
        clean(path, now=now, apply=True)

    conn = db.get_conn(path)
    assert conn.execute("SELECT COUNT(*) FROM gdelt_events").fetchone()[0] == 3
    conn.close()


def test_gdelt_retention_keeps_temporary_backup_when_postcheck_fails(tmp_path, monkeypatch):
    path = tmp_path / "arkwatch.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(gdelt_retention, "BACKUP_DIR", backup_dir)
    now = datetime.now(UTC) + timedelta(seconds=2)
    cutoff = _cutoff(now)
    old = (datetime.fromisoformat(cutoff) - timedelta(seconds=1)).isoformat()
    _seed(path, [old, cutoff])
    original_summary = gdelt_retention._summary

    def fail_during_postcheck(conn, table, bound):
        if conn.in_transaction and table == "gdelt_mentions":
            raise RuntimeError("simulated postcheck failure")
        return original_summary(conn, table, bound)

    monkeypatch.setattr(gdelt_retention, "_summary", fail_during_postcheck)
    with pytest.raises(RuntimeError, match="simulated postcheck failure"):
        clean(path, now=now, apply=True)

    snapshots = list(backup_dir.glob(".gdelt-retention-*.db"))
    assert len(snapshots) == 1
    conn = db.get_conn(path)
    assert conn.execute("SELECT COUNT(*) FROM gdelt_events").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM gdelt_mentions").fetchone()[0] == 2
    conn.close()


def test_market_news_retention_window_is_decoupled(tmp_path, monkeypatch):
    path = tmp_path / "arkwatch.db"
    conn = db.get_conn(path, allow_init=True)
    # Insert market_news: one 15 days ago (older than GDELT 8d, but within 90d), one 100 days ago
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    t_15d = "2026-09-16T12:00:00+00:00"
    t_100d = "2026-06-23T12:00:00+00:00"
    conn.execute(
        "INSERT INTO market_news (news_id, source, title, url, published_at_utc, symbols_json, cluster_id, relevance, novelty, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "n-15d",
            "RSS_FED",
            "Fed meeting note",
            "https://fed.gov/15",
            t_15d,
            "[]",
            "c1",
            1.0,
            1.0,
            t_15d,
        ),
    )
    conn.execute(
        "INSERT INTO market_news (news_id, source, title, url, published_at_utc, symbols_json, cluster_id, relevance, novelty, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "n-100d",
            "RSS_FED",
            "Ancient Fed meeting",
            "https://fed.gov/100",
            t_100d,
            "[]",
            "c2",
            1.0,
            1.0,
            t_100d,
        ),
    )
    conn.commit()
    conn.close()

    clean(path, now=now, apply=True)

    conn = db.get_conn(path)
    survived = [row[0] for row in conn.execute("SELECT news_id FROM market_news").fetchall()]
    conn.close()
    # 15-day-old news survived, 100-day-old news was purged
    assert survived == ["n-15d"]
