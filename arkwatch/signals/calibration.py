"""calibration.py - proper scoring of the probabilities ark-watch stores.

Brier score (Gneiting & Raftery 2007, a strictly proper scoring rule) of:
  - Polymarket crowd probabilities (`polymarket:<slug>`, value = P(first outcome)) at
    T-1d / T-7d / T-30d before resolution. Resolutions come from the Gamma API once per
    market and are cached as `calibration:pm_resolution:<slug>` (value 1/0, None = void).
  - FedWatch per-meeting probabilities (fedwatch_snapshots: ease/hold/hike) with the
    multi-category Brier sum_k (p_k - o_k)^2; the realized move is read from FRED:DFF
    (median of the week before vs the week after the decision, 25bp-step threshold).

Outputs `calibration:polymarket:brier_<h>` and `calibration:fedwatch:brier_<h>`
(value = Brier, inputs_json = n, base rate, skill, reliability bins, slope, breakdowns).
`--history N` additionally scores up to N closed topic markets from the Gamma API with
their CLOB daily price history (`calibration:polymarket_hist:brier_<h>`): a benchmark,
not ark-watch's own track record (different selection), so it is stored separately.
Fewer than MIN_N scored forecasts -> state 'insufficient'; the numbers are descriptive.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from statistics import median

import numpy as np
import requests

from ..fetchers import polymarket
from ..qa import fetch_log

GAMMA = polymarket.URL
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
TIMEOUT = (5, 20)
HORIZONS = {"1d": 1, "7d": 7, "30d": 30}
MIN_N = 30
MAX_STALE = timedelta(days=3)  # a snapshot older than this before T-h is "absent"
SLUG_BATCH = 20
HIST_PAGES = 20  # Gamma rejects offset > 2000 (HTTP 422)
RES_PREFIX = "calibration:pm_resolution:"
OUTCOMES = ("ease", "hold", "hike")


# ---- pure scoring -------------------------------------------------------------------


def brier(p, o) -> float:
    """Mean squared error of probability forecasts p against 0/1 outcomes o."""
    return float(np.mean((np.asarray(p, float) - np.asarray(o, float)) ** 2))


def brier_skill(bs: float, base_rate: float) -> float | None:
    """1 - BS / BS_climatology, climatology = always forecasting the base rate."""
    ref = base_rate * (1 - base_rate)
    return None if ref <= 0 else 1 - bs / ref


def reliability_bins(p, o, n_bins: int = 10) -> list[dict]:
    """Equal-width forecast bins: mean forecast, observed frequency, count."""
    p, o = np.asarray(p, float), np.asarray(o, float)
    idx = np.minimum((p * n_bins).astype(int), n_bins - 1)
    out = []
    for k in range(n_bins):
        m = idx == k
        out.append(
            {
                "lo": k / n_bins,
                "hi": (k + 1) / n_bins,
                "mean_forecast": round(float(p[m].mean()), 4) if m.any() else None,
                "observed_freq": round(float(o[m].mean()), 4) if m.any() else None,
                "count": int(m.sum()),
            }
        )
    return out


def calibration_slope(p, o) -> float | None:
    """Least-squares slope of outcome on forecast (1 = calibrated, <1 = overconfident)."""
    p = np.asarray(p, float)
    if len(p) < 2 or np.ptp(p) == 0:
        return None
    return float(np.polyfit(p, np.asarray(o, float), 1)[0])


def multiclass_brier(probs, realized) -> float:
    """Mean over forecasts of sum_k (p_k - o_k)^2; realized = index of the outcome."""
    P = np.asarray(probs, float)
    return float(((P - np.eye(P.shape[1])[np.asarray(realized)]) ** 2).sum(axis=1).mean())


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def summarize(p: list[float], o: list[int], *, bins: bool = True) -> dict:
    n = len(p)
    out: dict = {"n": n, "insufficient": n < MIN_N, "brier": None, "base_rate": None}
    if not n:
        return out
    bs, br = brier(p, o), sum(o) / n
    out |= {
        "brier": _r(bs),
        "base_rate": _r(br),
        "brier_climatology": _r(br * (1 - br)),
        "brier_skill": _r(brier_skill(bs, br)),
        "calibration_slope": _r(calibration_slope(p, o)),
    }
    if bins:
        out["reliability"] = reliability_bins(p, o)
    return out


def at_horizon(snaps: list[tuple[datetime, float]], t_end: datetime, days: int) -> float | None:
    """Latest snapshot at or before t_end - days (snaps sorted ascending), None if absent."""
    cut = t_end - timedelta(days=days)
    best = None
    for t, p in snaps:
        if t > cut:
            break
        best = (t, p)
    return best[1] if best and cut - best[0] <= MAX_STALE else None


# ---- IO -----------------------------------------------------------------------------


def _get(url: str, params: dict, stats: dict):
    stats["http"] += 1
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _dt(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _resolution(m: dict) -> tuple[float | None, datetime | None]:
    """(1/0 first outcome won, None void/unresolved; resolution time = min(end, closed))."""
    try:
        first = float(polymarket._listish(m["outcomePrices"])[0])
    except (KeyError, TypeError, ValueError, IndexError):
        first = None
    times = [t for t in (_dt(m.get("endDate")), _dt(m.get("closedTime"))) if t]
    return (first if first in (0.0, 1.0) else None), (min(times) if times else None)


def _upsert(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO computed_signals"
            "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _stored_markets(conn: sqlite3.Connection) -> dict[str, dict]:
    """polymarket:<id> -> {slug, end, topic, snaps[(computed_at, p)]}."""
    out: dict[str, dict] = {}
    for sid, computed, p, inputs in conn.execute(
        "SELECT signal_id, computed_at, value, inputs_json FROM computed_signals"
        " WHERE signal_id LIKE 'polymarket:%' ORDER BY computed_at"
    ):
        try:
            meta = json.loads(inputs or "{}")
        except ValueError:
            continue
        t = _dt(computed)
        if p is None or t is None:
            continue
        m = out.setdefault(sid, {"snaps": []})
        m["snaps"].append((t, float(p)))
        url, end = meta.get("url") or "", _dt(meta.get("end_date"))
        m |= {"slug": url.rsplit("/", 1)[-1], "end": end, "topic": meta.get("topic")}
    return out


def resolve_markets(conn: sqlite3.Connection, now: datetime, stats: dict) -> dict[str, dict]:
    """Stored markets whose end date passed, with cached or freshly fetched resolutions."""
    markets = {sid: m for sid, m in _stored_markets(conn).items() if m["end"] and m["end"] < now}
    cached = {
        sid: (v, ts, json.loads(j or "{}"))
        for sid, v, ts, j in conn.execute(
            "SELECT signal_id, value, ts, inputs_json FROM computed_signals WHERE signal_id LIKE ?",
            (RES_PREFIX + "%",),
        )
    }
    todo = [m["slug"] for sid, m in markets.items() if RES_PREFIX + sid[11:] not in cached]
    by_slug = {m["slug"]: sid for sid, m in markets.items()}
    computed = now.isoformat(timespec="seconds")
    rows, err = [], None
    for i in range(0, len(todo), SLUG_BATCH):
        try:
            got = _get(GAMMA, {"slug": todo[i : i + SLUG_BATCH], "closed": "true"}, stats)
        except (requests.RequestException, ValueError) as ex:
            err = f"{type(ex).__name__}: {ex}"
            break  # degraded: retried on the next run
        for g in got if isinstance(got, list) else []:
            sid = by_slug.get(g.get("slug"))
            if not sid or not g.get("closed"):
                continue  # end date passed but not resolved yet (e.g. UMA dispute)
            won, t = _resolution(g)
            t = t or markets[sid]["end"]
            inputs = {
                "slug": g["slug"],
                "resolved_at": t.isoformat(),
                "topic": markets[sid]["topic"],
            }
            rows.append(
                (
                    RES_PREFIX + sid[11:],
                    t.date().isoformat(),
                    computed,
                    computed,
                    won,
                    "void" if won is None else "resolved",
                    json.dumps(inputs),
                )
            )
            cached[RES_PREFIX + sid[11:]] = (won, t.date().isoformat(), inputs)
    _upsert(conn, rows)
    if todo:
        fetch_log.log_collection(
            conn,
            "calibration",
            "polymarket:resolutions",
            None,
            len(rows),
            err=err,
            status=None if err else "OK",  # unresolved yet is healthy, not EMPTY
        )
    stats["degraded"] = stats["degraded"] or bool(err)
    out = {}
    for sid, m in markets.items():
        c = cached.get(RES_PREFIX + sid[11:])
        if c and c[0] is not None:
            t = _dt(c[2].get("resolved_at")) or m["end"]
            out[sid] = m | {"won": int(c[0]), "t": min(t, m["end"])}
    return out


def score_binary(markets: list[dict]) -> dict[str, dict]:
    """markets: {won, t, topic, snaps} -> per-horizon summary with a per-topic breakdown."""
    out = {}
    for h, days in HORIZONS.items():
        fc = [
            (p, m["won"], m["topic"])
            for m in markets
            if (p := at_horizon(m["snaps"], m["t"], days)) is not None
        ]
        topics = sorted({t for _, _, t in fc if t})
        out[h] = summarize([f[0] for f in fc], [f[1] for f in fc]) | {
            "by_topic": {
                t: summarize(
                    [f[0] for f in fc if f[2] == t], [f[1] for f in fc if f[2] == t], bins=False
                )
                for t in topics
            }
        }
    return out


def history_markets(n_max: int, now: datetime, stats: dict) -> list[dict]:
    """Closed topic markets of the last year from Gamma + their CLOB daily price history."""
    cfg = polymarket.load_topics()
    min_vol = {t["name"]: float(t.get("min_volume", polymarket.MIN_VOLUME)) for t in cfg["topics"]}
    picked = []
    for page in range(HIST_PAGES):
        params = {
            "closed": "true",
            "order": "volumeNum",
            "ascending": "false",
            "limit": polymarket.PAGE,
            "offset": page * polymarket.PAGE,
            "end_date_min": (now - timedelta(days=365)).isoformat(timespec="seconds"),
        }
        try:
            rows = _get(GAMMA, params, stats)
        except (requests.RequestException, ValueError):
            stats["degraded"] = True
            break  # score what was collected so far
        for g in rows if isinstance(rows, list) else []:
            row = polymarket.parse_market(g) if isinstance(g, dict) else None
            topic = row and polymarket.match_topic(row["question"], cfg)
            won, t = _resolution(g) if topic else (None, None)
            if topic and row["volume"] >= min_vol[topic] and won is not None and t and t < now:
                picked.append(
                    {"slug": row["slug"], "topic": topic, "won": int(won), "t": t, "raw": g}
                )
        if len(rows) < polymarket.PAGE or len(picked) >= n_max:
            break
    out = []
    for m in picked[:n_max]:
        try:
            token = polymarket._listish(m.pop("raw")["clobTokenIds"])[0]
            hist = _get(CLOB_HISTORY, {"market": token, "interval": "max", "fidelity": 1440}, stats)
            m["snaps"] = sorted(
                (datetime.fromtimestamp(x["t"], UTC), float(x["p"])) for x in hist["history"]
            )
        except (requests.RequestException, KeyError, TypeError, ValueError, IndexError):
            stats["degraded"] = True
            continue
        out.append(m)
    return out


def fedwatch_realized(conn: sqlite3.Connection, meetings: list[date]) -> dict[date, int]:
    """Meeting -> index into OUTCOMES from the FRED:DFF level the week before vs after."""
    out = {}
    for m in meetings:
        vals = conn.execute(
            "SELECT ts, value FROM raw_observations WHERE series_id='FRED:DFF'"
            " AND vintage_ts='realtime' AND ts > ? AND ts <= ? ORDER BY ts",
            ((m - timedelta(days=7)).isoformat(), (m + timedelta(days=8)).isoformat()),
        ).fetchall()
        pre = [v for ts, v in vals if ts[:10] <= m.isoformat()]  # DFF@decision day = old rate
        post = [v for ts, v in vals if ts[:10] > (m + timedelta(days=1)).isoformat()]
        if pre and len(post) >= 2:
            d = median(post) - median(pre)
            out[m] = 2 if d > 0.125 else 0 if d < -0.125 else 1
    return out


def score_fedwatch(conn: sqlite3.Connection, today: date) -> dict[str, dict]:
    snaps: dict[tuple[str, date], list[tuple[datetime, list[float]]]] = {}
    for d, mtg, src, pe, ph, pk in conn.execute(
        "SELECT date, meeting_date, source, prob_ease, prob_hold, prob_hike FROM fedwatch_snapshots"
        " WHERE meeting_date < ? ORDER BY date",
        (today.isoformat(),),
    ):
        if None in (pe, ph, pk):
            continue
        t = datetime.combine(date.fromisoformat(d[:10]), datetime.min.time(), UTC)
        snaps.setdefault((src, date.fromisoformat(mtg[:10])), []).append((t, [pe, ph, pk]))
    realized = fedwatch_realized(conn, sorted({m for _, m in snaps}))
    out = {}
    for h, days in HORIZONS.items():
        by_src: dict[str, list] = {}
        for (src, mtg), ss in sorted(snaps.items()):
            t_end = datetime.combine(mtg, datetime.min.time(), UTC)
            p = at_horizon(ss, t_end, days)
            if p is not None and mtg in realized:
                by_src.setdefault(src, []).append((mtg, p, realized[mtg]))
        summ = {
            src: {
                "n": len(fs),
                "insufficient": len(fs) < MIN_N,
                "brier": _r(multiclass_brier([f[1] for f in fs], [f[2] for f in fs])),
                "mean_p_realized": _r(float(np.mean([f[1][f[2]] for f in fs]))),
                "meetings": [
                    {
                        "meeting": f[0].isoformat(),
                        "realized": OUTCOMES[f[2]],
                        "p": [_r(x) for x in f[1]],
                    }
                    for f in fs
                ],
            }
            for src, fs in by_src.items()
        }
        head = summ.get("diy", {"n": 0, "insufficient": True, "brier": None})
        out[h] = head | {
            "headline_source": "diy",
            "by_source": summ,
            "realized_meetings": len(realized),
        }
    return out


def run(conn: sqlite3.Connection, history: int = 0) -> tuple[dict[str, dict], dict]:
    now = datetime.now(UTC)
    stats = {"http": 0, "degraded": False}
    results = {
        f"calibration:polymarket:brier_{h}": s
        for h, s in score_binary(list(resolve_markets(conn, now, stats).values())).items()
    }
    results |= {
        f"calibration:fedwatch:brier_{h}": s for h, s in score_fedwatch(conn, now.date()).items()
    }
    if history:
        hist = score_binary(history_markets(history, now, stats))
        results |= {f"calibration:polymarket_hist:brier_{h}": s for h, s in hist.items()}
    computed = now.isoformat(timespec="seconds")
    _upsert(
        conn,
        [
            (
                sid,
                now.date().isoformat(),
                computed,
                computed,
                s["brier"],
                "insufficient" if s["insufficient"] else "ok",
                json.dumps(s),
            )
            for sid, s in results.items()
        ],
    )
    return results, stats


def main(argv: list[str] | None = None) -> int:
    from .. import db
    from ..__main__ import _DEFAULT_DB

    p = argparse.ArgumentParser(prog="arkwatch calibration")
    p.add_argument("--db", default=_DEFAULT_DB)
    p.add_argument("--history", type=int, default=0, help="also score N closed Gamma markets")
    a = p.parse_args(argv)
    conn = db.get_conn(a.db, allow_init=True)
    try:
        results, stats = run(conn, history=a.history)
    finally:
        conn.close()
    for sid, s in results.items():
        bss = s.get("brier_skill")
        print(
            f"{sid}: n={s['n']} brier={s['brier']}"
            + (f" bss={bss}" if bss is not None else "")
            + (" (insufficient)" if s["insufficient"] else "")
        )
    print(
        f"=== calibration: {stats['http']} HTTP calls{' DEGRADED' if stats['degraded'] else ''} ==="
    )
    # external-data degradation is logged in fetch_log; scoring is advisory, never page the owner
    return 0
