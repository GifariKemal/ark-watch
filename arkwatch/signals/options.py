"""options.py — CME options positioning: put/call OI ratio, OI walls, max-pain.

Data basis (migration v8, harvested daily by the cme job per
PLAN-CME-OPTIONS §2): cme_options_settlements (per-strike settlements for
the nearest ~6 expiries per product) + cme_option_underlyings (the futures
anchor row per trade_date/contract).

Units: counts and ratios — OI in contracts, PCR dimensionless, strikes and
underlyings in native price units. No money conversion anywhere (unlike
soma.py, nothing here is $).

Front contract = nearest expiry per (trade_date, product). The tables carry
no expiry date, so 'nearest' is parsed from the CME month code embedded in
contract_id (OGV26 → V = Oct, 26 = 2026); an unparseable id sorts LAST so
it can never become the front by accident.

Degradation: every public function no-ops (None / 0) when the v8 tables are
missing or empty — pre-migration DBs skip the whole layer (_soma_tables_ready
convention).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta

# Thresholds come from params_signals.yaml; the literals are only the
# unreadable-config fallback (watcher _PS pattern). Parsed inside the guard
# so a bad yaml value cannot raise at import time and silently disable the
# trigger.
try:
    from ..config import load_params_signals

    _PS = load_params_signals()
    OPTIONS_PCR_MIN_OBS = int(_PS.get("options_pcr_min_obs", 20))
    OPTIONS_WALL_MIN_OI_PCT = float(_PS.get("options_wall_min_oi_pct", 3.0))
    OPTIONS_PCR_EXTREME_RATIO = float(_PS.get("options_pcr_extreme_ratio", 0.30))
except Exception:
    _PS = {}
    OPTIONS_PCR_MIN_OBS = 20
    OPTIONS_WALL_MIN_OI_PCT = 3.0
    OPTIONS_PCR_EXTREME_RATIO = 0.30

# CME month code → month number (the letter before the year in contract_id)
MONTH_CODES = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}
_CONTRACT_RE = re.compile(r"^(?P<pre>.+?)(?P<mon>[FGHJKMNQUVXZ])(?P<yr>\d{1,2})$")

# Brief/store product order — metals first (the owner's book), then index/BTC.
PRODUCT_ORDER = ("OG", "SO", "HXE", "PO", "ES", "NQ", "BTC")
BRIEF_LABELS = {
    "OG": "Au",
    "SO": "Ag",
    "HXE": "Cu",
    "PO": "Pt",
    "ES": "ES",
    "NQ": "NQ",
    "BTC": "BTC",
}
GOLD = "OG"  # headline product: computed_signals value + the PCR trigger

# A settlement snapshot older than this never fires the PCR alert. CME keeps
# only ~5 days of settlements server-side (PLAN §2.2) — older than 4d here
# means the daily harvest broke and the data is unrecoverable; it must not
# re-alert forever.
OPTIONS_ALERT_MAX_AGE_DAYS = 4


def _options_tables_ready(conn: sqlite3.Connection) -> bool:
    """True when both v8 options tables exist (pre-migration DBs skip the signals)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name IN ('cme_options_settlements','cme_option_underlyings')"
    ).fetchone()
    return bool(row and row[0] == 2)


def _contract_sort_key(contract_id: str) -> tuple[int, int]:
    """(year, month) parsed from the CME month code in contract_id
    ('OGV26' → (2026, 10), 'BTCZ26' → (2026, 12)); unparseable ids sort
    LAST so they can never become the front by accident."""
    m = _CONTRACT_RE.match((contract_id or "").strip().upper())
    if not m:
        return (9999, 99)
    return (2000 + int(m.group("yr")), MONTH_CODES[m.group("mon")])


def _snapshot_fresh(trade_date: str) -> bool:
    age = (datetime.now(UTC).date() - date.fromisoformat(trade_date)).days
    return age <= OPTIONS_ALERT_MAX_AGE_DAYS


def _max_pain(calls: dict[float, int], puts: dict[float, int]) -> float | None:
    """Canonical max-pain strike for one contract.

    payoff(K) = Σ_calls OI_c·max(0, K − strike_c) + Σ_puts OI_p·max(0, strike_p − K)
    = the total intrinsic value option HOLDERS would receive if the
    underlying settled at K (call payoffs grow above their strike, put
    payoffs below theirs). Max pain = the candidate strike MINIMIZING that
    payoff — where option buyers lose the most and writers profit the most,
    classically a price magnet into expiry. Candidates = strikes present on
    either side. Ties resolve to the LOWER strike (deterministic).
    """
    best_k: float | None = None
    best_v: float | None = None
    for k in sorted(set(calls) | set(puts)):
        payoff = sum(oi * max(0.0, k - s) for s, oi in calls.items())
        payoff += sum(oi * max(0.0, s - k) for s, oi in puts.items())
        if best_v is None or payoff < best_v:
            best_k, best_v = k, payoff
    return best_k


def options_snapshot(conn: sqlite3.Connection, product_code: str) -> dict | None:
    """Front-contract positioning snapshot for one product at its latest date.

    - pcr: front put-OI / call-OI aggregated across strikes (None when the
      call side has no OI — no fabricated 0/0)
    - walls: every (side, strike) whose OI ≥ options_wall_min_oi_pct of the
      front-contract TOTAL OI (call+put, all strikes), largest first — the
      threshold IS the display floor
    - top_wall: walls[0], plus distance_pct vs the underlying settle when
      the anchor row exists for the same trade_date/contract
    - max_pain: see _max_pain

    Per-product MAX(trade_date): one product's failed harvest must not pin
    every other product to a stale snapshot (or vice versa).
    """
    if not _options_tables_ready(conn):
        return None
    row = conn.execute(
        "SELECT MAX(trade_date) FROM cme_options_settlements WHERE product_code=?",
        (product_code,),
    ).fetchone()
    if not row or not row[0]:
        return None
    td = row[0]
    rows = conn.execute(
        "SELECT contract_id, option_type, strike, open_interest FROM cme_options_settlements "
        "WHERE product_code=? AND trade_date=?",
        (product_code, td),
    ).fetchall()
    if not rows:
        return None
    front = min({r[0] for r in rows}, key=_contract_sort_key)
    call_oi = put_oi = 0
    calls: dict[float, int] = {}
    puts: dict[float, int] = {}
    for contract, otype, strike, oi in rows:
        if contract != front or oi is None:
            continue
        if otype == "Call":
            call_oi += oi
            calls[strike] = calls.get(strike, 0) + oi
        elif otype == "Put":
            put_oi += oi
            puts[strike] = puts.get(strike, 0) + oi
    total = call_oi + put_oi
    walls: list[dict] = []
    if total > 0:
        floor = OPTIONS_WALL_MIN_OI_PCT / 100.0 * total
        for side, book in (("Put", puts), ("Call", calls)):
            for strike, oi in book.items():
                if oi >= floor:
                    walls.append(
                        {"side": side, "strike": strike, "oi": oi, "share_pct": 100.0 * oi / total}
                    )
        walls.sort(key=lambda w: -w["oi"])
    top = walls[0] if walls else None
    und_row = conn.execute(
        "SELECT settle FROM cme_option_underlyings WHERE trade_date=? AND contract_id=?",
        (td, front),
    ).fetchone()
    und = und_row[0] if und_row else None
    if top and und:
        # distance of the magnet vs spot, signed % of the underlying
        top["distance_pct"] = (top["strike"] - und) / und * 100.0
    return {
        "trade_date": td,
        "product_code": product_code,
        "contract_id": front,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "total_oi": total,
        "pcr": (put_oi / call_oi) if call_oi > 0 else None,
        "underlying": und,
        "max_pain": _max_pain(calls, puts),
        "walls": walls,
        "top_wall": top,
    }


def _pcr_history(conn: sqlite3.Connection, product_code: str) -> list[tuple[str, float]]:
    """(trade_date, front PCR) ascending, one entry per day with a computable PCR.

    The front is re-derived PER DATE — a contract that was the front last
    month has expired since; keying the whole history to today's front would
    corrupt the trailing range the extreme trigger compares against.
    """
    rows = conn.execute(
        "SELECT trade_date, contract_id, option_type, SUM(open_interest) "
        "FROM cme_options_settlements WHERE product_code=? "
        "GROUP BY trade_date, contract_id, option_type",
        (product_code,),
    ).fetchall()
    per_day: dict[str, dict[str, dict[str, int]]] = {}
    for td, contract, otype, oi in rows:
        per_day.setdefault(td, {}).setdefault(contract, {})[otype] = oi or 0
    out: list[tuple[str, float]] = []
    for td in sorted(per_day):
        front = min(per_day[td], key=_contract_sort_key)
        call = per_day[td][front].get("Call", 0)
        put = per_day[td][front].get("Put", 0)
        if call > 0:
            out.append((td, put / call))
    return out


def options_pcr_extreme_alert(
    conn: sqlite3.Connection, min_obs: int | None = None, ratio: float | None = None
) -> dict | None:
    """Gold (OG) front PCR beyond its trailing range (watcher trigger).

    Fires when the latest PCR leaves [min − ratio·range, max + ratio·range]
    of the PRIOR days (history EXCLUDES today, so a record day cannot widen
    its own band). Requires ≥ min_obs prior observations — with the default
    20 the trigger stays quiet by construction until ~a month of daily
    harvests exist. The ratio buffer (options_pcr_extreme_ratio, placeholder
    0.30) keeps marginal new records quiet; kalibrasi after ≥8 weeks.

    A flat history (range 0) states nothing — no fire (there is no range to
    be extreme against). Freshness-capped at OPTIONS_ALERT_MAX_AGE_DAYS so a
    broken harvest cannot re-alert forever.
    """
    mo = OPTIONS_PCR_MIN_OBS if min_obs is None else min_obs
    ra = OPTIONS_PCR_EXTREME_RATIO if ratio is None else ratio
    if not _options_tables_ready(conn):
        return None
    hist_all = _pcr_history(conn, GOLD)
    if len(hist_all) < mo + 1:
        return None
    td, cur = hist_all[-1]
    hist = [p for _, p in hist_all[:-1]]
    lo, hi = min(hist), max(hist)
    rng = hi - lo
    if rng <= 0:
        return None
    if not _snapshot_fresh(td):
        return None
    if cur > hi + ra * rng:
        direction = "PUT_HEAVY"
    elif cur < lo - ra * rng:
        direction = "CALL_HEAVY"
    else:
        return None
    return {
        "trade_date": td,
        "pcr": cur,
        "direction": direction,
        "hist_min": lo,
        "hist_max": hi,
        "n_obs": len(hist),
        "ratio": ra,
    }


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def _snap_json(s: dict) -> dict:
    wall = s.get("top_wall")
    return {
        "front": s["contract_id"],
        "call_oi": s["call_oi"],
        "put_oi": s["put_oi"],
        "pcr": _r(s.get("pcr"), 4),
        "underlying": s.get("underlying"),
        "max_pain": s.get("max_pain"),
        "wall": None
        if wall is None
        else {
            "side": wall["side"],
            "strike": wall["strike"],
            "oi": wall["oi"],
            "share_pct": _r(wall.get("share_pct"), 2),
            "distance_pct": _r(wall.get("distance_pct"), 2),
        },
    }


def store_options_signals(conn: sqlite3.Connection) -> int:
    """Persist the daily options signals to computed_signals (audit trail).

    Two rows, ts = gold's latest trade_date (max across products when gold
    itself is missing) → natural daily dedup via INSERT OR REPLACE, same
    convention as store_soma_signals:
    - options_pcr   value = gold front PCR            state = PUTS_DOMINANT/CALLS_DOMINANT/N/A
    - options_walls value = gold top-wall OI share %  state = WALL/NONE
    inputs_json on BOTH rows carries every product's pcr + wall + max-pain
    context (the schema stores one value per signal_id — per-product detail
    lives in the JSON, soma convention).

    Returns 0 when there is no options data — storage is a no-op, not an error.
    """
    snaps: dict[str, dict] = {}
    for code in PRODUCT_ORDER:
        snap = options_snapshot(conn, code)
        if snap:
            snaps[code] = snap
    if not snaps:
        return 0
    gold = snaps.get(GOLD)
    ts = gold["trade_date"] if gold else max(s["trade_date"] for s in snaps.values())
    now = datetime.now(UTC).isoformat(timespec="seconds")
    products = {c: _snap_json(s) for c, s in snaps.items()}
    rows: list[tuple] = []

    gold_pcr = None if gold is None else gold.get("pcr")
    if any(s.get("pcr") is not None for s in snaps.values()):
        state = (
            "N/A" if gold_pcr is None else ("PUTS_DOMINANT" if gold_pcr > 1 else "CALLS_DOMINANT")
        )
        rows.append(
            (
                "options_pcr",
                ts,
                now,
                now,
                _r(gold_pcr, 4),
                state,
                json.dumps({"unit": "ratio", "products": products}),
            )
        )
    gold_wall = None if gold is None else gold.get("top_wall")
    any_wall = any(s.get("top_wall") for s in snaps.values())
    rows.append(
        (
            "options_walls",
            ts,
            now,
            now,
            None if gold_wall is None else round(gold_wall["share_pct"], 2),
            "WALL" if any_wall else "NONE",
            json.dumps(
                {
                    "unit": "pct_of_total_oi",
                    "wall_min_oi_pct": OPTIONS_WALL_MIN_OI_PCT,
                    "products": products,
                }
            ),
        )
    )
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
    return len(rows)


def options_brief_line(conn: sqlite3.Connection) -> str | None:
    """Render the one-line options positioning segment (after the CVOL line).

    Opt: Au PCR 1.15 (wall 2700P -1.1%) · BTC PCR 0.40 · pain Au 2700

    Segments render only for products with data; the wall parenthetical only
    when a wall clears options_wall_min_oi_pct (that threshold IS the display
    floor); 'pain' segments append after the PCR ones. The spec example's
    'Nd exp' (days to expiry) is NOT computable from the v8 tables (no
    expiry-date column) — distance-vs-underlying replaces it. Returns None
    when the tables are missing/empty (pre-v8 DB or failed daily harvest —
    degradable).
    """
    if not _options_tables_ready(conn):
        return None
    pcr_segs: list[str] = []
    pain_segs: list[str] = []
    for code in PRODUCT_ORDER:
        snap = options_snapshot(conn, code)
        if snap is None:
            continue
        label = BRIEF_LABELS.get(code, code)
        parts = []
        if snap["pcr"] is not None:
            parts.append(f"PCR {snap['pcr']:.2f}")
        wall = snap.get("top_wall")
        if wall:
            seg = f"wall {wall['strike']:g}{wall['side'][0]}"
            if wall.get("distance_pct") is not None:
                seg += f" {wall['distance_pct']:+.1f}%"
            parts.append(f"({seg})")
        if not parts:
            continue
        pcr_segs.append(f"{label} {' '.join(parts)}")
        if snap.get("max_pain") is not None:
            pain_segs.append(f"pain {label} {snap['max_pain']:g}")
    if not pcr_segs:
        return None
    line = "Opt: " + " · ".join(pcr_segs)
    if pain_segs:
        line += " · " + " · ".join(pain_segs)
    return line


def third_friday_of_month(year: int, month: int, calendar_shift: bool = True) -> date:
    """Calculate the 3rd Friday of the given year/month, shifting to Thursday if exchange holiday."""
    first_day = date(year, month, 1)
    first_friday = 1 + (4 - first_day.weekday()) % 7
    d = date(year, month, first_friday + 14)
    if calendar_shift:
        try:
            import exchange_calendars as xcals

            cal = xcals.get_calendar("XNYS")
            if not cal.is_session(d.isoformat()):
                d -= timedelta(days=1)
        except Exception:
            pass
    return d


def next_opex(as_of: date | datetime | str | None = None) -> dict:
    """Returns info about the upcoming US monthly equity/index OPEX date relative to as_of.

    Returns dict: {opex_date, days_to_opex, is_opex_week, is_quad_witching, cycle}
    """
    if as_of is None:
        target_d = datetime.now(UTC).date()
    elif isinstance(as_of, datetime):
        target_d = as_of.date()
    elif isinstance(as_of, str):
        target_d = date.fromisoformat(as_of[:10])
    else:
        target_d = as_of

    y, m = target_d.year, target_d.month
    cand = third_friday_of_month(y, m)
    if target_d > cand:
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        cand = third_friday_of_month(y, m)

    days_diff = (cand - target_d).days
    monday = cand - timedelta(days=cand.weekday())
    is_week = monday <= target_d <= cand
    is_quad = cand.month in (3, 6, 9, 12)
    quarter = f"Q{cand.month // 3}" if is_quad else "MONTHLY"

    return {
        "opex_date": cand.isoformat(),
        "days_to_opex": days_diff,
        "is_opex_week": is_week,
        "is_quad_witching": is_quad,
        "cycle": quarter,
    }


def is_opex_week(as_of: date | datetime | str | None = None) -> bool:
    """True if target date falls in the Monday..Friday of a monthly OPEX week."""
    return bool(next_opex(as_of)["is_opex_week"])


def opex_calendar(year: int) -> list[dict]:
    """All 12 monthly OPEX dates for the given year, tagging Quadruple Witching."""
    out = []
    for m in range(1, 13):
        d = third_friday_of_month(year, m)
        is_quad = m in (3, 6, 9, 12)
        out.append(
            {
                "year": year,
                "month": m,
                "opex_date": d.isoformat(),
                "is_quad_witching": is_quad,
                "cycle": f"Q{m // 3}" if is_quad else "MONTHLY",
            }
        )
    return out
