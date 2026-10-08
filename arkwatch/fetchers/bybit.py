"""bybit.py — stablecoin supply (DefiLlama).

RETIRED 2026-09-16 (owner decision): Bybit is unreachable from both the PC
(TCP-intermittent, D-020) and the server (Indonesian ISP DNS/SNI block);
Coinglass aggregator is paid. The funding/OI/positioning legs served one
brief line + one alert at 22% fill rate — not worth the complexity.

The stablecoin total (DefiLlama) STAYS: it is a different provider, works
from any network, and feeds the "Stablecoin $XXB" brief line daily.
The module keeps its historical name to avoid churn in imports; it now
contains only the DefiLlama fetcher.
"""

from __future__ import annotations

import requests

LLAMA = "https://stablecoins.llama.fi"


def _llama_rows() -> list[dict]:
    """The full stablecoin chart, normalized (shared by latest + window)."""
    r = requests.get(f"{LLAMA}/stablecoincharts/all", timeout=(10, 60))
    if r.status_code != 200:
        raise RuntimeError(f"llama: HTTP {r.status_code}")
    rows = r.json()
    if not rows:
        raise RuntimeError("llama: empty response")
    return rows


def _norm(row: dict) -> tuple[str, float]:
    """One chart row → (iso_date, usd_total). 2026-09-17 schema change
    (twice in one day): totalCirculatingUSD became a per-peg dict — the OLD
    scalar carried ONLY the USD-pegged total (peggedUSD matches the last
    stored scalar to the cent), and `date` became an epoch (int or numeric
    STRING live)."""
    from datetime import UTC, datetime

    raw = row.get("totalCirculatingUSD")
    if isinstance(raw, dict):
        v = raw.get("peggedUSD")
        if v is None:
            v = (row.get("totalCirculating") or {}).get("peggedUSD")
        if v is None:
            v = sum(x for x in raw.values() if isinstance(x, (int, float)))
        raw = v
    ts = str(row.get("date") or "")[:10]
    if len(ts) == 10 and ts.isdigit():
        ts = datetime.fromtimestamp(int(ts), tz=UTC).date().isoformat()
    return ts, float(raw)


def fetch_stablecoin_total() -> dict:
    """Total stablecoin circulating USD (USD-pegged definition — see _norm)."""
    ts, usd = _norm(_llama_rows()[-1])
    return {"ts": ts, "total_usd": usd}


def fetch_stablecoin_window(n: int = 7) -> list[dict]:
    """The last n chart points [{ts, total_usd}] — the harvest writes the
    window each run so outage-day holes heal (round-2: 09-09/14/15 stayed
    NULL for days because only the latest point was ever written)."""
    return [{"ts": ts, "total_usd": usd} for ts, usd in (_norm(r) for r in _llama_rows()[-n:])]
