"""LLM provider layer (§13).

``LLMProvider.complete`` tries providers in order from ``config.LLM_FALLBACK``
(``gemini`` → ``groq`` → ``openrouter`` → ``canned``). Each real provider is
attempted with exponential backoff up to ``config.LLM_RETRIES`` retries (base
``config.LLM_BACKOFF_BASE_S``); a 429 / 5xx / timeout is retried and, once
retries are exhausted, the chain moves to the next provider. After a successful
inter-provider transition (i.e. one network attempt was made and we fall
through to the next provider) we sleep ``config.LLM_INTER_CALL_SLEEP_S`` (free
-tier RPM protection, §13).

When ``json_schema`` is given the raw text is parsed and validated with a
pydantic model built dynamically from the schema; on a parse/validation failure
the provider is re-asked once, then the chain falls through (ultimately to the
canned response). Otherwise the raw string is returned.

Results are memoised in an in-process dict keyed by
``sha256(json.dumps(messages, sort_keys=True) + str(json_schema))``; calls with
``use_site="generation"`` always run fresh (never cached). Every call-site has a
canned fallback so the demo never crashes when no API keys are configured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Type, Union

from pydantic import BaseModel, ValidationError, create_model

from . import config

logger = logging.getLogger(__name__)

# Marker placed on canned dicts so call-sites (e.g. voice) can detect that the
# LLM did not actually answer and engage their own deterministic fallback.
CANNED_NOTE = "canned_fallback"

# Provider endpoints / models (§13, pinned).
GEMINI_MODEL = config.GEMINI_MODEL
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "mistralai/mistral-7b-instruct:free"


class _SkipProvider(Exception):
    """Raised to move on to the next provider (no key / non-retryable error /
    retries exhausted)."""


class _RetryableError(Exception):
    """A 429 / 5xx / timeout — retry within the same provider, then skip."""


# JSON-schema primitive → python type, for building a pydantic validator.
_JSON_TYPES: Dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _strip_fences(text: str) -> str:
    """Strip a ```json ... ``` (or bare ```) markdown fence if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Drop the opening fence (``` or ```json) and a trailing fence.
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _model_from_schema(json_schema: Dict[str, Any]) -> Optional[Type[BaseModel]]:
    """Build a pydantic model from a JSON-schema-like dict (properties +
    required). Returns ``None`` when no usable ``properties`` are present."""
    props = (json_schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return None

    required = set((json_schema or {}).get("required", []) or [])
    fields: Dict[str, Any] = {}
    for name, spec in props.items():
        py_type = _JSON_TYPES.get((spec or {}).get("type", "string"), Any)
        if name in required:
            fields[name] = (py_type, ...)
        else:
            fields[name] = (Optional[py_type], None)
    try:
        return create_model("LLMJSONSchema", **fields)  # type: ignore[call-overload]
    except Exception:  # pragma: no cover - defensive
        return None


class LLMProvider:
    """Fallback chain + cache + canned responses for every LLM call-site (§13)."""

    def __init__(
        self,
        fallback: Optional[List[str]] = None,
        retries: Optional[int] = None,
        backoff_base_s: Optional[float] = None,
        inter_call_sleep_s: Optional[float] = None,
        timeout_s: float = 20.0,
    ):
        self.fallback = list(fallback if fallback is not None else config.LLM_FALLBACK)
        self.retries = retries if retries is not None else config.LLM_RETRIES
        self.backoff_base_s = (
            backoff_base_s if backoff_base_s is not None else config.LLM_BACKOFF_BASE_S
        )
        self.inter_call_sleep_s = (
            inter_call_sleep_s
            if inter_call_sleep_s is not None
            else config.LLM_INTER_CALL_SLEEP_S
        )
        self.timeout_s = timeout_s

        # In-process cache (TTL = process lifetime, §13).
        self._cache: Dict[str, Union[str, dict]] = {}
        # Injectable sleep so tests don't actually block on backoff.
        self._sleep = time.sleep
        # Diagnostics: number of outbound provider requests actually issued.
        self.request_count = 0
        self._gemini_client: Optional[Any] = None

        self._canned = self._build_canned()

    # -- canned registry (§13) ---------------------------------------------

    @staticmethod
    def _build_canned() -> Dict[str, Union[str, dict]]:
        """Deterministic canned outputs for every LLM use-site (§13)."""
        return {
            # Voice extraction: a neutral "other" extraction carrying the
            # canned marker so VoiceProcessor falls back to its regex parse.
            "voice": {
                "intent": "other",
                "entity_type": "",
                "entity_ref": None,
                "attribute": "",
                "value": None,
                "effective_window": None,
                "confidence": 0.0,
                "note": CANNED_NOTE,
            },
            # Review sentiment/insight: a neutral insight.
            "review": {
                "severity": "low",
                "summary": "No significant trend detected in recent reviews.",
                "suggested_action": "none",
                "dish_mentions": [],
                "sentiment": "neutral",
                "note": CANNED_NOTE,
            },
            # Competitor call turn (undercover customer persona) — raw line.
            "call_competitor": (
                "Hi there! I'm thinking of ordering tonight — "
                "what's your most popular dish right now?"
            ),
            # Supplier call turn (Market Spectator negotiation) — raw line.
            "call_supplier": (
                "Hello, I'd like to revisit the pricing on our regular order — "
                "is there any room to improve the unit price?"
            ),
            # Dataset generation: a small valid qualitative slice (§12).
            "generation": {
                "cuisine": "cafe",
                "stations": ["Line", "Cold"],
                "menu_items": [
                    {
                        "name": "House Sandwich",
                        "category": "main",
                        "station": "Line",
                        "dine_in_price": 9.0,
                        "online_price": 11.0,
                        "is_batchable": False,
                        "ingredients": [
                            {"name": "Bread", "qty": 80.0, "unit": "g"},
                            {"name": "Cheese", "qty": 40.0, "unit": "g"},
                        ],
                    },
                    {
                        "name": "Garden Salad",
                        "category": "salad",
                        "station": "Cold",
                        "dine_in_price": 7.0,
                        "online_price": 8.0,
                        "is_batchable": False,
                        "ingredients": [
                            {"name": "Lettuce", "qty": 120.0, "unit": "g"},
                            {"name": "Dressing", "qty": 30.0, "unit": "ml"},
                        ],
                    },
                ],
                "suppliers": [{"name": "Local Wholesale", "lead_time_days": 2.0}],
                "staff": [
                    {"name": "Sam", "role": "cook", "station": "Line"},
                    {"name": "Riley", "role": "cook", "station": "Cold"},
                ],
                "note": CANNED_NOTE,
            },
            # Forecaster periodic suggestions (§18.7): "no change".
            "forecaster_suggestion": {
                "suggestions": [],
                "summary": "no_change",
                "note": CANNED_NOTE,
            },
            # Call outcome extraction (§8.5): nothing usable -> safe no-op.
            "outcome_extraction": {
                "outcome": {},
                "agreed": False,
                "note": CANNED_NOTE,
            },
        }

    def canned(self, use_site: str) -> Union[str, dict]:
        """Return the registered canned response for ``use_site`` (§13)."""
        return self._canned.get(
            use_site, {"result": "no_change", "note": CANNED_NOTE}
        )

    # -- public API ---------------------------------------------------------

    def complete(
        self,
        messages: List[dict],
        json_schema: Optional[dict] = None,
        max_tokens: int = 800,
        use_site: str = "",
    ) -> Union[str, dict]:
        """Run the fallback chain and return a parsed dict (when ``json_schema``
        is given) or a raw string. Never raises on provider failure — the chain
        always terminates at the canned response."""
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "model": GEMINI_MODEL,
                    "messages": messages,
                    "json_schema": json_schema,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        use_cache = use_site != "generation"
        if use_cache and cache_key in self._cache:
            return self._cache[cache_key]

        want_json = json_schema is not None
        made_network_attempt = False
        result: Optional[Union[str, dict]] = None

        for provider in self.fallback:
            if provider == "canned":
                result = self.canned(use_site)
                break

            if not self._has_key(provider):
                # Missing key: skip instantly (no network, no inter-call sleep).
                continue

            # §13: sleep between successive provider network calls.
            if made_network_attempt:
                self._sleep(self.inter_call_sleep_s)
            made_network_attempt = True

            try:
                raw = self._attempt_provider(
                    provider, messages, json_schema, max_tokens, want_json
                )
            except _SkipProvider:
                continue

            if not want_json:
                result = raw
                break

            parsed = self._try_parse(raw, json_schema)
            if parsed is None:
                # One re-ask on parse failure within this provider (§13).
                try:
                    raw2 = self._attempt_provider(
                        provider,
                        self._augment_for_json(messages),
                        json_schema,
                        max_tokens,
                        want_json,
                    )
                    parsed = self._try_parse(raw2, json_schema)
                except _SkipProvider:
                    parsed = None
            if parsed is None:
                continue  # fall through to the next provider
            result = parsed
            break

        if result is None:
            result = self.canned(use_site)

        if use_cache:
            self._cache[cache_key] = result
        return result

    # -- provider attempt + retries ----------------------------------------

    def _attempt_provider(
        self,
        provider: str,
        messages: List[dict],
        json_schema: Optional[dict],
        max_tokens: int,
        want_json: bool,
    ) -> str:
        """Call one provider with exponential backoff over retryable errors.

        Returns the raw text. Raises :class:`_SkipProvider` once retries are
        exhausted or on a non-retryable error.
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                return self._call_provider(
                    provider, messages, json_schema, max_tokens, want_json
                )
            except _RetryableError as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    self._sleep(self.backoff_base_s * (2 ** attempt))
                continue
            except _SkipProvider:
                raise
            except Exception as exc:  # unexpected provider error -> skip
                logger.warning("Provider %s errored: %s", provider, exc)
                raise _SkipProvider() from exc
        logger.warning("Provider %s exhausted retries: %s", provider, last_error)
        raise _SkipProvider()

    def _call_provider(
        self,
        provider: str,
        messages: List[dict],
        json_schema: Optional[dict],
        max_tokens: int,
        want_json: bool,
    ) -> str:
        if provider == "gemini":
            return self._gemini(messages, json_schema, max_tokens, want_json)
        if provider == "groq":
            return self._openai_compatible(
                GROQ_URL, GROQ_MODEL, self._key("groq"), messages, max_tokens, want_json
            )
        if provider == "openrouter":
            return self._openai_compatible(
                OPENROUTER_URL,
                OPENROUTER_MODEL,
                self._key("openrouter"),
                messages,
                max_tokens,
                want_json,
            )
        raise _SkipProvider()

    # -- concrete providers -------------------------------------------------

    def _gemini(
        self,
        messages: List[dict],
        json_schema: Optional[dict],
        max_tokens: int,
        want_json: bool,
    ) -> str:
        """Gemini via the official Google GenAI SDK."""
        system_parts: List[str] = []
        content_specs: List[Dict[str, str]] = []
        for msg in messages:
            role = msg.get("role", "user")
            text = str(msg.get("content", ""))
            if role == "system":
                system_parts.append(text)
                continue
            gem_role = "model" if role == "assistant" else "user"
            content_specs.append({"role": gem_role, "text": text})
        if not content_specs:
            content_specs.append({"role": "user", "text": ""})

        client = self._get_gemini_client()
        contents = self._gemini_contents(content_specs)
        gen_config = self._gemini_config(
            system_instruction="\n".join(system_parts) if system_parts else None,
            json_schema=json_schema,
            max_tokens=max_tokens,
            want_json=want_json,
        )

        try:
            response = self._gemini_generate(
                client, GEMINI_MODEL, contents, gen_config
            )
        except Exception as exc:
            self._classify_gemini_error(exc)
        return self._gemini_response_text(response)

    def _get_gemini_client(self) -> Any:
        if self._gemini_client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise _SkipProvider() from exc
            self._gemini_client = genai.Client(api_key=self._key("gemini"))
        return self._gemini_client

    @staticmethod
    def _gemini_contents(content_specs: List[Dict[str, str]]) -> List[Any]:
        try:
            from google.genai import types

            return [
                types.Content(
                    role=spec["role"],
                    parts=[types.Part(text=spec["text"])],
                )
                for spec in content_specs
            ]
        except Exception:
            return [
                {"role": spec["role"], "parts": [{"text": spec["text"]}]}
                for spec in content_specs
            ]

    @staticmethod
    def _gemini_config(
        system_instruction: Optional[str],
        json_schema: Optional[dict],
        max_tokens: int,
        want_json: bool,
    ) -> Any:
        kwargs: Dict[str, Any] = {"max_output_tokens": max_tokens}
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        if want_json:
            kwargs["response_mime_type"] = "application/json"
            if json_schema:
                kwargs["response_json_schema"] = json_schema
        try:
            from google.genai import types

            return types.GenerateContentConfig(**kwargs)
        except Exception:
            return kwargs

    def _gemini_generate(
        self,
        client: Any,
        model: str,
        contents: List[Any],
        gen_config: Any,
    ) -> Any:
        self.request_count += 1
        return client.models.generate_content(
            model=model,
            contents=contents,
            config=gen_config,
        )

    @staticmethod
    def _gemini_response_text(response: Any) -> str:
        text = getattr(response, "text", None)
        if text:
            return str(text)
        try:
            return response.candidates[0].content.parts[0].text
        except (AttributeError, IndexError, TypeError) as exc:
            raise _SkipProvider() from exc

    @staticmethod
    def _classify_gemini_error(exc: Exception) -> None:
        status = (
            getattr(exc, "status_code", None)
            or getattr(exc, "code", None)
            or getattr(getattr(exc, "response", None), "status_code", None)
        )
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = 0
        if status_int == 429 or status_int >= 500:
            raise _RetryableError(f"status {status_int}") from exc
        raise _SkipProvider() from exc

    def _openai_compatible(
        self,
        url: str,
        model: str,
        api_key: str,
        messages: List[dict],
        max_tokens: int,
        want_json: bool,
    ) -> str:
        """OpenAI chat-completions shape (Groq and OpenRouter, §13)."""
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if want_json:
            body["response_format"] = {"type": "json_object"}

        resp = self._request(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json_body=body,
        )
        self._raise_for_status(resp)
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise _SkipProvider() from exc

    # -- HTTP seam (single chokepoint; monkeypatchable in tests) -----------

    def _request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Issue one HTTP request. Tests may monkeypatch this method to count
        calls / inject fake responses. Timeouts are surfaced as retryable."""
        import httpx

        self.request_count += 1
        try:
            return httpx.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise _RetryableError("timeout") from exc
        except httpx.HTTPError as exc:
            raise _SkipProvider() from exc

    @staticmethod
    def _raise_for_status(resp: Any) -> None:
        """Classify an HTTP response: 429/5xx → retryable; other non-2xx → skip."""
        status = getattr(resp, "status_code", 200)
        if status == 429 or status >= 500:
            raise _RetryableError(f"status {status}")
        if status >= 400:
            raise _SkipProvider()

    # -- json parsing / validation -----------------------------------------

    @staticmethod
    def _try_parse(raw: str, json_schema: Optional[dict]) -> Optional[dict]:
        """Parse ``raw`` as JSON and validate against ``json_schema`` via a
        dynamically-built pydantic model. Returns the dict or ``None`` on
        failure."""
        try:
            data = json.loads(_strip_fences(raw))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        model = _model_from_schema(json_schema or {})
        if model is None:
            return data
        try:
            return model.model_validate(data).model_dump()
        except ValidationError:
            return None

    @staticmethod
    def _augment_for_json(messages: List[dict]) -> List[dict]:
        """Append a terse 'reply with valid JSON only' nudge for the re-ask."""
        return list(messages) + [
            {
                "role": "user",
                "content": "Reply with valid JSON only — no prose, no markdown fences.",
            }
        ]

    # -- keys ---------------------------------------------------------------

    @staticmethod
    def _env_var(provider: str) -> str:
        return {
            "gemini": "GEMINI_API_KEY",
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }.get(provider, "")

    def _key(self, provider: str) -> str:
        return os.getenv(self._env_var(provider), "") or ""

    def _has_key(self, provider: str) -> bool:
        return bool(self._key(provider).strip())
