"""xccy.py — EUR/USD cross-currency basis computed from CME settlements.

CIP formula (verified to ±0.00bp against the official CME tool; this
pipeline uses the exact third-Monday IMM date plus EOD spot, so daily
reproduction can deviate a few bp from the tool — the brief number is an
estimate, not a claim of identity):
  F_cip = S · (1 + r_US·τ) / (1 + r_EU·τ)
  basis_bps = ((F_actual − F_cip) / F_cip) / τ · 10^4
  τ = ACT/360 per IMM period; rates from the SR3 + ESR strips; F from 6E futures.

All inputs come from cme_settlements + instrument_prices.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from .ecbwatch import _MONTHS, _third_wednesday

# Product IDs
SR3_PID, ESR_PID, EUR_PID = 8462, 10247, 58


@dataclass
class XccyRow:
    contract: str  # e.g. "SEP 26"
    imm_date: str  # e.g. "2026-09-21" (approx)
    tau: float  # ACT/360
    r_us: float  # SR3 implied rate
    r_eu: float  # ESR implied rate
    f_actual: float  # 6E futures price
    f_cip: float  # theoretical CIP forward
    basis_bps: float  # ((F-F_cip)/F_cip)/tau * 10^4
    spot: float  # EUR/USD spot


def _get_settlement(
    conn: sqlite3.Connection, product_id: int, trade_date: str | None = None
) -> dict[str, float]:
    """Settlement strip for one specific trade_date.

    Taking MAX(trade_date) per product independently can mix legs from
    DIFFERENT trade dates when one leg fails to harvest on a given day,
    making the basis jump tens of bp. The shared trade date is computed by
    the caller via INTERSECT.
    """
    if trade_date is None:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM cme_settlements WHERE product_id=?", (product_id,)
        ).fetchone()
        trade_date = row[0] if row else None
    if not trade_date:
        return {}
    rows = conn.execute(
        "SELECT month, settle FROM cme_settlements "
        "WHERE product_id=? AND trade_date=? ORDER BY month",
        (product_id, trade_date),
    ).fetchall()
    return {m: s for m, s in rows if s is not None}


def _common_trade_date(conn: sqlite3.Connection, pids: list[int]) -> str | None:
    """Trade date that has data for ALL legs at once (INTERSECT)."""
    sets = []
    for pid in pids:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM cme_settlements WHERE product_id=? "
            "AND settle IS NOT NULL ORDER BY trade_date DESC LIMIT 5",
            (pid,),
        ).fetchall()
        sets.append({r[0] for r in rows})
    common = set.intersection(*sets) if sets else set()
    return max(common) if common else None


def _get_spot(conn: sqlite3.Connection, td: str | None = None) -> float | None:
    """EURUSD close ON OR BEFORE the settlements trade_date.

    Round-2 P0 (2026-09-17): ORDER BY ts DESC mixed a FORMING intraday bar
    (swept 06:16 WIB) with T-1 futures settlements — the brief printed
    +278.1bp on a day the same-day basis was ~5bp. Pinning the spot to the
    settlements' trade_date keeps both legs on the same calendar day; a
    forming bar is never eligible."""
    if td:
        row = conn.execute(
            "SELECT close FROM instrument_prices WHERE symbol='EURUSD'"
            " AND source IN ('EODHD','YAHOO') AND ts <= ?"
            " AND julianday(?) - julianday(ts) <= 3"
            " ORDER BY ts DESC, CASE source WHEN 'EODHD' THEN 0 ELSE 1 END LIMIT 1",
            (td, td),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT close FROM instrument_prices WHERE symbol='EURUSD'"
            " AND source IN ('EODHD','YAHOO') AND ts >= date('now','-4 day')"
            " ORDER BY ts DESC, CASE source WHEN 'EODHD' THEN 0 ELSE 1 END LIMIT 1"
        ).fetchone()
    # A silent 1.0 fallback would produce a garbage basis that still gets
    # printed; return None and let the caller skip instead.
    return row[0] if row else None


def _imm_approx(contract_month: str) -> date | None:
    """Parse 'SEP 26' -> IMM date = the third WEDNESDAY of that month (15th-21st).

    The 6E future delivers on the third Wednesday and the SR3/ESR reference quarters start
    there (CME Rulebook Ch.480, the same date ecbwatch uses): it is the forward's value date
    for the CIP leg. The third Monday used before is the 6E last trading day, 2 days early
    (2026-10-12 audit). The year must be 2 digits ('SEP 2026' is rejected, not silently
    parsed as the year 4026).
    """
    parts = contract_month.strip().upper().split()
    if len(parts) != 2:
        return None
    mon_str, yr_str = parts
    if len(yr_str) != 2 or not yr_str.isdigit() or mon_str not in _MONTHS:
        return None
    return _third_wednesday(2000 + int(yr_str), _MONTHS[mon_str])


def compute_xccy(conn: sqlite3.Connection) -> list[XccyRow]:
    """Compute the basis for contracts that have SR3+ESR+6E data (not a
    hardcoded quarterly list).

    Contracts within 30 days of expiry are skipped: the basis is annualized
    by dividing by τ, so as τ→0 a few pip of settlement noise explodes into
    hundreds of bp (a 14-day SEP-26 once printed −414bp from a 16-pip gap).
    All three legs must share one trade date (INTERSECT).
    """
    td = _common_trade_date(conn, [SR3_PID, ESR_PID, EUR_PID])
    if not td:
        return []
    sr3 = _get_settlement(conn, SR3_PID, td)
    esr = _get_settlement(conn, ESR_PID, td)
    eur = _get_settlement(conn, EUR_PID, td)
    spot = _get_spot(conn, td)
    if not spot:
        return []

    common = set(sr3.keys()) & set(esr.keys()) & set(eur.keys())
    # Quarterly contracts (MAR/JUN/SEP/DEC) only — monthly SR3/ESR contracts
    # are far thinner, so their settlements are unreliable (OCT/NOV printed
    # −173/−192bp vs DEC −56bp = noise, not basis).
    common = {m for m in common if m.split()[0].upper() in ("MAR", "JUN", "SEP", "DEC")}
    if not common:
        return []

    from datetime import UTC as _U
    from datetime import datetime as _dt

    today = _dt.now(_U).date()
    out = []
    # Sort by IMM date, not alphabetically: alphabetical order puts
    # "DEC 27" < "JUN 27" < "MAR 27", so once DEC 26 expires the FARTHEST
    # contract would become the brief headline.
    for month in sorted(common, key=lambda m: _imm_approx(m) or date.max):
        imm = _imm_approx(month)
        if imm is None or imm < today:
            continue
        days = (imm - today).days
        if days < 30:  # 1/τ amplification zone — see docstring
            continue
        tau = days / 360.0

        r_us = 100.0 - sr3[month]
        r_eu = 100.0 - esr[month]
        f_actual = eur[month]

        f_cip = spot * (1 + (r_us / 100.0) * tau) / (1 + (r_eu / 100.0) * tau)
        if f_cip == 0:
            continue
        basis = ((f_actual - f_cip) / f_cip) / tau * 10_000 if tau > 0 else 0.0

        out.append(
            XccyRow(
                contract=month,
                imm_date=imm.isoformat(),
                tau=round(tau, 6),
                r_us=round(r_us, 4),
                r_eu=round(r_eu, 4),
                f_actual=f_actual,
                f_cip=round(f_cip, 6),
                basis_bps=round(basis, 2),
                spot=spot,
            )
        )
    return out


def format_brief(rows: list[XccyRow]) -> str:
    """Format the brief line for the nearest contract."""
    if not rows:
        return "xccy: no data"
    r = rows[0]  # nearest contract
    return (
        f"xccy {r.contract}: {r.basis_bps:+.1f}bp "
        f"(F={r.f_actual:.4f} vs CIP={r.f_cip:.4f}, τ={r.tau:.3f})"
    )
