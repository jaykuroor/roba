"""Tests for the Gemini-only LLM provider layer."""

import pytest
from pydantic import BaseModel

from core import config
from core.llm import CANNED_NOTE, LLMProvider
from core.models import LLMCallLog


class _FakeGeminiResponse:
    def __init__(self, text: str):
        self.text = text


SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "tags": {"type": "array"},
    },
    "required": ["intent"],
}


class StructuredIntent(BaseModel):
    intent: str
    confidence: float = 0.0


def _gemini_llm(monkeypatch, responses):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = {"n": 0, "configs": [], "models": [], "contents": []}
    queue = list(responses)

    def fake_generate(_client, model, contents, gen_config):
        calls["n"] += 1
        calls["models"].append(model)
        calls["contents"].append(contents)
        calls["configs"].append(gen_config)
        text = queue.pop(0) if queue else responses[-1]
        return _FakeGeminiResponse(text)

    llm = LLMProvider(fallback=["gemini", "canned"])
    llm._sleep = lambda *_a, **_k: None
    monkeypatch.setattr(llm, "_get_gemini_client", lambda: object())
    monkeypatch.setattr(llm, "_gemini_generate", fake_generate)
    return llm, calls


def test_canned_fallback_without_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None

    result = llm.complete(
        [{"role": "user", "content": "How are reviews?"}],
        use_site="review",
    )

    assert isinstance(result, dict)
    assert result.get("note") == CANNED_NOTE
    assert result["severity"] == "low"
    assert llm.request_count == 0


def test_cache_single_gemini_request(monkeypatch):
    llm, calls = _gemini_llm(monkeypatch, ["hello world"])

    messages = [{"role": "user", "content": "say hi"}]
    first = llm.complete(messages, use_site="review")
    second = llm.complete(messages, use_site="review")

    assert first == "hello world"
    assert second == "hello world"
    assert calls["n"] == 1


def test_gemini_uses_google_genai_sdk_and_default_model(monkeypatch):
    llm, calls = _gemini_llm(monkeypatch, ["gemini hello"])

    result = llm.complete(
        [
            {"role": "system", "content": "Be short."},
            {"role": "user", "content": "Say hi."},
        ],
        use_site="call_supplier",
    )

    assert result == "gemini hello"
    assert calls["models"][0] == config.GEMINI_MODEL == "gemini-3.1-flash-lite"
    assert calls["contents"][0]


def test_gemini_json_config_carries_schema(monkeypatch):
    body = '{"intent":"set_leave","confidence":0.8,"tags":[]}'
    llm, calls = _gemini_llm(monkeypatch, [body])

    parsed = llm.complete(
        [{"role": "user", "content": "Priya is off tomorrow"}],
        json_schema=SCHEMA,
        use_site="voice",
    )

    cfg = calls["configs"][0]
    mime = getattr(cfg, "response_mime_type", None)
    schema = getattr(cfg, "response_json_schema", None)
    if isinstance(cfg, dict):
        mime = cfg.get("response_mime_type")
        schema = cfg.get("response_json_schema")
    assert parsed["intent"] == "set_leave"
    assert mime == "application/json"
    assert schema == SCHEMA


def test_gemini_config_carries_generation_controls(monkeypatch):
    llm, calls = _gemini_llm(monkeypatch, ["steady"])

    result = llm.complete(
        [{"role": "user", "content": "forecast"}],
        use_site="forecaster_optimization",
        temperature=0.2,
        top_p=0.8,
    )

    cfg = calls["configs"][0]
    temperature = getattr(cfg, "temperature", None)
    top_p = getattr(cfg, "top_p", None)
    if isinstance(cfg, dict):
        temperature = cfg.get("temperature")
        top_p = cfg.get("top_p")
    assert result == "steady"
    assert temperature == 0.2
    assert top_p == 0.8


def test_generation_use_site_never_cached(monkeypatch):
    llm, calls = _gemini_llm(monkeypatch, ["fresh"])

    messages = [{"role": "user", "content": "generate"}]
    llm.complete(messages, use_site="generation")
    llm.complete(messages, use_site="generation")

    assert calls["n"] == 2


def test_json_mode_valid_roundtrip(monkeypatch):
    body = '{"intent":"set_leave","confidence":0.9,"tags":["a","b"]}'
    llm, calls = _gemini_llm(monkeypatch, [body])

    parsed = llm.complete(
        [{"role": "user", "content": "x"}],
        json_schema=SCHEMA,
        use_site="voice",
    )

    assert parsed["intent"] == "set_leave"
    assert parsed["confidence"] == pytest.approx(0.9)
    assert parsed["tags"] == ["a", "b"]
    assert parsed.get("note") != CANNED_NOTE
    assert calls["n"] == 1


def test_json_mode_fenced_response_is_parsed(monkeypatch):
    fenced = '```json\n{"intent":"record_receipt","confidence":0.5}\n```'
    llm, _calls = _gemini_llm(monkeypatch, [fenced])

    parsed = llm.complete(
        [{"role": "user", "content": "x"}],
        json_schema=SCHEMA,
        use_site="voice",
    )

    assert parsed["intent"] == "record_receipt"


def test_json_mode_malformed_triggers_one_reask_then_canned(monkeypatch):
    llm, calls = _gemini_llm(monkeypatch, ["this is not json", "still not json"])

    result = llm.complete(
        [{"role": "user", "content": "y"}],
        json_schema=SCHEMA,
        use_site="voice",
    )

    assert result.get("note") == CANNED_NOTE
    assert calls["n"] == 2


def test_json_mode_validation_failure_falls_back(monkeypatch):
    llm, calls = _gemini_llm(
        monkeypatch,
        ['{"confidence":0.4}', '{"confidence":0.5}'],
    )

    result = llm.complete(
        [{"role": "user", "content": "z"}],
        json_schema=SCHEMA,
        use_site="voice",
    )

    assert result.get("note") == CANNED_NOTE
    assert calls["n"] == 2


def test_hosted_provider_chain_falls_back_to_canned(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-groq")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    attempted = []

    def fake_openai_compatible(self, provider, *_args, **_kwargs):
        attempted.append(provider)
        raise RuntimeError("offline")

    monkeypatch.setattr(LLMProvider, "_openai_compatible", fake_openai_compatible)

    llm = LLMProvider(fallback=["groq", "openrouter"])
    result = llm.complete([{"role": "user", "content": "x"}], use_site="review")

    assert llm.fallback == ["groq", "openrouter", "canned"]
    assert attempted == ["groq", "openrouter"]
    assert result.get("note") == CANNED_NOTE


def test_complete_structured_logs_success_and_cache(monkeypatch, session_factory):
    body = '{"intent":"set_leave","confidence":0.8}'
    llm, calls = _gemini_llm(monkeypatch, [body])
    llm.db_session_factory = session_factory

    first = llm.complete_structured(
        "voice.route",
        StructuredIntent,
        {"text": "Ansi is on leave"},
        use_site="voice",
    )
    second = llm.complete_structured(
        "voice.route",
        StructuredIntent,
        {"text": "Ansi is on leave"},
        use_site="voice",
    )

    assert first["data"] == {"intent": "set_leave", "confidence": pytest.approx(0.8)}
    assert first["provider"] == "gemini"
    assert first["fallback_used"] is False
    assert first["error"] is None
    assert second["provider"] == "cache"
    assert second["cached"] is True
    assert calls["n"] == 1

    session = session_factory()
    try:
        rows = session.query(LLMCallLog).order_by(LLMCallLog.id.asc()).all()
        assert len(rows) == 2
        assert rows[0].prompt_id == "voice.route"
        assert rows[0].provider == "gemini"
        assert rows[0].status == "ok"
        assert rows[1].provider == "cache"
        assert rows[1].cached == 1
    finally:
        session.close()


def test_complete_structured_canned_fallback_logs(monkeypatch, session_factory):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm = LLMProvider(fallback=["gemini", "canned"], db_session_factory=session_factory)

    result = llm.complete_structured(
        "review.trend",
        StructuredIntent,
        {"reviews": []},
        use_site="review",
    )

    assert result["provider"] == "canned"
    assert result["fallback_used"] is True
    assert result["data"]["note"] == CANNED_NOTE

    session = session_factory()
    try:
        row = session.query(LLMCallLog).one()
        assert row.prompt_id == "review.trend"
        assert row.provider == "canned"
        assert row.fallback_used == 1
    finally:
        session.close()
