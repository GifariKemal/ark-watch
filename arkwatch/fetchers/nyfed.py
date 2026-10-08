"""nyfed.py — NY Fed Markets API: rates, repo, SOMA agency, desk operations, PD survey.

Verified pitfalls:
  - SOFR is a SECURED rate (/rates/secured/sofr; the unsecured path returns 400)
  - repo endpoints report RAW USD amounts (the "$Billions" CSV label is misleading)
  - SRF = operationType 'Repo' + operationMethod 'Full Allotment'; sum the window per day
  - TRANSPORT: plain `requests` only — curl_cffi double-encodes %20 in paths, so the
    "agency debts" enum 400s through it (live-verified 2026-09-03). Space-bearing path
    segments go through urllib.parse.quote(safe="").
  - Agency schema split: MBS/CMBS rows carry currentFaceValue (parValue is EMPTY for
    them; the "all" enum mixes schemas — never sum parValue across it); agency-debts
    rows look like tsy rows (parValue/coupon/maturityDate/issuer) and are normalized
    into current_face_value. MBS/CMBS rows have NO changeFromPriorWeek.
  - Desk operations: `latest` = CURRENT-DAY window only (looks empty — that is
    correct); history via results/{summary|details}/last/{N}.json; announcements
    have ONLY latest + search (no last/N variant).
  - ambs amounts live in totalAcceptedOrigFace/…CurrFace (the …Par fields are "")
    while tsy amounts live in totalParAmtAccepted.
  - rates unsecured/secured "all" feeds: EFFR volume is volumeInBillions (native $B);
    the per-type last/{N} history path exists only for sofr — the new rate series are
    latest-only (the registry harvest pattern for non-FRED sources).
  - pd: every one of the 1,539 series lives under the SBN2024 break only
    (SBP2001/SBP2013 return 0 obs) — methodology breaks must not be z-scored across.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import requests

BASE = "https://markets.newyorkfed.org/api"
PERCENTILE_SERIES = {
    "SOFR_P1": "percentPercentile1",
    "SOFR_P25": "percentPercentile25",
    "SOFR_P75": "percentPercentile75",
    "SOFR_P99": "percentPercentile99",
}

# --- SOMA agency (MBS/CMBS/agency-debts) + WAM --------------------------------

# asset_type (stored) -> API path enum. "agency debts" carries a space — see the
# transport pitfall in the module docstring.
AGENCY_ENUMS = {
    "mbs": "mbs",
    "cmbs": "cmbs",
    "agency_debts": "agency debts",
}
# WAM path enums (live-verified 2026-09-03: all 8.26 · bills 0.20 · notesbonds 9.47
# · tips 8.89 · frn 1.16 — note the SINGULAR "frn"; "frns" returns 400).
WAM_TYPES = ("all", "bills", "notesbonds", "tips", "frn")
BIG_PAYLOAD_TIMEOUT = (10, 60)  # mbs payload = ~8.4k rows / ~1.5MB


# --- Desk outright operations + fxs ---------------------------------------------

# [KEPUTUSAN: window 14 operasi — 2 minggu kalender, cukup bagi sinyal explainer
#  weekly_change SOMA tanpa menimbun histori; ops diam-diam identik antar-minggu]
OPS_WINDOW_N = 14
OPERATION_RESPONSE_KEYS = {"tsy": "treasury", "ambs": "ambs"}  # family -> JSON key

# --- Primary Dealer Positions Survey --------------------------------------------

# [RISET: pd/list/timeseries.json 2026-09-03 — seluruh 1.539 seri hanya hidup di
#  break SBN2024 (SBP2001/SBP2013 → 0 obs); unit = $ juta, mingguan (rabu)]
PD_SERIESBREAK = "SBN2024"
PD_KEYIDS = (
    # posisi (total = neto long−short): dealer primer — komplemen COT (futures crowd)
    "PDPOSGST-TOT",  # UST ex-TIPS
    "PDPOSMBS-TOT",  # Federal agency & GSE MBS
    "PDPOSCS-TOT",  # korporat
    "PDPOSFGS-TOT",  # agency ex-MBS
    "PDPOSSMGO-TOT",  # munis
    # financing (fails): stress specials — pasangan alami sinyal repo-specials SOMA
    "PDFTR-USTET",  # UST fails-to-receive
    "PDFTR-FGM",  # agency MBS fails-to-receive
    "PDFTD-USTET",  # UST fails-to-deliver
)

# --- Rates: unsecured (EFFR/OBFR) + secured/all (TGCR/BGCR/SOFRAI) ---------------

# key (series_id after "NYFED:") -> response field. The rate TYPE is the key's
# underscore prefix (OBFR_P99 -> OBFR). EFFR_VOL is native $B — not converted to
# raw USD (volumeInBillions is the only form the API reports).
UNSECURED_ALL_SERIES = {
    "OBFR": "percentRate",
    "OBFR_P1": "percentPercentile1",
    "OBFR_P25": "percentPercentile25",
    "OBFR_P75": "percentPercentile75",
    "OBFR_P99": "percentPercentile99",
    "EFFR_P1": "percentPercentile1",
    "EFFR_P25": "percentPercentile25",
    "EFFR_P75": "percentPercentile75",
    "EFFR_P99": "percentPercentile99",
    "EFFR_VOL": "volumeInBillions",
}
SECURED_ALL_SERIES = {
    "TGCR": "percentRate",
    "BGCR": "percentRate",
    # SOFRAI = SOFR Average Index — parsing tersedia, tapi TIDAK didaftarkan sbg
    # seri: FRED:SOFRINDEX sudah membawa angka identik (aturan satu-sumber).
    "SOFRAI": "index",
}


class NyFedError(RuntimeError):
    pass


def _f(v) -> float | None:
    """API value -> float; '' or missing -> None (missing must not become 0.0)."""
    if v is None or v == "":
        return None
    return float(v)


def _get(path: str, params: dict | None = None, session=None, timeout=(10, 30)) -> dict:
    rq = session or requests
    r = rq.get(f"{BASE}{path}", params=params, timeout=timeout)
    if r.status_code != 200:
        raise NyFedError(f"nyfed {path}: HTTP {r.status_code}")
    return r.json()


def fetch_sofr_percentiles(n: int = 5) -> list[dict]:
    """Returns [{ts, p1, p25, p75, p99}] for the last n days."""
    j = _get(f"/rates/secured/sofr/last/{min(n, 10000)}.json")
    out = []
    for rec in j.get("refRates", []):
        out.append(
            {
                "ts": rec["effectiveDate"],
                "p1": rec.get("percentPercentile1"),
                "p25": rec.get("percentPercentile25"),
                "p75": rec.get("percentPercentile75"),
                "p99": rec.get("percentPercentile99"),
            }
        )
    return out


def _repo_daily(base_path: str, *, full_allotment: bool) -> dict[str, float]:
    """Aggregate totalAmtAccepted per operationDate over a 14-day window."""
    s = (datetime.now(UTC) - timedelta(days=14)).strftime("%Y-%m-%d")
    e = datetime.now(UTC).strftime("%Y-%m-%d")
    j = _get(base_path, {"startDate": s, "endDate": e})
    daily: dict[str, float] = {}
    for op in j.get("repo", {}).get("operations", []):
        if full_allotment and op.get("operationMethod") != "Full Allotment":
            continue
        d = op.get("operationDate")
        v = op.get("totalAmtAccepted")
        if d and v is not None:
            daily[d] = daily.get(d, 0.0) + float(v)
    return daily


def fetch_window(series_id: str, days: int = 12, *, today: str | None = None) -> list[dict]:
    """GAP-HEAL (audit P1-1, 2026-09-13): return EVERY observation in a
    ~`days`-business-day window, not just the latest — a PC-shutdown night
    that misses a publication day left a permanent hole because these
    endpoints are latest-only snapshots. Landing the whole window is
    idempotent (PK dedup) and self-heals gaps up to the window length.

    One search.json call per family (unsecured/secured) covers all rate
    series; SOFR percentiles ride their own last/N endpoint; SRF/ONRRP
    aggregate the 14-day operations window they already fetch.
    `today` (ISO) forwards to the research families' injectable clock."""
    from datetime import timedelta

    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    from . import nyfedresearch

    if nyfedresearch.knows(key):
        return nyfedresearch.fetch_window(series_id, days, today=today)
    start = (datetime.now(UTC).date() - timedelta(days=int(days * 1.8))).isoformat()
    all_fields = UNSECURED_ALL_SERIES | SECURED_ALL_SERIES
    if key in all_fields:
        path = (
            "/rates/unsecured/all/search.json"
            if key in UNSECURED_ALL_SERIES
            else "/rates/secured/all/search.json"
        )
        j = _get(path, {"startDate": start})
        rate_type, field = key.split("_", 1)[0], all_fields[key]
        pts = [
            {"ts": r["effectiveDate"], "value": _f(r.get(field))}
            for r in j.get("refRates", [])
            if r.get("type") == rate_type and _f(r.get(field)) is not None
        ]
        pts.sort(key=lambda p: p["ts"])
        return pts
    if key in PERCENTILE_SERIES:
        col = PERCENTILE_SERIES[key].replace("percentPercentile", "p")
        rows = fetch_sofr_percentiles(days + 5)
        pts = [{"ts": r["ts"], "value": _f(r.get(col))} for r in rows if _f(r.get(col)) is not None]
        pts.sort(key=lambda p: p["ts"])
        return pts
    if key in ("SRF_TOTAL", "ONRRP_TOTAL"):
        daily = _repo_daily(
            "/rp/results/search.json"
            if key == "SRF_TOTAL"
            else "/rp/reverserepo/propositions/search.json",
            full_allotment=key == "SRF_TOTAL",
        )
        return [{"ts": d, "value": v} for d, v in sorted(daily.items())]
    raise NyFedError(f"unknown nyfed series: {key}")


def fetch_latest(series_id: str) -> dict:
    key = series_id.split(":", 1)[1] if ":" in series_id else series_id
    # research/survey datasets (HHDC/SCE/GSCPI/ESMS/ACM/HPW/LW/MCT) live in
    # their own module but keep the NYFED: registry family — delegate (lazy
    # import keeps the modules loadable independently)
    from . import nyfedresearch

    if nyfedresearch.knows(key):
        return nyfedresearch.fetch_latest(series_id)
    all_fields = UNSECURED_ALL_SERIES | SECURED_ALL_SERIES
    if key in all_fields:
        path = (
            "/rates/unsecured/all/latest.json"
            if key in UNSECURED_ALL_SERIES
            else "/rates/secured/all/latest.json"
        )
        return _rate_value(_cached_latest(path), key.split("_", 1)[0], all_fields[key])
    if key in PERCENTILE_SERIES:
        rows = fetch_sofr_percentiles(5)
        rows = [
            r
            for r in rows
            if r.get(PERCENTILE_SERIES[key].replace("percentPercentile", "p")) is not None
        ]
        if not rows:
            raise NyFedError(f"sofr percentile {key}: empty")
        r0 = max(rows, key=lambda x: x["ts"])
        return {
            "ts": r0["ts"],
            "value": r0[PERCENTILE_SERIES[key].replace("percentPercentile", "p")],
        }
    if key == "SRF_TOTAL":
        daily = _repo_daily("/rp/results/search.json", full_allotment=True)
        # operationType=Repo filtering is already applied by the endpoint
        # parameters; take the latest day
        if not daily:
            raise NyFedError("SRF: no Full Allotment operations in the last 14 days")
        d = max(daily)
        return {"ts": d, "value": daily[d]}
    if key == "ONRRP_TOTAL":
        daily = _repo_daily("/rp/reverserepo/propositions/search.json", full_allotment=False)
        if not daily:
            raise NyFedError("ONRRP: empty")
        d = max(daily)
        return {"ts": d, "value": daily[d]}
    raise NyFedError(f"unknown nyfed series: {key}")


def fetch_history_sofr(n: int = 500) -> list[dict]:
    return fetch_sofr_percentiles(n)


# --- SOMA agency holdings + WAM -------------------------------------------------


def fetch_agency_holdings(asset_type: str, as_of: str, session=None) -> list[dict]:
    """Per-CUSIP agency holdings for one weekly as-of date (same Wednesday chain
    as tsy). Returns [{as_of_date, cusip, asset_type, security_description, term,
    issuer, current_face_value, change_week}] — money in RAW USD with the two
    API schemas normalized into current_face_value (MBS/CMBS: currentFaceValue;
    agency debts: parValue).
    """
    if asset_type not in AGENCY_ENUMS:
        raise NyFedError(f"unknown agency asset_type: {asset_type}")
    enum = quote(AGENCY_ENUMS[asset_type], safe="")
    j = _get(
        f"/soma/agency/get/{enum}/asof/{as_of}.json", session=session, timeout=BIG_PAYLOAD_TIMEOUT
    )
    rows = j.get("soma", {}).get("holdings", [])
    if not rows:
        raise NyFedError(f"agency {asset_type} {as_of}: empty")
    out = []
    for r in rows:
        value = _f(r.get("currentFaceValue"))
        if value is None:
            value = _f(r.get("parValue"))  # agency-debts schema
        out.append(
            {
                "as_of_date": r.get("asOfDate", as_of),
                "cusip": r["cusip"],
                "asset_type": asset_type,
                "security_description": r.get("securityDescription") or None,
                "term": r.get("term") or None,
                "issuer": r.get("issuer") or None,
                "current_face_value": value,
                "change_week": _f(r.get("changeFromPriorWeek")),
            }
        )
    return out


def compute_agency_summary(holdings: list[dict]) -> dict:
    """Per-CUSIP agency rows -> one soma_agency_summary row (raw USD)."""
    as_of = holdings[0]["as_of_date"] if holdings else ""
    totals = {t: 0.0 for t in AGENCY_ENUMS}
    for h in holdings:
        v = h.get("current_face_value")
        if v is not None:
            totals[h["asset_type"]] = totals.get(h["asset_type"], 0.0) + v
    return {
        "as_of_date": as_of,
        "mbs": totals["mbs"],
        "cmbs": totals["cmbs"],
        "agency_debts": totals["agency_debts"],
        "total": sum(totals.values()),
        "n_cusips": len(holdings),
    }


def fetch_wam(as_of: str, session=None) -> list[dict]:
    """Official weighted-average maturity per security type + overall.

    Returns [{as_of_date, wam_type, years}] — wam_type is the path enum
    (all|bills|notesbonds|tips|frn).
    """
    out = []
    for wam_type in WAM_TYPES:
        j = _get(f"/soma/tsy/wam/{wam_type}/asof/{as_of}.json", session=session)
        out.append(
            {
                "as_of_date": j.get("soma", {}).get("asOfDate", as_of),
                "wam_type": wam_type,
                "years": _f(j.get("soma", {}).get("wam")),
            }
        )
    return out


# --- Desk outright operations (tsy/ambs) + fxs swap lines ------------------------


def _op_amount(r: dict) -> float | None:
    """Accepted amount, raw USD. tsy rows report totalParAmtAccepted; ambs rows
    report face-value fields (their par fields are "")."""
    for k in (
        "totalParAmtAccepted",
        "totalAmtAcceptedPar",
        "totalAcceptedOrigFace",
        "totalAcceptedCurrFace",
    ):
        v = _f(r.get(k))
        if v is not None:
            return v
    return None


def _parse_operation(r: dict, family: str) -> dict:
    return {
        "operation_id": r.get("operationId"),
        "family": family,
        "operation_date": r.get("operationDate"),
        "settlement_date": r.get("settlementDate") or None,
        "operation_type": r.get("operationType"),
        "direction": r.get("operationDirection"),
        "maturity_start": r.get("maturityRangeStart") or None,
        "maturity_end": r.get("maturityRangeEnd") or None,
        "status": r.get("auctionStatus"),
        "amount": _op_amount(r),
        "details_json": json.dumps(r, sort_keys=True),
    }


def fetch_operations(family: str, n: int = OPS_WINDOW_N, session=None) -> list[dict]:
    """Desk operation RESULTS (realized amounts), most recent n operations."""
    if family not in OPERATION_RESPONSE_KEYS:
        raise NyFedError(f"unknown operations family: {family}")
    j = _get(f"/{family}/all/results/summary/last/{n}.json", session=session)
    auctions = j.get(OPERATION_RESPONSE_KEYS[family], {}).get("auctions", [])
    return [_parse_operation(r, family) for r in auctions]


def fetch_announcements(family: str, session=None) -> list[dict]:
    """Operation announcements — CURRENT-DAY window only (the feed has no
    last/{N} variant); normally empty at 06:30 WIB, which is correct."""
    if family not in OPERATION_RESPONSE_KEYS:
        raise NyFedError(f"unknown operations family: {family}")
    j = _get(f"/{family}/all/announcements/summary/latest.json", session=session)
    auctions = j.get(OPERATION_RESPONSE_KEYS[family], {}).get("auctions", [])
    return [_parse_operation(r, family) for r in auctions]


def fetch_fxs_latest(session=None) -> dict:
    """Dollar swap-line feed + standby counterparty list.

    operations is normally EMPTY — a non-empty list is a dollar-funding event.
    counterparties is a static name list (11 central banks @ 2026-09-03), stored
    by the harvester as fetch metadata, not as a table.
    """
    j = _get("/fxs/all/latest.json", session=session)
    j_cp = _get("/fxs/list/counterparties.json", session=session)
    ops = []
    for i, r in enumerate(j.get("fxSwaps", {}).get("operations", [])):
        row = _parse_operation(r, "fxs")
        if not row["operation_id"]:
            row["operation_id"] = f"fxs:{row['operation_date']}:{i}"
        ops.append(row)
    return {
        "operations": ops,
        "counterparties": j_cp.get("fxSwaps", {}).get("counterparties", []),
    }


# --- Primary Dealer Positions Survey ---------------------------------------------


def fetch_pd_series(keyid: str, seriesbreak: str = PD_SERIESBREAK, session=None) -> list[dict]:
    """FULL history for one curated keyid (small: ~112 weekly obs).

    Returns [{asofdate, keyid, seriesbreak, value_musd}] — native $millions.
    """
    j = _get(
        f"/pd/get/{seriesbreak}/timeseries/{keyid}.json",
        session=session,
        timeout=BIG_PAYLOAD_TIMEOUT,
    )
    ts = j.get("pd", {}).get("timeseries", [])
    if not ts:
        raise NyFedError(f"pd {keyid}@{seriesbreak}: empty")
    return [
        {
            "asofdate": t["asofdate"],
            "keyid": keyid,
            "seriesbreak": seriesbreak,
            "value_musd": _f(t.get("value")),
        }
        for t in ts
    ]


# --- Rates: unsecured + secured/all ------------------------------------------------

_latest_cache: dict[str, list[dict]] = {}


def _cached_latest(path: str) -> list[dict]:
    """Per-process memo — a dozen registry series drink from two endpoints and
    the harvest job is a short-lived process, so one HTTP hit per path."""
    if path not in _latest_cache:
        _latest_cache[path] = _get(path).get("refRates", [])
    return _latest_cache[path]


def _rate_value(rows: list[dict], rate_type: str, field: str) -> dict:
    for r in rows:
        if r.get("type") == rate_type:
            v = _f(r.get(field))
            if v is None:
                raise NyFedError(f"{rate_type}.{field}: missing")
            return {"ts": r["effectiveDate"], "value": v}
    raise NyFedError(f"{rate_type}: not in feed")
