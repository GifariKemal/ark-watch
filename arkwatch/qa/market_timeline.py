"""Five-minute cross-asset timeline and sector ETF breadth proxy."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import requests

from .. import db as _db
from ..config import PLAN_LIMIT_STATUSES, PlanLimited
from ..fetchers import yahoo
from .fetch_log import log_collection
from .okx_market import collect as collect_okx_market

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "arkwatch.db"
INTERVAL = "5m"
TRACKED = {
    "NQ1": "NQ=F",
    "ES1": "ES=F",
    "YM1": "YM=F",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "CL1": "CL=F",
    "BZ1": "BZ=F",
    "DXY": "DX-Y.NYB",
    "TNX": "^TNX",
    "VIX": "^VIX",
    "SPY": "SPY",
    "RSP": "RSP",
    "XLK": "XLK",
    "XLY": "XLY",
    "XLC": "XLC",
    "XLF": "XLF",
    "XLV": "XLV",
    "XLI": "XLI",
    "XLB": "XLB",
    "XLE": "XLE",
    "XLP": "XLP",
    "XLRE": "XLRE",
    "XLU": "XLU",
    "SMH": "SMH",
    "SOXX": "SOXX",
    "GC1": "GC=F",
    "SI1": "SI=F",
    "HG1": "HG=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
}
EODHD = {
    "NQ1": "NQ.COMM",
    "ES1": "ES.COMM",
    "BTCUSD": "BTC-USD.CC",
    "ETHUSD": "ETH-USD.CC",
    "CL1": "CL.COMM",
    "BZ1": "BZ.COMM",
    "DXY": "DXY.INDX",
    "TNX": "TNX.INDX",
}
FMP = {symbol: ticker for symbol, ticker in TRACKED.items() if ticker.isalpha()}
FMP.update(
    {
        "BTCUSD": "BTCUSD",
        "ETHUSD": "ETHUSD",
        "EURUSD": "EURUSD",
        "GBPUSD": "GBPUSD",
        "USDJPY": "USDJPY",
    }
)
SECTORS = ("XLK", "XLY", "XLC", "XLF", "XLV", "XLI", "XLB", "XLE", "XLP", "XLRE", "XLU")
# SMH/SOXX print full extended hours like SPY: regular-hours expectations flagged every
# post-market bar FUTURE (prod 2026-10-09: ~250 DEGRADED rows/day)
EQUITY_SYMBOLS = frozenset({"SPY", "RSP", "SMH", "SOXX", *SECTORS})
# spot FX on Yahoo trades through the CME daily break: Sun 17:00 -> Fri 17:00 New York
FX_SYMBOLS = frozenset({"EURUSD", "GBPUSD", "USDJPY"})
CALENDAR_BY_SYMBOL = {
    **dict.fromkeys(("NQ1", "ES1", "YM1", "CL1", "GC1", "SI1", "HG1"), "CMES"),
    # Yahoo BZ=F is the NYMEX Brent contract: Globex hours, not ICE London (bars follow CL1)
    "BZ1": "CMES",
    "DXY": "IEPA",
    "TNX": "XNYS",
    **dict.fromkeys(EQUITY_SYMBOLS, "XNYS"),
}
FRESHNESS_GRACE = timedelta(minutes=15)
NEW_YORK = ZoneInfo("America/New_York")
CHICAGO = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class Freshness:
    status: str
    latest_bar_ts: str | None
    expected_bar_ts: str | None
    lag_minutes: float | None


@dataclass(frozen=True)
class ProviderAttempt:
    source: str
    rows: list[dict]
    freshness: Freshness | None
    error: str | None = None


@dataclass(frozen=True)
class ProviderSelection:
    source: str | None
    rows: list[dict]
    freshness: Freshness | None
    attempts: tuple[ProviderAttempt, ...]


@lru_cache(maxsize=4)
def _calendar(name: str):
    return xcals.get_calendar(name)


def _bar_start(value: str | datetime) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("bar timestamp must be timezone-aware")
    return stamp.astimezone(UTC)


def _floor_interval(value: datetime) -> datetime:
    value = value.astimezone(UTC).replace(second=0, microsecond=0)
    return value.replace(minute=value.minute - value.minute % 5)


def _open_minute(calendar, value: datetime) -> bool:
    stamp = pd.Timestamp(value).floor("min")
    if calendar.name == "CMES":
        local = value.astimezone(CHICAGO)
        minute_of_day = local.hour * 60 + local.minute
        if local.weekday() == 5 or (local.weekday() == 6 and minute_of_day < 17 * 60):
            return False
        if 16 * 60 <= minute_of_day < 17 * 60:
            return False
        if local.weekday() == 4 and minute_of_day >= 16 * 60:
            return False
    elif calendar.name == "IEPA":
        local = value.astimezone(NEW_YORK)
        minute_of_day = local.hour * 60 + local.minute
        if local.weekday() == 5 or (local.weekday() == 4 and minute_of_day >= 17 * 60):
            return False
        if 17 * 60 <= minute_of_day < 20 * 60 and not (
            local.weekday() == 6 and minute_of_day >= 18 * 60
        ):
            return False
        if local.weekday() == 6 and minute_of_day < 18 * 60:
            return False
        if local.weekday() == 6 and minute_of_day < 20 * 60:
            return bool(
                _calendar("IEPA").is_session((local.date() + timedelta(days=1)).isoformat())
            )
    return bool(calendar.is_open_on_minute(stamp))


def _equity_open_minute(calendar, value: datetime, *, regular_only: bool = False) -> bool:
    local = value.astimezone(NEW_YORK)
    session = local.date().isoformat()
    if not calendar.is_session(session):
        return False
    if regular_only:
        return bool(calendar.is_open_on_minute(pd.Timestamp(value).floor("min")))
    regular_close = calendar.session_close(session).to_pydatetime().astimezone(NEW_YORK)
    extended_close = (
        regular_close.replace(hour=17, minute=0)
        if regular_close.hour < 16
        else regular_close.replace(hour=20, minute=0)
    )
    return local.replace(hour=4, minute=0, second=0, microsecond=0) <= local < extended_close


def _valid_bar_slot(
    calendar,
    start: datetime,
    *,
    equity_extended: bool = False,
    equity_regular_only: bool = False,
) -> bool:
    def is_open(cal, stamp):
        if equity_extended or equity_regular_only:
            return _equity_open_minute(cal, stamp, regular_only=equity_regular_only)
        return _open_minute(cal, stamp)

    return is_open(calendar, start) and is_open(calendar, start + timedelta(minutes=4))


def _previous_market_minute(
    calendar,
    now: datetime,
    *,
    equity_extended: bool = False,
    equity_regular_only: bool = False,
) -> datetime | None:
    if equity_extended or equity_regular_only:
        local_now = now.astimezone(NEW_YORK)
        for offset in range(8):
            day = local_now.date() - timedelta(days=offset)
            if not calendar.is_session(day.isoformat()):
                continue
            if equity_regular_only:
                candidate = calendar.session_close(day.isoformat()).to_pydatetime()
                candidate = candidate.astimezone(NEW_YORK)
            else:
                regular_close = calendar.session_close(day.isoformat()).to_pydatetime()
                regular_close = regular_close.astimezone(NEW_YORK)
                close_hour = 17 if regular_close.hour < 16 else 20
                candidate = datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=NEW_YORK)
            candidate -= timedelta(minutes=1)
            if candidate <= local_now and _equity_open_minute(
                calendar, candidate, regular_only=equity_regular_only
            ):
                return candidate
        return None
    previous = calendar.previous_minute(pd.Timestamp(now).floor("min"))
    stamp = previous.to_pydatetime()
    if _open_minute(calendar, stamp):
        return stamp
    if calendar.name in ("CMES", "IEPA"):
        timezone = CHICAGO if calendar.name == "CMES" else NEW_YORK
        close_hour = 16 if calendar.name == "CMES" else 17
        local_now = now.astimezone(timezone)
        for offset in range(8):
            day = local_now.date() - timedelta(days=offset)
            candidate = datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=timezone)
            candidate -= timedelta(minutes=1)
            if _open_minute(calendar, candidate):
                return candidate
    return None


def _vix_expected_bar(now: datetime) -> tuple[bool, datetime | None]:
    calendar = _calendar("XNYS")
    local_now = now.astimezone(NEW_YORK)
    today_is_session = calendar.is_session(local_now.date().isoformat())
    if today_is_session:
        regular_close = calendar.session_close(local_now.date().isoformat()).to_pydatetime()
        regular_close = regular_close.astimezone(NEW_YORK) + timedelta(minutes=15)
        day_start = datetime(local_now.year, local_now.month, local_now.day, tzinfo=NEW_YORK)
        session_windows = (
            (day_start.replace(hour=3, minute=15), day_start.replace(hour=9, minute=25)),
            (day_start.replace(hour=9, minute=30), regular_close),
        )
        for opening, closing in session_windows:
            if opening <= local_now < closing:
                candidate = _floor_interval(now) - timedelta(minutes=5)
                if candidate.astimezone(NEW_YORK) >= opening and candidate + timedelta(
                    minutes=5
                ) <= closing.astimezone(UTC):
                    return True, candidate
                return True, None
    for offset in range(8):
        day = local_now.date() - timedelta(days=offset)
        if not calendar.is_session(day.isoformat()):
            continue
        regular_close = calendar.session_close(day.isoformat()).to_pydatetime()
        regular_close = regular_close.astimezone(NEW_YORK) + timedelta(minutes=15)
        closing_times = (
            datetime(day.year, day.month, day.day, 9, 25, tzinfo=NEW_YORK),
            regular_close,
        )
        for closing in reversed(closing_times):
            if closing.astimezone(UTC) <= now:
                return False, closing.astimezone(UTC) - timedelta(minutes=5)
    return False, None


def _fx_open(value: datetime) -> bool:
    local = value.astimezone(NEW_YORK)
    minute, weekday = local.hour * 60 + local.minute, local.weekday()
    return not (
        weekday == 5 or (weekday == 4 and minute >= 17 * 60) or (weekday == 6 and minute < 17 * 60)
    )


def _tnx_open(value: datetime) -> bool:
    """^TNX is calculated 08:30-15:00 New York on NYSE days (bars observed 2026-10, not the
    09:30-16:00 equity session)."""
    local = value.astimezone(NEW_YORK)
    minute = local.hour * 60 + local.minute
    return 8 * 60 + 30 <= minute < 15 * 60 and bool(
        _calendar("XNYS").is_session(local.date().isoformat())
    )


def _window_expected(now: datetime, is_open) -> tuple[bool, datetime | None]:
    """(market_open, expected last complete 5m slot) for a session given as a predicate."""
    candidate = _floor_interval(now) - timedelta(minutes=5)
    if is_open(now):
        full = is_open(candidate) and is_open(candidate + timedelta(minutes=4))
        return True, candidate if full else None
    for _ in range(8 * 288):  # back to the last fully open slot, at most 8 days
        if is_open(candidate) and is_open(candidate + timedelta(minutes=4)):
            return False, candidate
        candidate -= timedelta(minutes=5)
    return False, None


def assess_freshness(
    symbol: str,
    rows: list[dict],
    now: datetime | None = None,
    *,
    regular_only: bool = False,
) -> Freshness:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("freshness assessment requires a timezone-aware timestamp")
    now = now.astimezone(UTC)
    try:
        latest = max((_bar_start(row["bar_ts_utc"]) for row in rows), default=None)
    except (KeyError, TypeError, ValueError):
        return Freshness("UNKNOWN", None, None, None)
    try:
        if symbol in ("BTCUSD", "ETHUSD"):
            expected = _floor_interval(now) - timedelta(minutes=5)
            market_open = True
        elif symbol == "VIX":
            market_open, expected = _vix_expected_bar(now)
        elif symbol in FX_SYMBOLS:
            market_open, expected = _window_expected(now, _fx_open)
        elif symbol == "TNX":
            market_open, expected = _window_expected(now, _tnx_open)
        elif symbol in EQUITY_SYMBOLS:
            calendar = _calendar("XNYS")
            market_open = _equity_open_minute(calendar, now, regular_only=regular_only)
            candidate = _floor_interval(now) - timedelta(minutes=5)
            if market_open:
                expected = (
                    candidate
                    if _valid_bar_slot(
                        calendar,
                        candidate,
                        equity_extended=not regular_only,
                        equity_regular_only=regular_only,
                    )
                    else None
                )
            else:
                previous = _previous_market_minute(
                    calendar,
                    now,
                    equity_extended=not regular_only,
                    equity_regular_only=regular_only,
                )
                expected = _floor_interval(previous) if previous is not None else None
        else:
            calendar = _calendar(CALENDAR_BY_SYMBOL.get(symbol, "XNYS"))
            market_open = _open_minute(calendar, now)
            candidate = _floor_interval(now) - timedelta(minutes=5)
            if market_open:
                expected = candidate if _valid_bar_slot(calendar, candidate) else None
            else:
                previous = _previous_market_minute(calendar, now)
                expected = _floor_interval(previous) if previous is not None else None
    except (ValueError, KeyError, TypeError):
        market_open, expected = False, None
    expected_iso = expected.isoformat(timespec="seconds") if expected else None
    latest_iso = latest.isoformat(timespec="seconds") if latest else None
    if latest is None:
        return Freshness("EMPTY", latest_iso, expected_iso, None)
    if expected is None:
        return Freshness("WAITING" if market_open else "UNKNOWN", latest_iso, None, None)
    latest_slot = _floor_interval(latest)
    lag = (expected - latest_slot).total_seconds() / 60
    if lag < 0:
        if regular_only and symbol in EQUITY_SYMBOLS:
            return Freshness("OUT_OF_SESSION", latest_iso, expected_iso, lag)
        # FUTURE only when the bar is really ahead of the clock (a mis-zoned feed); a bar the
        # session model did not expect yet still arrived in the past and is just fresh
        if latest_slot > _floor_interval(now):
            return Freshness("FUTURE", latest_iso, expected_iso, lag)
        return Freshness("FRESH" if market_open else "CLOSED", latest_iso, expected_iso, lag)
    status = "FRESH" if lag <= FRESHNESS_GRACE.total_seconds() / 60 else "STALE"
    if status == "STALE" and symbol in EQUITY_SYMBOLS and not regular_only:
        # thin ETFs (XLY, XLRE, ...) print few or no extended-hours bars: outside the regular
        # session a bar that covers the regular close is complete
        regular = assess_freshness(symbol, rows, now, regular_only=True)
        if regular.status in ("CLOSED", "OUT_OF_SESSION"):
            return Freshness("CLOSED", latest_iso, expected_iso, lag)
    if not market_open and status == "FRESH":
        status = "CLOSED"
    return Freshness(status, latest_iso, expected_iso, lag)


def _freshness_error(freshness: Freshness) -> str:
    return (
        f"{freshness.status} bars: latest={freshness.latest_bar_ts or 'none'} "
        f"expected={freshness.expected_bar_ts or 'unknown'} "
        f"lag_minutes={freshness.lag_minutes if freshness.lag_minutes is not None else 'unknown'}"
    )


def _safe_error(ex: Exception) -> str:
    if isinstance(ex, NoFallbackDataError):
        return f"no mapped provider returned bars{': ' + ex.errors if ex.errors else ''}"
    if isinstance(ex, UnusableBarsError):
        return _freshness_error(ex.freshness)
    if isinstance(ex, requests.HTTPError):
        status = ex.response.status_code if ex.response is not None else "unknown"
        return f"HTTP {status}"
    if isinstance(ex, requests.Timeout):
        return "timeout"
    if isinstance(ex, requests.exceptions.SSLError):
        return "TLS error"
    if isinstance(ex, requests.ConnectionError):
        return "connection error"
    return type(ex).__name__


def _store(conn, symbol: str, source: str, rows: list[dict], interval: str = INTERVAL) -> int:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    values = [
        (
            symbol,
            r["bar_ts_utc"],
            interval,
            source,
            r.get("open"),
            r.get("high"),
            r.get("low"),
            r.get("close"),
            r.get("volume"),
            now,
        )
        for r in rows
    ]
    if not values:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO intraday_bars (symbol,bar_ts_utc,interval,source,open,high,low,close,volume,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def _utc_stamp(value, *, source: str, ticker: str) -> str:
    if isinstance(value, int | float) or str(value).isdigit():
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        zone = UTC if source == "EODHD" else ZoneInfo("America/New_York")
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _normalized_rows(payload: list[dict], *, source: str, ticker: str) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=2)
    completed = datetime.now(UTC).replace(second=0, microsecond=0)
    completed -= timedelta(minutes=completed.minute % 5)
    out = []
    for row in payload:
        raw_ts = row.get("timestamp") or row.get("datetime") or row.get("date")
        if raw_ts is None or row.get("close") is None:
            continue
        stamp = _utc_stamp(raw_ts, source=source, ticker=ticker)
        dt = datetime.fromisoformat(stamp)
        if dt < cutoff or dt >= completed:
            continue
        out.append(
            {
                "bar_ts_utc": stamp,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            }
        )
    return sorted(out, key=lambda row: row["bar_ts_utc"])


def _eodhd_bars(symbol: str) -> list[dict]:
    token = os.environ.get("EODHD_API_TOKEN", "")
    if not token or symbol not in EODHD:
        return []
    now = datetime.now(UTC)
    response = requests.get(
        f"https://eodhd.com/api/intraday/{EODHD[symbol]}",
        params={
            "api_token": token,
            "interval": INTERVAL,
            "fmt": "json",
            "from": int((now - timedelta(days=2)).timestamp()),
            "to": int(now.timestamp()),
        },
        timeout=(10, 45),
    )
    response.raise_for_status()
    payload = response.json()
    return _normalized_rows(
        payload if isinstance(payload, list) else [], source="EODHD", ticker=EODHD[symbol]
    )


# one FMP plan-limit/quota hit blocks FMP for the rest of the run and, via fetch_log, for
# FMP_COOLDOWN across runs: the 5-minute fallback burned the free daily quota that the
# recession, earnings, news and COT-gate calls also need (prod 2026-10-09)
FMP_COOLDOWN = timedelta(hours=3)
_fmp_blocked = False


def _fallback_ready(symbol: str) -> bool:
    eodhd = symbol in EODHD and bool(os.environ.get("EODHD_API_TOKEN"))
    fmp = symbol in FMP and bool(os.environ.get("FMP_API_KEY")) and not _fmp_blocked
    return eodhd or fmp


def _fmp_cooling(conn, now: datetime) -> bool:
    since = (now - FMP_COOLDOWN).isoformat(timespec="seconds")
    return (
        conn.execute(
            "SELECT 1 FROM fetch_log WHERE ts >= ? AND fetcher = 'market' AND status = 'SKIPPED'"
            " AND target LIKE '%:FMP:5m' AND error LIKE 'plan-limited%' LIMIT 1",
            (since,),
        ).fetchone()
        is not None
    )


def _fmp_bars(symbol: str) -> list[dict]:
    global _fmp_blocked
    key = os.environ.get("FMP_API_KEY", "")
    if not key or symbol not in FMP:
        return []
    if _fmp_blocked:
        raise PlanLimited("plan-limited: FMP cooling down after a quota/plan limit")
    now = datetime.now(UTC)
    ticker = FMP[symbol]
    response = requests.get(
        "https://financialmodelingprep.com/stable/historical-chart/5min",
        params={
            "symbol": ticker,
            "from": (now - timedelta(days=2)).date().isoformat(),
            "to": now.date().isoformat(),
            "apikey": key,
        },
        timeout=(10, 45),
    )
    if response.status_code in PLAN_LIMIT_STATUSES:
        _fmp_blocked = True
        raise PlanLimited("plan-limited: FMP historical-chart/5min")
    response.raise_for_status()
    payload = response.json()
    return _normalized_rows(
        payload if isinstance(payload, list) else [], source="FMP", ticker=ticker
    )


def _provider_bars(symbol: str, now: datetime | None = None) -> ProviderSelection:
    now = now or datetime.now(UTC)
    attempts = []
    stale = []
    providers = (
        (("FMP", _fmp_bars), ("EODHD", _eodhd_bars))
        if symbol in ("BTCUSD", "ETHUSD")
        else (("EODHD", _eodhd_bars), ("FMP", _fmp_bars))
    )
    for source, fetch in providers:
        try:
            rows = fetch(symbol)
        except PlanLimited as ex:  # next provider; not a failure by itself
            attempts.append(ProviderAttempt(source, [], None, str(ex)))
            continue
        except requests.RequestException as ex:
            attempts.append(ProviderAttempt(source, [], None, _safe_error(ex)))
            continue
        if not rows:
            attempts.append(ProviderAttempt(source, [], Freshness("EMPTY", None, None, None)))
            continue
        try:
            freshness = assess_freshness(symbol, rows, now)
        except (KeyError, ValueError) as ex:
            freshness = Freshness("UNKNOWN", rows[-1].get("bar_ts_utc"), None, None)
            attempts.append(ProviderAttempt(source, rows, freshness, _safe_error(ex)))
            stale.append((source, rows, freshness))
            continue
        attempts.append(ProviderAttempt(source, rows, freshness))
        if freshness.status in ("FRESH", "CLOSED", "WAITING"):
            return ProviderSelection(source, rows, freshness, tuple(attempts))
        stale.append((source, rows, freshness))
    if stale:
        source, rows, freshness = max(
            stale, key=lambda item: max(row["bar_ts_utc"] for row in item[1])
        )
        raise UnusableBarsError(source, rows, freshness, tuple(attempts))
    errors = [item.error for item in attempts if item.error]
    if errors and all(e.startswith("plan-limited") for e in errors):
        raise PlanLimited("; ".join(errors))
    raise NoFallbackDataError(", ".join(f"{a.source}={a.error}" for a in attempts if a.error))


class NoFallbackDataError(RuntimeError):
    def __init__(self, errors: str = ""):
        self.errors = errors
        super().__init__("no mapped provider returned intraday bars")


class UnusableBarsError(RuntimeError):
    def __init__(
        self,
        source: str,
        rows: list[dict],
        freshness: Freshness,
        attempts: tuple[ProviderAttempt, ...],
    ):
        super().__init__(_freshness_error(freshness))
        self.source = source
        self.rows = rows
        self.freshness = freshness
        self.attempts = attempts

    def __str__(self) -> str:
        return _freshness_error(self.freshness)


def _record_bars(conn, symbol: str, source: str, rows: list[dict], freshness: Freshness) -> int:
    stored = _store(conn, symbol, source, rows)
    if freshness.status not in ("FRESH", "CLOSED", "WAITING"):
        log_collection(
            conn,
            "market",
            f"{symbol}:{source}:5m",
            rows[-1] if rows else None,
            len(rows),
            err=_freshness_error(freshness),
            status="DEGRADED",
        )
        return -1
    status = {"FRESH": "OK", "CLOSED": "CLOSED", "WAITING": "WAITING"}.get(
        freshness.status, "DEGRADED"
    )
    log_collection(
        conn,
        "market",
        f"{symbol}:{source}:5m",
        rows[-1] if rows else None,
        len(rows),
        status=status,
    )
    return stored


def _record_provider_attempts(
    conn, symbol: str, attempts: tuple[ProviderAttempt, ...], selected_source: str | None = None
) -> None:
    for attempt in attempts:
        if attempt.source == selected_source:
            continue
        if attempt.error:
            log_collection(
                conn,
                "market",
                f"{symbol}:{attempt.source}:5m",
                None,
                0,
                err=attempt.error,
                status="SKIPPED" if attempt.error.startswith("plan-limited") else "ERROR",
            )
        elif attempt.freshness and attempt.freshness.status not in (
            "FRESH",
            "CLOSED",
            "WAITING",
            "EMPTY",
        ):
            _record_bars(conn, symbol, attempt.source, attempt.rows, attempt.freshness)


def _breadth(conn, now: datetime | None = None) -> int:
    latest = {}
    for sector in SECTORS:  # one index seek each; the old IN (...) loaded every bar ever stored
        row = conn.execute(
            "SELECT symbol,bar_ts_utc,open,close FROM intraday_bars WHERE symbol=? AND"
            " interval=? AND source='YAHOO' ORDER BY bar_ts_utc DESC LIMIT 1",
            (sector, INTERVAL),
        ).fetchone()
        if row:
            latest[sector] = row
    if any(s not in latest or not latest[s][2] or not latest[s][3] for s in SECTORS):
        return 0
    stamps = {row[1] for row in latest.values()}
    if len(stamps) != 1 or any(
        assess_freshness(
            symbol,
            [{"bar_ts_utc": latest[symbol][1]}],
            now,
            regular_only=True,
        ).status
        not in ("FRESH", "CLOSED")
        for symbol in SECTORS
    ):
        return 0
    ret = {s: latest[s][3] / latest[s][2] - 1 for s in SECTORS}
    stamp = max(latest[s][1] for s in SECTORS)
    refs = conn.execute(
        "SELECT symbol,open,close FROM intraday_bars WHERE source='YAHOO' AND interval=? AND bar_ts_utc=? AND symbol IN ('SPY','RSP')",
        (INTERVAL, stamp),
    ).fetchall()
    refs = {r[0]: r[2] / r[1] - 1 for r in refs if r[1] and r[2]}
    adv, dec = sum(v > 0 for v in ret.values()), sum(v < 0 for v in ret.values())
    conn.execute(
        "INSERT OR REPLACE INTO market_breadth VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            stamp,
            "US_SECTOR_ETF_PROXY",
            "YAHOO",
            adv,
            dec,
            len(SECTORS) - adv - dec,
            adv / len(SECTORS),
            sum(ret.values()) / len(ret),
            refs.get("SPY"),
            json.dumps({"rsp_return": refs.get("RSP"), "sector_returns": ret}, sort_keys=True),
        ),
    )
    return 1


def _closed_with_final_bar(conn, symbol: str, now: datetime) -> bool:
    """Market closed and the stored bar already covers the session close: a fetch would only
    return the same bars (weekends: 29 of 31 symbols; nights: the equity ETFs)."""
    latest = conn.execute(
        "SELECT MAX(bar_ts_utc) FROM intraday_bars WHERE symbol=? AND interval=? AND source='YAHOO'",
        (symbol, INTERVAL),
    ).fetchone()[0]
    return bool(latest) and assess_freshness(symbol, [{"bar_ts_utc": latest}], now).status == (
        "CLOSED"
    )


def run(
    db_path: str = str(DEFAULT_DB),
    *,
    only: str | None = None,
    force_fallback: bool = False,
    collect_1m: bool = False,
) -> dict[str, int]:
    global _fmp_blocked
    conn = _db.get_conn(db_path, allow_init=True)
    result = {}
    now = datetime.now(UTC)
    _fmp_blocked = _fmp_cooling(conn, now)
    skipped_closed = 0
    for symbol, ticker in TRACKED.items():
        if only and symbol != only:
            continue
        if not force_fallback and _closed_with_final_bar(conn, symbol, now):
            result[symbol] = 0
            skipped_closed += 1
            continue
        try:
            if force_fallback:
                raise RuntimeError("forced fallback verification")
            rows = yahoo.fetch_intraday(ticker)
        except Exception as ex:
            primary_error = _safe_error(ex)
            rows = []
        else:
            primary_error = None

        primary_freshness = (
            assess_freshness(symbol, rows, now) if rows else Freshness("EMPTY", None, None, None)
        )
        if rows and primary_freshness.status in ("FRESH", "CLOSED", "WAITING"):
            result[symbol] = _record_bars(conn, symbol, "YAHOO", rows, primary_freshness)
            if collect_1m:
                try:
                    rows_1m = yahoo.fetch_intraday(ticker, interval="1m", range_="1d")
                    if rows_1m:
                        _store(conn, symbol, "YAHOO", rows_1m, interval="1m")
                except Exception:
                    pass
            continue
        if not rows:  # keep the cause: a Yahoo 429 or proxy outage is not an empty market
            status = "ERROR" if primary_error else "EMPTY"
            log_collection(
                conn, "market", f"{symbol}:YAHOO:5m", None, 0, err=primary_error, status=status
            )
        else:
            _record_bars(conn, symbol, "YAHOO", rows, primary_freshness)
        if primary_freshness.status == "WAITING":
            result[symbol] = 0
            continue
        if not _fallback_ready(symbol):  # no keyed provider maps it: nothing to try or log
            result[symbol] = -1  # degraded (stale or no bars), nothing better to try
            continue

        try:
            selection = _provider_bars(symbol, now)
            _record_provider_attempts(conn, symbol, selection.attempts, selection.source)
            result[symbol] = _record_bars(
                conn, symbol, selection.source, selection.rows, selection.freshness
            )
        except PlanLimited as ex:
            result[symbol] = 0
            log_collection(
                conn, "market", f"{symbol}:FMP:5m", None, 0, err=str(ex), status="SKIPPED"
            )
        except UnusableBarsError as ex:
            _record_provider_attempts(conn, symbol, ex.attempts, ex.source)
            result[symbol] = _record_bars(conn, symbol, ex.source, ex.rows, ex.freshness)
            print(f"{symbol} fallback degraded: {_freshness_error(ex.freshness)}")
        except Exception as ex:
            result[symbol] = -1
            summary = _safe_error(ex)
            print(f"{symbol} fallback unavailable: {summary}")
            log_collection(
                conn, "market", f"{symbol}:FALLBACK:5m", None, 0, err=summary, status="DEGRADED"
            )
    result["breadth"] = _breadth(conn, now)
    conn.close()
    if skipped_closed:
        print(f"{skipped_closed} closed market(s) already complete: not refetched")
    try:
        result.update({f"okx:{name}": count for name, count in collect_okx_market(db_path).items()})
    except Exception as ex:
        result["okx:collector"] = -1
        print(f"OKX market: {type(ex).__name__}: {str(ex)[:160]}")
    return result


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="arkwatch market")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--only", choices=sorted(TRACKED))
    parser.add_argument("--force-fallback", action="store_true")
    # 1m bars have no reader (signals use 5m, Zonelab never asks /v1/prices): opt-in only
    parser.add_argument("--with-1m", action="store_true", help="also store 1m bars")
    parser.add_argument("--no-1m", action="store_true", help="default; kept for old callers")
    args = parser.parse_args(argv)
    result = run(
        args.db,
        only=args.only,
        force_fallback=args.force_fallback,
        collect_1m=args.with_1m and not args.no_1m,
    )
    bad = [name for name, value in result.items() if value < 0]
    hard = [name for name in bad if name.startswith("okx:")]
    degraded = [name for name in bad if not name.startswith("okx:")]
    assets = [name for name in result if not name.startswith("okx:")]
    print(
        f"=== market timeline: {len(result) - len(bad)} completed, "
        f"{len(degraded)} degraded, {len(hard)} failed ==="
    )
    # Stale/future after-hours bars degrade a few ETFs every night: that is data quality, not a job
    # failure (a failure pages the owner). Fail on a collector error or a broad outage (> half).
    return 1 if hard or (assets and len(degraded) > len(assets) / 2) else 0
