"""Tests for the LLM provider layer (§13) — gates 4 & 5."""

import pytest

from core.llm import CANNED_NOTE, LLMProvider


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_canned_fallback_without_keys(monkeypatch):
    """Gate 4: with no API keys set, complete(use_site='review') returns the
    canned dict without raising."""
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None  # never block

    result = llm.complete([{"role": "user", "content": "How are reviews?"}],
                          use_site="review")
    assert isinstance(result, dict)
    assert result.get("note") == CANNED_NOTE
    assert result["severity"] == "low"
    # No network was attempted (all providers skipped on missing keys).
    assert llm.request_count == 0


def test_cache_single_http_request(monkeypatch):
    """Gate 5: two identical calls produce exactly one HTTP request."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    calls = {"n": 0}

    def fake_request(method, url, headers=None, params=None, json_body=None):
        calls["n"] += 1
        return _FakeResp({"choices": [{"message": {"content": "hello world"}}]})

    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    monkeypatch.setattr(llm, "_request", fake_request)

    messages = [{"role": "user", "content": "say hi"}]
    first = llm.complete(messages, use_site="review")
    second = llm.complete(messages, use_site="review")

    assert first == "hello world"
    assert second == "hello world"
    assert calls["n"] == 1  # second call served from cache


def test_generation_use_site_never_cached(monkeypatch):
    """generation is always fresh -> a second identical call hits HTTP again."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    calls = {"n": 0}

    def fake_request(method, url, headers=None, params=None, json_body=None):
        calls["n"] += 1
        return _FakeResp({"choices": [{"message": {"content": "fresh"}}]})

    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    monkeypatch.setattr(llm, "_request", fake_request)

    messages = [{"role": "user", "content": "generate"}]
    llm.complete(messages, use_site="generation")
    llm.complete(messages, use_site="generation")
    assert calls["n"] == 2


SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "tags": {"type": "array"},
    },
    "required": ["intent"],
}


def test_json_mode_valid_roundtrip(monkeypatch):
    """A valid structured response round-trips through the dynamically-built
    pydantic model and returns the parsed dict (real schema construction, not
    the canned path). The optional 'confidence' field is coerced to float; the
    'required' field is present; types are validated."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    calls = {"n": 0}

    def fake_request(method, url, headers=None, params=None, json_body=None):
        calls["n"] += 1
        body = '{"intent":"set_leave","confidence":0.9,"tags":["a","b"]}'
        return _FakeResp({"choices": [{"message": {"content": body}}]})

    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    monkeypatch.setattr(llm, "_request", fake_request)

    parsed = llm.complete(
        [{"role": "user", "content": "x"}], json_schema=SCHEMA, use_site="voice"
    )
    assert isinstance(parsed, dict)
    assert parsed["intent"] == "set_leave"
    assert parsed["confidence"] == pytest.approx(0.9)
    assert parsed["tags"] == ["a", "b"]
    assert parsed.get("note") != CANNED_NOTE  # a real parse, not canned
    assert calls["n"] == 1  # parsed first try, no re-ask


def test_json_mode_fenced_response_is_parsed(monkeypatch):
    """A ```json fenced response is still parsed (fence stripped)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    fenced = "```json\n{\"intent\":\"record_receipt\",\"confidence\":0.5}\n```"
    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    monkeypatch.setattr(
        llm, "_request",
        lambda *a, **k: _FakeResp({"choices": [{"message": {"content": fenced}}]}),
    )
    parsed = llm.complete(
        [{"role": "user", "content": "x"}], json_schema=SCHEMA, use_site="voice"
    )
    assert parsed["intent"] == "record_receipt"


def test_json_mode_malformed_triggers_one_reask_then_canned(monkeypatch):
    """A malformed structured response triggers exactly one re-ask on the same
    provider, then (no other provider available) falls back to canned."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # groq is the only provider

    calls = {"n": 0}

    def fake_request(method, url, headers=None, params=None, json_body=None):
        calls["n"] += 1
        return _FakeResp({"choices": [{"message": {"content": "this is not json"}}]})

    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    monkeypatch.setattr(llm, "_request", fake_request)

    result = llm.complete(
        [{"role": "user", "content": "y"}], json_schema=SCHEMA, use_site="voice"
    )
    assert result.get("note") == CANNED_NOTE
    # Exactly one re-ask: initial attempt + one re-ask = 2 requests on groq.
    assert calls["n"] == 2


def test_json_mode_validation_failure_falls_back(monkeypatch):
    """Valid JSON that violates the schema (missing required field) fails
    pydantic validation and falls through to canned after one re-ask."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    calls = {"n": 0}

    def fake_request(method, url, headers=None, params=None, json_body=None):
        calls["n"] += 1
        # Well-formed JSON but missing the required "intent" field.
        return _FakeResp({"choices": [{"message": {"content": '{"confidence":0.4}'}}]})

    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    monkeypatch.setattr(llm, "_request", fake_request)

    result = llm.complete(
        [{"role": "user", "content": "z"}], json_schema=SCHEMA, use_site="voice"
    )
    assert result.get("note") == CANNED_NOTE
    assert calls["n"] == 2
