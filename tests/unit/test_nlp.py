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
