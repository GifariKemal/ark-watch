import pytest

from arkwatch.fetchers import nlp


class _Response:
    def __init__(self, text):
        self.text = text

    def json(self):
        raise nlp.requests.exceptions.JSONDecodeError("extra", self.text, 1)


def test_custom_v1_base_url_resolves_chat_completions(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "custom")
    monkeypatch.setenv("NLP_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("NLP_API_KEY", "test")
    assert nlp._config()["endpoint"] == "https://example.test/v1/chat/completions"


def test_custom_full_endpoint_is_unchanged(monkeypatch):
    monkeypatch.setenv("NLP_PROVIDER", "custom")
    monkeypatch.setenv("NLP_BASE_URL", "https://example.test/v1/chat/completions")
    monkeypatch.setenv("NLP_API_KEY", "test")
    assert nlp._config()["endpoint"] == "https://example.test/v1/chat/completions"


def test_openai_content_accepts_json_before_sse_done_marker():
    raw = '{"choices":[{"message":{"content":"ok"}}]}data: [DONE]'
    assert nlp._openai_content(_Response(raw)) == "ok"


def test_call_falls_back_to_next_model_on_failure(monkeypatch):
    """Free lanes 502/429 at random: NLP_MODEL may be a comma list tried in order."""
    tried: list[str] = []

    def one(cfg, system, user):
        tried.append(cfg["model"])
        if cfg["model"] == "flaky":
            raise nlp.NlpError("NLP: HTTP 502")
        return "ok"

    monkeypatch.setattr(nlp, "_call_one", one)
    cfg = {"format": "openai", "model": "flaky, steady ,", "endpoint": "x", "api_key": "k"}
    assert nlp._call(cfg, "s", "u") == "ok"
    assert tried == ["flaky", "steady"]


def test_call_raises_last_error_when_every_model_fails(monkeypatch):
    def one(cfg, system, user):
        raise nlp.NlpError(f"NLP: HTTP 502 {cfg['model']}")

    monkeypatch.setattr(nlp, "_call_one", one)
    cfg = {"format": "openai", "model": "a,b", "endpoint": "x", "api_key": "k"}
    try:
        nlp._call(cfg, "s", "u")
    except nlp.NlpError as ex:
        assert "b" in str(ex)  # the last failure surfaces
    else:
        raise AssertionError("expected NlpError")


# --- per-model circuit breaker + read timeout --------------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _failing_lane(monkeypatch, bad: set[str]):
    tried: list[str] = []

    def one(cfg, system, user):
        tried.append(cfg["model"])
        if cfg["model"] in bad:
            raise nlp.requests.ConnectionError(f"down {cfg['model']}")
        return "ok"

    monkeypatch.setattr(nlp, "_call_one", one)
    clock = _Clock()
    monkeypatch.setattr(nlp, "_clock", clock)
    return tried, clock


CFG = {"format": "openai", "model": "dead,live", "endpoint": "https://e.test", "api_key": "k"}


def test_breaker_opens_after_three_failures_then_half_opens(monkeypatch):
    tried, clock = _failing_lane(monkeypatch, {"dead"})
    for _ in range(3):
        assert nlp._call(CFG, "s", "u") == "ok"
    assert tried == ["dead", "live"] * 3
    tried.clear()
    assert nlp._call(CFG, "s", "u") == "ok"
    assert tried == ["live"]  # dead lane skipped while cooling down
    clock.t += nlp.BREAKER_COOLDOWN_S
    tried.clear()
    assert nlp._call(CFG, "s", "u") == "ok"
    assert tried == ["dead", "live"]  # half-open: one probe
    tried.clear()
    assert nlp._call(CFG, "s", "u") == "ok"
    assert tried == ["live"]  # the failed probe re-opened it at once


def test_breaker_success_resets_failure_count(monkeypatch):
    bad = {"dead"}
    tried, _clock = _failing_lane(monkeypatch, bad)
    nlp._call(CFG, "s", "u")
    nlp._call(CFG, "s", "u")
    bad.clear()  # lane recovers
    nlp._call(CFG, "s", "u")
    bad.add("dead")
    nlp._call(CFG, "s", "u")
    nlp._call(CFG, "s", "u")
    tried.clear()
    nlp._call(CFG, "s", "u")
    assert tried == ["dead", "live"]  # 2 failures since the reset: still closed


def test_breaker_is_keyed_by_endpoint(monkeypatch):
    tried, _clock = _failing_lane(monkeypatch, {"dead"})
    for _ in range(3):
        nlp._call(CFG, "s", "u")
    tried.clear()
    nlp._call({**CFG, "endpoint": "https://other.test"}, "s", "u")
    assert tried == ["dead", "live"]


def test_all_models_cooling_fails_fast_without_network(monkeypatch):
    tried, _clock = _failing_lane(monkeypatch, {"a", "b"})
    cfg = {**CFG, "model": "a,b"}
    for _ in range(3):
        with pytest.raises(nlp.requests.ConnectionError):
            nlp._call(cfg, "s", "u")
    tried.clear()
    with pytest.raises(nlp.NlpError, match="all NLP models are cooling down"):
        nlp._call(cfg, "s", "u")
    assert tried == []


def test_read_timeout_from_env(monkeypatch):
    seen = []

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(nlp.requests, "post", lambda *a, **k: seen.append(k["timeout"]) or R())
    nlp._call_one(CFG, "s", "u")
    monkeypatch.setenv("NLP_TIMEOUT_S", "7.5")
    nlp._call_one(CFG, "s", "u")
    monkeypatch.setenv("NLP_TIMEOUT_S", "junk")
    nlp._call_one(CFG, "s", "u")
    assert seen == [(10, 60.0), (10, 7.5), (10, 60.0)]


# --- truncation / unparseable output are failures, not empty successes ----------------


def _post_returning(monkeypatch, bodies: dict[str, dict]):
    class R:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    monkeypatch.setattr(nlp.requests, "post", lambda url, **k: R(bodies[k["json"]["model"]]))


@pytest.mark.parametrize(
    ("fmt", "cut", "whole"),
    [
        (
            "openai",
            {"choices": [{"message": {"content": '{"a": 1'}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ),
        (
            "anthropic",
            {"stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"a": 1'}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]},
        ),
    ],
)
def test_truncated_completion_fails_over_to_next_model(monkeypatch, fmt, cut, whole):
    _post_returning(monkeypatch, {"cut": cut, "whole": whole})
    endpoint = f"https://trunc-{fmt}.test"
    cfg = {"format": fmt, "model": "cut,whole", "endpoint": endpoint, "api_key": "k"}
    assert nlp._call(cfg, "s", "u") == "ok"
    assert nlp._breaker[(endpoint, "cut")][0] == 1  # counted against the breaker
    with pytest.raises(nlp.NlpError, match="truncated"):
        nlp._call_one({**cfg, "model": "cut"}, "s", "u")


def test_unparseable_json_raises_instead_of_error_dict():
    assert nlp._extract_json('```json\n{"score": 5}\n```') == {"score": 5}
    with pytest.raises(nlp.NlpError, match="unparseable"):
        nlp._extract_json('{"score": 5,, }')
