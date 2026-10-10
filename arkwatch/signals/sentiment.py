"""sentiment.py — LLM-powered multi-asset sentiment radar and macro stance extraction.

Processes curated market news into structured multi-dimensional macro stance records
per asset, providing auditable evidence quotes and transmission channel analysis
without black-box opacity.
"""

from __future__ import annotations

import json
import math
import sqlite3
import string
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, field_validator

from ..fetchers.nlp import _call, _config, _extract_json
from .asof import parse_as_of

# |net score| at which a radar is labelled BULLISH/BEARISH; playbook.py triggers on the same value
STANCE_THRESHOLD = 0.20
# Pseudo-weight of a neutral (0) prior. A plain weighted mean divides the decay back out, so a
# single stale article kept its full score; shrinkage lets total evidence weight set conviction.
PRIOR_WEIGHT = 0.5

TRACKED_ASSETS = (
    "NQ1",  # Nasdaq 100 / Tech
    "ES1",  # S&P 500
    "YM1",  # Dow Jones
    "GC1",  # Gold (XAUUSD)
    "SI1",  # Silver (XAGUSD)
    "HG1",  # Copper (XCUUSD)
    "CL1",  # WTI Crude Oil
    "BZ1",  # Brent Crude Oil
    "DXY",  # US Dollar Index
    "EURUSD",  # Euro / US Dollar
    "GBPUSD",  # British Pound / US Dollar
    "USDJPY",  # US Dollar / Yen
    "BTCUSD",  # Bitcoin
    "ETHUSD",  # Ethereum
)

ASSET_ALIASES: dict[str, str] = {
    "XAU": "GC1",
    "XAUUSD": "GC1",
    "GOLD": "GC1",
    "XAG": "SI1",
    "XAGUSD": "SI1",
    "SILVER": "SI1",
    "XCU": "HG1",
    "XCUUSD": "HG1",
    "COPPER": "HG1",
    "WTI": "CL1",
    "OIL": "CL1",
    "CRUDE": "CL1",
    "BRENT": "BZ1",
    "NASDAQ": "NQ1",
    "QQQ": "NQ1",
    "SP500": "ES1",
    "SPY": "ES1",
    "DOW": "YM1",
    "DIA": "YM1",
    "DOLLAR": "DXY",
    "USD": "DXY",
    "BTC": "BTCUSD",
    "BITCOIN": "BTCUSD",
    "ETH": "ETHUSD",
    "ETHEREUM": "ETHUSD",
    "GBP": "GBPUSD",
    "CABLE": "GBPUSD",
}

AUTHORITY_SOURCES = {"RSS_FED", "RSS_BOE", "RSS_TREASURY", "RSS_SEC", "RSS_OILPRICE"}

EXTRACTION_SYSTEM_PROMPT = """You are a senior US-macro and multi-asset swing trading strategist.
Analyze financial news for a multi-asset book:
- US Indices: NQ1 (Nasdaq), ES1 (S&P 500), YM1 (Dow)
- Metals: GC1/XAU (Gold), SI1 (Silver), HG1 (Copper)
- Energy: CL1 (WTI), BZ1 (Brent)
- FX: DXY (US Dollar), EURUSD, USDJPY
- Crypto: BTCUSD, ETHUSD

Evaluate multi-dimensional transmission:
1. Impacted assets: which assets in the book are directly or indirectly impacted?
2. Stance: BULLISH (+1), BEARISH (-1), or NEUTRAL (0).
3. Magnitude: STRONG (1.0), MODERATE (0.5), WEAK (0.2).
4. Macro Channel: RATES_POLICY | GROWTH_DEMAND | LIQUIDITY_FINANCIAL | SUPPLY_SHOCK | GEOPOLITICAL_RISK | REGULATORY_LEGAL.
5. Horizon: INTRADAY_VOLATILITY (hours) | SWING_MULTIDAY (days/weeks) | STRUCTURAL_LONGTERM (months).
6. Evidence Level: OBSERVED (official data print) | SOURCED (official spokesperson/leadership statement) | INFERRED (analyst opinion/market commentary).
7. Evidence Quote: exact sentence snippet justifying the conclusion.

Respond ONLY with valid JSON (no markdown):
{
  "article_analysis": "one sentence summary of market significance",
  "primary_channel": "<primary macro channel>",
  "evidence_level": "OBSERVED|SOURCED|INFERRED",
  "asset_impacts": [
    {
      "asset": "<ticker from book>",
      "stance": "BULLISH|BEARISH|NEUTRAL",
      "magnitude": 0.2|0.5|1.0,
      "confidence": 0.0-1.0,
      "horizon": "INTRADAY_VOLATILITY|SWING_MULTIDAY|STRUCTURAL_LONGTERM",
      "transmission_rationale": "brief mechanism explanation",
      "evidence_quote": "exact quote from text"
    }
  ]
}"""


_Text = Annotated[str, BeforeValidator(lambda v: "" if v is None else str(v).strip())]


def _upper(v: Any) -> str:
    return str(v).strip().upper()


_Upper = Annotated[str, BeforeValidator(_upper)]


class _Impact(BaseModel):
    """One LLM asset impact. Anything off-schema raises, so the caller skips that item only."""

    model_config = ConfigDict(allow_inf_nan=False)

    asset: _Text
    stance: Annotated[Literal["BULLISH", "BEARISH", "NEUTRAL"], BeforeValidator(_upper)] = "NEUTRAL"
    magnitude: float = 0.5
    confidence: float = 0.7
    horizon: _Upper = "SWING_MULTIDAY"
    macro_channel: _Text = ""
    evidence_level: _Text = ""
    evidence_quote: _Text = ""
    transmission_rationale: _Text = ""

    @field_validator("magnitude")
    @classmethod
    def _clamp_mag(cls, v: float) -> float:
        return max(0.1, min(1.0, v))

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


# ends of an LLM quote often carry quote marks / ellipsis / a full stop the source lacks
_QUOTE_EDGE = string.punctuation + string.whitespace + "\u201c\u201d\u2018\u2019\u2026"
UNGROUNDED_CONFIDENCE = 0.5  # a quote not found in the article is the model's words, not evidence


def _squash(text: str) -> str:
    return " ".join(text.casefold().split())


def _grounded(quote: str, title: str, summary: str | None) -> bool:
    """Is the evidence quote (normalised) really a substring of the article the LLM was shown?"""
    q = _squash(quote).strip(_QUOTE_EDGE)
    return bool(q) and q in _squash(f"{title} {summary or ''}")


def _normalize_asset(ticker: str) -> str | None:
    t = ticker.strip().upper()
    if t in TRACKED_ASSETS:
        return t
    return ASSET_ALIASES.get(t)


def _net_stance(weighted_score: float, total_weight: float) -> tuple[float, str]:
    """Weighted mean shrunk toward 0 by PRIOR_WEIGHT, clamped, plus its stance label."""
    net = max(-1.0, min(1.0, round(weighted_score / (total_weight + PRIOR_WEIGHT), 3)))
    if net >= STANCE_THRESHOLD:
        return net, "BULLISH"
    if net <= -STANCE_THRESHOLD:
        return net, "BEARISH"
    return net, "NEUTRAL"


def extract_news_intelligence(
    conn: sqlite3.Connection,
    *,
    limit: int = 15,
    min_relevance: float = 0.40,
    min_novelty: float = 0.35,
    cfg: dict[str, Any] | None = None,
) -> int:
    """Extract multi-asset structured stances from pending articles in market_news."""
    if cfg is None:
        from ..config import nlp_missing

        if skip := nlp_missing():  # one line, not a keyless HTTP call per article
            print(f"  · NLP skipped ({skip})")
            return 0
        cfg = _config()

    pending = conn.execute(
        """
        SELECT m.news_id, m.source, m.title, m.summary, m.published_at_utc
        FROM market_news m
        WHERE NOT EXISTS (
            SELECT 1 FROM news_intelligence ni WHERE ni.news_id = m.news_id
        )
        AND (
            (m.relevance >= ? AND m.novelty >= ?)
            OR m.source IN ('RSS_FED', 'RSS_BOE', 'RSS_TREASURY', 'RSS_SEC', 'RSS_OILPRICE')
        )
        ORDER BY m.published_at_utc DESC
        LIMIT ?
        """,
        (min_relevance, min_novelty, limit),
    ).fetchall()

    if not pending:
        return 0

    now_utc = datetime.now(UTC).isoformat(timespec="seconds")
    processed_count = 0
    failures: list[str] = []
    invalid = ungrounded = 0

    for news_id, source, title, summary, pub_utc in pending:
        user_prompt = f"SOURCE: {source}\nTITLE: {title}\nSUMMARY: {summary or ''}"
        try:
            raw_resp = _call(cfg, system=EXTRACTION_SYSTEM_PROMPT, user=user_prompt)
            payload = _extract_json(raw_resp)
        except Exception as ex:
            failures.append(f"{type(ex).__name__}: {ex}")
            continue

        if not isinstance(payload, dict):
            continue

        impacts = payload.get("asset_impacts", [])
        if not isinstance(impacts, list):
            continue

        inserted_for_article = 0
        for raw in impacts:
            try:
                imp = _Impact.model_validate(raw)
            except ValidationError:
                invalid += 1  # one bad item ("magnitude": null) must not abort the batch
                continue
            norm_asset = _normalize_asset(imp.asset)
            if not norm_asset:
                continue

            confidence = imp.confidence
            channel = (
                imp.macro_channel or str(payload.get("primary_channel") or "") or "GROWTH_DEMAND"
            ).upper()

            evidence_level = (
                imp.evidence_level or str(payload.get("evidence_level") or "") or "INFERRED"
            ).upper()
            if evidence_level not in ("OBSERVED", "SOURCED", "INFERRED"):
                evidence_level = "INFERRED"

            quote, rationale = imp.evidence_quote, imp.transmission_rationale
            if not _grounded(quote, title, summary):
                # the radar weighs OBSERVED/SOURCED higher and shows the quote: do not trust it
                ungrounded += 1
                evidence_level = "INFERRED"
                confidence = round(confidence * UNGROUNDED_CONFIDENCE, 3)

            try:
                conn.execute(
                    """
                    INSERT INTO news_intelligence (
                        news_id, asset, stance, magnitude, confidence,
                        macro_channel, impact_horizon, evidence_level,
                        evidence_quote, transmission_rationale,
                        created_at, published_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(news_id, asset) DO UPDATE SET
                        stance=excluded.stance,
                        magnitude=excluded.magnitude,
                        confidence=excluded.confidence,
                        macro_channel=excluded.macro_channel,
                        impact_horizon=excluded.impact_horizon,
                        evidence_level=excluded.evidence_level,
                        evidence_quote=excluded.evidence_quote,
                        transmission_rationale=excluded.transmission_rationale
                    """,
                    (
                        news_id,
                        norm_asset,
                        imp.stance,
                        imp.magnitude,
                        confidence,
                        channel,
                        imp.horizon,
                        evidence_level,
                        quote,
                        rationale,
                        now_utc,
                        pub_utc,
                    ),
                )
                inserted_for_article += 1
            except sqlite3.Error:
                pass

        if inserted_for_article > 0:
            processed_count += 1
            conn.commit()

    if invalid or ungrounded or failures:
        print(
            f"  · NLP: {len(failures)} failed calls, {invalid} invalid impact items skipped,"
            f" {ungrounded} ungrounded quotes downgraded to INFERRED"
        )
    if failures and not processed_count:
        # the endpoint is down / every call failed: say so instead of a silent rc 0
        from ..qa.fetch_log import log_collection

        msg = f"all {len(failures)} NLP calls failed; last: {failures[-1]}"
        print(f"  ! NLP: {msg}")
        log_collection(conn, "sentiment", "NLP", None, 0, err=msg, status="DEGRADED")
    return processed_count


def compute_asset_sentiment_radar(
    conn: sqlite3.Connection,
    asset: str,
    *,
    window_days: int = 3,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Compute rolling time-decayed sentiment radar and catalyst breakdown for a specific asset."""
    norm_asset = _normalize_asset(asset)
    if not norm_asset:
        raise ValueError(f"Unknown asset '{asset}', must be one of {TRACKED_ASSETS}")

    target_dt = parse_as_of(as_of)

    since_dt = target_dt - timedelta(days=window_days)
    since_str = since_dt.isoformat(timespec="seconds")
    until_str = target_dt.isoformat(timespec="seconds")

    rows = conn.execute(
        """
        SELECT ni.news_id, ni.stance, ni.magnitude, ni.confidence,
               ni.macro_channel, ni.impact_horizon, ni.evidence_level,
               ni.evidence_quote, ni.transmission_rationale, ni.published_at_utc,
               m.source, m.title
        FROM news_intelligence ni
        JOIN market_news m ON m.news_id = ni.news_id
        WHERE ni.asset = ?
          AND ni.published_at_utc >= ?
          AND ni.published_at_utc <= ?
        ORDER BY ni.published_at_utc DESC
        """,
        (norm_asset, since_str, until_str),
    ).fetchall()

    if not rows:
        return {
            "asset": norm_asset,
            "sample_count": 0,
            "net_stance_score": 0.0,
            "stance": "NEUTRAL",
            "confidence": 0.0,
            "catalysts": {},
            "evidence_breakdown": {"OBSERVED": 0, "SOURCED": 0, "INFERRED": 0},
            "top_quotes": [],
            "as_of": until_str,
        }

    total_weighted_score = 0.0
    total_weights = 0.0
    catalyst_weights: dict[str, float] = {}
    evidence_counts = {"OBSERVED": 0, "SOURCED": 0, "INFERRED": 0}
    quotes = []

    for r in rows:
        (
            news_id,
            stance,
            magnitude,
            confidence,
            channel,
            horizon,
            evidence_level,
            quote,
            rationale,
            pub_utc,
            source,
            title,
        ) = r

        # Calculate time elapsed in hours for exponential decay (half-life: 48h)
        try:
            pub_dt = datetime.fromisoformat(pub_utc).astimezone(UTC)
            hours_elapsed = max(0.0, (target_dt - pub_dt).total_seconds() / 3600.0)
        except Exception:
            hours_elapsed = 24.0

        time_decay = math.exp(-hours_elapsed / 48.0)

        # Evidence reliability weight
        ev_weight = (
            1.0 if evidence_level == "OBSERVED" else (0.8 if evidence_level == "SOURCED" else 0.5)
        )

        # Horizon weight (swing trading prioritizes multiday/structural over intraday noise)
        hor_weight = 1.0 if horizon in ("SWING_MULTIDAY", "STRUCTURAL_LONGTERM") else 0.6

        # Composite item weight
        item_weight = time_decay * ev_weight * hor_weight

        # Directional sign
        direction = 1.0 if stance == "BULLISH" else (-1.0 if stance == "BEARISH" else 0.0)

        item_score = direction * magnitude * confidence
        total_weighted_score += item_score * item_weight
        total_weights += item_weight

        # Channel aggregation
        catalyst_weights[channel] = catalyst_weights.get(channel, 0.0) + item_weight

        # Evidence count
        if evidence_level in evidence_counts:
            evidence_counts[evidence_level] += 1

        if quote and len(quotes) < 5:
            quotes.append(
                {
                    "source": source,
                    "title": title,
                    "stance": stance,
                    "quote": quote,
                    "rationale": rationale,
                    "published_at": pub_utc,
                }
            )

    net_score, overall_stance = _net_stance(total_weighted_score, total_weights)

    # Normalize catalyst shares
    sum_cat = sum(catalyst_weights.values()) or 1.0
    sorted_catalysts = {
        k: round(v / sum_cat, 3)
        for k, v in sorted(catalyst_weights.items(), key=lambda kv: kv[1], reverse=True)
    }

    avg_confidence = round(
        sum(r[3] * (1.0 if r[6] == "OBSERVED" else 0.8) for r in rows) / len(rows),
        2,
    )

    return {
        "asset": norm_asset,
        "sample_count": len(rows),
        "net_stance_score": net_score,
        "stance": overall_stance,
        "confidence": avg_confidence,
        "catalysts": sorted_catalysts,
        "evidence_breakdown": evidence_counts,
        "top_quotes": quotes,
        "as_of": until_str,
    }


def compute_intraday_catalyst_radar(
    conn: sqlite3.Connection,
    asset: str,
    *,
    window_hours: int = 4,
    half_life_hours: float = 1.5,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Compute fast-decaying intraday catalyst radar for active trading session."""
    norm_asset = _normalize_asset(asset)
    if not norm_asset:
        raise ValueError(f"Unknown asset '{asset}', must be one of {TRACKED_ASSETS}")

    target_dt = parse_as_of(as_of)

    since_dt = target_dt - timedelta(hours=window_hours)
    since_str = since_dt.isoformat(timespec="seconds")
    until_str = target_dt.isoformat(timespec="seconds")

    rows = conn.execute(
        """
        SELECT ni.news_id, ni.stance, ni.magnitude, ni.confidence,
               ni.macro_channel, ni.impact_horizon, ni.evidence_level,
               ni.evidence_quote, ni.transmission_rationale, ni.published_at_utc,
               m.source, m.title
        FROM news_intelligence ni
        JOIN market_news m ON m.news_id = ni.news_id
        WHERE ni.asset = ?
          AND ni.published_at_utc >= ?
          AND ni.published_at_utc <= ?
        ORDER BY ni.published_at_utc DESC
        """,
        (norm_asset, since_str, until_str),
    ).fetchall()

    if not rows:
        return {
            "asset": norm_asset,
            "sample_count": 0,
            "net_stance_score": 0.0,
            "stance": "NEUTRAL",
            "confidence": 0.0,
            "active_catalysts": {},
            "evidence_breakdown": {"OBSERVED": 0, "SOURCED": 0, "INFERRED": 0},
            "top_intraday_quotes": [],
            "as_of": until_str,
            "provenance": {
                "window_hours": window_hours,
                "half_life_hours": half_life_hours,
                "articles_evaluated": 0,
                "calculated_at_utc": until_str,
            },
        }

    total_weighted_score = 0.0
    total_weights = 0.0
    catalyst_weights: dict[str, float] = {}
    evidence_counts = {"OBSERVED": 0, "SOURCED": 0, "INFERRED": 0}
    quotes = []

    for r in rows:
        (
            news_id,
            stance,
            magnitude,
            confidence,
            channel,
            horizon,
            evidence_level,
            quote,
            rationale,
            pub_utc,
            source,
            title,
        ) = r

        try:
            pub_dt = datetime.fromisoformat(pub_utc).astimezone(UTC)
            hours_elapsed = max(0.0, (target_dt - pub_dt).total_seconds() / 3600.0)
        except Exception:
            hours_elapsed = 1.0

        time_decay = math.exp(-hours_elapsed / half_life_hours)
        ev_weight = (
            1.0 if evidence_level == "OBSERVED" else (0.8 if evidence_level == "SOURCED" else 0.5)
        )
        hor_weight = 1.0 if horizon in ("INTRADAY_VOLATILITY", "SWING_MULTIDAY") else 0.7
        item_weight = time_decay * ev_weight * hor_weight

        direction = 1.0 if stance == "BULLISH" else (-1.0 if stance == "BEARISH" else 0.0)
        item_score = direction * magnitude * confidence
        total_weighted_score += item_score * item_weight
        total_weights += item_weight

        catalyst_weights[channel] = catalyst_weights.get(channel, 0.0) + item_weight
        if evidence_level in evidence_counts:
            evidence_counts[evidence_level] += 1

        if quote and len(quotes) < 5:
            quotes.append(
                {
                    "source": source,
                    "title": title,
                    "stance": stance,
                    "quote": quote,
                    "rationale": rationale,
                    "published_at": pub_utc,
                    "minutes_ago": round(hours_elapsed * 60, 1),
                }
            )

    net_score, overall_stance = _net_stance(total_weighted_score, total_weights)

    sum_cat = sum(catalyst_weights.values()) or 1.0
    sorted_catalysts = {
        k: round(v / sum_cat, 3)
        for k, v in sorted(catalyst_weights.items(), key=lambda kv: kv[1], reverse=True)
    }

    avg_confidence = round(
        sum(r[3] * (1.0 if r[6] == "OBSERVED" else 0.8) for r in rows) / len(rows),
        2,
    )

    return {
        "asset": norm_asset,
        "sample_count": len(rows),
        "net_stance_score": net_score,
        "stance": overall_stance,
        "confidence": avg_confidence,
        "active_catalysts": sorted_catalysts,
        "evidence_breakdown": evidence_counts,
        "top_intraday_quotes": quotes,
        "as_of": until_str,
        "provenance": {
            "window_hours": window_hours,
            "half_life_hours": half_life_hours,
            "articles_evaluated": len(rows),
            "calculated_at_utc": until_str,
        },
    }


def compute_all_asset_radars(
    conn: sqlite3.Connection,
    *,
    window_days: int = 3,
    as_of: datetime | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute sentiment radar for all tracked assets in the portfolio."""
    return {
        asset: compute_asset_sentiment_radar(conn, asset, window_days=window_days, as_of=as_of)
        for asset in TRACKED_ASSETS
    }


def store_asset_radars(
    conn: sqlite3.Connection,
    *,
    window_days: int = 3,
    as_of: datetime | str | None = None,
) -> list[str]:
    """Compute and store sentiment radar signals in computed_signals for all assets."""
    radars = compute_all_asset_radars(conn, window_days=window_days, as_of=as_of)
    stored_ids = []
    now_utc = datetime.now(UTC).isoformat(timespec="seconds")

    for asset, data in radars.items():
        signal_id = f"news_radar_{asset.lower()}"
        as_of_str = data["as_of"]
        score = data["net_stance_score"]
        status = data["stance"]
        inputs_json = json.dumps(data)

        conn.execute(
            """
            INSERT OR REPLACE INTO computed_signals
            (signal_id, ts, run_id, computed_at, value, state, inputs_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (signal_id, as_of_str, "sentiment_radar", now_utc, score, status, inputs_json),
        )
        stored_ids.append(signal_id)
    conn.commit()
    return stored_ids
