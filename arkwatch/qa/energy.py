"""energy.py — daily curve signals from same-month dated contracts.

Crack/bwd legs must share an expiry month: continuous contracts roll at
different times per commodity, mixing months around rolls (the 2026-09-21
crack jump 40.65→47.86). Primary = EODHD dated contracts; fallback = Yahoo
dated contracts (free, same approach)."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"

MONTH_CODES = "FGHJKMNQUVXZ"
MONTH_NAMES = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


class EnergyError(RuntimeError):
    pass


def next_month_code(month: int, yy: int) -> str:
    m, y = (month + 1, yy) if month < 12 else (1, yy + 1)
    return f"{MONTH_CODES[m - 1]}{y % 100:02d}"


def _front_month() -> str:
    """Month code (e.g. 'X26') of the active WTI front, from Yahoo meta."""
    from ..fetchers import yahoo

    meta = yahoo.fetch_meta("CL=F")
    parts = meta["shortName"].split()
    for i, p in enumerate(parts):
        if p in MONTH_NAMES and i + 1 < len(parts):
            m = MONTH_NAMES[p]
            yy = int(parts[i + 1]) + 2000
            return f"{MONTH_CODES[m - 1]}{yy % 100:02d}"
    raise EnergyError(f"cannot parse front month from {meta.get('shortName')!r}")


def _eodhd_dated(root: str, code: str) -> dict | None:
    import os

    import requests as _rq

    key = os.environ.get("EODHD_API_TOKEN", "")
    if not key:
        return None
    try:
        r = _rq.get(
            f"https://eodhd.com/api/eod/{root}{code}-NYM.COMM",
            params={
                "api_token": key,
                "fmt": "json",
                "from": (datetime.now(UTC).date() - timedelta(days=7)).isoformat(),
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(10, 30),
        )
        rows = r.json() if r.status_code == 200 else []
        if isinstance(rows, list) and rows:
            last = rows[-1]
            return {"ts": last["date"][:10], "close": float(last["close"]), "source": "EODHD"}
    except Exception:
        pass
    return None


def _yahoo_dated(root: str, code: str) -> dict | None:
    from ..fetchers import yahoo

    try:
        bars = yahoo.fetch_daily(f"{root}{code}.NYM")
        if bars:
            last = bars[-1]
            return {"ts": last["ts"], "close": float(last["close"]), "source": "YAHOO"}
    except Exception:
        pass
    return None


def _leg(root: str, code: str) -> dict:
    """EODHD primary, Yahoo fallback — both same-month dated."""
    out = _eodhd_dated(root, code) or _yahoo_dated(root, code)
    if out is None:
        raise EnergyError(f"no data for {root}{code} (EODHD + Yahoo both failed)")
    return out


def _curve_legs(front: str, second: str) -> dict[str, dict]:
    specs = {
        "cl1": ("CL", front),
        "rb1": ("RB", front),
        "ho1": ("HO", front),
        "cl2": ("CL", second),
    }
    for fetch in (_eodhd_dated, _yahoo_dated):
        legs = {name: fetch(*spec) for name, spec in specs.items()}
        if any(leg is None for leg in legs.values()):
            continue
        dates = {leg["ts"] for leg in legs.values() if leg is not None}
        if len(dates) == 1:
            return legs
    raise EnergyError("no complete same-date curve from EODHD or Yahoo")


def _spot_pair(conn, days: int = 10) -> tuple[str, float, float]:
    series = {}
    for sid in ("FRED:DCOILBRENTEU", "FRED:DCOILWTICO"):
        rows = conn.execute(
            "SELECT substr(ts,1,10), value FROM raw_observations WHERE series_id=? "
            "AND vintage_ts='realtime' ORDER BY ts DESC LIMIT ?",
            (sid, days),
        ).fetchall()
        if not rows:
            raise EnergyError(f"no spot for {sid}")
        series[sid] = {r[0]: r[1] for r in rows}
    common = set(series["FRED:DCOILBRENTEU"]) & set(series["FRED:DCOILWTICO"])
    if not common:
        raise EnergyError("no common spot date in window")
    ts = max(common)
    return ts, series["FRED:DCOILBRENTEU"][ts], series["FRED:DCOILWTICO"][ts]


def compute(conn) -> dict[str, dict]:
    front = _front_month()
    second = next_month_code(MONTH_CODES.index(front[0]) + 1, int(front[1:]) + 2000)

    legs = _curve_legs(front, second)
    cl1, rb1, ho1, cl2 = (legs[k] for k in ("cl1", "rb1", "ho1", "cl2"))
    ts_f = cl1["ts"]
    crack_321 = round(
        (2 * (rb1["close"] * 42) + 1 * (ho1["close"] * 42) - 3 * cl1["close"]) / 3.0, 2
    )
    out: dict[str, dict] = {
        "energy_crack_321": {"ts": ts_f, "value": crack_321},
        "energy_crack_gas": {"ts": ts_f, "value": round(rb1["close"] * 42 - cl1["close"], 2)},
        "energy_crack_ho": {"ts": ts_f, "value": round(ho1["close"] * 42 - cl1["close"], 2)},
        "energy_wti_bwd": {"ts": ts_f, "value": round(cl1["close"] - cl2["close"], 2)},
    }

    ts_spot, brent, wti = _spot_pair(conn)
    out["energy_brent_wti_spot"] = {"ts": ts_spot, "value": round(brent - wti, 2)}

    for sym, leg_info in (("CL1", cl1), ("CL2", cl2)):
        conn.execute(
            "INSERT OR REPLACE INTO instrument_prices(symbol, ts, source, close) VALUES (?,?,?,?)",
            (sym, leg_info["ts"], leg_info["source"], leg_info["close"]),
        )
    conn.execute(
        "DELETE FROM instrument_prices WHERE symbol='CL2' AND ts NOT IN "
        "(SELECT ts FROM instrument_prices WHERE symbol='CL2' ORDER BY ts DESC LIMIT 10)"
    )
    conn.commit()
    return out


def compute_crack_321_history(conn, limit: int = 1260) -> list[tuple[str, float]]:
    """Historical daily 3:2:1 crack spread ($/bbl) from instrument_prices."""
    rows = conn.execute(
        "SELECT c.ts, round((2 * (r.close * 42) + 1 * (h.close * 42) - 3 * c.close) / 3.0, 2) "
        "FROM instrument_prices c "
        "JOIN instrument_prices r ON r.symbol='RB1' AND r.ts=c.ts AND r.source=c.source "
        "JOIN instrument_prices h ON h.symbol='HO1' AND h.ts=c.ts AND h.source=c.source "
        "WHERE c.symbol='CL1' AND c.source='YAHOO' "
        "ORDER BY c.ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows][::-1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arkwatch energy")
    p.add_argument("--db", default=str(DEFAULT_DB))
    args = p.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    conn = db.get_conn(args.db, allow_init=True)
    # fresh volume: the FRED spot legs land with the 06:00 harvest; until then
    # there is nothing to compute (a fetch failure with rows present still raises)
    waiting = [
        sid
        for sid in ("FRED:DCOILBRENTEU", "FRED:DCOILWTICO")
        if not conn.execute(
            "SELECT 1 FROM raw_observations WHERE series_id=? LIMIT 1", (sid,)
        ).fetchone()
    ]
    if waiting:
        from .fetch_log import log

        msg = f"waiting for FRED spot (harvest has not run yet): {', '.join(waiting)}"
        log(conn, "energy", "ENERGY:CURVE", "SKIPPED", 0, err=msg)
        print(f"  {msg}")
        conn.close()
        return 0
    out = compute(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for sid, rec in out.items():
        conn.execute(
            "INSERT OR REPLACE INTO computed_signals(signal_id, ts, run_id, computed_at,"
            " value, state, inputs_json) VALUES (?,?,?,?,?,?,?)",
            (sid, rec["ts"], "energy", now, rec["value"], None, None),
        )
    conn.commit()
    for k in sorted(out):
        print(f"  {k:26s} {out[k]['value']:>8} @ {out[k]['ts']}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
