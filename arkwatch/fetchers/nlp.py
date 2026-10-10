"""nlp.py — provider-agnostic NLP analysis for central bank communications.

The ANALYSIS layer: send text to any LLM provider, get structured output.
The SOURCING layer is separate — callers pass text from wherever they got it
(Fed website scrape, manual paste, third-party API, future agent harness).

Multi-provider via NLP_PROVIDER env (zai|openai|anthropic|custom):
  NLP_PROVIDER=zai       → GLM-5.3 via Anthropic Messages (default)
  NLP_PROVIDER=openai    → GPT-4o via chat completions
  NLP_PROVIDER=anthropic → Claude via Messages API
  NLP_PROVIDER=custom    → any OpenAI-compatible endpoint

All overridable: NLP_MODEL, NLP_BASE_URL, NLP_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

ZAI_ANTHROPIC = "https://api.z.ai/api/anthropic/v1/messages"


class NlpError(RuntimeError):
    pass


def _openai_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return base


def _openai_content(response: requests.Response) -> str:
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError:
        decoder = json.JSONDecoder()
        text = response.text.lstrip()
        try:
            payload, _ = decoder.raw_decode(text)
        except json.JSONDecodeError:
            payload = None
            for line in response.text.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    candidate = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if candidate.get("choices"):
                    payload = candidate
                    break
            if payload is None:
                raise NlpError("NLP: malformed OpenAI-compatible response") from None
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as ex:
        raise NlpError("NLP: response has no completion content") from ex
    if choice.get("finish_reason") == "length":
        raise NlpError("NLP: completion truncated (finish_reason=length)")
    return content


def _config() -> dict:
    """Resolve NLP provider config from env (see module docstring)."""
    if "NLP_PROVIDER" not in os.environ and "ZAI_API_KEY" not in os.environ:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
    provider = os.environ.get("NLP_PROVIDER", "zai").lower()
    model = os.environ.get("NLP_MODEL", "")
    base_url = os.environ.get("NLP_BASE_URL", "")
    api_key = (
        os.environ.get("NLP_API_KEY")
        or os.environ.get("ZAI_API_KEY")
        or os.environ.get("Z_AI_API_KEY", "")
    )
    if provider == "zai":
        return {
            "endpoint": base_url or ZAI_ANTHROPIC,
            "api_key": api_key
            or os.environ.get("ZAI_API_KEY")
            or os.environ.get("Z_AI_API_KEY", ""),
            "model": model or "glm-5.3",
            "format": "anthropic",
        }
    if provider == "openai":
        return {
            "endpoint": base_url or "https://api.openai.com/v1/chat/completions",
            "api_key": api_key or os.environ.get("OPENAI_API_KEY", ""),
            "model": model or "gpt-4o",
            "format": "openai",
        }
    if provider == "anthropic":
        return {
            "endpoint": base_url or "https://api.anthropic.com/v1/messages",
            "api_key": api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
            "model": model or "claude-sonnet-5",
            "format": "anthropic",
        }
    if provider == "custom":
        if not base_url:
            raise NlpError("NLP_PROVIDER=custom requires NLP_BASE_URL")
        return {
            "endpoint": _openai_endpoint(base_url),
            "api_key": api_key,
            "model": model or "default",
            "format": os.environ.get("NLP_FORMAT", "openai"),
        }
    raise NlpError(f"unknown NLP_PROVIDER: {provider}")


# Per-model circuit breaker: a dead free lane must not cost every call a full
# timeout. (endpoint, model) -> (consecutive failures, open until on _clock).
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 120.0
_clock = time.monotonic
_lock = threading.Lock()
_breaker: dict[tuple[str, str], tuple[int, float]] = {}


def _read_timeout() -> float:
    try:
        return float(os.environ.get("NLP_TIMEOUT_S", "60"))
    except ValueError:
        return 60.0


def _call(cfg: dict, system: str, user: str) -> str:
    """Try each model of a comma-separated NLP_MODEL in order (free lanes 502/429 at random),
    skipping models whose breaker is open. After the cooldown one call probes the model
    again (half-open): a failure re-opens it at once, a success closes it."""
    models = [m.strip() for m in str(cfg["model"]).split(",") if m.strip()] or ["default"]
    with _lock:
        now = _clock()
        ready = [m for m in models if _breaker.get((cfg["endpoint"], m), (0, 0.0))[1] <= now]
    if not ready:
        raise NlpError("all NLP models are cooling down")
    last: Exception | None = None
    for model in ready:
        key = (cfg["endpoint"], model)
        try:
            out = _call_one({**cfg, "model": model}, system, user)
        except (NlpError, requests.RequestException) as ex:
            last = ex
            with _lock:
                fails = _breaker.get(key, (0, 0.0))[0] + 1
                until = _clock() + BREAKER_COOLDOWN_S if fails >= BREAKER_FAILURES else 0.0
                _breaker[key] = (fails, until)
            continue
        with _lock:
            _breaker.pop(key, None)
        return out
    raise last  # type: ignore[misc]  # ready is never empty


def _call_one(cfg: dict, system: str, user: str) -> str:
    """Route to the correct API format and extract text response."""
    if cfg["format"] == "anthropic":
        r = requests.post(
            cfg["endpoint"],
            headers={
                "Content-Type": "application/json",
                "x-api-key": cfg["api_key"],
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": cfg["model"],
                "max_tokens": 4096,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=(10, _read_timeout()),
        )
        if r.status_code != 200:
            raise NlpError(f"NLP: HTTP {r.status_code} — {r.text[:100]}")
        body = r.json()
        if body.get("stop_reason") == "max_tokens":
            # half a JSON object: fail the call so the breaker / next model take over
            raise NlpError("NLP: completion truncated (stop_reason=max_tokens)")
        blocks = body.get("content", [])
        return next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")

    # openai format
    r = requests.post(
        cfg["endpoint"],
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        json={
            "model": cfg["model"],
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=(10, _read_timeout()),
    )
    if r.status_code != 200:
        raise NlpError(f"NLP: HTTP {r.status_code} — {r.text[:100]}")
    return _openai_content(r)


def _extract_json(content: str) -> dict:
    """Extract JSON from LLM response (may be wrapped in markdown)."""
    jm = re.search(r"\{.*\}", content, re.DOTALL)
    if not jm:
        raise NlpError(f"no JSON in response — {content[:100]}")
    try:
        return json.loads(jm.group())
    except json.JSONDecodeError as ex:
        # raise, not {"error": ...}: a dict here read as a successful (empty) analysis
        raise NlpError(f"unparseable JSON in response ({ex.msg}) — {content[:100]}") from None


# --- Analysis functions -------------------------------------------------------


def analyze_tone(text: str, source_type: str = "minutes") -> dict:
    """Hawkish/dovish tone analysis for any central bank communication.

    source_type: "minutes" | "statement" | "press_conference" | "speech"
    Returns {"score": float, "summary": str, "key_concerns": [str]}.
    """
    cfg = _config()
    if not cfg["api_key"]:
        raise NlpError("NLP API key not set")

    # truncate to ~12k chars for API limits
    body = text[:12000]
    prompt = f"""Analyze this {source_type.replace("_", " ")} for monetary policy tone.

Rate the overall tone on a scale from -100 (maximally dovish: rate cuts, easing concern) to +100 (maximally hawkish: inflation fighting, tightening bias).

Also identify the top 3 specific concerns or key messages discussed.

Respond in this exact JSON format:
{{"score": <number>, "summary": "<one sentence>", "key_concerns": ["<concern1>", "<concern2>", "<concern3>"]}}

TEXT:
{body}"""

    content = _call(
        cfg,
        system="You are a central bank policy analyst. Respond ONLY with valid JSON, no markdown.",
        user=prompt,
    )
    if not content:
        raise NlpError("empty response")
    return _extract_json(content)


def compare_tones(text_a: str, text_b: str, label_a: str, label_b: str) -> dict:
    """Compare tone between two communications (e.g. statement vs minutes).

    Returns {"shift": float, "direction": str, "detail": str}.
    """
    cfg = _config()
    if not cfg["api_key"]:
        raise NlpError("NLP API key not set")

    ta = analyze_tone(text_a, label_a)
    tb = analyze_tone(text_b, label_b)
    shift = tb.get("score", 0) - ta.get("score", 0)
    return {
        "a": {"label": label_a, **ta},
        "b": {"label": label_b, **tb},
        "shift": shift,
        "direction": "more hawkish"
        if shift > 5
        else ("more dovish" if shift < -5 else "unchanged"),
        "detail": f"{label_a} {ta.get('score', 0):+.0f} → {label_b} {tb.get('score', 0):+.0f} (shift {shift:+.0f})",
    }


def extract_data_points(text: str, source_type: str = "statement") -> dict:
    """Extract specific numeric/data claims from a communication.

    Returns {"claims": [{"what": str, "value": str, "context": str}]}.
    """
    cfg = _config()
    if not cfg["api_key"]:
        raise NlpError("NLP API key not set")

    prompt = f"""Extract all specific numeric or data claims from this {source_type.replace("_", " ")}.
For each claim, capture: what it's about, the value/number, and the surrounding context sentence.

Respond as JSON: {{"claims": [{{"what": "<topic>", "value": "<number>", "context": "<sentence>"}}]}}

TEXT:
{text[:10000]}"""

    content = _call(
        cfg,
        system="You are a financial data extraction analyst. Respond ONLY with valid JSON.",
        user=prompt,
    )
    if not content:
        raise NlpError("empty response")
    return _extract_json(content)
