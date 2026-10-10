"""cot_signals.py — 12 research COT signals + z-scores + computed_signals persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .pillars import PILLAR_WEIGHTS, REGIME_RISK_OFF, REGIME_RISK_ON, compute_pillars

# COT z-score parameters come from config (params_signals.yaml) — the same file
# the watcher and the brief read, so the crowded flag is one definition everywhere.
try:
    from ..config import load_params_signals

    _PS = load_params_signals()
except Exception:
    _PS = {}
COT_CROWDED_Z = float(_PS.get("cot_crowded_z", 1.5))
COT_Z_WINDOW_WEEKS = int(_PS.get("cot_z_window_weeks", 156))
COT_Z_MIN_WEEKS = int(_PS.get("cot_z_min_weeks", 60))
COT_Z_CONTRACTS = (
    "067651",
    "088691",
    "084691",
    "085692",
    "076651",
    "099741",
    "097741",
    "096742",
    "232741",
    "098662",
    "133741",
    "13874+",
    "209742",
    "146021",
)


def _cot_zscore(conn: sqlite3.Connection, contract_code: str) -> float | None:
    """3y z-score — auto-selects the right category (mm for metals, lev for financials)."""
    # try 'mm' (Disaggregated) first; fall back to 'lev' (TFF) for financials
    # only the newest window is ever used: the watcher calls this every 60 s, so read just
    # that tail (max(): the MIN gate below must see the same row count as a full read)
    for cat in ("mm", "lev"):
        rows = conn.execute(
            "SELECT report_date, long, short FROM cot_raw "
            "WHERE contract_code=? AND category=? AND long IS NOT NULL AND long > 0 "
            "AND report_type NOT LIKE '%_c' "  # futures-only (not combined)
            "ORDER BY report_date DESC LIMIT ?",
            (contract_code, cat, max(COT_Z_WINDOW_WEEKS, COT_Z_MIN_WEEKS)),
        ).fetchall()[::-1]
        if len(rows) >= COT_Z_MIN_WEEKS:
            break
    else:
        return None
    if len(rows) < COT_Z_MIN_WEEKS:  # minimum ~60 weeks (1+ year)
        return None
    nets = [(r[1] or 0) - (r[2] or 0) for r in rows]
    window = nets[-COT_Z_WINDOW_WEEKS:]  # 3 years
    if len(window) < COT_Z_MIN_WEEKS:
        return None
    x = window[-1]
    mean = sum(window) / len(window)
    var = sum((v - mean) ** 2 for v in window) / len(window)
    std = var**0.5
    if std == 0:
        return None
    return (x - mean) / std


def _btc_smart_money(conn: sqlite3.Connection) -> dict | None:
    """BTC Smart-Money Follower — ΔSHORT of Leveraged Funds in CME BTC futures.

    Baur & Smales 2022 (JFM) + Kosc et al. 2022 — Leveraged Funds are the
    'smart money' in BTC futures; other categories follow with a lag. Market
    timing comes from SHORT-side adjustments, horizon 1-6 weeks.
    """
    # fetch 156 weeks of Leveraged Funds data
    rows = conn.execute(
        "SELECT report_date, change_long, change_short, long, short "
        "FROM cot_raw WHERE contract_code='133741' AND report_type='tff' "
        "AND category='lev' AND report_type NOT LIKE '%_c' "
        "ORDER BY report_date DESC LIMIT 156"
    ).fetchall()
    if len(rows) < 60:
        return None
    rows = list(reversed(rows))
    # Weekly ΔSHORT (positive = adding shorts, negative = covering). Column
    # [3] is LONG and [4] is SHORT — reading [3] here would label a ΔLONG as
    # ΔSHORT and invert the signal's direction.
    d_shorts = []
    for i in range(1, len(rows)):
        if rows[i][4] is not None and rows[i - 1][4] is not None:
            d_shorts.append(rows[i][4] - rows[i - 1][4])
    if len(d_shorts) < 60:
        return None
    cur_d = d_shorts[-1]
    mean = sum(d_shorts) / len(d_shorts)
    std = (sum((v - mean) ** 2 for v in d_shorts) / len(d_shorts)) ** 0.5
    if std == 0:
        return None
    z = (cur_d - mean) / std
    # strongly negative z = Leveraged Funds rapidly closing shorts = bullish BTC
    # strongly positive z = Leveraged Funds rapidly adding shorts = bearish BTC
    direction = (
        "COVERING (bullish)" if z < -1.0 else ("ADDING SHORTS (bearish)" if z > 1.0 else "neutral")
    )
    return {
        "z_dshort": round(z, 2),
        "direction": direction,
        "net_lev": (rows[-1][3] or 0) - (rows[-1][4] or 0) if rows[-1][3] else 0,
        "d_short": cur_d,
    }


def _price_oi_quadrant(conn: sqlite3.Connection, symbol: str, source: str) -> str | None:
    """Price × OI quadrant — market structure classification.

    price↑ + OI↑ = NEW MONEY LONG (solid trend)
    price↑ + OI↓ = SHORT COVERING (weak rally, can reverse)
    price↓ + OI↑ = NEW SHORTS (solid downward pressure)
    price↓ + OI↓ = LONG LIQUIDATION (weak selling, can bounce)
    """
    # weekly price change
    prices = conn.execute(
        "SELECT ts, close FROM instrument_prices WHERE symbol=? AND source=? "
        "AND close IS NOT NULL ORDER BY ts DESC LIMIT 10",
        (symbol, source),
    ).fetchall()
    if len(prices) < 6:
        return None
    price_now = prices[0][1]
    price_wk = prices[5][1]  # ~1 week ago
    d_price = (price_now - price_wk) / price_wk * 100 if price_wk else 0

    # ΔOI from the latest COT week vs the previous one — the contract must be
    # resolved via an explicit symbol→code map. An arbitrary contract
    # (subquery LIMIT 1 without ORDER) plus no category filter lets LIMIT 2
    # return two categories of the SAME date, forcing ΔOI≈0 forever and a
    # fictitious quadrant label.
    OI_CONTRACT = {"XAUUSD": ("088691", "mm"), "US500": ("13874+", "lev")}
    code, cat = OI_CONTRACT.get(symbol, (None, None))
    if not code:
        return None
    cot_oi = conn.execute(
        "SELECT report_date, open_interest_all FROM cot_raw "
        "WHERE contract_code=? AND category=? "
        "AND report_type NOT LIKE '%_c' "
        "AND open_interest_all IS NOT NULL "
        "GROUP BY report_date "  # one row per week (OI is identical across categories)
        "ORDER BY report_date DESC LIMIT 2",
        (code, cat),
    ).fetchall()
    if len(cot_oi) < 2 or not cot_oi[0][1] or not cot_oi[1][1]:
        return None
    d_oi = (cot_oi[0][1] - cot_oi[1][1]) / cot_oi[1][1] * 100

    if d_price > 0 and d_oi > 0:
        return "NEW MONEY (solid trend)"
    if d_price > 0 and d_oi < 0:
        return "SHORT COVERING (weak rally)"
    if d_price < 0 and d_oi > 0:
        return "NEW SHORTS (downward pressure)"
    return "LONG LIQUIDATION (weak selling)"


def _hedging_pressure(conn: sqlite3.Connection, contract_code: str) -> dict | None:
    """Hedging Pressure slow signal — commercial net-short / OI, SMA-8 smoothed.

    Kang-Rouwenhorst-Tang 2020 (Journal of Finance) — commercial hedging
    pressure carries a multi-week/month risk premium, low frequency.
    """
    rows = conn.execute(
        "SELECT report_date, long, short, open_interest_all FROM cot_raw "
        "WHERE contract_code=? AND category='prod' "
        "AND report_type NOT LIKE '%_c' AND long IS NOT NULL "
        "ORDER BY report_date DESC LIMIT 20",
        (contract_code,),
    ).fetchall()
    if len(rows) < 10:
        return None
    # HP = -(net Producer) / OI → positive = commercials net short = active hedging
    hps = []
    for r in rows:
        if r[3] and r[3] > 0:
            hp = -((r[1] or 0) - (r[2] or 0)) / r[3]
            hps.append(hp)
    if len(hps) < 8:
        return None
    hps.reverse()
    # SMA-8 smoothing
    sma = sum(hps[-8:]) / 8
    # Without 16 observations there is no comparison window; calling that
    # "FALLING" states a direction from zero information → N/A.
    if len(hps) >= 16:
        sma_prev = sum(hps[-16:-8]) / 8
        direction = "RISING" if sma > sma_prev else "FALLING"
    else:
        direction = "N/A"
    return {"hp": round(sma, 4), "direction": direction}


def _cross_contract_aggregate(conn: sqlite3.Connection) -> dict | None:
    """Cross-Contract Aggregate — Σ net (mm+lev+am) across financial contracts.

    Dunbar & Jiang 2020 — total net-long across financial futures predicts
    aggregate equity returns.
    """
    latest = conn.execute("SELECT MAX(report_date) FROM cot_raw").fetchone()[0]
    if not latest:
        return None
    # financial subset: EUR + JPY + GBP + DXY + SPX + NQ + BTC
    financial_codes = ("099741", "097741", "096742", "098662", "13874+", "209742", "133741")
    nets = []
    # Count contracts that actually contributed rows on the latest date — a
    # hardcoded total would overstate coverage when a contract has no rows
    # that day.
    contributing = 0
    for code in financial_codes:
        rows = conn.execute(
            "SELECT category, long, short FROM cot_raw "
            "WHERE contract_code=? AND report_date=? "
            "AND report_type NOT LIKE '%_c' "
            "AND category IN ('mm','lev','am')",
            (code, latest),
        ).fetchall()
        n_before = len(nets)
        for _cat, lng, sht in rows:
            if lng is not None:
                nets.append(lng - (sht or 0))
        if len(nets) > n_before:
            contributing += 1
    if not nets:
        return None
    total = sum(nets)
    return {
        "total_net": total,
        "n_contracts": contributing,
        "signal": "RISK-ON" if total > 0 else "RISK-OFF",
    }


def _fx_turning_point(conn: sqlite3.Connection, contract_code: str) -> str | None:
    """FX Turning-Point Gate — fires ONLY when position crosses into/out of the extreme zone.

    Tornell & Yuan 2012 + Wang 2003 — in FX, what is predictive are PEAKS &
    TROUGHS of positioning, not ordinary levels.
    """
    rows = conn.execute(
        "SELECT report_date, long, short FROM cot_raw "
        "WHERE contract_code=? AND category='lev' "
        "AND report_type NOT LIKE '%_c' AND long IS NOT NULL "
        "ORDER BY report_date DESC LIMIT 156",
        (contract_code,),
    ).fetchall()
    if len(rows) < 60:
        return None
    rows = list(reversed(rows))
    nets = [(r[1] or 0) - (r[2] or 0) for r in rows]
    # 90th and 10th percentiles over 3 years
    sorted_nets = sorted(nets)
    p90 = sorted_nets[int(len(sorted_nets) * 0.9)]
    p10 = sorted_nets[int(len(sorted_nets) * 0.1)]
    cur = nets[-1]
    prev = nets[-2] if len(nets) >= 2 else cur

    # cross INTO extreme
    if prev < p90 and cur >= p90:
        return "⚡ ENTERING EXTREME LONG (z-territory)"
    if prev > p10 and cur <= p10:
        return "⚡ ENTERING EXTREME SHORT (z-territory)"
    # cross OUT of extreme
    if prev >= p90 and cur < p90:
        return "UNWINDING from extreme long"
    if prev <= p10 and cur > p10:
        return "UNWINDING from extreme short"
    return None


def _silver_52wk_gate(conn: sqlite3.Connection) -> bool:
    """Silver 52wk-high × Commercial Δ — the only metal signal that survived
    the skeptical Laubsch 2025 study."""
    # check XAGUSD price against the 52-week high
    prices = conn.execute(
        "SELECT close FROM instrument_prices WHERE symbol='XAGUSD' "
        "AND source='EODHD' AND close IS NOT NULL "
        "AND (SELECT MAX(ts) FROM instrument_prices WHERE symbol='XAGUSD' "
        "AND source='EODHD' AND close IS NOT NULL) >= date('now','-4 day') "
        "ORDER BY ts DESC LIMIT 260"
    ).fetchall()
    if len(prices) < 52:
        return False
    cur = prices[0][0]
    high_52w = max(p[0] for p in prices)
    at_high = cur >= high_52w * 0.98  # within 2% of the 52w high

    if not at_high:
        return False

    # check Δ net Producer for silver
    rows = conn.execute(
        "SELECT change_long, change_short FROM cot_raw "
        "WHERE contract_code='084691' AND category='prod' "
        "AND report_type NOT LIKE '%_c' "
        "ORDER BY report_date DESC LIMIT 1"
    ).fetchone()
    if not rows:
        return False
    d_prod = (rows[0] or 0) - (rows[1] or 0)
    return d_prod > 0  # commercials closing shorts / adding longs at the 52w high


def _price_positioning_divergence(
    conn: sqlite3.Connection, symbol: str, source: str, contract_code: str
) -> str | None:
    """Price-vs-Positioning Divergence — price extreme without positioning confirmation.

    Answer to Laubsch 2025 (level z-scores are not predictive): DIVERGENCE is
    predictive — price makes a new high but the z-score does not = signal.
    """
    # price: new 20-week high/low?
    prices = conn.execute(
        "SELECT close FROM instrument_prices WHERE symbol=? AND source=? "
        "AND close IS NOT NULL ORDER BY ts DESC LIMIT 100",
        (symbol, source),
    ).fetchall()
    if len(prices) < 60:
        return None
    cur = prices[0][0]
    high_20w = max(p[0] for p in prices[:100])
    low_20w = min(p[0] for p in prices[:100])
    at_high = cur >= high_20w * 0.99
    at_low = cur <= low_20w * 1.01

    # current positioning z-score
    z = _cot_zscore(conn, contract_code)
    if z is None:
        return None

    if at_high and z < 0.5:
        return f"⚡ DIVERGENCE: price at 20w-high BUT positioning z={z:+.1f} does not confirm (weak rally)"
    if at_low and z > -0.5:
        return f"⚡ DIVERGENCE: price at 20w-low BUT positioning z={z:+.1f} does not confirm (weak selling)"
    if at_high and z >= 1.0:
        return f"✓ CONFIRMED: price high + positioning z={z:+.1f} (conviction)"
    if at_low and z <= -1.0:
        return f"✓ CONFIRMED: price low + positioning z={z:+.1f} (conviction)"
    return None


def _regime_conditioned_cot(
    conn: sqlite3.Connection, contract_code: str, regime_score: float
) -> dict | None:
    """Regime-Conditioned COT — thresholds calibrated per regime.

    Pradkhan 2016 (JFM) — COT predictability for precious metals appears only
    when bull/bear regimes are modeled. Asymmetric thresholds.
    """
    z = _cot_zscore(conn, contract_code)
    if z is None:
        return None

    # asymmetric thresholds: in a RISK-ON regime a crowded long is more
    # dangerous because everyone is already long → the reversal is sharper
    if regime_score > REGIME_RISK_ON:  # RISK-ON
        crowded_threshold = 1.0  # lower = earlier warning
        contrarian_threshold = -2.0  # more extreme required for a buy signal
    elif regime_score < REGIME_RISK_OFF:  # RISK-OFF
        crowded_threshold = 2.0  # higher = short crowding must be more extreme
        contrarian_threshold = -1.0  # earlier buy signal
    else:  # NEUTRAL
        crowded_threshold = COT_CROWDED_Z
        contrarian_threshold = -COT_CROWDED_Z

    if z > crowded_threshold:
        signal = "CROWDED (regime-adjusted)"
    elif z < contrarian_threshold:
        signal = "CONTRARIAN BUY ZONE (regime-adjusted)"
    else:
        signal = "normal"
    return {"z": z, "signal": signal, "regime": regime_score, "threshold": crowded_threshold}


def _spread_share_filter(conn: sqlite3.Connection, contract_code: str) -> float | None:
    """Spread-share conviction filter — signal quality calibration.

    Robe & Roberts — when >1/3 of large-trader positions are calendar spreads,
    the net z-score can mislead if spreads dominate.
    """
    rows = conn.execute(
        "SELECT report_date, category, long, short, spread FROM cot_raw "
        "WHERE contract_code=? AND report_type NOT LIKE '%_c' "
        "AND category='mm' AND long IS NOT NULL "
        "ORDER BY report_date DESC LIMIT 156",
        (contract_code,),
    ).fetchall()
    if len(rows) < 60:
        return None
    # spread / (long + short + spread) ratio for the latest week
    r = rows[0]
    total = (r[2] or 0) + (r[3] or 0) + (r[4] or 0)
    if total == 0:
        return None
    return (r[4] or 0) / total


def store_cot_signals(conn: sqlite3.Connection, score: float) -> int:
    """Persist the 12 COT signals + regime → computed_signals (audit trail).

    COT signal ts = report_date (natural weekly dedup — re-running the same
    week REPLACEs); daily signal ts (regime/pillars) = UTC date. Called from
    generate_brief so history is available for `explore signal` + backtests.
    """
    now_utc = datetime.now(UTC)
    run_id = now_utc.isoformat(timespec="seconds")
    today = now_utc.date().isoformat()
    rows: list[tuple] = []

    def _add(sid: str, ts: str, value, state: str, inputs: dict):
        rows.append(
            (
                sid,
                ts,
                run_id,
                run_id,
                value,
                state,
                json.dumps(inputs, ensure_ascii=False, default=str),
            )
        )

    # — daily signals: regime + 6 pillars —
    pillars = compute_pillars(conn)
    _add(
        "regime_score",
        today,
        round(score, 4),
        "RISK-ON"
        if score > REGIME_RISK_ON
        else ("RISK-OFF" if score < REGIME_RISK_OFF else "NEUTRAL"),
        {"weights": PILLAR_WEIGHTS},
    )
    for blk, p in pillars.items():
        _add(
            f"pillar_{blk.lower()}",
            today,
            p.get("z"),
            p.get("state", ""),
            {"label": p.get("label", ""), "n_series": len(p.get("parts", []) or [])},
        )

    # — weekly COT signals (ts = latest report_date) —
    latest = conn.execute("SELECT MAX(report_date) FROM cot_raw").fetchone()[0]
    if latest:
        # crowded base, used by divergence/regime-conditioned signals
        for code in COT_Z_CONTRACTS:
            z = _cot_zscore(conn, code)
            if z is not None:
                state = (
                    "CROWDED_LONG"
                    if z > COT_CROWDED_Z
                    else "CROWDED_SHORT"
                    if z < -COT_CROWDED_Z
                    else "NORMAL"
                )
                _add(
                    f"cot_z_{code.replace('+', 'p')}",
                    latest,
                    round(z, 3),
                    state,
                    {"window_wk": COT_Z_WINDOW_WEEKS, "threshold": COT_CROWDED_Z},
                )
        # BTC smart money
        sm = _btc_smart_money(conn)
        if sm:
            _add(
                "cot_smart_money_btc",
                latest,
                sm.get("z_dshort"),
                sm.get("direction", ""),
                {"signal": "#1 Baur-Smales"},
            )
        # price-vs-positioning divergence
        for sym, src, code, tag in (
            ("XAUUSD", "EODHD", "088691", "xauusd"),
            ("US500", "YAHOO", "13874+", "us500"),
        ):
            div = _price_positioning_divergence(conn, sym, src, code)
            if div:
                _add(f"cot_price_pos_div_{tag}", latest, None, div, {"signal": "#3"})
        # regime-conditioned (Gold)
        rc = _regime_conditioned_cot(conn, "088691", score)
        if rc:
            _add(
                "cot_regime_cond_gold",
                latest,
                rc.get("z"),
                rc.get("signal", ""),
                {"threshold": rc.get("threshold"), "regime": rc.get("regime"), "signal": "#4"},
            )
        # price × OI quadrant
        for sym, src, tag in (("XAUUSD", "EODHD", "xauusd"), ("US500", "YAHOO", "us500")):
            quad = _price_oi_quadrant(conn, sym, src)
            if quad:
                _add(f"cot_oi_quadrant_{tag}", latest, None, quad, {"signal": "#5"})
        # FX turning point (EuroFX)
        fx = _fx_turning_point(conn, "099741")
        if fx:
            _add("cot_fx_turning_eurfx", latest, None, fx, {"signal": "#6"})
        # hedging pressure (Gold + Copper)
        for code, name in (("088691", "gold"), ("085692", "copper")):
            hp = _hedging_pressure(conn, code)
            if hp:
                _add(
                    f"cot_hedging_pressure_{name}",
                    latest,
                    hp.get("hp"),
                    hp.get("direction", ""),
                    {"signal": "#7", "sma": 8},
                )
        # top-4 concentration (metals Disagg) — squeeze risk
        for code, name in (
            ("088691", "gold"),
            ("084691", "silver"),
            ("085692", "copper"),
            ("076651", "platinum"),
        ):
            row = conn.execute(
                "SELECT conc_top4_long, conc_top4_short FROM cot_raw "
                "WHERE contract_code=? AND report_date=? AND report_type='disagg' "
                "AND category='mm'",
                (code, latest),
            ).fetchone()
            if row and (row[0] or row[1]):
                mx, side = max((row[0] or 0, "LONG"), (row[1] or 0, "SHORT"))
                _add(
                    f"cot_conc_top4_{name}",
                    latest,
                    mx,
                    f"SQUEEZE_{side}" if mx > 40 else "NORMAL",
                    {"signal": "#8", "threshold_pct": 40.0},
                )
        # cross-contract aggregate
        agg = _cross_contract_aggregate(conn)
        if agg:
            _add(
                "cot_cross_contract_agg",
                latest,
                agg.get("total_net"),
                agg.get("signal", ""),
                {"n_contracts": agg.get("n_contracts"), "signal": "#9"},
            )
        # silver 52wk gate
        _add(
            "cot_silver_52wk_gate",
            latest,
            1.0 if _silver_52wk_gate(conn) else 0.0,
            "FIRED" if _silver_52wk_gate(conn) else "QUIET",
            {"signal": "#10"},
        )
        # spread-share conviction (Gold)
        ss = _spread_share_filter(conn, "088691")
        if ss is not None:
            _add(
                "cot_spread_share_gold",
                latest,
                round(ss, 4),
                "REDUCED_QUALITY" if ss > 0.25 else "OK",
                {"signal": "#11", "threshold": 0.25},
            )
        # retail extreme (metals Disagg nonrep) — risk context, not a predictor
        for code, name in (
            ("088691", "gold"),
            ("084691", "silver"),
            ("085692", "copper"),
            ("076651", "platinum"),
        ):
            row = conn.execute(
                "SELECT long, short FROM cot_raw WHERE contract_code=? "
                "AND report_date=? AND report_type='disagg' AND category='nonrep'",
                (code, latest),
            ).fetchone()
            if row and row[0] is not None:
                net = (row[0] or 0) - (row[1] or 0)
                state = (
                    "LONG_EXTREME" if net > 20000 else "SHORT_EXTREME" if net < -20000 else "NORMAL"
                )
                _add(
                    f"cot_retail_extreme_{name}",
                    latest,
                    net,
                    state,
                    {"signal": "#12", "threshold_abs": 20000},
                )

    if not rows:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT OR REPLACE INTO computed_signals"
        "(signal_id, ts, run_id, computed_at, value, state, inputs_json)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute("COMMIT")
    return len(rows)
