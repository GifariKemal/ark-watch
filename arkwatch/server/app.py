"""app.py — read-mostly REST API over the ark-watch SQLite DB (BFF backend).

Run: uvicorn arkwatch.server.app:app --host 127.0.0.1 --port 8000

- Every /v1 route needs header x-arkwatch-key == env ARKWATCH_API_KEY
  (>= 32 chars, checked at startup; ARKWATCH_ALLOW_NO_AUTH=1 only for tests).
  /v1/health is unauthenticated. No CORS (server-to-server only).
- Reads use a read-only connection per request (sync endpoints run in the
  threadpool; WAL lets them run beside the daemon's writer).
- Writes are few, allowlisted, audited (audit_log) and idempotent. Heavy or
  long work is never run in a request: POST /v1/jobs only queues it for the
  daemon (qa/jobs_runner.run_pending_jobs).
- DB path: env ARKWATCH_DB, else $ARKWATCH_DATA_DIR/arkwatch.db, else repo data/.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request, Security
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import api, db
from .models import (
    CalendarEventOut,
    CancelIn,
    CancelOut,
    CotRowOut,
    ErrorOut,
    FreshnessReport,
    HealthOut,
    JobEnqueueOut,
    JobIn,
    JobOut,
    LevelsOut,
    NewsOut,
    ObservationOut,
    OutboxOut,
    Page,
    PerformanceOut,
    PlaybookDetailOut,
    PlaybookOut,
    PriceBarOut,
    RadarOut,
    RegimeOut,
    RetryOut,
    SeriesDetailOut,
    SeriesOut,
    SeriesPatchIn,
    SeriesPatchOut,
    SignalOut,
    SignalPointOut,
)

log = logging.getLogger("arkwatch.server")
CACHE_TTL_S = 45
HEARTBEAT_OK_S = 600
MIN_KEY_LEN = 32


class ServerError(Exception):
    def __init__(self, status: int, code: str, error: str, detail: Any = None):
        self.status, self.code, self.error, self.detail = status, code, error, detail


def _err(status: int, code: str, error: str, detail: Any = None) -> JSONResponse:
    body = {"error": error, "code": code, "detail": jsonable_encoder(detail)}
    return JSONResponse(status_code=status, content=body)


@asynccontextmanager
async def lifespan(app: FastAPI):
    key = os.environ.get("ARKWATCH_API_KEY", "").strip()
    # the no-auth switch only exists for the test suite: a stray env line must not open prod
    no_auth_ok = os.environ.get("ARKWATCH_ALLOW_NO_AUTH") == "1" and bool(
        os.environ.get("PYTEST_CURRENT_TEST")
    )
    if len(key) < MIN_KEY_LEN and not no_auth_ok:
        raise RuntimeError(
            f"ARKWATCH_API_KEY must be set to >= {MIN_KEY_LEN} chars "
            "(ARKWATCH_ALLOW_NO_AUTH=1 disables auth, tests only)"
        )
    app.state.api_key = key or None
    app.state.db_path = db.default_db_path()
    app.state.cache, app.state.cache_lock = {}, threading.Lock()
    app.state.probe, app.state.probe_lock = None, threading.Lock()
    yield
    if app.state.probe is not None:
        app.state.probe.close()


_ERR = {s: {"model": ErrorOut} for s in (400, 401, 404, 409, 422, 503)}
app = FastAPI(
    title="ark-watch API",
    version="1.0.0",
    lifespan=lifespan,
    responses=_ERR,
    # no unauthenticated schema disclosure; `export_openapi` calls app.openapi() directly
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# --- error shape {error, code, detail} -------------------------------------

_HTTP_CODES = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}


@app.exception_handler(ServerError)
def _server_error(_: Request, exc: ServerError) -> JSONResponse:
    return _err(exc.status, exc.code, exc.error, exc.detail)


@app.exception_handler(StarletteHTTPException)
def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _err(exc.status_code, _HTTP_CODES.get(exc.status_code, "http_error"), str(exc.detail))


@app.exception_handler(RequestValidationError)
def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _err(422, "validation_error", "request validation failed", exc.errors())


@app.exception_handler(api.ApiNotFound)
def _not_found(_: Request, exc: api.ApiNotFound) -> JSONResponse:
    return _err(404, "not_found", str(exc))


@app.exception_handler(api.ApiConflict)
def _conflict(_: Request, exc: api.ApiConflict) -> JSONResponse:
    return _err(409, "conflict", str(exc))


@app.exception_handler(api.ApiBadRequest)
def _bad_request(_: Request, exc: api.ApiBadRequest) -> JSONResponse:
    return _err(400, "bad_request", str(exc))


@app.exception_handler(Exception)
def _internal(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return _err(500, "internal", "internal server error")


# --- dependencies ----------------------------------------------------------

_key_header = APIKeyHeader(name="x-arkwatch-key", auto_error=False)


def require_key(request: Request, key: Annotated[str | None, Security(_key_header)]) -> None:
    expected = request.app.state.api_key
    if expected is None:
        return
    if not key or not hmac.compare_digest(key.encode(), expected.encode()):
        raise ServerError(401, "unauthorized", "missing or invalid API key")


def _open(request: Request, *, read_only: bool) -> sqlite3.Connection:
    path = request.app.state.db_path
    try:
        if not path.exists():  # a writer get_conn would CREATE the file
            raise FileNotFoundError(path)
        return db.get_conn(path, read_only=read_only)
    except (OSError, RuntimeError, sqlite3.Error) as ex:
        log.error("db open failed: %s", ex)
        raise ServerError(503, "db_unavailable", "database unavailable") from None


def read_conn(request: Request) -> Iterator[sqlite3.Connection]:
    c = _open(request, read_only=True)
    try:
        yield c
    finally:
        c.close()


def write_conn(request: Request) -> Iterator[sqlite3.Connection]:
    c = _open(request, read_only=False)
    try:
        yield c
    finally:
        c.close()


Conn = Annotated[sqlite3.Connection, Depends(read_conn)]
WConn = Annotated[sqlite3.Connection, Depends(write_conn)]
Actor = Annotated[
    str,
    Header(alias="x-arkwatch-actor", max_length=64, pattern=r"^[\w.@:-]+$"),
]
Cursor = Annotated[str | None, Query(max_length=512)]
FromTs = Annotated[str | None, Query(alias="from", max_length=32)]
ToTs = Annotated[str | None, Query(alias="to", max_length=32)]


def cached(request: Request, name: str, args: tuple, fn: Callable[[], Any]) -> Any:
    """TTL cache for compute-heavy reads, keyed by args + PRAGMA data_version
    of one long-lived probe connection (data_version is only comparable
    within a connection; it moves whenever any other connection commits)."""
    st = request.app.state
    with st.probe_lock:
        if st.probe is None:
            st.probe = db.get_conn(st.db_path, read_only=True)
        version = st.probe.execute("PRAGMA data_version").fetchone()[0]
    key, now = (name, args, version), time.monotonic()
    with st.cache_lock:
        hit = st.cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    value = fn()
    with st.cache_lock:
        if len(st.cache) > 256:
            st.cache.clear()
        st.cache[key] = (now, value)
    return value


# --- health (no auth) ------------------------------------------------------


def _heartbeat_age_s() -> float | None:
    try:
        beat = (db.data_dir() / "daemon_heartbeat").read_text().strip()
        return (datetime.now(UTC) - datetime.fromisoformat(beat)).total_seconds()
    except (OSError, ValueError):
        return None


def _daemon_succeeded_today() -> list[str]:
    try:
        state = json.loads((db.data_dir() / "daemon_state.json").read_text())
    except (OSError, ValueError):
        return []
    # keys look like "0600-harvest@2026-10-08" (successes of the WIB day only)
    return sorted(k.split("-", 1)[-1].rsplit("@", 1)[0] for k in state)


@app.get(
    "/v1/health",
    response_model=HealthOut,
    tags=["health"],
    responses={503: {"model": HealthOut}},
)
def health(request: Request):
    base = {"expected_schema_version": db.SCHEMA_VERSION, "heartbeat_age_s": _heartbeat_age_s()}
    path = request.app.state.db_path
    try:
        if not path.exists():
            raise FileNotFoundError(path)
        c = db.get_conn(path, read_only=True)
    except FileNotFoundError:
        reason = "db missing"
    except RuntimeError:
        reason = "schema mismatch"
    except sqlite3.Error:
        reason = "db error"
    else:
        try:
            jobs = c.execute(
                "SELECT kind, status FROM jobs WHERE id IN (SELECT MAX(id) FROM jobs GROUP BY kind)"
            ).fetchall()
            fresh = cached(request, "freshness", (), lambda: api.data_freshness(c))
            age = base["heartbeat_age_s"]
            return HealthOut(
                **base,
                status="ok" if age is not None and age < HEARTBEAT_OK_S else "degraded",
                db_readable=True,
                schema_version=db._schema_version(c),
                daemon_succeeded_today=_daemon_succeeded_today(),
                jobs_last_status={r[0]: r[1] for r in jobs},
                freshness=dict(Counter(r["status"] for r in fresh)),
            )
        finally:
            c.close()
    body = HealthOut(**base, status="down", db_readable=False, detail=reason)
    return JSONResponse(status_code=503, content=body.model_dump())


# --- /v1 (authenticated) ---------------------------------------------------

v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_key)])


@v1.get("/series", response_model=Page[SeriesOut], tags=["series"])
def series_list(
    c: Conn,
    block: Annotated[str | None, Query(max_length=32)] = None,
    active: bool | None = None,
    freq: Annotated[str | None, Query(max_length=4)] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    return api.list_series(c, block=block, active=active, freq=freq, cursor=cursor, limit=limit)


@v1.get("/series/{series_id}", response_model=SeriesDetailOut, tags=["series"])
def series_get(c: Conn, series_id: Annotated[str, Path(max_length=128)]):
    out = api.series_detail(c, series_id)
    if out is None:
        raise api.ApiNotFound(f"series '{series_id}' not found")
    return out


@v1.get("/series/{series_id}/observations", response_model=Page[ObservationOut], tags=["series"])
def series_observations(
    c: Conn,
    series_id: Annotated[str, Path(max_length=128)],
    start: FromTs = None,
    end: ToTs = None,
    vintage: Annotated[str, Query(max_length=32)] = "realtime",
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
):
    return api.get_observations(
        c, series_id, start=start, end=end, vintage=vintage, cursor=cursor, limit=limit
    )


@v1.get("/signals", response_model=Page[SignalOut], tags=["signals"])
def signals_list(
    c: Conn,
    prefix: Annotated[str | None, Query(max_length=64)] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    return api.list_signals(c, prefix=prefix, cursor=cursor, limit=limit)


@v1.get("/signals/{signal_id}/history", response_model=Page[SignalPointOut], tags=["signals"])
def signals_history(
    c: Conn,
    signal_id: Annotated[str, Path(max_length=128)],
    start: FromTs = None,
    end: ToTs = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
):
    return api.signal_history(c, signal_id, start=start, end=end, cursor=cursor, limit=limit)


@v1.get("/regime", response_model=RegimeOut, tags=["intelligence"])
def regime(request: Request, c: Conn):
    return cached(request, "regime", (), lambda: api.get_regime_snapshot(c))


@v1.get("/sessions/{symbol}/levels", response_model=LevelsOut, tags=["intelligence"])
def session_levels(request: Request, c: Conn, symbol: Annotated[str, Path(max_length=16)]):
    raw = cached(request, "levels", (symbol.upper(),), lambda: api.get_session_levels(symbol, c))
    if raw is None:
        raise api.ApiNotFound(f"no intraday bars for '{symbol}'")
    raw = dict(raw)
    levels, status = {}, {}
    for k, v in (raw.pop("levels", None) or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            levels[k] = v
        elif isinstance(v, str):  # e.g. OR15_HIGH = "FORMING_IN_RTH"
            levels[k], status[k] = None, v
        else:  # lists/dicts (single prints, ...) are context, not levels
            raw.setdefault("level_context", {})[k] = v
    head = {k: raw.pop(k, None) for k in ("symbol", "as_of", "last_price", "last_bar_utc")}
    return {**head, "levels": levels, "level_status": status, "context": raw}


@v1.get("/playbooks", response_model=Page[PlaybookOut], tags=["playbooks"])
def playbooks_list(
    c: Conn,
    state: Annotated[str | None, Query(max_length=32)] = None,
    symbol: Annotated[str | None, Query(max_length=16)] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    return api.list_playbooks(c, state=state, symbol=symbol, cursor=cursor, limit=limit)


@v1.get("/playbooks/{uid}", response_model=PlaybookDetailOut, tags=["playbooks"])
def playbooks_get(c: Conn, uid: Annotated[str, Path(max_length=200)]):
    out = api.playbook_detail(c, uid)
    if out is None:
        raise api.ApiNotFound(f"playbook scenario '{uid}' not found")
    return out


@v1.get("/playbook/performance", response_model=PerformanceOut, tags=["playbooks"])
def playbook_performance(
    c: Conn,
    symbol: Annotated[str | None, Query(max_length=16)] = None,
    horizon: Annotated[str | None, Query(max_length=16)] = None,
):
    return api.get_playbook_performance(symbol=symbol, horizon=horizon, conn=c)


@v1.get("/calendar", response_model=Page[CalendarEventOut], tags=["markets"])
def calendar(
    c: Conn,
    days_forward: Annotated[int, Query(ge=0, le=90)] = 7,
    days_backward: Annotated[int, Query(ge=0, le=90)] = 3,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
):
    return api.list_calendar(c, days_forward, days_backward, cursor=cursor, limit=limit)


@v1.get("/news", response_model=Page[NewsOut], tags=["markets"])
def news(
    c: Conn,
    source: Annotated[str | None, Query(max_length=32)] = None,
    symbol: Annotated[str | None, Query(max_length=32)] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
):
    return api.list_news(c, source=source, symbol=symbol, cursor=cursor, limit=limit)


@v1.get("/sentiment", response_model=dict[str, RadarOut], tags=["intelligence"])
def sentiment(request: Request, c: Conn, window_days: Annotated[int, Query(ge=1, le=30)] = 3):
    return cached(
        request,
        "sentiment",
        (window_days,),
        lambda: api.get_all_sentiment_radars(c, window_days=window_days),
    )


@v1.get("/cot/{contract}", response_model=list[CotRowOut], tags=["markets"])
def cot(
    c: Conn,
    contract: Annotated[str, Path(max_length=32)],
    window: Annotated[int, Query(ge=1, le=520)] = 52,
):
    return api.get_cot(c, contract, window=window)


@v1.get("/prices/{symbol}", response_model=Page[PriceBarOut], tags=["markets"])
def prices(
    c: Conn,
    symbol: Annotated[str, Path(max_length=32)],
    interval: Annotated[str, Query(pattern=r"^\d{1,3}[mhd]$")] = "1d",
    start: FromTs = None,
    end: ToTs = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
):
    return api.get_prices(
        c, symbol, interval=interval, start=start, end=end, cursor=cursor, limit=limit
    )


@v1.get("/outbox", response_model=Page[OutboxOut], tags=["ops"])
def outbox(
    c: Conn,
    status: Annotated[str | None, Query(max_length=16)] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    return api.list_outbox(c, status=status, cursor=cursor, limit=limit)


@v1.get("/freshness", response_model=FreshnessReport, tags=["ops"])
def freshness(request: Request, c: Conn):
    items = cached(request, "freshness", (), lambda: api.data_freshness(c))
    return {"summary": dict(Counter(r["status"] for r in items)), "items": items}


@v1.get("/jobs", response_model=Page[JobOut], tags=["ops"])
def jobs(c: Conn, cursor: Cursor = None, limit: Annotated[int, Query(ge=1, le=1000)] = 100):
    return api.jobs_status(c, cursor=cursor, limit=limit)


@v1.get("/jobs/{job_id}", response_model=JobOut, tags=["ops"])
def job_get(c: Conn, job_id: int):
    out = api.job_detail(c, job_id)
    if out is None:
        raise api.ApiNotFound(f"job {job_id} not found")
    return out


# --- writes (audited, idempotent) ------------------------------------------


@v1.post("/playbooks/{uid}/cancel", response_model=CancelOut, tags=["playbooks"])
def playbooks_cancel(
    c: WConn, uid: Annotated[str, Path(max_length=200)], body: CancelIn, actor: Actor = "api"
):
    return api.cancel_playbook(c, uid, actor=actor, note=body.note, exit_price=body.exit_price)


@v1.post("/outbox/{outbox_id}/retry", response_model=RetryOut, tags=["ops"])
def outbox_retry(c: WConn, outbox_id: int, actor: Actor = "api"):
    return api.retry_outbox(c, outbox_id, actor=actor)


@v1.patch(
    "/series/{series_id}",
    response_model=SeriesPatchOut,
    tags=["series"],
    description="Sets series_registry.active and locks it against the YAML registry sync "
    "(locked_by_ui). YAML stays the source of truth for every other column; the harvest "
    "selects series from the YAML, so this toggles what the brief/API treat as active.",
)
def series_patch(
    c: WConn,
    series_id: Annotated[str, Path(max_length=128)],
    body: SeriesPatchIn,
    actor: Actor = "api",
):
    return api.set_series_active(c, series_id, body.active, actor=actor)


@v1.post("/jobs", response_model=JobEnqueueOut, status_code=202, tags=["ops"])
def jobs_enqueue(c: WConn, body: JobIn, actor: Actor = "api"):
    return api.enqueue_job(c, body.kind, actor=actor)


app.include_router(v1)
