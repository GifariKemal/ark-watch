"""brief.py — brief renderer + persistence + run() orchestration.

A pure renderer: domain results → markdown.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from ..transforms.fedwatch import format_brief as fmt_fedwatch
from .cot_signals import (
    COT_CROWDED_Z,
    _btc_smart_money,
    _cot_zscore,
    _cross_contract_aggregate,
    _fx_turning_point,
    _hedging_pressure,
    _price_oi_quadrant,
    _price_positioning_divergence,
    _regime_conditioned_cot,
    _silver_52wk_gate,
    _spread_share_filter,
    store_cot_signals,
)
from .pillars import (
    ET,
    REGIME_RISK_OFF,
    REGIME_RISK_ON,
    WIB,
    _identity_checks,
    _latest,
    compute_dollar_smile,
    compute_pillars,
    compute_quadrant,
    compute_regime_score,
)
from .pillars import (
    regime_label as _regime_label,
)


def overnight_changes(conn: sqlite3.Connection, n: int = 6) -> list[str]:
    """Series whose value changed vs their previous release.

    Scaling rules: raw counts are persons (ICSA 203,000 must render '203K',
    not '203000K') so scale=1e-3; deltas are scaled together with the level
    to avoid mixed units ('6.74T (−20,000)'). Weekly/monthly series count as
    'overnight' only when the release itself is new (D: ≤2 days, W: ≤8, M: ≤45).
    """
    # (sid, label, fmt, scale, max_age_days)
    # ROUND-2: GDPNow REMOVED — its quarter-start ts can never pass a
    # meaningful daily age gate (a 10d gate rendered cross-quarter deltas
    # like 5.1−1.5 = "+3.6pp vs Q2 FINAL" with the WRONG consensus event).
    # Nowcast coverage lives on the CLEVE:NOWCAST series + its own lines.
    WATCH = [
        ("FRED:DFII10", "Real Yield 10Y", "{:.2f}%", 1, 2),
        ("FRED:DGS10", "10Y Yield", "{:.2f}%", 1, 2),
        ("FRED:ICSA", "Claims", "{:.0f}K", 1e-3, 8),
        ("FRED:DTWEXBGS", "Dollar Idx", "{:.1f}", 1, 2),
        ("FRED:VIXCLS", "VIX", "{:.1f}", 1, 2),
        ("FRED:BAMLH0A0HYM2", "HY OAS", "{:.1f}%", 1, 2),
        ("FRED:CPIAUCSL", "CPI", "{:.1f}", 1, 45),
        ("FRED:WALCL", "Fed BS", "{:.2f}T", 1e-6, 10),
        ("FRED:M2SL", "M2", "{:.1f}T", 1e-3, 45),
    ]
    changes = []
    today = datetime.now(UTC).date()
    stale_map = _stale_suffix(conn)  # stale series → ⚠(stale Nd) suffix
    for sid, label, fmt, scale, max_age in WATCH:
        rows = conn.execute(
            "SELECT ts, value FROM raw_observations "
            "WHERE series_id=? AND vintage_ts='realtime' "
            "ORDER BY ts DESC LIMIT 3",
            (sid,),
        ).fetchall()
        if len(rows) < 2:
            continue
        cur_ts, cur_v = rows[0]
        prev_v = rows[1][1]
        if cur_v is None or prev_v is None or cur_v == prev_v:
            continue
        try:
            age = (today - datetime.fromisoformat(cur_ts[:10]).date()).days
        except ValueError:
            age = 999
        if age > max_age:  # an old release is not an 'overnight' change
            continue
        d_scaled = (cur_v - prev_v) * scale
        if abs(d_scaled) > 1000:
            d_str = f"{d_scaled:+,.0f}"
        elif abs(d_scaled) > 1:
            d_str = f"{d_scaled:+.2f}"
        else:
            d_str = f"{d_scaled:+.3f}"
        val_str = fmt.format(cur_v * scale)
        # '(cons N)' formatting — join the calendar event for series that
        # have one (GDPNow/ICSA/CPI)
        cons = _event_consensus(conn, cur_ts[:10], sid)
        cons_str = f" (cons {cons:g})" if cons is not None else ""
        # stale rows carry a suffix — no number renders plain once its
        # frequency window has been exceeded
        suffix = f" ⚠(stale {stale_map[sid]}d)" if sid in stale_map else ""
        changes.append(f"  • {label:<14} {val_str:>8}{cons_str} ({d_str}) {cur_ts[:10]}{suffix}")
    return changes[:n]


# Series ↔ calendar-indicator pairing — exact phrase + exclusion list.
# Name matching alone is doubly wrong: (1) an event's date is its RELEASE
# date while the observation date is the reference PERIOD (ICSA Saturday
# week vs Thursday release; CPI reference day 1 vs release day 12), so
# Claims never matched and CPI matched at random; (2) a bare LIKE captures
# CONTINUING claims (8.8x the value) and 4-WEEK-AVERAGE entries.
_S2_EVENT_MAP = {
    "FRED:GDPNOW": ("GDPNOW", ()),
    "FRED:ICSA": ("INITIAL JOBLESS CLAIMS", ("CONTINUING", "4 WEEK", "AVERAGE")),
    "FRED:CPIAUCSL": ("CPI S A", ("CORE", "Y Y", "M M", "EX FOOD", "ENERGY")),
}


# CNN F&G component short labels for the divergence line
_CNN_COMP_LBL = {
    "market_momentum_sp500": "SPX mom",
    "market_momentum_sp125": "SP125 mom",
    "stock_price_strength": "52w strength",
    "stock_price_breadth": "breadth",
    "put_call_options": "P/C",
    "market_volatility_vix": "VIX",
    "market_volatility_vix_50": "VIX/50d",
    "junk_bond_demand": "junk bonds",
    "safe_haven_demand": "stocks-vs-bonds",
}


def _event_consensus(conn: sqlite3.Connection, date_iso: str, sid: str) -> float | None:
    """Consensus for a changed series — matched via the PERIOD embedded in the event name.

    Joining on release date around the observation date fails systematically:
    (a) weekly Claims release T+5 vs last week's T-2 always picks the WRONG
    week; (b) CPI/GDPNow release ~42 days after the observation never match.
    Instead the month(-day) suffix in normalized_name ('INITIAL JOBLESS
    CLAIMS AUG 22') is matched against the observation ts; GDPNOW (weekly
    updates, no date-stamped name) falls back to a ±45-day post-observation
    window.
    """
    entry = _S2_EVENT_MAP.get(sid)
    if not entry:
        return None
    incl, excl = entry
    from datetime import date as _date

    obs = _date.fromisoformat(date_iso)
    mon_abbr = obs.strftime("%b").upper()  # 'AUG'
    if sid == "FRED:ICSA":
        # weekly: the name carries the DAY ('...AUG 22') = the observation;
        # without the day, distance ordering picks LAST week (T-2 < T+5).
        # ZERO-PADDED 02d: FMP writes 'SEP 05' — an unpadded 'SEP 5' token
        # never matches single-digit week-endings (round-2: silent join fail)
        period_token = f"{incl} {mon_abbr} {obs.day:02d}"
    else:
        period_token = f"{incl} {mon_abbr}"
    q = (
        "SELECT consensus FROM events "
        "WHERE normalized_name LIKE ? AND consensus IS NOT NULL AND actual IS NOT NULL "
        "AND substr(ts_utc,1,10) BETWEEN date(?, '-5 day') AND date(?, '+45 day')"
    )
    params: list = [f"%{period_token}%", date_iso, date_iso]
    for ex in excl:
        q += " AND normalized_name NOT LIKE ?"
        params.append(f"%{ex}%")
    q += " ORDER BY abs(julianday(substr(ts_utc,1,10)) - julianday(?)) LIMIT 1"
    params.append(date_iso)
    row = conn.execute(q, params).fetchone()
    if row:
        return row[0]
    # 'ATLANTA FED GDPNOW Q3' carries a QUARTER, not a month, so the month
    # token can never match; fall back to the base name + nearest release
    # after the observation (GDPNow updates ~10x per quarter)
    if sid == "FRED:GDPNOW":
        row2 = conn.execute(
            "SELECT consensus FROM events "
            "WHERE normalized_name LIKE '%GDPNOW%' AND consensus IS NOT NULL "
            "AND actual IS NOT NULL "
            "AND substr(ts_utc,1,10) BETWEEN date(?, '-1 day') AND date(?, '+45 day') "
            "ORDER BY abs(julianday(substr(ts_utc,1,10)) - julianday(?)) LIMIT 1",
            (date_iso, date_iso, date_iso),
        ).fetchone()
        return row2[0] if row2 else None
    return None


# Data-age thresholds (days) per frequency — W/M/Q/A get proportional
# windows so monthly/quarterly series are not always flagged 'stale' by the
# daily threshold.
# RONDE-6 (P2-3): widened for release lag — the ts is the REFERENCE period,
# not the release date, so a monthly series ages ref_period + publication
# lag before its next print (the flat M:45 false-flagged 30/115 series
# that were sitting at the source frontier)
_STALE_DAYS = {"D": 5, "W": 14, "M": 95, "Q": 190, "A": 550}
# Per-family staleness overrides (audit round-2): freq is a poor staleness
# proxy for sources whose release cadence is irregular — FRB charge-off
# publishes Q+2..Q+5 months erratically (Q2-2026 still unpublished as of
# 2026-09-20 while Q1 is 262d old), which false-flagged all 12 FRB series
# STALE on their first day wired. Longest prefix wins (sorted below).
_STALE_OVERRIDE = {
    "FRB:": 300,  # charge-off/delinquency release lags
    "EODHD:CMDI": 45,  # weekly cadence; OBSERVED lag 2-4wk (2026-09-20: newest
    # 30d old, feed alive) — 35d had only ~5d headroom and
    # would have false-flagged all 3 series on 2026-09-26
    "GEO:GPRD": 14,  # daily file but refreshed Mondays only
    "GEO:HARPEX": 14,  # weekly stamps
}


def _brief_health_check(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    """Active series health → (n_ok, n_warn, n_fail, n_total)."""
    ok, warn, fail, total, _detail = _health_detail(conn)
    return ok, warn, fail, total


def _health_detail(conn: sqlite3.Connection) -> tuple[int, int, int, int, list[tuple]]:
    """Health check + per-series DETAIL for stale suffixes.

    Returns (n_ok, n_warn, n_fail, n_total, [(series_id, status, age, stale)])
    with status ∈ 'ok'|'stale'|'error'; feeds the ⚠(stale Nd) suffix on
    changed rows and the health.csv attachment when pct_bad > 25%.
    """
    # ROUND-10: anchor the fetch_log subqueries to the WIB day (the brief's
    # canonical 07:00 WIB slot = 00:00 UTC — a UTC-day anchor saw 0 of the
    # morning's 69 ERRORs, a false-clean Quality line)
    wib_day_start = (
        datetime.now(WIB)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
        .isoformat(timespec="seconds")
    )
    now_d = datetime.now(UTC).date()
    rows = conn.execute(
        "SELECT r.series_id, r.freq,"
        " (SELECT MAX(o.ts) FROM raw_observations o"
        "   WHERE o.series_id=r.series_id AND o.vintage_ts='realtime'),"
        " (SELECT f.status FROM fetch_log f WHERE f.target=r.series_id AND f.ts>=?"
        "   ORDER BY f.id DESC LIMIT 1),"
        " (SELECT COUNT(*) FROM fetch_log f WHERE f.target=r.series_id AND f.ts>=?"
        "   AND f.status='ERROR')"
        " FROM series_registry r WHERE r.active=1",
        (wib_day_start, wib_day_start),
    ).fetchall()
    n_ok = n_warn = n_fail = 0
    detail: list[tuple] = []
    for sid, freq, last_obs, last_status, n_err in rows:
        stale = True  # no data at all → stale
        age = None
        if last_obs:
            try:
                age = (now_d - datetime.fromisoformat(str(last_obs)[:10]).date()).days
                _limit = next(
                    (
                        v
                        for k, v in sorted(_STALE_OVERRIDE.items(), key=lambda kv: -len(kv[0]))
                        if sid.startswith(k)
                    ),
                    None,
                ) or _STALE_DAYS.get((freq or "D").upper(), 5)
                stale = age > _limit
            except ValueError:
                pass  # unparseable ts → conservatively stale
        if last_status == "ERROR" or stale:
            n_fail += 1
            detail.append((sid, "stale" if stale else "error", age, stale))
        elif n_err:
            n_warn += 1
        else:
            n_ok += 1
    return n_ok, n_warn, n_fail, len(rows), detail


def _stale_suffix(conn: sqlite3.Connection) -> dict[str, int]:
    """Map series_id → age-in-days for rows that APPEAR in the brief (⚠ stale suffix)."""
    _ok, _w, _f, _t, detail = _health_detail(conn)
    # 'error' status with fresh data is not staleness; the suffix text must
    # not contradict the date shown on the same row
    return {sid: age for sid, st, age, _s in detail if st == "stale" and age is not None}


def generate_brief(conn: sqlite3.Connection, db_path: str) -> str:
    """Generate the daily brief markdown; return the string."""
    now_wib = datetime.now(WIB)
    now_et = datetime.now(ET)
    pillars = compute_pillars(conn)
    score = compute_regime_score(pillars)
    quadrant = compute_quadrant(pillars)
    smile = compute_dollar_smile(conn)

    # audit trail: persist the 12 COT signals + regime (fail-safe — storage
    # must not kill the brief; but a failure MUST be visible, a silent pass
    # would stop the audit trail without a trace)
    try:
        store_cot_signals(conn, score)
    except Exception as ex:
        print(f"⚠ store_cot_signals failed: {str(ex)[:100]}")

    # SOMA audit trail — same fail-visible convention (buckets/TIPS/net-liq
    # history for `explore signal` + backtests)
    try:
        from .soma import store_soma_signals

        store_soma_signals(conn)
    except Exception as ex:
        print(f"⚠ store_soma_signals failed: {str(ex)[:100]}")

    # Options (CME per-strike OI, migration v8) audit trail — same
    # fail-visible convention (PCR/walls history for `explore signal`)
    try:
        from .options import store_options_signals

        store_options_signals(conn)
    except Exception as ex:
        print(f"⚠ store_options_signals failed: {str(ex)[:100]}")

    # Dealer positioning (NY Fed PD survey, migration v9) audit trail — same
    # fail-visible convention (weekly dealer-inventory z history)
    try:
        from .dealers import store_dealer_signals

        store_dealer_signals(conn)
    except Exception as ex:
        print(f"⚠ store_dealer_signals failed: {str(ex)[:100]}")

    # Fiscal (Treasury fiscaldata, migration v10) audit trail — same
    # fail-visible convention (auction-demand percentiles / net issuance /
    # interest burden history for `explore signal` + backtests)
    try:
        from .fiscal import store_fiscal_signals

        store_fiscal_signals(conn)
    except Exception as ex:
        print(f"⚠ store_fiscal_signals failed: {str(ex)[:100]}")

    # Expectations (Cleveland model, paket B) audit trail — irp_10y history
    try:
        from .expectations import store_expectations_signals

        store_expectations_signals(conn)
    except Exception as ex:
        print(f"⚠ store_expectations_signals failed: {str(ex)[:100]}")

    # Recession triangulation (paket C) audit trail
    try:
        from .recession import store_recession_signals

        store_recession_signals(conn)
    except Exception as ex:
        print(f"⚠ store_recession_signals failed: {str(ex)[:100]}")

    # day detection: Saturday → positioning special; Monday → weekend window
    is_saturday = now_wib.weekday() == 5  # Sunday is handled in run(): skip + return
    is_monday = now_wib.weekday() == 0

    regime_label = _regime_label(score, pillars)

    # header — quality from the health check (fetch_log ERRORs today + stale data)
    n_ok, n_warn, n_fail, n_total, detail = _health_detail(conn)
    pct_bad = (n_fail * 100 / n_total) if n_total else 0.0
    k_icon = "✓" if n_fail == 0 and n_warn == 0 else ("✗" if pct_bad > 40 else "⚠")
    # health.csv attachment when >25% of series are troubled — sent to the
    # Channel via sendDocument afterwards; the artifact stays available for
    # audit
    if pct_bad > 25 and detail:
        import csv
        from pathlib import Path

        health_path = Path(db_path).parent / "health.csv"
        with open(health_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["series_id", "status", "age_days"])
            w.writerows(detail)
    # 'data as of' stamp = the actual latest observation date (not an automatic
    # 'today' — that would claim freshness the harvest has not delivered)
    freshest = conn.execute(
        "SELECT MAX(substr(ts,1,10)) FROM raw_observations "
        "WHERE series_id IN ('FRED:DFF','FRED:VIXCLS','FRED:DFII10') "
        "AND vintage_ts='realtime'"
    ).fetchone()[0]
    data_per = (
        datetime.fromisoformat(freshest).strftime("%d-%b") if freshest else now_et.strftime("%d-%b")
    )
    lines = [
        f"=== US MACRO BRIEF — {now_wib.strftime('%a, %d %b %Y')} ===",
        f"WIB {now_wib.strftime('%H:%M')} · data as of {data_per} ET",
        f"REGIME : {regime_label} (score {score:+.1f})",
        f"QUADRANT: {quadrant}",
        f"Dollar  : {smile}",
        f"Quality: {k_icon} {n_ok}/{n_total} healthy · {n_warn} watch · {n_fail} issues",
    ]
    if pct_bad > 25:
        lines.append(f"⚠ {n_fail} series with issues — attachment data/health.csv")
    if pct_bad > 40:
        lines.append("✗ BRIEF DEGRADED — see health section")
    lines.append("")

    # SOMA granularity — decomposes the Fed BS (WALCL) row: buckets, TIPS,
    # roll-off, net liquidity. Degradeable: no soma_summary rows (pre-v7 DB
    # or a failed Thursday harvest) → the block simply disappears.
    soma_line = None
    try:
        from .soma import soma_brief_line

        soma_line = soma_brief_line(conn)
    except Exception as ex:
        # fail-visible like the store_* calls above: a crash must not look
        # identical to "no SOMA data yet"
        print(f"⚠ soma brief line failed: {str(ex)[:100]}")

    # overnight changes — the Saturday variant (positioning special) drops
    # this section; gated on is_saturday
    changes = overnight_changes(conn) if not is_saturday else []
    if changes:
        header = "Overnight changes (since last brief):" if is_monday else "Overnight changes:"
        lines.append(header)
        lines.extend(changes)
        if soma_line:
            lines.append(soma_line)
        lines.append("")
    elif soma_line:
        lines.append(soma_line)
        lines.append("")

    # surprise — ESI + latest high-z releases (Saturday skips rendering, but
    # ESI is still persisted for history)
    try:
        from ..qa.surprise import store_esi

        esi = store_esi(conn)
        if esi is not None and not is_saturday:
            tone = (
                "positive momentum data"
                if esi > 0.25
                else ("negative momentum data" if esi < -0.25 else "neutral")
            )
            lines.append(f"Surprise: ESI {esi:+.2f} ({tone})")
            # filter |z|>=1 in WHERE: a LIMIT 2 applied before filtering
            # would cut other large-z events
            big = conn.execute(
                "SELECT normalized_name, surprise_z FROM events "
                "WHERE surprise_z IS NOT NULL AND ABS(surprise_z) >= 1.0 "
                "AND ts_utc >= ? "
                "ORDER BY ABS(surprise_z) DESC LIMIT 2",
                ((datetime.now(UTC) - timedelta(days=4)).isoformat(timespec="seconds"),),
            ).fetchall()
            for nm, z in big:
                lines.append(f"  {nm[:40]:42} z={z:+.2f}")
            lines.append("")
    except Exception as ex:
        print(f"⚠ surprise section skipped: {str(ex)[:80]}")

    # pillars
    lines.append("Pillars:")
    for blk in "ABCDEF":
        p = pillars.get(blk, {})
        lines.append(
            f"  {p.get('label', blk):<12} = {p.get('state', '?'):<16} {p.get('detail', '')}"
        )
    lines.append("")

    # daily identity checks
    identities = _identity_checks(conn)
    if identities:
        # filtering on ✗ alone would hide ⚠ Sahm-trigger warnings exactly
        # when everything else looks green; include ⚠ rows too
        issues = [r for r in identities if "✗" in r[1] or "⚠" in r[1]]
        if issues:
            lines.append(f"Identity: ⚠ {len(issues)} violations")
            for name, status, detail in identities:
                lines.append(f"  {name}: {status} ({detail})")
        else:
            lines.append(f"Identity: ✓ {len(identities)}/{len(identities)} green")
        lines.append("")

    # positioning & flows — MM + Producer + Lev-vs-AM divergence
    latest_cot = conn.execute("SELECT MAX(report_date) FROM cot_raw").fetchone()[0]
    if latest_cot:
        COT_NAMES = {
            "088691": "Gold",
            "084691": "Silver",
            "085692": "Copper",
            "076651": "Platinum",
            "099741": "EuroFX",
            "097741": "Yen",
            "096742": "Pound",
            "232741": "AUD",
            "098662": "DXY-Fut",
            "133741": "BTC",
            "13874+": "SPX",
            "209742": "NQ-Emini",
            "146021": "ETH",
        }
        lines.append(f"Positioning (COT {latest_cot}):")

        # metals (Disaggregated): MM + Producer + weekly momentum + squeeze + retail
        for code in ("088691", "084691", "085692", "076651"):
            rows = conn.execute(
                "SELECT category, long, short, change_long, change_short, "
                "conc_top4_long, conc_top4_short, traders_long "
                "FROM cot_raw "
                "WHERE contract_code=? AND report_date=? AND report_type='disagg'",
                (code, latest_cot),
            ).fetchall()
            if not rows:
                continue
            name = COT_NAMES.get(code, code)
            # concentration is contract-level (identical across categories) —
            # read it from the mm row explicitly, not from loop residue
            # (leftover-iteration patterns are fragile)
            mm_row = next((r for r in rows if r[0] == "mm" and r[5] is not None), None)
            for cat, lng, sht, chg_l, chg_s, _c4l, _c4s, _trl in rows:
                if lng is None:
                    continue
                net = lng - (sht or 0)
                chg = (chg_l or 0) - (chg_s or 0)
                chg_str = f" Δ{chg:+,}/w" if abs(chg) > 500 else ""
                if cat == "mm":
                    z = _cot_zscore(conn, code)
                    z_str = f" z={z:+.1f}" if z is not None else ""
                    crowd = " ⚠CROWDED" if z is not None and abs(z) > COT_CROWDED_Z else ""
                    lines.append(f"  {name:<10} MM    net {net:+8,}{z_str}{crowd}{chg_str}")
                elif cat == "prod" and net < 0:
                    cover = " ⚡COVERING" if chg > 2000 else ""  # Producer closing shorts = bullish
                    lines.append(f"  {name:<10} Prod  net {net:+8,}  hedge{cover}{chg_str}")
                elif cat == "nonrep":
                    # Retail — a heuristic without strong paper support:
                    # labeled 'risk context', not a direction predictor
                    if net > 20000:
                        lines.append(
                            f"  {name:<10} Retl  net {net:+8,}  (retail long — risk context)"
                        )
                    elif net < -20000:
                        lines.append(
                            f"  {name:<10} Retl  net {net:+8,}  (retail short — risk context)"
                        )
            # squeeze risk from concentration (mm row — see note above)
            if mm_row:
                c4l, c4s = mm_row[5], mm_row[6]
                if c4l and c4s and (c4l > 40 or c4s > 40):
                    side = "LONG" if c4l > 40 else "SHORT"
                    lines.append(
                        f"  {name:<10} ⚡ SQUEEZE RISK: top-4 traders control "
                        f"{max(c4l, c4s):.0f}% of {side} OI"
                    )

        # financials (TFF): Lev vs AM divergence + dealer. ROUND-2 fix: all
        # 9 TFF contracts render — the old 4-contract loop left Yen/Pound/
        # AUD/DXY/ETH positioning invisible (live: AUD CROWDED_LONG 3 weeks
        # running never surfaced while the harvest paid for it).
        for code in (
            "099741",
            "097741",
            "096742",
            "232741",
            "098662",
            "133741",
            "146021",
            "13874+",
            "209742",
        ):
            rows = conn.execute(
                "SELECT category, long, short, change_long, change_short "
                "FROM cot_raw "
                "WHERE contract_code=? AND report_date=? AND report_type='tff'",
                (code, latest_cot),
            ).fetchall()
            if not rows:
                continue
            name = COT_NAMES.get(code, code)
            lev_net = am_net = None
            lev_chg = 0
            for cat, lng, sht, chg_l, chg_s in rows:
                if lng is None:
                    continue
                net = lng - (sht or 0)
                chg = (chg_l or 0) - (chg_s or 0)
                if cat == "lev":
                    lev_net = net
                    lev_chg = chg
                elif cat == "am":
                    am_net = net
            if lev_net is not None:
                z = _cot_zscore(conn, code)
                z_str = f" z={z:+.1f}" if z is not None else ""
                chg_s = f" Δ{lev_chg:+,}/w" if abs(lev_chg) > 500 else ""
                lines.append(f"  {name:<10} Lev   net {lev_net:+8,}{z_str}{chg_s}")
            if am_net is not None:
                lines.append(f"  {name:<10} AM    net {am_net:+8,}")
            if lev_net is not None and am_net is not None:
                if lev_net > 0 and am_net < 0:
                    lines.append(f"  {'':10} ⚡ DIVERGE: Lev long vs AM short")
                elif lev_net < 0 and am_net > 0:
                    lines.append(f"  {'':10} ⚡ DIVERGE: Lev short vs AM long")
        lines.append("")

    # Saturday variant: full 13 contracts + VOI
    if is_saturday:
        # ROUND-2: prefer the newest FINAL restatement (published T+1 with
        # corrected OI); Preliminary only when no Final exists at all —
        # otherwise the two report types double-render as separate days
        voi_td = conn.execute(
            "SELECT COALESCE((SELECT MAX(trade_date) FROM voi_daily WHERE report_type='Final'),"
            " (SELECT MAX(trade_date) FROM voi_daily))"
        ).fetchone()[0]
        voi_rows = conn.execute(
            "SELECT product_id, volume, oi, oi_diff FROM voi_daily "
            "WHERE trade_date = ? "
            "AND report_type = (SELECT report_type FROM voi_daily WHERE trade_date=?"
            " ORDER BY CASE WHEN report_type='Final' THEN 0 ELSE 1 END LIMIT 1)"
            " ORDER BY oi DESC LIMIT 5",
            (voi_td, voi_td),
        ).fetchall()
        if voi_rows:
            lines.append("VOI (volume/OI per product):")
            for v in voi_rows:
                lines.append(
                    f"  PID {v[0]:<8} vol={v[1] or 0:>10,.0f} "
                    f"OI={v[2] or 0:>10,.0f} ΔOI={v[3] or 0:+,.0f}"
                )
            lines.append("")

    flow_row = conn.execute(
        "SELECT date, funding_bps, stablecoin_usd, btc_etf_musd, eth_etf_musd, gld_tonnes "
        "FROM flows_daily ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if flow_row:
        # Bybit funding RETIRED 2026-09-16 (unreachable from all our networks;
        # served one line at 22% fill rate) — the stablecoin line below
        # (DefiLlama) is the surviving flows indicator. ROUND-4: the section
        # HEADER died with the funding line — keep an unconditional anchor so
        # the block stays greppable and a disappearing line is visible.
        lines.append("Flows:")
        # ETF flows (Farside — release dates can lag today → separate query)
        etf_row = conn.execute(
            "SELECT date, btc_etf_musd, eth_etf_musd FROM flows_daily "
            "WHERE btc_etf_musd IS NOT NULL OR eth_etf_musd IS NOT NULL "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if etf_row:
            etf_parts = []
            # per-issuer structural pair (GBTC vs IBIT / ETHE vs ETHA): the
            # aggregate net hides the bleed-vs-accumulation divergence
            for etf, agg, pair in (
                ("BTC", etf_row[1], ("GBTC", "IBIT")),
                ("ETH", etf_row[2], ("ETHE", "ETHA")),
            ):
                if agg is None:
                    continue
                txt = f"{etf} {agg:+.0f}M$"
                try:
                    issuer_row = conn.execute(
                        "SELECT issuer, flow_musd FROM etf_flows_issuer "
                        "WHERE etf=? AND date=? AND issuer IN (?,?)",
                        (etf, etf_row[0], *pair),
                    ).fetchall()
                except sqlite3.OperationalError:
                    issuer_row = []  # pre-v13 DB — per-issuer data not yet landed
                if len(issuer_row) == 2 and any(
                    (v or 0) for _i, v in issuer_row if abs(v or 0) >= 1
                ):
                    d = dict(issuer_row)
                    txt += f" ({pair[0]} {d[pair[0]] or 0:+.0f}·{pair[1]} {d[pair[1]] or 0:+.0f})"
                etf_parts.append(txt)
            if etf_parts:
                # freshness: US flows land T+0/T+1; >4 calendar days = frozen
                # source (D-021) — flag instead of serving the number cold
                age = (datetime.now(UTC).date() - datetime.fromisoformat(etf_row[0]).date()).days
                stale = " ⚠stale" if age > 4 else ""
                lines.append(f"  ETF flows ({etf_row[0][5:]}): {' · '.join(etf_parts)}{stale}")
        # ROUND-2: per-column latest non-NULL rows — the overall latest
        # flows_daily row is often partial (GLD/SLV legs land at different
        # hours), which silently vanished these lines; mirror the etf_row
        # pattern with an age marker.
        gld_row = conn.execute(
            "SELECT date, gld_tonnes FROM flows_daily WHERE gld_tonnes IS NOT NULL"
            " ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if gld_row:
            gld_approx = conn.execute(
                "SELECT value FROM flows_periodic WHERE kind='gld_shares_approx' "
                "AND period=(SELECT MAX(period) FROM flows_periodic "
                "            WHERE kind='gld_shares_approx')"
            ).fetchone()
            mark = " (approx)" if gld_approx and gld_approx[0] else ""
            gld_age = (datetime.now(UTC).date() - datetime.fromisoformat(gld_row[0]).date()).days
            gld_stale = " ⚠stale" if gld_age > 4 else ""
            lines.append(f"  GLD {gld_row[1]:,.0f}t{mark}{gld_stale}")
        sc_row = conn.execute(
            "SELECT date, stablecoin_usd FROM flows_daily WHERE stablecoin_usd > 0"
            " ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if sc_row:
            sc_age = (datetime.now(UTC).date() - datetime.fromisoformat(sc_row[0]).date()).days
            sc_stale = " ⚠stale" if sc_age > 4 else ""
            lines.append(f"  Stablecoin ${sc_row[1] / 1e9:.0f}B{sc_stale}")
        # periodic flows — latest period per kind only (without MAX(period)
        # the brief would print the entire monthly history)
        per = conn.execute(
            "SELECT kind, period, value FROM flows_periodic fp "
            "WHERE kind IN ('pboc_gold','pboc_gold_share','lbma_gold','lbma_silver',"
            "'tic_china','tic_japan','tic_belgium','tic_grand_total','wgc_gold') "
            "AND period=(SELECT MAX(period) FROM flows_periodic f2 "
            "            WHERE f2.kind=fp.kind)"
        ).fetchall()
        if per:
            d = {k: (p, v) for k, p, v in per}
            # physical-map line: official sector + London vault (gold map:
            # PBoC tonnage + share, LBMA vault gold & silver)
            phys = []
            if "pboc_gold" in d:
                phys.append(f"PBoC Au {d['pboc_gold'][1]:,.0f}t ({d['pboc_gold'][0][2:]})")
            if "pboc_gold_share" in d:
                phys.append(f"Au share {d['pboc_gold_share'][1]:.1f}%")
            if "lbma_gold" in d:
                phys.append(f"LBMA Au {d['lbma_gold'][1]:,.0f}t")
            if "lbma_silver" in d:
                phys.append(f"LBMA Ag {d['lbma_silver'][1]:,.0f}t")
            if phys:
                lines.append(f"  {' · '.join(phys)}")
            # foreign-holdings line: China + Belgium = the classic Euroclear
            # 'stealth China' composite; Grand Total = foreign absorption vs
            # deficit supply (term-premium pressure)
            tic = []
            if "tic_china" in d and "tic_belgium" in d:
                cn, be = d["tic_china"][1], d["tic_belgium"][1]
                if cn is not None and be is not None:
                    tic.append(f"TIC CN {cn:,.0f}B$+BE {be:,.0f}B$={cn + be:,.0f}B$")
                elif cn is not None:
                    tic.append(f"TIC CN {cn:,.0f}B$")
            elif "tic_china" in d:
                tic.append(f"TIC CN {d['tic_china'][1]:,.0f}B$")
            # ROUND-7: Japan — the other classic TIC headline; the kind was
            # written every month with zero readers (write-only inventory)
            if "tic_japan" in d and d["tic_japan"][1] is not None:
                tic.append(f"JP {d['tic_japan'][1]:,.0f}B$")
            if "tic_grand_total" in d:
                tic.append(f"foreign total {d['tic_grand_total'][1]:,.0f}B$")
            if "wgc_gold" in d:  # manual CLI entry (FR-28), shown when fresher
                tic.append(f"WGC Au {d['wgc_gold'][1]:,.0f}t")
            if tic:
                tic_p = d.get(
                    "tic_china", d.get("tic_belgium", d.get("tic_grand_total", ("", None)))
                )[0][2:]
                suffix = f" ({tic_p})" if tic_p else ""
                lines.append(f"  {' · '.join(tic)}{suffix}")
        # CNN Fear & Greed — degradable: the line disappears when the fetch failed
        fg_row = conn.execute(
            "SELECT period, value, meta_json FROM flows_periodic WHERE kind='cnn_fg'"
            " AND period=(SELECT MAX(period) FROM flows_periodic WHERE kind='cnn_fg')"
        ).fetchone()
        if fg_row and fg_row[1] is not None:
            sc = fg_row[1]
            # Use the canonical labels from fetchers/cnn.py (single source)
            from ..fetchers.cnn import score_to_label

            lbl = score_to_label(sc)
            # ronde-7 P2: age gate mirrors the ETF-flows line — a frozen
            # endpoint still writes its last score before the fetch_log gate
            # fires, so the renderer must flag staleness itself (D-021 class)
            fg_age = (
                (datetime.now(UTC).date() - datetime.fromisoformat(fg_row[0]).date()).days
                if fg_row[0]
                else 999
            )
            fg_stale = f" ⚠stale {fg_age}d" if fg_age > 4 else ""
            txt = f"  Fear&Greed: {sc:.0f} ({lbl}){fg_stale}"
            try:
                meta = json.loads(fg_row[2] or "{}")
            except ValueError:
                meta = {}
            if meta.get("prev_1m") is not None:
                txt += f" · 1m-ago {meta['prev_1m']:.0f}"
            lines.append(txt)
            # cross-asset split: the composite averages away divergence —
            # equity "extreme fear" while credit reads "greed" IS the signal
            comp = conn.execute(
                "SELECT kind, value FROM flows_periodic WHERE kind LIKE 'cnn_comp_%' AND period=?",
                (fg_row[0],),
            ).fetchall()
            if len(comp) >= 2:
                scores = sorted(
                    (v, _CNN_COMP_LBL.get(k[9:], k[9:])) for k, v in comp if v is not None
                )
                lo, hi = scores[0], scores[-1]
                if hi[0] - lo[0] >= 40:
                    lines.append(f"  F&G split: {lo[1]} {lo[0]:.0f} vs {hi[1]} {hi[0]:.0f}")
        # Crypto news sentiment (EODHD) — side-by-side with F&G per owner
        # decision 2026-09-13: DIFFERENT gauges (equity risk appetite vs
        # BTC/ETH news tone), never cross-calibrated; sparse (few points a
        # month) → the date is always shown, and >45d-old points vanish
        sent_parts = []
        for sid, lbl in (("EODHD:SENT_BTC", "BTC"), ("EODHD:SENT_ETH", "ETH")):
            srow = conn.execute(
                "SELECT ts, value FROM raw_observations WHERE series_id=?"
                " AND vintage_ts='realtime' ORDER BY ts DESC LIMIT 1",
                (sid,),
            ).fetchone()
            if srow and srow[1] is not None:
                age = (datetime.now(UTC).date() - datetime.fromisoformat(srow[0]).date()).days
                if age <= 45:
                    sent_parts.append(f"{lbl} {srow[1]:+.2f} ({srow[0][5:]})")
        if sent_parts:
            lines.append(f"  Crypto sentiment: {' · '.join(sent_parts)}")
        lines.append("")

    # FedWatch — ronde-7 P1: ORDER BY meeting_date alone let a tie on
    # meeting_date across snapshot dates resolve to the OLDEST row (scan
    # order) — the brief showed a 19-day-stale probability as current.
    # The date = MAX(date) subquery pins the newest snapshot.
    fw_rows = conn.execute(
        "SELECT meeting_date, prob_ease, prob_hold, prob_hike, implied_rate, date "
        "FROM fedwatch_snapshots WHERE source='diy' AND meeting_date >= ? "
        "AND date=(SELECT MAX(date) FROM fedwatch_snapshots WHERE source='diy')"
        " ORDER BY meeting_date LIMIT 1",
        (datetime.now(UTC).date().isoformat(),),
    ).fetchone()
    if fw_rows:
        lines.append(
            f"Policy: FedWatch {
                fmt_fedwatch(
                    [
                        type(
                            'P',
                            (),
                            {
                                'meeting_date': datetime.fromisoformat(fw_rows[0]).date(),
                                'prob_ease': fw_rows[1],
                                'prob_hold': fw_rows[2],
                                'prob_hike': fw_rows[3],
                                'implied_rate': fw_rows[4],
                            },
                        )
                    ],
                    asof=fw_rows[5],
                )
            }"
        )
    # ECBWatch (D-006 ESTRWatch) — the euro counterpart, next meeting only;
    # degradable identically to FedWatch (no rows → no line)
    try:
        from ..transforms.ecbwatch import ECBMeetingProb
        from ..transforms.ecbwatch import format_brief as fmt_ecb

        # ronde-7 P1: same tiebreak fix as FedWatch — pin the newest snapshot
        eb_rows = conn.execute(
            "SELECT meeting_date, prob_ease, prob_hold, prob_hike, implied_rate, raw_json,"
            " (SELECT MAX(date) FROM fedwatch_snapshots WHERE source='diy_ecb')"
            " FROM fedwatch_snapshots WHERE source='diy_ecb' AND meeting_date >= ?"
            " AND date=(SELECT MAX(date) FROM fedwatch_snapshots WHERE source='diy_ecb')"
            " ORDER BY meeting_date LIMIT 1",
            (datetime.now(UTC).date().isoformat(),),
        ).fetchone()
        if eb_rows:
            import contextlib

            diag = exact = None
            delta_bp = 0.0
            with contextlib.suppress(ValueError):
                meta = json.loads(eb_rows[5] or "{}")
                diag = meta.get("diag")
                # ROUND-2 fix: the >= first-match painted the DECIDED Sep-10
                # meeting's delta onto the displayed Oct-29 row (a passed
                # meeting satisfies meet >= impl too). Pair by EXACT
                # implementation date of the displayed meeting.
                from ..transforms.ecbwatch import implementation_date as _impl_date

                meet_iso = eb_rows[0]
                impl_sel = _impl_date(datetime.fromisoformat(meet_iso).date()).isoformat()
                rows_meta = meta.get("rows") or [{}]
                first = next(
                    (r for r in rows_meta if (r.get("impl") or "")[:10] == impl_sel),
                    rows_meta[0],
                )
                # exact/delta belong to the displayed meeting
                exact = first.get("exact")
                delta_bp = float(first.get("delta_bp") or 0.0)
            p = ECBMeetingProb(
                meeting_date=datetime.fromisoformat(eb_rows[0]).date(),
                impl_date=datetime.fromisoformat(eb_rows[0]).date(),
                prob_ease=eb_rows[1],
                prob_hold=eb_rows[2],
                prob_hike=eb_rows[3],
                implied_rate=eb_rows[4],
                expected_moves=0.0,
                delta_bp=delta_bp,
                exact=bool(exact),
                noise_amp=None,
            )
            lines.append(f"Policy: {fmt_ecb([p], diag, asof=eb_rows[6])}")
    except Exception as ex:
        print(f"⚠ ecbwatch brief line failed: {str(ex)[:100]}")
    lines.append("")

    # XCCY — computed straight from cme_settlements
    try:
        from ..transforms.xccy import compute_xccy

        xccy_rows = compute_xccy(conn)
        if xccy_rows:
            # ROUND-10 outage-sim: mark a stale spot/futures pairing (the
            # basis freezes unmarked during a multi-day data outage)
            _xv = xccy_rows[0]
            _stale = " ⚠stale" if getattr(_xv, "stale_days", 0) > 7 else ""
            lines.append(f"XCCY: {_xv.contract} {_xv.basis_bps:+.1f}bp{_stale}")
    except Exception:
        pass

    # ECB implied path — the FedWatch counterpart; degradable
    try:
        from ..transforms.ecb_path import compute_ecb_path
        from ..transforms.ecb_path import format_brief as fmt_ecb

        ecb_rows = compute_ecb_path(conn)
        if ecb_rows:
            lines.append(fmt_ecb(ecb_rows))
    except Exception:
        pass

    # CVOL — CME implied vol; consumed here so the daily harvest is not a
    # dead write. ROUND-2 fix: symbols publish on different trade dates —
    # pinning the GLOBAL MAX silently dropped symbols and rendered stale vol
    # unmarked. Per-symbol latest within a bounded 4d lookback + the asof.
    cvol_row = conn.execute(
        "SELECT symbol, cvol, trade_date FROM cvol_snapshots c "
        "WHERE symbol IN ('GCVL','SIVL','HGVL','POVL') "
        "AND trade_date=(SELECT MAX(trade_date) FROM cvol_snapshots c2"
        " WHERE c2.symbol=c.symbol AND c2.trade_date >= date('now','-4 day'))"
        " ORDER BY symbol"
    ).fetchall()
    if cvol_row:
        nm = {"GCVL": "Au", "SIVL": "Ag", "HGVL": "Cu", "POVL": "Pt"}
        parts = [f"{nm.get(s, s)} {v:.1f}" for s, v, _td in cvol_row]
        # Treasury YIELD vol (varian VY, satuan bp) — rates-turmoil read for
        # the metals book; one compact segment after the price-vols
        yrow = conn.execute(
            "SELECT symbol, cvol, trade_date FROM cvol_snapshots c "
            "WHERE symbol IN ('TUVY','FVVY','TYVY','USVY') "
            "AND trade_date=(SELECT MAX(trade_date) FROM cvol_snapshots c2"
            " WHERE c2.symbol=c.symbol AND c2.trade_date >= date('now','-4 day'))"
        ).fetchall()
        ynm = {"TUVY": "y2", "FVVY": "y5", "TYVY": "y10", "USVY": "y30"}
        # chronological order y2→y30 (tenor, not alphabetical symbol)
        yrow.sort(key=lambda r: list(ynm).index(r[0]) if r[0] in ynm else 99)
        if yrow:
            parts.append(" · ".join(f"{ynm.get(s, s)} {v:.0f}" for s, v, _td in yrow))
        asof = max((r[2] for r in cvol_row + yrow), default=None)
        tail = f" (td {asof[5:]})" if asof else ""
        lines.append(f"Vol (CVOL): {' · '.join(parts)}{tail}")

    # Inflation EXPECTATIONS — Cleveland model vs market (paket B): the Exp
    # line decomposes the breakeven into expectation + risk premium, and
    # reads the TIPS liquidity distortion. Degradeable; fail-visible.
    try:
        from .expectations import expectations_brief_line

        exp_line = expectations_brief_line(conn)
        if exp_line:
            lines.append(exp_line)
    except Exception as ex:
        print(f"⚠ expectations brief line failed: {str(ex)[:100]}")

    # Recession TRIANGULATION (paket C): curve model / SPF survey / Sahm —
    # three methodologically independent gauges on one line.
    try:
        from .recession import recession_brief_line

        rec_line = recession_brief_line(conn)
        if rec_line:
            lines.append(rec_line)
    except Exception as ex:
        print(f"⚠ recession brief line failed: {str(ex)[:100]}")

    # Vol TERM structure (CBOE revival 2026-09-08): VIX9D/spot ratio — where
    # stress sits on the curve (front-end backwardation vs steep contango).
    try:
        from .vixterm import store_vixterm_signals, vixterm_brief_line

        store_vixterm_signals(conn)
        vt_line = vixterm_brief_line(conn)
        if vt_line:
            lines.append(vt_line)
    except Exception as ex:
        print(f"⚠ vixterm brief line failed: {str(ex)[:100]}")

    # Options positioning — CME per-strike OI (migration v8): PCR + OI walls
    # + max-pain of each product's front contract. Degradeable like
    # CVOL/SOMA: no data → no line.
    try:
        from .options import options_brief_line

        opt_line = options_brief_line(conn)
        if opt_line:
            lines.append(opt_line)
    except Exception as ex:
        # fail-visible (soma pattern): a crash must not look like "no data"
        print(f"⚠ options brief line failed: {str(ex)[:100]}")

    # Dealer positioning — NY Fed PD survey (migration v9): cash-Treasury
    # inventory z per asset class, the third positioning angle after COT
    # (futures crowd) and Opt (per-strike). Degradeable like the Opt line:
    # no pd_positions data → no line.
    try:
        from .dealers import dealers_brief_line

        dlr_line = dealers_brief_line(conn)
        if dlr_line:
            lines.append(dlr_line)
    except Exception as ex:
        # fail-visible (options pattern): a crash must not look like "no data"
        print(f"⚠ dealers brief line failed: {str(ex)[:100]}")

    # Fiscal — Treasury supply/demand (migration v10): debt stock, interest
    # burden, auction demand percentile vs full history, net issuance. The
    # supply side of the dealer-capacity story (weak auctions + heavy issuance
    # = absorption stress). Degradeable like the Opt/DLR lines: no v10 data →
    # no line.
    try:
        from .fiscal import fiscal_brief_line

        fiscal_line = fiscal_brief_line(conn)
        if fiscal_line:
            lines.append(fiscal_line)
    except Exception as ex:
        # fail-visible (dealers pattern): a crash must not look like "no data"
        print(f"⚠ fiscal brief line failed: {str(ex)[:100]}")

    # Physical copper — LME stocks (squeeze-watch trigger). The DRAIN flag and
    # the copper_stocks_drain alert share ONE threshold (params_signals.yaml),
    # so the brief can never contradict the alert.
    from ..qa.watcher import COPPER_DRAIN_20D_PCT

    cu = conn.execute(
        "SELECT value, ts FROM raw_observations WHERE series_id='LME:CA_STOCKS' "
        "AND vintage_ts='realtime' ORDER BY ts DESC LIMIT 25"
    ).fetchall()
    # AUDIT P2: monthly publication channel — the Δ20d reads a window whose
    # newest point can be ~5 weeks old mid-month; show the age, and never
    # print a DRAIN flag off a frozen (>45d) feed
    cu_age = (
        (datetime.now(UTC).date() - datetime.fromisoformat(cu[0][1][:10]).date()).days
        if cu
        else 9999
    )
    if len(cu) >= 21 and cu_age <= 45:
        lvl = cu[0][0]
        d20 = lvl / cu[20][0] - 1  # cu is DESC → cu[20] = 20 business days ago
        age_txt = f", {cu_age}d old" if cu_age > 10 else ""
        flag = " ⚠DRAIN" if d20 <= COPPER_DRAIN_20D_PCT else ""
        # off-warrant shadow supply (daily OWSR, T+3): a DRAIN with thick
        # shadow supply is far less scary than a genuine physical scarcity
        ow = conn.execute(
            "SELECT period, value FROM flows_periodic WHERE kind='lme_owsr_cu'"
            " AND period=(SELECT MAX(period) FROM flows_periodic WHERE kind='lme_owsr_cu')"
        ).fetchone()
        ow_txt = ""
        if (
            ow
            and ow[1]
            and (datetime.now(UTC).date() - datetime.fromisoformat(ow[0]).date()).days <= 7
        ):
            share = ow[1] / lvl * 100 if lvl else None
            pct_txt = f" ({share:.0f}% of LME)" if share is not None else ""
            ow_txt = f" · off-warrant {ow[1]:,.0f}t{pct_txt}"
        lines.append(f"Cu physical: LME {lvl:,.0f}t (Δ20d {d20:+.0%}{age_txt}){flag}{ow_txt}")
    elif len(cu) >= 21:
        # frozen channel: show the level + age with a stale suffix rather
        # than vanishing (review ronde-2 — a silent gap reads as 'no data',
        # which is a different claim than 'stale data')
        lines.append(f"Cu physical: LME {cu[0][0]:,.0f}t ⚠stale {cu_age}d (monthly channel)")

    # events (7 days — dedup by normalized name + date)
    events = conn.execute(
        "SELECT ts_utc, name, importance, consensus FROM events "
        "WHERE importance='high' AND ts_utc >= ? AND ts_utc <= ? "
        "ORDER BY ts_utc",
        (datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(days=7)).isoformat()),
    ).fetchall()
    if events:
        import re as _re

        seen = set()
        lines.append("Event radar (7 days):")
        # equivalent names across sources (FMP/TV/CME/EODHD name the same
        # event differently)
        NAME_MAP = {
            "NONFARM PAYROLLS PRIVATE": "NON FARM PAYROLLS",
            "NONFARM PAYROLLS": "NON FARM PAYROLLS",
            "EMPLOYMENT SITUATION": "NON FARM PAYROLLS",
            "INITIAL JOBLESS CLAIMS": "JOBLESS CLAIMS",
            "NON-MANUFACTURING PMI": "SERVICES PMI",
            "NON-MANUFACTURING PRICES": "SERVICES PRICES",
            "ISM SERVICES": "SERVICES",
            "MANUFACTURING PMI": "MANUFACTURING",
            "PETROLEUM STATUS REPORT": "EIA PETROLEUM",
            "CRUDE OIL INVENTORIES": "EIA PETROLEUM",
        }
        for ev in events:
            name = _re.sub(r"^US:\s*", "", ev[1])
            name = _re.sub(r"\s*\([^)]*\)\s*", "", name)
            base = name.strip().upper()
            # apply name map
            for old, new in NAME_MAP.items():
                if old in base:
                    base = base.replace(old, new)
            # strip generic suffixes
            base = _re.sub(r"\s+(INDEX|PMI|RATE|CHANGE)$", "", base)
            # skip derived metrics
            if "4-WEEK" in base or "AVERAGE" in base or "CONTINUING" in base:
                continue
            key = (base, ev[0][:10])
            if key in seen:
                continue
            seen.add(key)
            display = _re.sub(r"^US:\s*", "", ev[1])[:44]
            cons = f"  cons={ev[3]}" if ev[3] is not None else ""
            # times render in WIB (UTC stays the storage basis); zoneinfo
            # handles EDT/EST automatically
            try:
                t_utc = datetime.fromisoformat(ev[0])
                t_wib = t_utc.astimezone(WIB)
                stamp = t_wib.strftime("%d-%b %H:%M") + " WIB"
            except ValueError:
                stamp = ev[0][:16].replace("T", " ")
            lines.append(f"  {stamp}  {display}{cons}")
        lines.append("")

    # book implications (from regime + pillar states)
    lines.append("Book implications:")
    ry = pillars.get("B", {}).get("state", "")
    dol_state = smile
    stress = pillars.get("F", {}).get("state", "")
    liq = pillars.get("E", {}).get("state", "")

    # Cross-Contract Aggregate — macro positioning overlay
    agg = _cross_contract_aggregate(conn)
    if agg:
        lines.append(
            f"  Positioning macro: {agg['signal']} "
            f"(Σ net {agg['total_net']:+,} across {agg['n_contracts']} contracts)"
        )

    # Price × OI Quadrant
    for sym, src, label in [("XAUUSD", "EODHD", "Gold"), ("US500", "YAHOO", "US500")]:
        quad = _price_oi_quadrant(conn, sym, src)
        if quad:
            lines.append(f"  {label:<8} structure: {quad}")

    # Hedging Pressure (slow bias)
    for code, name in [("088691", "Gold"), ("085692", "Copper")]:
        hp = _hedging_pressure(conn, code)
        if hp:
            lines.append(f"  {name:<8} hedge pressure: {hp['hp']:+.3f} ({hp['direction']})")

    # FX Turning Point
    fx_tp = _fx_turning_point(conn, "099741")
    if fx_tp:
        lines.append(f"  EuroFX   positioning: {fx_tp}")

    # Price-vs-Positioning Divergence (Gold + US500)
    for sym, src, code, name in [
        ("XAUUSD", "EODHD", "088691", "Gold"),
        ("US500", "YAHOO", "13874+", "US500"),
    ]:
        div = _price_positioning_divergence(conn, sym, src, code)
        if div:
            lines.append(f"  {name:<8} {div}")

    # Regime-Conditioned COT (Gold — thresholds calibrated per regime score)
    gold_rc = _regime_conditioned_cot(conn, "088691", score)
    if gold_rc and gold_rc["signal"] != "normal":
        lines.append(
            f"  Gold     regime-adj: z={gold_rc['z']:+.1f} → {gold_rc['signal']} "
            f"(threshold {gold_rc['threshold']} at score {gold_rc['regime']:+.1f})"
        )

    # Spread-share conviction (Gold — spread dominance = downweight)
    gold_ss = _spread_share_filter(conn, "088691")
    if gold_ss is not None and gold_ss > 0.25:
        lines.append(f"  Gold     ⚠ spread share {gold_ss:.0%} — positioning quality reduced")

    # Silver 52wk gate
    if _silver_52wk_gate(conn):
        lines.append("  ⚡ SILVER 52wk-high + Commercial covering = bias bullish XAGUSD")

    # Synthetic crosses (XAGGBP = XAGUSD / GBPUSD — NOTE: division, not ×)
    try:
        from ..transforms.synthetic import compute_all_synthetic

        synth = compute_all_synthetic(conn)
        if synth.get("XAGGBP"):
            lines.append(f"  XAGGBP  : {synth['XAGGBP']['value']:.2f} (synthetic)")
        if synth.get("XAUGBP"):
            lines.append(f"  XAUGBP  : {synth['XAUGBP']['value']:.0f} (synthetic)")
    except Exception:
        pass

    # XAUUSD
    gold_biases = []
    if ry == "FALLING":
        gold_biases.append("RY↓ tailwind")
    elif ry == "RISING":
        gold_biases.append("RY↑ headwind")
    if dol_state.startswith("WEAK"):
        gold_biases.append("USD↓ tailwind")
    elif dol_state.startswith("STRONG"):
        gold_biases.append("USD↑ headwind")
    # COT crowded check — the category/report_type filters are required:
    # a bare LIMIT 1 lands on an arbitrary category row (mm/prod/nonrep/…)
    # and the flag becomes nondeterministic
    gold_cot = conn.execute(
        "SELECT long, short FROM cot_raw WHERE contract_code='088691' "
        "AND category='mm' AND report_type NOT LIKE '%_c' "
        "AND long IS NOT NULL AND long > 0 "
        "ORDER BY report_date DESC LIMIT 1"
    ).fetchone()
    if gold_cot and gold_cot[0] and gold_cot[1]:
        gold_net = gold_cot[0] - gold_cot[1]
        if gold_net > 100000:
            gold_biases.append("⚠ CROWDED LONG")
    lines.append(f"  XAUUSD  : {' · '.join(gold_biases) if gold_biases else 'neutral'}")

    # BTC — smart-money follower (Baur & Smales 2022)
    btc_biases = []
    if liq == "EXPANDING":
        btc_biases.append("liq↑ supportive")
    elif liq == "CONTRACTING":
        btc_biases.append("liq↓ headwind")
    if stress == "CALM":
        btc_biases.append("low stress")
    btc_sm = _btc_smart_money(conn)
    if btc_sm:
        if btc_sm["direction"].startswith("COVERING"):
            btc_biases.append(f"💡 Lev covering z={btc_sm['z_dshort']}")
        elif btc_sm["direction"].startswith("ADDING"):
            btc_biases.append(f"⚠ Lev adding short z={btc_sm['z_dshort']}")
    lines.append(f"  BTC     : {' · '.join(btc_biases) if btc_biases else 'neutral'}")

    # Index
    idx_bias = (
        "risk-on"
        if score > REGIME_RISK_ON
        else ("risk-off" if score < REGIME_RISK_OFF else "neutral")
    )
    vix_row = _latest(conn, "FRED:VIXCLS")
    if vix_row and vix_row[1] and vix_row[1] > 20:
        idx_bias += " ⚠ high vol → size down"
    lines.append(f"  US500   : {idx_bias}")

    # FX
    if dol_state.startswith("WEAK"):
        lines.append("  EURUSD  : ↗ dollar weak")
    elif dol_state.startswith("STRONG"):
        lines.append("  EURUSD  : ↘ dollar strong")
    else:
        lines.append("  EURUSD  : · neutral")

    # XCUUSD (China proxy)
    lines.append(f"  XCUUSD  : · growth={pillars.get('D', {}).get('state', '?').lower()}")
    lines.append("")

    # Saturday variant drops the event radar and book implications; cut
    # post-hoc so the large blocks need no re-indentation.
    if is_saturday:

        def _idx_of(prefix):
            return next((i for i, ln in enumerate(lines) if ln.startswith(prefix)), None)

        # 'Sources:' is appended AFTER this block, so a guard looking for it
        # here never matches; cut explicitly to the end of the list
        i_impl = _idx_of("Book implications:")
        i_evr = _idx_of("Event radar")
        cuts = []
        if i_impl is not None:
            cuts.append((i_impl, len(lines)))
        if i_evr is not None:
            end_ev = next(
                (
                    k
                    for k in range(i_evr + 1, len(lines))
                    if lines[k].startswith(("Book implications:", "Sources:"))
                ),
                len(lines),
            )
            cuts.append((i_evr, end_ev))
        for a, b in sorted(cuts, reverse=True):
            del lines[a:b]

    # Today-window fetch ratio, not lifetime: idempotent re-runs append EMPTY
    # rows that monotonically drag a lifetime ratio down, and a harvester
    # reporting 0 new rows is healthy, not failing.
    # ROUND-4: the window anchor is the WIB DAY (the brief's canonical slot
    # is 07:00 WIB = 00:00 UTC — a UTC-day bound reads ~'1/1' fake-perfect
    # because only send/verify have run since UTC midnight).
    wib_day_start = (
        datetime.now(WIB)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
        .isoformat(timespec="seconds")
    )
    today_s = wib_day_start
    total_ok = conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE status='OK' AND ts>=?", (today_s,)
    ).fetchone()[0]
    total_all = conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE status IN ('OK','ERROR') AND ts>=?",
        (today_s,),
    ).fetchone()[0]
    lines.append(f"Sources: {total_ok}/{total_all} fetch OK (today)")
    lines.append("")

    return "\n".join(lines)


def save_brief(db_path: str, markdown: str, score: float) -> str:
    """Write the brief to brief_log + outbox → return the date key.

    The date key is WIB, not UTC: a Saturday 04:15 WIB brief is Friday 21:15
    UTC, so a UTC key would overwrite Friday's brief and the Saturday outbox
    row would never be created (INSERT OR IGNORE would hit Friday's row)."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    today = datetime.now(WIB).date().isoformat()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR REPLACE INTO brief_log(date, markdown, regime_score, generated_at)"
        " VALUES (?,?,?,?)",
        (today, markdown, score, now),
    )
    # one row per known channel; an unconfigured one ends 'skipped' at send time
    conn.executemany(
        "INSERT OR IGNORE INTO brief_deliveries(brief_date, channel, status, created_at)"
        " VALUES (?,?, 'pending', ?)",
        [(today, ch, now) for ch in ("telegram", "ntfy")],
    )
    conn.execute("COMMIT")
    conn.close()
    return today


def run(db_path: str) -> str:
    """Full pipeline: compute → generate → save → return markdown.

    No brief on Sunday; the special Saturday positioning brief is published
    04:15 (after COT) and must not be overwritten by the daily 07:00 job.
    """
    now_wib = datetime.now(WIB)
    conn = sqlite3.connect(db_path)
    today_row = conn.execute(
        "SELECT markdown FROM brief_log WHERE date=?", (now_wib.date().isoformat(),)
    ).fetchone()
    if now_wib.weekday() == 6:  # Sunday — return the latest brief unchanged
        prev = conn.execute("SELECT markdown FROM brief_log ORDER BY date DESC LIMIT 1").fetchone()
        conn.close()
        if prev:
            print("Sunday — brief not generated")
            return prev[0]
    elif now_wib.weekday() == 5 and today_row:  # Saturday — keep the 04:15 brief
        conn.close()
        print("Saturday — positioning brief already published (04:15), not overwritten")
        return today_row[0]
    markdown = generate_brief(conn, db_path)
    pillars = compute_pillars(conn)
    score = compute_regime_score(pillars)
    conn.close()
    save_brief(db_path, markdown, score)
    return markdown


# English name; the Indonesian alias is kept for compatibility with old callers
_berubah_semalam = overnight_changes
