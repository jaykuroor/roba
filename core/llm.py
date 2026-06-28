"""Structured LLM provider layer (§13) — Vertex AI backend.

``LLMProvider.complete`` walks the configured provider chain
(``gemini -> canned`` by default). Gemini runs on **Vertex AI** via the
``google-genai`` SDK (``genai.Client(vertexai=True, ...)``) authenticated
by a service-account JSON (``roba.json``) or Application Default Credentials.
Auth and project resolution are handled by ``core.vertex``.

Hosted provider attempts use exponential backoff up to ``config.LLM_RETRIES``
retries (base ``config.LLM_BACKOFF_BASE_S``). A 429 / 5xx / timeout is retried;
if the project is missing or all attempts fail, the call-site receives its canned
fallback instead of an exception.

When ``json_schema`` is given the raw text is parsed and validated with a
pydantic model built dynamically from the schema; on a parse/validation failure
the provider is re-asked once, then the chain falls through (ultimately to the
canned response). Otherwise the raw string is returned.

Results are memoised in an in-process dict keyed by
``sha256(json.dumps(messages, sort_keys=True) + str(json_schema))``; calls with
``use_site="generation"`` always run fresh (never cached). Every call-site has a
canned fallback so the demo never crashes when Vertex AI is not configured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Type, Union

from pydantic import BaseModel, ValidationError, create_model

from . import config
from .models import LLMCallLog

logger = logging.getLogger(__name__)

# Marker placed on canned dicts so call-sites (e.g. voice) can detect that the
# LLM did not actually answer and engage their own deterministic fallback.
CANNED_NOTE = "canned_fallback"

# Provider model (§13, pinned).
GEMINI_MODEL = config.GEMINI_MODEL


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
        db_session_factory: Optional[Callable[[], Any]] = None,
    ):
        requested_fallback = list(fallback if fallback is not None else config.LLM_FALLBACK)
        self.fallback = [
            provider
            for provider in requested_fallback
            if provider in {"gemini", "canned"}
        ]
        if "canned" not in self.fallback:
            self.fallback.append("canned")
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
        self.db_session_factory = db_session_factory

        # In-process cache (TTL = process lifetime, §13).
        self._cache: Dict[str, Union[str, dict]] = {}
        # Injectable sleep so tests don't actually block on backoff.
        self._sleep = time.sleep
        # Diagnostics: number of outbound provider requests actually issued.
        self.request_count = 0
        self._gemini_client: Optional[Any] = None
        self._last_call_meta: Dict[str, Any] = {}

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
            # Manual Demand Forecaster optimization: no overrides.
            "forecaster_optimization": {
                "item_adjustments": [],
                "global_notes": [],
                "memory_updates": [],
                "confidence": 0.0,
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
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
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
                    "temperature": temperature,
                    "top_p": top_p,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        use_cache = use_site != "generation"
        if use_cache and cache_key in self._cache:
            self._last_call_meta = {
                "provider": "cache",
                "cached": True,
                "fallback_used": False,
                "status": "ok",
                "error": None,
            }
            return self._cache[cache_key]

        want_json = json_schema is not None
        result: Optional[Union[str, dict]] = None

        for provider in self.fallback:
            if provider == "canned":
                result = self.canned(use_site)
                self._last_call_meta = {
                    "provider": "canned",
                    "cached": False,
                    "fallback_used": True,
                    "status": "fallback",
                    "error": None,
                }
                break

            if not self._has_key(provider):
                # Missing key: skip instantly (no network, no inter-call sleep).
                continue

            try:
                raw = self._attempt_provider(
                    provider, messages, json_schema, max_tokens, want_json, temperature, top_p
                )
            except _SkipProvider:
                continue

            if not want_json:
                result = raw
                self._last_call_meta = {
                    "provider": provider,
                    "cached": False,
                    "fallback_used": False,
                    "status": "ok",
                    "error": None,
                }
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
                        temperature,
                        top_p,
                    )
                    parsed = self._try_parse(raw2, json_schema)
                except _SkipProvider:
                    parsed = None
            if parsed is None:
                continue  # fall through to the next provider
            result = parsed
            self._last_call_meta = {
                "provider": provider,
                "cached": False,
                "fallback_used": False,
                "status": "ok",
                "error": None,
            }
            break

        if result is None:
            result = self.canned(use_site)
            self._last_call_meta = {
                "provider": "canned",
                "cached": False,
                "fallback_used": True,
                "status": "fallback",
                "error": "all providers skipped",
            }

        if use_cache:
            self._cache[cache_key] = result
        return result

    def complete_structured(
        self,
        prompt_id: str,
        response_model: Type[BaseModel],
        context: Dict[str, Any],
        use_site: str,
        timeout_s: Optional[float] = None,
        fallback: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Run a schema-bound LLM call and return data plus provider metadata.

        ``fallback`` and ``timeout_s`` are applied per-call without mutating the
        shared instance, so concurrent requests on the app-wide singleton are safe.
        """
        started = time.perf_counter()

        # Resolve per-call overrides without touching shared state.
        effective_fallback: List[str] = self.fallback
        if fallback is not None:
            effective_fallback = [
                p for p in fallback if p in {"gemini", "canned"}
            ] or ["canned"]
            if "canned" not in effective_fallback:
                effective_fallback.append("canned")
        effective_timeout = float(timeout_s) if timeout_s is not None else self.timeout_s

        messages = [
            {
                "role": "system",
                "content": f"Return JSON matching the {response_model.__name__} schema.",
            },
            {"role": "user", "content": json.dumps(context, sort_keys=True, default=str)},
        ]
        error = None
        data: Union[str, dict]
        try:
            # Temporarily override for this call only — done inline to avoid race.
            orig_fallback, orig_timeout = self.fallback, self.timeout_s
            self.fallback, self.timeout_s = effective_fallback, effective_timeout
            try:
                raw = self.complete(
                    messages,
                    json_schema=response_model.model_json_schema(),
                    use_site=use_site,
                )
            finally:
                self.fallback, self.timeout_s = orig_fallback, orig_timeout

            if isinstance(raw, dict) and raw.get("note") != CANNED_NOTE:
                data = response_model.model_validate(raw).model_dump(mode="json")
            elif isinstance(raw, dict):
                data = raw
            else:
                data = {"result": raw}
        except Exception as exc:  # noqa: BLE001 - structured wrapper degrades.
            error = f"{type(exc).__name__}: {exc}"
            data = self.canned(use_site)
            self._last_call_meta = {
                "provider": "canned",
                "cached": False,
                "fallback_used": True,
                "status": "failed",
                "error": error,
            }

        latency_ms = (time.perf_counter() - started) * 1000.0
        meta = dict(self._last_call_meta)
        result = {
            "data": data,
            "provider": meta.get("provider", "unknown"),
            "latency_ms": latency_ms,
            "cached": bool(meta.get("cached")),
            "fallback_used": bool(meta.get("fallback_used")),
            "error": error or meta.get("error"),
        }
        self._log_call(prompt_id, use_site, context, result)
        return result

    # -- provider attempt + retries ----------------------------------------

    def _attempt_provider(
        self,
        provider: str,
        messages: List[dict],
        json_schema: Optional[dict],
        max_tokens: int,
        want_json: bool,
        temperature: Optional[float],
        top_p: Optional[float],
    ) -> str:
        """Call one provider with exponential backoff over retryable errors.

        Returns the raw text. Raises :class:`_SkipProvider` once retries are
        exhausted or on a non-retryable error.
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                return self._call_provider(
                    provider, messages, json_schema, max_tokens, want_json, temperature, top_p
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
        temperature: Optional[float],
        top_p: Optional[float],
    ) -> str:
        if provider == "gemini":
            return self._gemini(messages, json_schema, max_tokens, want_json, temperature, top_p)
        raise _SkipProvider()

    # -- concrete providers -------------------------------------------------

    def _gemini(
        self,
        messages: List[dict],
        json_schema: Optional[dict],
        max_tokens: int,
        want_json: bool,
        temperature: Optional[float],
        top_p: Optional[float],
    ) -> str:
        """Gemini on Vertex AI via the google-genai SDK (vertexai=True)."""
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
            temperature=temperature,
            top_p=top_p,
            timeout_s=self.timeout_s,
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
                from . import vertex
                self._gemini_client = vertex.build_genai_client()
            except ImportError as exc:
                raise _SkipProvider() from exc
            except RuntimeError as exc:
                logger.debug("Vertex AI unavailable: %s", exc)
                raise _SkipProvider() from exc
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
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> Any:
        kwargs: Dict[str, Any] = {"max_output_tokens": max_tokens}
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        if top_p is not None:
            kwargs["top_p"] = float(top_p)
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        if want_json:
            kwargs["response_mime_type"] = "application/json"
            if json_schema:
                kwargs["response_json_schema"] = json_schema
        try:
            from google.genai import types

            if timeout_s:
                # google-genai expects the HTTP timeout in milliseconds.
                kwargs["http_options"] = types.HttpOptions(
                    timeout=int(timeout_s * 1000)
                )
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

    # -- provider availability -----------------------------------------------

    @staticmethod
    def _has_key(provider: str) -> bool:
        """Return True when the named provider can be attempted.

        For the Vertex AI backend ``provider="gemini"`` maps to
        ``vertex.vertex_available()`` — which checks that a GCP project is
        resolvable (via env var or service-account JSON) rather than an API key.
        """
        if provider == "gemini":
            from . import vertex
            return vertex.vertex_available()
        return False

    def _log_call(
        self,
        prompt_id: str,
        use_site: str,
        context: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        if self.db_session_factory is None:
            return
        session = self.db_session_factory()
        try:
            session.add(
                LLMCallLog(
                    prompt_id=prompt_id,
                    use_site=use_site,
                    provider=str(result.get("provider") or ""),
                    status="error" if result.get("error") else "ok",
                    latency_ms=float(result.get("latency_ms") or 0.0),
                    cached=1 if result.get("cached") else 0,
                    fallback_used=1 if result.get("fallback_used") else 0,
                    error=result.get("error"),
                    created_at=time.time(),
                    request=context,
                    response=result.get("data"),
                )
            )
            session.commit()
        finally:
            session.close()
