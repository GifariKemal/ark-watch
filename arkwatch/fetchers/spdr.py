"""spdr.py — daily GLD holdings plus SLV via FMP shares-float."""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime

import requests
from curl_cffi import requests as creq

from ..config import PlanLimited

GLD_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive"
FMP_URL = "https://financialmodelingprep.com/stable"


def _fetch_shares_fmp(symbol: str) -> int | None:
    """Shares outstanding via the FMP shares-float endpoint; None on failure."""
    key = os.environ.get("FMP_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.get(
            f"{FMP_URL}/shares-float", params={"symbol": symbol, "apikey": key}, timeout=(10, 30)
        )
        j = r.json()
        if isinstance(j, list) and j and j[0].get("outstandingShares"):
            return int(j[0]["outstandingShares"])
    except Exception:
        pass
    return None


def fetch_gld_archive() -> list[dict]:
    """Full GLD archive rows: [{ts, oz_per_share, total_oz, tonnes}] ascending.

    ROUND-2 DISCOVERY (2026-09-17): the archive carries the OFFICIAL
    'Tonnes of Gold' column (col 10) per trading date since 2004 — live
    through yesterday (5,489/5,694 rows filled; the 205 unfilled are the
    2004-era head). This replaces the shares×oz/share approximation for
    BOTH the current print and historical backfill (holes 09-09/14/15 and
    the 08-31 fallback-phantom heal with real values)."""
    from datetime import datetime as _dt

    from openpyxl import load_workbook

    s = creq.Session(impersonate="chrome")
    r = s.get(
        GLD_URL, params={"product": "gld", "exchange": "NYSE", "lang": "en"}, timeout=(10, 60)
    )
    if r.status_code != 200 or r.content[:2] != b"PK":
        raise RuntimeError(f"GLD: HTTP {r.status_code} / not XLSX")
    wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    sheet_name = [sn for sn in wb.sheetnames if "Historical" in sn]
    if not sheet_name:
        raise RuntimeError(f"GLD: Historical sheet not found in {wb.sheetnames}")
    ws = wb[sheet_name[0]]
    out: list[dict] = []
    for row in list(ws.iter_rows(values_only=True))[1:]:
        if not row or not row[0]:
            continue
        d = str(row[0])
        try:
            ts = _dt.strptime(d, "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        oz = row[2] if len(row) > 2 and isinstance(row[2], (int, float)) else None
        total_oz = row[8] if len(row) > 8 and isinstance(row[8], (int, float)) else None
        tonnes = row[9] if len(row) > 9 and isinstance(row[9], (int, float)) else None
        if oz is None and tonnes is None:
            continue
        out.append({"ts": ts, "oz_per_share": oz, "total_oz": total_oz, "tonnes": tonnes})
    if not out:
        raise RuntimeError("GLD: no archive rows parsed")
    return out


def fetch_gld_tonnes(arch: list[dict] | None = None) -> dict:
    """Latest GLD holdings — the OFFICIAL archive tonnes when the column is
    filled (the norm); the shares×oz/share approximation only as a
    degradable fallback when the tonnes cell is empty (approx=True —
    renderers must mark it and f2 must NOT write it as a real value).
    arch: pass an already-fetched fetch_gld_archive() to avoid a second
    full-download of the ~5,700-row XLSX (ROUND-3: f2 was downloading 2x)."""
    from ..units import oz_to_tonnes

    if arch is None:
        arch = fetch_gld_archive()
    rows = [r for r in arch if r["tonnes"] is not None]
    last = rows[-1] if rows else arch[-1]
    if last["tonnes"] is not None:
        return {
            "ts": last["ts"],
            "oz_per_share": last["oz_per_share"],
            "tonnes": round(last["tonnes"], 1),
            "approx": False,
        }
    # fallback path (tonnes cell empty on the newest row)
    shares = _fetch_shares_fmp("GLD")
    approx = shares is None
    if approx:
        shares = 291_000_000
    tonnes = oz_to_tonnes((last["oz_per_share"] or 0.0) * shares)
    return {
        "ts": last["ts"],
        "oz_per_share": last["oz_per_share"],
        "tonnes": round(tonnes, 1),
        "shares_assumed_m": shares / 1e6,
        "approx": approx,
    }


def fetch_slv_shares() -> dict:
    """SLV outstanding shares via FMP (weekly cadence is enough — sourced from SEC 10-Q filings)."""
    key = os.environ.get("FMP_API_KEY", "")
    r = requests.get(
        f"{FMP_URL}/shares-float", params={"symbol": "SLV", "apikey": key}, timeout=(10, 30)
    )
    if r.status_code == 402:  # endpoint outside the FMP plan: SKIPPED, not ERROR
        raise PlanLimited("plan-limited: FMP shares-float")
    if r.status_code != 200:
        raise RuntimeError(f"SLV: HTTP {r.status_code}")
    j = r.json()
    if isinstance(j, list) and j:
        shares = j[0].get("outstandingShares")
        if shares:
            return {"ts": datetime.now(UTC).date().isoformat(), "shares": int(shares)}
    raise RuntimeError("SLV: outstandingShares missing")
