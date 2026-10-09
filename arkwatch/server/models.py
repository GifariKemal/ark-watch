"""Pydantic v2 response/request models of the REST API (the OpenAPI contract
Zonelab generates its types from). Numeric fields use `Num`: SQLite columns
are dynamically typed and some compute outputs mix numbers with status
strings, so anything non-numeric (or NaN/inf) becomes null."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ..qa.jobs_runner import JOB_KINDS


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


Num = Annotated[float | None, BeforeValidator(_num)]
FreshnessStatus = Literal["fresh", "late", "stale", "never", "unconfigured", "plan_limited"]
JobKind = Literal[tuple(JOB_KINDS)]  # type: ignore[valid-type]


class ErrorOut(BaseModel):
    error: str
    code: str
    detail: Any = None


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class SeriesOut(BaseModel):
    series_id: str
    name: str
    block: str
    tier: int
    unit: str
    value_format: str
    freq: str
    primary_source: str
    secondary_source: str | None = None
    active: bool
    calendar_family: str | None = None
    locked_by_ui: bool


class Freshness(BaseModel):
    expected_lag_days: int
    age_days: int | None = None
    status: FreshnessStatus


class SeriesDetailOut(SeriesOut):
    ts_convention: str | None = None
    release_schedule: str | None = None
    expected_start: str | None = None
    sanity_min: Num = None
    sanity_max: Num = None
    tolerance: Num = None
    last_ts: str | None = None
    n_obs: int
    freshness: Freshness


class SeriesFreshnessOut(Freshness):
    series_id: str
    freq: str
    last_ts: str | None = None


class FreshnessReport(BaseModel):
    summary: dict[str, int]
    items: list[SeriesFreshnessOut]


class ObservationOut(BaseModel):
    ts: str
    source: str
    value: Num = None
    release_ts: str | None = Field(None, description="null = release time unknown")
    vintage_ts: str


class SignalOut(BaseModel):
    signal_id: str
    last_ts: str
    value: Num = None
    state: str | None = None
    computed_at: str | None = None


class SignalPointOut(BaseModel):
    ts: str
    value: Num = None
    state: str | None = None
    run_id: str | None = None
    computed_at: str | None = None


class PillarOut(BaseModel):
    label: str | None = None
    z_score: Num = None
    state: str | None = None
    detail: str | None = None
    parts: list[Any] = []


class RegimeOut(BaseModel):
    regime_score: Num = None
    label: str
    quadrant: str | None = None
    dollar_smile: str | None = None
    pillars: dict[str, PillarOut]


class LevelsOut(BaseModel):
    symbol: str
    as_of: str | None = None
    last_price: Num = None
    last_bar_utc: str | None = None
    levels: dict[str, Num] = Field(description="null when not numeric, see level_status")
    level_status: dict[str, str] = Field(description="status text of non-numeric levels")
    context: dict[str, Any] = {}


class PlaybookOut(BaseModel):
    scenario_uid: str
    symbol: str
    horizon: str
    direction: str
    scenario_id: str
    title: str
    trigger_condition: str
    trigger_price: Num = None
    target_profit: Num = None
    invalidation_level: Num = None
    risk_reward_ratio: Num = None
    created_at_utc: str
    session_id: str
    state: str
    triggered_at_utc: str | None = None
    resolved_at_utc: str | None = None
    entry_price: Num = None
    exit_price: Num = None
    mfe_points: Num = None
    mae_points: Num = None
    pnl_points: Num = None
    r_multiple: Num = None
    cfd_basis_offset: Num = None
    note: str | None = None


class PlaybookDetailOut(PlaybookOut):
    payload: Any = None


class PerformanceOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    total_scenarios: int
    win_rate_pct: Num = None
    profit_factor: Num = None
    completed_trades: int
    wins: int
    losses: int
    pending: int
    active: int
    avg_r_multiple: Num = None
    avg_mfe: Num = None
    avg_mae: Num = None


class CalendarEventOut(BaseModel):
    event_uid: str
    ts_utc: str
    country: str
    name: str
    importance: str | None = None
    actual: Num = None
    consensus: Num = None
    previous: Num = None
    surprise_z: Num = None


class NewsOut(BaseModel):
    news_id: str
    published_at_utc: str
    source: str
    title: str
    url: str | None = None
    summary: str | None = None
    symbols: list[str]
    cluster_id: str
    relevance: Num = None
    novelty: Num = None


class RadarOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    asset: str
    sample_count: int
    net_stance_score: Num = None
    stance: str
    confidence: Num = None
    as_of: str | None = None


class CotRowOut(BaseModel):
    report_date: str
    report_type: str
    category: str
    release_ts: str | None = None
    long: Num = None
    short: Num = None
    spread: Num = None
    open_interest_all: Num = None
    pct_of_oi: Num = None
    change_long: Num = None
    change_short: Num = None
    traders_long: Num = None
    traders_short: Num = None
    conc_top4_long: Num = None
    conc_top4_short: Num = None


class PriceBarOut(BaseModel):
    ts: str
    source: str
    open: Num = None
    high: Num = None
    low: Num = None
    close: Num = None
    volume: Num = None


class OutboxOut(BaseModel):
    id: int
    brief_date: str
    channel: str
    status: str
    attempts: int | None = None
    last_error: str | None = None
    sent_at: str | None = None
    created_at: str
    claimed_at: str | None = None


class JobOut(BaseModel):
    id: int
    kind: str
    params: dict[str, Any]
    status: Literal["queued", "running", "done", "failed"]
    requested_by: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


AlertStatus = Literal["pending", "sending", "sent", "skipped", "failed"]


class AlertOut(BaseModel):
    id: int
    alert_type: str
    priority: str
    status: str
    triggered_at: str
    message: str | None = Field(None, description="plain text, not HTML-escaped")
    last_error: str | None = None


class AlertCounts(BaseModel):
    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]


class AlertsSummaryOut(BaseModel):
    last_24h: AlertCounts
    last_7d: AlertCounts
    newest_triggered_at: str | None = None


class BriefListOut(BaseModel):
    date: str
    regime_score: Num = None
    generated_at: str | None = None
    chars: int


class BriefOut(BaseModel):
    date: str
    regime_score: Num = None
    generated_at: str | None = None
    markdown: str = Field(description="raw markdown, rendered by the client")


class GraphNodeOut(BaseModel):
    id: str
    kind: Literal["regime", "pillar", "series", "asset", "scenario", "event"]
    label: str
    group: str | None = None
    value: Num = None
    status: Literal[FreshnessStatus, "live", "pending", "n/a"]
    weight: float = Field(ge=0, le=1, description="importance hint")
    meta: dict[str, Any] = {}


class GraphLinkOut(BaseModel):
    source: str
    target: str
    kind: Literal["pillar_of", "series_in", "drives", "trades", "scheduled"]
    weight: float


class GraphOut(BaseModel):
    generated_at: str
    counts: dict[str, int]
    nodes: list[GraphNodeOut]
    links: list[GraphLinkOut]


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "down"]
    db_readable: bool
    detail: str | None = None
    schema_version: int | None = None
    expected_schema_version: int
    heartbeat_age_s: Num = None
    daemon_succeeded_today: list[str] = []
    jobs_last_status: dict[str, str] = {}
    freshness: dict[str, int] = {}


# --- write bodies / results -------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CancelIn(_In):
    note: str = Field(min_length=1, max_length=500)
    exit_price: float | None = Field(None, gt=0, allow_inf_nan=False)


class SeriesPatchIn(_In):
    active: bool


class JobIn(_In):
    kind: JobKind


class CancelOut(BaseModel):
    changed: bool
    scenario: PlaybookDetailOut


class RetryOut(BaseModel):
    changed: bool
    item: OutboxOut


class SeriesPatchOut(BaseModel):
    changed: bool
    series: SeriesDetailOut


class JobEnqueueOut(BaseModel):
    changed: bool
    job: JobOut
