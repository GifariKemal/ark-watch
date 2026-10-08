import csv
import io
import zipfile
from datetime import UTC, datetime

import pytest

from arkwatch import db
from arkwatch.gdelt_storage import compress_record
from arkwatch.qa import gdelt_archive, market_news


def _updates(latest="20260924181500"):
    base = "http://data.gdeltproject.org/gdeltv2/"
    return "\n".join(
        (
            f"80539 hash {base}{latest}.export.CSV.zip",
            f"119940 hash {base}{latest}.mentions.CSV.zip",
            f"5674680 hash {base}{latest}.gkg.csv.zip",
        )
    )


def test_parse_lastupdate_maps_all_three_archive_types():
    archives = gdelt_archive.parse_lastupdate(_updates())

    assert set(archives) == {"export", "mentions", "gkg"}
    assert all(
        item.latest_at == datetime(2026, 9, 24, 18, 15, tzinfo=UTC) for item in archives.values()
    )
    assert (
        archives["mentions"]
        .url_at(archives["mentions"].latest_at)
        .endswith("20260924181500.mentions.CSV.zip")
    )
    assert archives["gkg"].url_at(archives["gkg"].latest_at).endswith("20260924181500.gkg.csv.zip")


def test_parse_lastupdate_rejects_missing_or_unaligned_windows():
    with pytest.raises(ValueError, match="missing feeds"):
        gdelt_archive.parse_lastupdate(_updates().splitlines()[0])
    with pytest.raises(ValueError, match="15-minute boundary"):
        gdelt_archive.parse_lastupdate(_updates("20260924181000"))


def test_windows_after_is_ordered_and_bounded():
    start = datetime(2026, 9, 24, 17, 45, tzinfo=UTC)
    end = datetime(2026, 9, 24, 18, 30, tzinfo=UTC)

    assert gdelt_archive.windows_after(start, end, limit=2) == [
        datetime(2026, 9, 24, 18, 0, tzinfo=UTC),
        datetime(2026, 9, 24, 18, 15, tzinfo=UTC),
    ]
    assert gdelt_archive.windows_after(end, end, limit=2) == []
    with pytest.raises(ValueError, match="15-minute boundaries"):
        gdelt_archive.windows_after(start.replace(minute=46), end, limit=2)


def test_archive_download_parses_zipped_tsv_and_holds_on_404():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.csv", "one\ttwo\n\nthree\tfour\n")
    zip_content = content.getvalue()

    class Response:
        def __init__(self):
            self.status_code = 200
            self.content = zip_content

        @staticmethod
        def raise_for_status():
            return None

    class Session:
        @staticmethod
        def get(_url, *, timeout):
            assert timeout == (10, 120)
            return Response()

    assert gdelt_archive.download_rows(Session(), "export", "https://example.test/a.zip") == [
        ["one", "two"],
        ["three", "four"],
    ]

    class MissingSession:
        @staticmethod
        def get(_url, *, timeout):
            return type("NotFound", (), {"status_code": 404})()

    with pytest.raises(gdelt_archive.ArchiveUnavailable):
        gdelt_archive.download_rows(MissingSession(), "export", "https://example.test/missing.zip")


def test_archive_download_accepts_large_fields_and_restores_csv_limit():
    value = "x" * (128 * 1024 + 1)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.csv", f"{value}\tvalue\n")
    zip_content = content.getvalue()

    class Response:
        status_code = 200
        content = zip_content

        @staticmethod
        def raise_for_status():
            return None

    class Session:
        @staticmethod
        def get(_url, *, timeout):
            return Response()

    previous_limit = csv.field_size_limit()
    assert gdelt_archive.download_rows(Session(), "gkg", "https://example.test/a.zip") == [
        [value, "value"]
    ]
    assert csv.field_size_limit() == previous_limit


def test_catchup_advances_watermark_only_after_each_window_commits(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "gdelt-catchup.db", allow_init=True)
    archive = gdelt_archive.Archive(
        "export",
        datetime(2026, 9, 24, 18, 30, tzinfo=UTC),
        "https://data.gdeltproject.org/gdeltv2/{timestamp}.export.CSV.zip",
    )
    conn.execute(
        "INSERT INTO gdelt_feed_state VALUES (?,?,?)",
        ("export", "2026-09-24T17:45:00+00:00", "now"),
    )
    downloads = []

    def download(_session, feed, url):
        assert feed == "export"
        downloads.append(url)
        if len(downloads) == 2:
            raise gdelt_archive.ArchiveUnavailable("not published yet")
        return []

    monkeypatch.setattr(gdelt_archive, "download_rows", download)
    sql = (
        "INSERT OR IGNORE INTO gdelt_events "
        "(event_id,event_date,added_at_utc,fetched_at,raw_record_json,raw_record_gzip) "
        "VALUES (?,?,?,?,?,?)"
    )
    with pytest.raises(gdelt_archive.ArchiveUnavailable):
        market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql)

    first_watermark = conn.execute(
        "SELECT last_window_ts_utc FROM gdelt_feed_state WHERE feed='export'"
    ).fetchone()[0]
    assert first_watermark == "2026-09-24T18:00:00+00:00"

    downloads.clear()
    monkeypatch.setattr(
        gdelt_archive, "download_rows", lambda _session, _feed, url: downloads.append(url) or []
    )
    inserted, processed = market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql)

    assert (inserted, processed) == (0, 2)
    assert [url.rsplit("/", 1)[-1][:14] for url in downloads] == [
        "20260924181500",
        "20260924183000",
    ]
    assert (
        conn.execute(
            "SELECT last_window_ts_utc FROM gdelt_feed_state WHERE feed='export'"
        ).fetchone()[0]
        == "2026-09-24T18:30:00+00:00"
    )
    conn.close()


def test_catchup_duplicate_archives_are_idempotent(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "gdelt-idempotent.db", allow_init=True)
    latest = datetime(2026, 9, 24, 18, 30, tzinfo=UTC)
    archive = gdelt_archive.Archive(
        "export", latest, "https://example.com/{timestamp}.export.CSV.zip"
    )
    conn.execute(
        "INSERT INTO gdelt_feed_state VALUES (?,?,?)",
        ("export", "2026-09-24T17:45:00+00:00", "now"),
    )
    monkeypatch.setattr(
        gdelt_archive, "download_rows", lambda _session, _feed, _url: [["same-event"]]
    )
    sql = (
        "INSERT OR IGNORE INTO gdelt_events "
        "(event_id,event_date,added_at_utc,fetched_at,raw_record_json,raw_record_gzip) "
        "VALUES (?,?,?,?,?,?)"
    )

    def transform(_rows):
        raw = '{"GlobalEventID":"same-event"}'
        return [("same-event", "20260924", latest.isoformat(), "now", "", compress_record(raw))]

    inserted, processed = market_news._collect_gdelt_feed(conn, archive, transform, sql)

    assert (inserted, processed) == (1, 3)
    assert conn.execute("SELECT COUNT(*) FROM gdelt_events").fetchone()[0] == 1
    conn.close()


def test_stale_lastupdate_does_not_regress_watermark(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "gdelt-stale-list.db", allow_init=True)
    watermark = "2026-09-24T18:30:00+00:00"
    conn.execute("INSERT INTO gdelt_feed_state VALUES (?,?,?)", ("export", watermark, "now"))
    archive = gdelt_archive.Archive(
        "export",
        datetime(2026, 9, 24, 18, 15, tzinfo=UTC),
        "https://example.com/{timestamp}.export.CSV.zip",
    )
    downloads = []
    monkeypatch.setattr(
        gdelt_archive,
        "download_rows",
        lambda _session, _feed, url: downloads.append(url) or [],
    )
    sql = "INSERT OR IGNORE INTO gdelt_events (event_id,event_date,added_at_utc,fetched_at,raw_record_json,raw_record_gzip) VALUES (?,?,?,?,?,?)"

    assert market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql) == (0, 0)
    assert downloads == []
    assert (
        conn.execute(
            "SELECT last_window_ts_utc FROM gdelt_feed_state WHERE feed='export'"
        ).fetchone()[0]
        == watermark
    )
    conn.close()


def test_catchup_drains_long_backlog_in_bounded_batches(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "gdelt-backlog.db", allow_init=True)
    start = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)
    latest = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)
    archive = gdelt_archive.Archive(
        "export", latest, "https://example.com/{timestamp}.export.CSV.zip"
    )
    conn.execute(
        "INSERT INTO gdelt_feed_state VALUES (?,?,?)",
        ("export", start.isoformat(), "now"),
    )
    downloads = []
    monkeypatch.setattr(
        gdelt_archive,
        "download_rows",
        lambda _session, _feed, url: downloads.append(url) or [],
    )
    sql = "INSERT OR IGNORE INTO gdelt_events (event_id,event_date,added_at_utc,fetched_at,raw_record_json,raw_record_gzip) VALUES (?,?,?,?,?,?)"

    assert market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql) == (0, 16)
    assert market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql) == (0, 16)
    assert market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql) == (0, 8)
    assert market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql) == (0, 0)
    assert len(downloads) == 40
    assert (
        conn.execute(
            "SELECT last_window_ts_utc FROM gdelt_feed_state WHERE feed='export'"
        ).fetchone()[0]
        == latest.isoformat()
    )
    conn.close()


def test_first_catchup_replays_a_day_before_advancing_its_watermark(tmp_path, monkeypatch):
    conn = db.get_conn(tmp_path / "gdelt-bootstrap.db", allow_init=True)
    latest = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)
    archive = gdelt_archive.Archive(
        "export", latest, "https://example.com/{timestamp}.export.CSV.zip"
    )
    downloads = []
    monkeypatch.setattr(
        gdelt_archive,
        "download_rows",
        lambda _session, _feed, url: downloads.append(url) or [],
    )
    sql = "INSERT OR IGNORE INTO gdelt_events (event_id,event_date,added_at_utc,fetched_at,raw_record_json,raw_record_gzip) VALUES (?,?,?,?,?,?)"

    for _ in range(6):
        assert market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql) == (0, 16)

    assert len(downloads) == 96
    assert (
        conn.execute(
            "SELECT last_window_ts_utc FROM gdelt_feed_state WHERE feed='export'"
        ).fetchone()[0]
        == latest.isoformat()
    )
    assert market_news._collect_gdelt_feed(conn, archive, lambda rows: [], sql) == (0, 0)
    conn.close()


def test_market_news_empty_success_is_not_reported_as_job_failure():
    assert market_news._exit_code({"FMP": 0, "EODHD": 0, "GDELT": 0}) == 0
    assert market_news._exit_code({"FMP": -1, "EODHD": -1, "GDELT": -1}) == 1
