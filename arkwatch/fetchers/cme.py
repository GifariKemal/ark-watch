"""cme.py — gray-zone CME harvesters: settlements, CVOL, and VOI.

cmegroup.com rejects plain HTTP clients and cloud IPs, so every request must
go through curl_cffi with chrome impersonation from a residential IP.
Settlements are retained server-side for only ~5 trading days, so harvest
gaps longer than ~3 trading days mean permanently lost data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # lazy at runtime: watcher imports this every 60 s, only sessions need it
    from curl_cffi import requests as creq

BASE = "https://www.cmegroup.com"

# Product IDs (verified via the ProductSlate endpoint).
PRODUCTS = {
    "ZQ": 305,
    "SR1": 8463,
    "SR3": 8462,
    "ESR": 10247,
    "GC": 437,
    "SI": 458,
    "PL": 446,
    "HG": 438,
    "ES": 133,
    "NQ": 146,
    "YM": 318,
    "6E": 58,
    "6B": 42,
    "BTC": 8478,
}

# CVOL symbol suite. The endpoint serves 40 symbols (2026-09-03); we take the
# ones covering the owner's instruments. The *VY variants are Treasury YIELD
# volatility (2y/5y/10y/30y) — a purer rates-turmoil read for the gold book
# than the price-vol TYVL/USVL. No BTC CVOL exists (BVL/BTCVL return empty).
CVOL_SYMBOLS = (
    "GCVL",
    "SIVL",
    "HGVL",
    "POVL",
    "MVL",
    "EUVL",
    "GBVL",
    "JPVL",
    "ADVL",
    "CAVL",
    "FXVL",
    "CLVL",
    "NGVL",
    "TYVL",
    "USVL",
    "SRVL",
    "TUVY",
    "FVVY",
    "TYVY",
    "USVY",
)

# VOI asset-class IDs.
VOI_ASSET_CLASSES = {
    2: "Agriculture",
    3: "FX",
    4: "Equity",
    6: "Interest Rate",
    7: "Energy",
    8: "Metals",
}

# Options product ids — SEPARATE from the futures ids in PRODUCTS: the options
# endpoints return 0 rows for a futures id. [RISET: /services/product-slate +
# live-verified 2026-09-03; BTC options are European-style — the only product
# whose discovery payload has no AME group]
OPTIONS_PRODUCTS = {
    "OG": 192,  # gold options (underlying GC)
    "SO": 193,  # silver options (underlying SI)
    "HXE": 797,  # copper options (underlying HG)
    "PO": 2910,  # platinum options (underlying PL)
    "ES": 138,
    "NQ": 148,
    "BTC": 8875,
}

# Options product → the FUTURES code carrying the underlying settle (IV anchor).
# [RISET: OGV26 strip settle vs cme_settlements GC strip, 2026-09-03]
OPTIONS_UNDERLYING = {
    "OG": "GC",
    "SO": "SI",
    "HXE": "HG",
    "PO": "PL",
    "ES": "ES",
    "NQ": "NQ",
    "BTC": "BTC",
}


class CmeError(RuntimeError):
    pass


_SESSION: creq.Session | None = None


def _session() -> creq.Session:
    """ONE shared chrome-impersonation session per process (ROUND-7).

    A fresh session per call = distinct TLS fingerprints in rapid bursts —
    exactly the churn pattern that gets gray-zone scrapers detected. The
    options fetchers already thread a shared session; now settlements/
    CVOL/VOI ride the same one (the LME fetcher documented this pattern
    first)."""
    global _SESSION
    if _SESSION is None:
        from curl_cffi import requests as creq

        _SESSION = creq.Session(impersonate="chrome")
    return _SESSION


def _biz_date_str(d: datetime) -> str:
    """Format MM/DD/YYYY for the tradeDate parameter."""
    return d.strftime("%m/%d/%Y")


def fetch_settlements(product_code: str, trade_date: datetime | None = None) -> list[dict]:
    """Fetch the settlement strip for one product. Walks back at most 5 days to find a trading day."""
    pid = PRODUCTS.get(product_code)
    if pid is None:
        raise CmeError(f"unknown product: {product_code}")
    s = _session()
    # start from yesterday and walk back (weekend/holiday aware)
    td = trade_date or datetime.now(UTC) - timedelta(days=1)
    for _attempt in range(6):
        r = s.get(
            f"{BASE}/CmeWS/mvc/Settlements/Futures/Settlements/{pid}/FUT",
            params={"strategy": "DEFAULT", "tradeDate": _biz_date_str(td), "pageSize": 500},
            timeout=(10, 30),
        )
        # A 403 (Cloudflare block) must NOT be treated as a non-trading day:
        # silently walking back would "succeed" with a stale settlement 3-6
        # days old and no marker that it is stale. Raise on 403/429/5xx;
        # walk back only on other non-200s (404 = non-trading day).
        if r.status_code in (403, 429) or r.status_code >= 500:
            raise CmeError(
                f"CME {product_code}: HTTP {r.status_code} (blocked?) "
                f"— a stale settlement is better than no data on the wrong date"
            )
        if r.status_code != 200:
            td -= timedelta(days=1)
            continue
        j = r.json()
        if not j.get("empty"):
            # use the trade date from the RESPONSE when present, not the
            # requested date (they can differ on holidays). CME echoes either
            # 'YYYY-MM-DD' or 'MM/DD/YYYY' (both observed 2026-09-03) —
            # normalize to ISO; every consumer joins on ISO dates.
            raw_td = str(j.get("tradeDate", ""))
            if len(raw_td) == 10 and raw_td[4] == "-":
                resp_td = raw_td
            else:
                try:
                    resp_td = datetime.strptime(raw_td, "%m/%d/%Y").date().isoformat()
                except ValueError:
                    resp_td = td.date().isoformat()
            out = []
            for row in j.get("settlements", []):
                if row.get("month") == "Total":
                    continue
                settle = _num(row.get("settle"))
                if settle is None:
                    continue
                out.append(
                    {
                        "trade_date": resp_td,
                        "product_id": pid,
                        "month": str(row.get("month", "")).upper().strip(),
                        "settle": settle,
                        "volume": _num(row.get("volume")),
                        "open_interest": _num(row.get("openInterest")),
                    }
                )
            if out:
                return out
        td -= timedelta(days=1)
    raise CmeError(
        f"CME {product_code}: empty for the last 6 days (5-day retention — possibly lost)"
    )


def fetch_cvol() -> list[dict]:
    """Snapshot of the CVOL suite for all symbols. Returns [{symbol, cvol, atm, skew, ...}]."""
    s = _session()
    r = s.get(f"{BASE}/services/cvol", params={"symbol": ",".join(CVOL_SYMBOLS)}, timeout=(10, 30))
    if r.status_code != 200:
        raise CmeError(f"CVOL: HTTP {r.status_code}")
    out = []
    for row in r.json():
        td = str(row.get("tradeDate", ""))[:10]
        if not td:
            continue
        out.append(
            {
                "trade_date": td,
                "symbol": row.get("symbol", ""),
                "cvol": row.get("cvolPrice"),
                "atm": row.get("atmInd"),
                "skew": row.get("skew"),
                "upvar": row.get("upvarMetric"),
                "dnvar": row.get("dnvarMetric"),
                "convexity": row.get("convexInd"),
            }
        )
    return out


def fetch_voi_dates() -> list[dict]:
    """The TradeDates listing: [{trade_date, td_raw, report_type}, …] —
    Preliminary AND Final restatement entries (round-2: harvest only ever
    read entry [0], so Final restatements were never captured)."""
    s = _session()
    r0 = s.get(f"{BASE}/CmeWS/mvc/VoiTotals/V2/TradeDates", timeout=(10, 30))
    if r0.status_code != 200:
        raise CmeError(f"VOI TradeDates: HTTP {r0.status_code}")
    dates = r0.json().get("voiTradeDateTOList", [])
    out = []
    for e in dates:
        td_raw = e.get("tradeDate", "")
        if not td_raw:
            continue
        td_iso = f"{td_raw[:4]}-{td_raw[4:6]}-{td_raw[6:8]}" if len(td_raw) == 8 else td_raw
        out.append(
            {
                "trade_date": td_iso,
                "td_raw": td_raw,
                "report_type": e.get("reportType", "Preliminary"),
            }
        )
    if not out:
        raise CmeError("VOI: no dates available")
    return out


def fetch_voi(
    asset_class_id: int = 8, td_raw: str | None = None, report_type: str | None = None
) -> list[dict]:
    """Volume/OI per product for one asset class, for the LATEST date entry
    (or an explicit one from fetch_voi_dates). Products live in the nested
    voiProductsTOList key."""
    s = _session()
    if td_raw is None:
        latest = fetch_voi_dates()[0]
        td_raw = latest["td_raw"]
        report_type = latest["report_type"]
    if report_type is None:
        report_type = "Preliminary"

    r = s.post(
        f"{BASE}/CmeWS/mvc/VoiTotals/V2/AssetClass/{asset_class_id}",
        json={"tradeDate": td_raw, "excludeExchanges": []},
        timeout=(10, 30),
    )
    if r.status_code != 200:
        raise CmeError(f"VOI assetClass {asset_class_id}: HTTP {r.status_code}")
    td_iso = f"{td_raw[:4]}-{td_raw[4:6]}-{td_raw[6:8]}" if len(td_raw) == 8 else td_raw
    products = r.json().get("voiProductsTOList", [])
    out = []
    for row in products:
        out.append(
            {
                "trade_date": td_iso,
                "product_id": int(_num(row.get("productId")) or 0),
                "volume": _num(row.get("totalVolume")),
                "oi": _num(row.get("oi")),
                "oi_diff": _num_signed(row.get("oiDiff")),
                "report_type": report_type,
            }
        )
    return out


def _num(v):
    if v in (None, "", "nan"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _num_signed(v):
    if v in (None, "", "nan"):
        return None
    try:
        s = str(v).replace(",", "").replace("+", "")
        return float(s)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Options on futures (per-strike settlements). Live contract 2026-09-03:
# numeric fields are STRINGS, openInterest carries commas ("1,205"), "" or "-"
# means missing → None. The single type='' row per response is the contract
# TOTAL (strike "Total", settle "-") — it is skipped, not stored.
# ---------------------------------------------------------------------------

_MONTH_CODES = {
    "F": "JAN",
    "G": "FEB",
    "H": "MAR",
    "J": "APR",
    "K": "MAY",
    "M": "JUN",
    "N": "JUL",
    "Q": "AUG",
    "U": "SEP",
    "V": "OCT",
    "X": "NOV",
    "Z": "DEC",
}
_MONTH_NUM = {
    name: i + 1
    for i, name in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    )
}


def _int(v):
    """'1,205' → 1205; ''/'-'/None → None (missing must not become a fictional 0)."""
    if v in (None, "", "-", "nan"):
        return None
    try:
        return int(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _iso_date(mmddyyyy: str) -> str | None:
    """'09/02/2026' (API param format) → '2026-09-02' (storage format)."""
    try:
        return datetime.strptime(str(mmddyyyy), "%m/%d/%Y").date().isoformat()
    except (ValueError, TypeError):
        return None


def _mmddyyyy_from_iso(iso: str) -> str | None:
    """'2026-09-02' → '09/02/2026'. cme_settlements.trade_date is written from
    the futures response echo, which the endpoint serves in EITHER date form
    (both are 10 chars, so the [:10] truncation in fetch_settlements passes
    both through) — the underlying join matches both spellings."""
    try:
        return datetime.strptime(str(iso), "%Y-%m-%d").strftime("%m/%d/%Y")
    except (ValueError, TypeError):
        return None


def _month_num(v):
    """expiration.month is an int on the wire; tolerate 'OCT'-style strings."""
    if v is None:
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return _MONTH_NUM.get(str(v).strip()[:3].upper(), 0)


def futures_month_from_contract(contract_id: str) -> str | None:
    """Options contractId → the month label used by cme_settlements ('OGV26' → 'OCT 26').

    Parsed from the RIGHT (month code + 2-digit year) because the prefix length
    varies across products (OG/SO/HXE/PO/ES/NQ/BTC).
    """
    if len(contract_id) < 4:
        return None
    name = _MONTH_CODES.get(contract_id[-3].upper())
    return f"{name} {contract_id[-2:]}" if name else None


def parse_option_row(
    row: dict, trade_date: str, product_id: int, product_code: str, contract_id: str
) -> dict | None:
    """Normalize one OOF settlement row → option dict.

    None for the type='' Total row and for rows without a numeric strike
    (never stored — a 'Total' strike must not enter the strike ladder).
    """
    t = str(row.get("type") or "")
    if t not in ("Call", "Put"):
        return None
    strike = _num(row.get("strike"))
    if strike is None:
        return None
    return {
        "trade_date": trade_date,
        "product_id": product_id,
        "product_code": product_code,
        "contract_id": contract_id,
        "option_type": t,
        "strike": strike,
        "settle": _num(row.get("settle")),
        "volume": _int(row.get("volume")),
        "open_interest": _int(row.get("openInterest")),
    }


def pick_expirations(groups: list[dict], n: int = 6) -> list[tuple[str, str]]:
    """The n NEAREST monthly expirations as (contractId, latest tradeDate MM/DD/YYYY).

    Discovery already returns expirations nearest-first, but they are sorted
    defensively by (year, month) — the tenor ladder must not depend on wire
    order. AME (American) is the standard monthly group; BTC only publishes a
    EUR group, so the first group is the fallback.
    """
    group = next((g for g in groups if g.get("optionType") == "AME"), groups[0] if groups else None)
    if group is None:
        return []

    def _key(e):
        ex = e.get("expiration") or {}
        return (int(ex.get("year") or 0), _month_num(ex.get("month")))

    out = []
    for e in sorted(group.get("expirations") or [], key=_key)[:n]:
        cid = str(e.get("contractId") or "")
        dated = [
            (iso, raw)
            for raw in (td.get("formatedDate") for td in (e.get("tradeDates") or []))
            if raw and (iso := _iso_date(raw))
        ]
        if cid and dated:
            out.append((cid, max(dated)[1]))
    return out


def fetch_option_expirations(product_code: str, session: creq.Session | None = None) -> list[dict]:
    """Discovery: the monthly expirations + available trade dates of one product."""
    pid = OPTIONS_PRODUCTS.get(product_code)
    if pid is None:
        raise CmeError(f"unknown options product: {product_code}")
    own = session is None
    s = session or _session()
    try:
        r = s.get(
            f"{BASE}/CmeWS/mvc/Settlements/Options/TradeDateAndExpirations/{pid}",
            timeout=(10, 30),
        )
        if r.status_code != 200:
            raise CmeError(f"CME options {product_code} discovery: HTTP {r.status_code}")
        return r.json()
    finally:
        if own:
            s.close()


def fetch_option_settlements(
    product_code: str,
    contract_id: str,
    trade_date_mmddyyyy: str,
    session: creq.Session | None = None,
) -> tuple[list[dict], str | None]:
    """Per-strike settlements for one expiry on one trade date → (option_rows, trade_date ISO).

    monthYear/optionExpiration must be the contractId ('OGV26'), NOT the label
    ('Oct 2026' returns 0 rows). pageSize is decorative here — the endpoint
    returns the full month in one response (OG: 1,281 rows vs pageSize 500).
    """
    pid = OPTIONS_PRODUCTS.get(product_code)
    if pid is None:
        raise CmeError(f"unknown options product: {product_code}")
    own = session is None
    s = session or _session()
    try:
        r = s.get(
            f"{BASE}/CmeWS/mvc/Settlements/Options/Settlements/{pid}/OOF",
            params={
                "strategy": "DEFAULT",
                "optionProductId": pid,
                "monthYear": contract_id,
                "optionExpiration": contract_id,
                "tradeDate": trade_date_mmddyyyy,
                "pageSize": 500,
            },
            timeout=(10, 30),
        )
        if r.status_code != 200:
            raise CmeError(f"CME options {product_code} {contract_id}: HTTP {r.status_code}")
        j = r.json()
        # trust the response echo when parseable, else the requested date
        td_iso = _iso_date(str(j.get("tradeDate") or "")) or _iso_date(trade_date_mmddyyyy)
        rows = []
        for raw in j.get("settlements", []):
            parsed = parse_option_row(raw, td_iso, pid, product_code, contract_id)
            if parsed is not None:
                rows.append(parsed)
        return rows, td_iso
    finally:
        if own:
            s.close()
