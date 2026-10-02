import json
from datetime import date

from arkwatch import db
from arkwatch.qa import explore


def _month_at(index: int) -> str:
    month_num = index
    year, month0 = divmod(month_num, 12)
    return date(2020 + year, month0 + 1, 1).isoformat()


def test_trace_shows_delta_zscore_source_and_skips_irregular_z(tmp_path, monkeypatch, capsys):
    conn = db.get_conn(tmp_path / "trace.db", allow_init=True)
    entries = [
        {
            "series_id": "TEST:MONTHLY",
            "name": "Monthly series",
            "block": "A",
            "tier": 1,
            "unit": "pct",
            "value_format": "pct",
            "freq": "M",
            "primary_source": "PRIMARY",
            "active": 1,
        },
        {
            "series_id": "TEST:EVENT",
            "name": "Event series",
            "block": "A",
            "tier": 1,
            "unit": "pct",
            "value_format": "pct",
            "freq": "E",
            "primary_source": "EVENTS",
            "active": 1,
        },
    ]
    for entry in entries:
        conn.execute(
            "INSERT INTO series_registry"
            "(series_id,name,block,tier,unit,value_format,freq,primary_source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            tuple(
                entry[key]
                for key in (
                    "series_id",
                    "name",
                    "block",
                    "tier",
                    "unit",
                    "value_format",
                    "freq",
                    "primary_source",
                )
            ),
        )
    now = "2026-09-29T00:00:00+00:00"
    observations = [
        (
            "TEST:MONTHLY",
            _month_at(i),
            "na",
            float(i + 1),
            "realtime",
            "PRIMARY",
            None,
            now,
        )
        for i in range(60)
    ]
    observations.extend(
        [
            ("TEST:MONTHLY", _month_at(59), "na", 999.0, "realtime", "SECONDARY", None, now),
            ("TEST:EVENT", "2026-09-01", "na", 4.2, "realtime", "EVENTS", None, now),
        ]
    )
    conn.executemany(
        "INSERT INTO raw_observations"
        "(series_id,ts,release_ts,value,vintage_ts,source,precision_k,fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        observations,
    )
    conn.execute(
        "INSERT INTO computed_signals"
        "(signal_id,ts,run_id,computed_at,value,state,inputs_json) VALUES (?,?,?,?,?,?,?)",
        (
            "trace_test",
            "2026-09-29",
            "run",
            now,
            1.0,
            "test",
            json.dumps(
                {
                    "monthly": "TEST:MONTHLY",
                    "event": "TEST:EVENT",
                    "duplicate": "TEST:MONTHLY",
                }
            ),
        ),
    )
    monkeypatch.setattr(explore, "load_registry", lambda active_only=False: entries)

    explore.explore_signal(conn, "trace_test", trace=True)
    output = capsys.readouterr().out
    conn.close()

    assert output.count("TEST:MONTHLY") == 3  # duplicated input keys, one trace row
    assert "delta=+1.0000" in output
    assert "z5y(level)=+1.70 (60/60)" in output
    assert "via PRIMARY" in output
    assert "TEST:EVENT" in output
    assert "delta=N/A" in output
    assert "z5y(level)=N/A (irregular frequency)" in output


def test_pillar_trace_resolves_input_series_from_pillar_definition(tmp_path, monkeypatch, capsys):
    conn = db.get_conn(tmp_path / "pillar-trace.db", allow_init=True)
    entry = {
        "series_id": "TEST:POLICY",
        "name": "Policy series",
        "block": "A",
        "tier": 1,
        "unit": "pct",
        "value_format": "pct",
        "freq": "M",
        "primary_source": "FRED",
        "active": 1,
    }
    conn.execute(
        "INSERT INTO series_registry"
        "(series_id,name,block,tier,unit,value_format,freq,primary_source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        tuple(
            entry[key]
            for key in (
                "series_id",
                "name",
                "block",
                "tier",
                "unit",
                "value_format",
                "freq",
                "primary_source",
            )
        ),
    )
    conn.executemany(
        "INSERT INTO raw_observations"
        "(series_id,ts,release_ts,value,vintage_ts,source,fetched_at) "
        "VALUES ('TEST:POLICY',?,'na',?,'realtime','FRED','2026-09-29')",
        [("2026-08-01", 4.0), ("2026-09-01", 4.25)],
    )
    conn.execute(
        "INSERT INTO computed_signals"
        "(signal_id,ts,run_id,computed_at,value,state,inputs_json) VALUES (?,?,?,?,?,?,?)",
        ("pillar_a", "2026-09-29", "run", "2026-09-29", 0.2, "RISING", '{"n_series":1}'),
    )
    monkeypatch.setattr(explore, "load_registry", lambda active_only=False: [entry])
    monkeypatch.setattr(
        "arkwatch.signals.pillars.compute_pillars",
        lambda _conn: {"A": {"parts": ["TEST:POLICY"]}},
    )

    explore.explore_signal(conn, "pillar_a", trace=True)
    output = capsys.readouterr().out
    conn.close()

    assert "TEST:POLICY" in output
    assert "delta=+0.2500" in output
    assert "via FRED" in output
