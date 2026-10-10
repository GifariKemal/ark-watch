"""Tests for the surprise engine: sigma estimation, z-scoring, and ESI.

Formula: rolling 5y sigma winsorized at ±4 sigma; ESI weights events with an
exponential decay e^(-dt/90d). Also covers indicator key normalization."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from arkwatch import db
from arkwatch.qa.calendar import indicator_key, norm
from arkwatch.qa.surprise import (
    compute_esi,
    compute_sigma,
    store_esi,
    update_surprise_z,
)


class TestIndicatorKey:
    """Family keys stay stable across releases: temporal suffixes stripped, substance kept."""

    def test_strips_month_suffix(self):
        assert indicator_key(norm("Michigan Consumer Sentiment Aug")) == indicator_key(
            norm("Michigan Consumer Sentiment Final")
        )
        assert indicator_key(norm("Chicago PMI Aug")) == "CHICAGO PMI"
        assert indicator_key(norm("Goods Trade Balance Adv")) == "GOODS TRADE BALANCE"

    def test_strips_month_day_and_year(self):
        k1 = indicator_key(norm("Baker Hughes Oil Rig Count Aug 28"))
        k2 = indicator_key(norm("Baker Hughes Oil Rig Count"))
        assert k1 == k2

    def test_strips_us_prefix(self):
        assert indicator_key(norm("US Chicago PMI")) == "CHICAGO PMI"

    def test_alias_consumer_sentiment(self):
        assert indicator_key(norm("US Consumer Sentiment")) == indicator_key(
            norm("Michigan Consumer Sentiment Aug")
        )

    def test_substance_qualifiers_kept(self):
        # "1 YEAR" vs "5 YEAR" are different indicators and must stay distinct
        assert indicator_key(norm("Michigan 1 Year Inflation Expectations Aug")) != indicator_key(
            norm("Michigan 5 Year Inflation Expectations Aug")
        )
        assert indicator_key(norm("Core CPI m/m")) != indicator_key(norm("Core CPI y/y"))


@pytest.fixture()
def conn(tmp_path):
    c = db.get_conn(tmp_path / "t.db", allow_init=True)
    yield c
    c.close()


def _seed_events(conn, diffs: list[float], start="2025-10-01", key="TEST IND", consensus=50.0):
    """Seed n weekly releases with surprise = actual - consensus = diffs[i]."""
    d0 = datetime.fromisoformat(start).replace(tzinfo=UTC)
    rows = []
    for i, d in enumerate(diffs):
        ts = (d0 + timedelta(weeks=i)).isoformat(timespec="seconds")
        actual = consensus + d
        rows.append(
            (
                f"uid-{key}-{i}",
                ts,
                ts,
                "US",
                f"Test Ind {i}",
                "TEST IND",
                "high",
                consensus,
                "FMP",
                actual,
                "FMP",
                49.0,
                None,
                0,
                key,
            )
        )
    conn.executemany(
        "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
        "importance,consensus,consensus_source,actual,actual_source,previous,"
        "surprise_z,is_curated,indicator_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


class TestSigma:
    def test_sigma_matches_spread(self, conn):
        diffs = [1.0, -1.0, 2.0, -2.0] * 10  # 40 obs, population sigma 1.32, sample sigma 1.35
        _seed_events(conn, diffs)
        r = compute_sigma(conn)
        assert r["n_indicators"] == 1
        sigma, n, low_conf = conn.execute(
            "SELECT sigma, n_obs, low_conf FROM indicator_stats"
        ).fetchone()
        assert n == 40
        assert low_conf == 0  # meets MIN_OBS
        expected = math.sqrt(sum((d - sum(diffs) / 40) ** 2 for d in diffs) / 39)
        assert sigma == pytest.approx(expected, rel=1e-6)
        vintage = conn.execute(
            "SELECT n_indicators,payload_sha256 FROM indicator_sigma_snapshots WHERE snapshot_id=?",
            (r["snapshot_id"],),
        ).fetchone()
        assert vintage[0] == 1 and len(vintage[1]) == 64
        assert conn.execute(
            "SELECT sigma FROM indicator_sigma_vintages WHERE snapshot_id=?", (r["snapshot_id"],)
        ).fetchone()[0] == pytest.approx(expected, rel=1e-6)

    def test_float_dust_rounded_data(self, conn):
        """Float dust from 0.1-precision calendar data must not collapse MAD.

        Two float representations of -0.1 (3.6-3.4 vs 2.5-2.6) differ at the
        1e-16 level; without quantization MAD degenerates to that dust
        (sigma ~ 2.2e-15, z in the trillions). Diffs are rounded before MAD."""
        from datetime import datetime, timedelta

        pairs = [(3.6, 3.4), (2.5, 2.6), (3.5, 3.4), (2.6, 2.7)] * 5  # 20 releases
        d0 = datetime(2025, 10, 1, tzinfo=UTC)
        rows = []
        for i, (act, cons) in enumerate(pairs):
            ts = (d0 + timedelta(weeks=i)).isoformat(timespec="seconds")
            rows.append(
                (
                    f"uid-dust-{i}",
                    ts,
                    ts,
                    "US",
                    "Dust",
                    "DUST",
                    "high",
                    cons,
                    "FMP",
                    act,
                    "FMP",
                    None,
                    None,
                    0,
                    "DUST",
                )
            )
        conn.executemany(
            "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
            "importance,consensus,consensus_source,actual,actual_source,previous,"
            "surprise_z,is_curated,indicator_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        compute_sigma(conn)
        sigma = conn.execute("SELECT sigma FROM indicator_stats WHERE indicator='DUST'").fetchone()[
            0
        ]
        # rounded diffs = [+0.2, -0.1, +0.1, -0.1] x5 → sample stdev = 0.1333
        assert sigma == pytest.approx(0.1333, abs=0.005)  # not 1e-15

    def test_low_conf_when_few_obs(self, conn):
        _seed_events(conn, [1.0, -1.0, 2.0])  # 3 obs, below the 30-obs minimum
        compute_sigma(conn)
        low_conf = conn.execute("SELECT low_conf FROM indicator_stats").fetchone()[0]
        assert low_conf == 1

    def test_winsorize_protects_sigma_from_outlier(self, conn):
        clean = [1.0, -1.0] * 20  # 40 normal obs
        poisoned = clean + [500.0]  # one wild event
        _seed_events(conn, poisoned)
        compute_sigma(conn)
        sigma = conn.execute("SELECT sigma FROM indicator_stats").fetchone()[0]
        # without winsorizing sigma ≈ 78 (outlier-inflated); ±4 sigma clipping keeps it < 10
        assert sigma < 10.0

    def test_surprise_z_fill(self, conn):
        diffs = [1.0, -1.0, 2.0] * 12  # 36 obs, mean 0.667, sigma ≈ 1.24
        _seed_events(conn, diffs)
        compute_sigma(conn)
        n = update_surprise_z(conn)
        assert n == 36
        z = conn.execute("SELECT surprise_z FROM events ORDER BY ts_utc").fetchone()[0]
        assert z == pytest.approx(
            1.0 / conn.execute("SELECT sigma FROM indicator_stats").fetchone()[0], rel=1e-6
        )
        audit = conn.execute(
            "SELECT sigma_vintage,sigma_snapshot_id,sigma_n_obs,sigma_window,sigma_low_conf "
            "FROM events ORDER BY ts_utc LIMIT 1"
        ).fetchone()
        assert audit[0] == pytest.approx(
            conn.execute("SELECT sigma FROM indicator_stats").fetchone()[0]
        )
        assert audit[1] and audit[2:] == (36, "5y-excl10MAD", 0)

    def test_sigma_snapshots_are_append_only(self, conn):
        _seed_events(conn, [1.0, -1.0] * 20)
        result = compute_sigma(conn)
        with pytest.raises(sqlite3.IntegrityError, match="sigma snapshots are immutable"):
            conn.execute(
                "UPDATE indicator_sigma_snapshots SET method='changed' WHERE snapshot_id=?",
                (result["snapshot_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="sigma vintages are immutable"):
            conn.execute(
                "DELETE FROM indicator_sigma_vintages WHERE snapshot_id=?",
                (result["snapshot_id"],),
            )


class TestEsi:
    def test_decay_weights_recent_more(self, conn):
        _seed_events(
            conn, [1.0, -1.0, 1.0, -1.0] * 10, start="2025-11-01"
        )  # weekly cadence; all inputs are known by the replay date
        now = datetime(2026, 9, 2, tzinfo=UTC)
        compute_sigma(conn, as_of=now.isoformat())
        update_surprise_z(conn)
        esi = compute_esi(conn, as_of=now)
        assert esi is not None
        assert -5.0 < esi < 5.0

    def test_replay_uses_only_sigma_vintage_available_at_asof(self, conn):
        _seed_events(conn, [0.2, -0.2] * 20, start="2025-11-01")
        first_asof = datetime(2026, 9, 1, tzinfo=UTC)
        first = compute_sigma(conn, as_of=first_asof.isoformat())
        update_surprise_z(conn)
        replay_asof = datetime(2026, 9, 2, tzinfo=UTC)
        old_esi = compute_esi(conn, as_of=replay_asof)
        assert old_esi is not None

        release = "2026-09-03T12:00:00+00:00"
        conn.execute(
            "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
            "importance,consensus,consensus_source,actual,actual_source,previous,"
            "surprise_z,is_curated,indicator_key) VALUES ("
            "'future-release',?,?,'US','Test Later','TEST IND','high',50,'FMP',51,'FMP',"
            "NULL,NULL,0,'TEST IND')",
            (release, release),
        )
        pending = "2026-09-05T12:00:00+00:00"
        conn.execute(
            "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
            "importance,consensus,consensus_source,actual,actual_source,previous,"
            "surprise_z,is_curated,indicator_key) VALUES ("
            "'not-yet-released',?,?,'US','Pending','TEST IND','high',50,'FMP',52,'FMP',"
            "NULL,NULL,0,'TEST IND')",
            (pending, pending),
        )
        later_asof = datetime(2026, 9, 4, tzinfo=UTC)
        later = compute_sigma(conn, as_of=later_asof.isoformat())
        update_surprise_z(conn)
        pending_z = conn.execute(
            "SELECT surprise_z,sigma_snapshot_id FROM events WHERE event_uid='not-yet-released'"
        ).fetchone()

        replay_after_new_data = compute_esi(conn, as_of=replay_asof)
        new_esi = compute_esi(conn, as_of=datetime(2026, 9, 5, tzinfo=UTC))
        assert replay_after_new_data == pytest.approx(old_esi)
        assert new_esi != pytest.approx(old_esi)
        assert first["snapshot_id"] != later["snapshot_id"]
        assert pending_z == (None, None)

    def test_esi_persists_sigma_vintage_reference(self, conn):
        _seed_events(conn, [0.2, -0.2] * 20, start="2025-11-01")
        sigma = compute_sigma(conn)
        update_surprise_z(conn)
        result = store_esi(conn)
        assert result is not None
        raw_inputs = conn.execute(
            "SELECT inputs_json FROM computed_signals WHERE signal_id='esi'"
        ).fetchone()[0]
        inputs = json.loads(raw_inputs)
        assert inputs["engine"] == "esi-v2"
        assert inputs["sigma_snapshot_id"] == sigma["snapshot_id"]
        assert len(inputs["sigma_snapshot_sha256"]) == 64

    def test_clip_bad_z(self, conn):
        # a single wild z row (24 sigma) must not drag the ESI far
        _seed_events(conn, [0.1, -0.1] * 20)
        now = datetime(2026, 9, 2, tzinfo=UTC)
        compute_sigma(conn, as_of=now.isoformat())
        update_surprise_z(conn)
        conn.execute(
            "INSERT INTO events(event_uid,ts_utc,release_ts,country,name,normalized_name,"
            "importance,consensus,consensus_source,actual,actual_source,previous,"
            "surprise_z,is_curated,indicator_key) VALUES ("
            "'bad','2026-08-30T00:00:00+00:00','2026-08-30T00:00:00+00:00','US',"
            "'Bad','BAD','high',1,'FMP',200,'FMP',1,-24.0,0,'BAD')"
        )
        conn.commit()
        esi = compute_esi(conn, as_of=now)
        # 'BAD' has no stats row (implicitly low-confidence) → excluded from ESI;
        # TEST IND z values are small → ESI stays near 0
        assert esi is not None and abs(esi) < 0.5

    def test_esi_excludes_low_conf_indicators(self, conn):
        # a low-confidence indicator (n=1, large z) must not drive the ESI
        _seed_events(conn, [0.2, -0.2] * 20)  # confident, small z
        _seed_events(conn, [8.0], key="ONE OFF")  # single release → low_conf
        now = datetime(2026, 9, 2, tzinfo=UTC)
        compute_sigma(conn, as_of=now.isoformat())
        update_surprise_z(conn)
        esi = compute_esi(conn, as_of=now)
        assert esi is not None and abs(esi) < 0.5
