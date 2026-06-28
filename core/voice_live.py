"""Vertex AI Live API bridge for the Roba voice interface.

Architecture:
  Browser ⟷ our WS /ws/voice/live?role=<role>&mode=<mode>
           ⟷ Vertex AI Live session (google-genai SDK, vertexai=True)

The browser sends binary PCM16 @16kHz audio frames and optional JSON control
frames.  We relay audio to the Live session and forward audio output + transcript
events back.  Tool calls from the model (process_note, confirm_plan, etc.) are
executed server-side and their results returned to the session.

Falls back gracefully: when ``GOOGLE_CLOUD_PROJECT`` is unresolvable (no env var
and no ``roba.json``) or the genai import fails, the WS immediately sends
``{"type":"unavailable"}`` so the client can degrade to text + browser speech
synthesis.

Authentication: service-account JSON at ``roba.json`` (repo root) or the path in
``GOOGLE_APPLICATION_CREDENTIALS``, falling back to Application Default Credentials.
Handled by ``core.vertex.build_genai_client()``.

Audio spec (mandated by the Live API):
  • Browser → server: 16 kHz, mono, 16-bit LE PCM
  • Server → browser: 24 kHz, mono, 16-bit LE PCM

SDK notes (google-genai v2.8+):
  • session.send() is deprecated — use send_realtime_input / send_client_content
    / send_tool_response.
  • session.receive() yields chunks until turn_complete, then raises StopAsyncIteration.
    For a multi-turn session wrap it in ``while True:``.
  • chunk.data  → concatenated inline audio bytes (shortcut property)
  • chunk.text  → concatenated text parts (shortcut property)
  • chunk.server_content.input_transcription.text  → what the user said
  • chunk.server_content.output_transcription.text → what Roba said
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from .config import GEMINI_LIVE_MODEL

logger = logging.getLogger(__name__)

# How long (seconds) we wait for the Live session to produce a response
# before we log a heartbeat.  A silent user is normal in a voice session, so
# this is NOT fatal — we just keep listening.
_RESPONSE_TIMEOUT_S = 20.0
# Live API connection attempt timeout — guards ONLY the __aenter__ network IO,
# never the session lifetime.
_CONNECT_TIMEOUT_S = 10.0

# Exception type names that mean "the client (or the Live peer) hung up".
# Clients routinely open the voice WS on page load and tear it down moments
# later — React strict-mode double-mount, a quick navigation, or switching
# role — which races our connect handshake.  These are expected, not errors,
# so we log them quietly without a traceback.  Matched by name to avoid
# importing uvicorn/websockets internals.
_DISCONNECT_EXC_NAMES = {
    "WebSocketDisconnect",
    "ClientDisconnected",
    "ConnectionClosed",
    "ConnectionClosedOK",
    "ConnectionClosedError",
}


def _is_disconnect(exc: BaseException) -> bool:
    """True if ``exc`` represents a normal client/peer disconnect."""
    if type(exc).__name__ in _DISCONNECT_EXC_NAMES:
        return True
    # google-genai raises APIError with code 1000 on a normal session close.
    return getattr(exc, "code", None) == 1000


async def _safe_send_json(websocket: Any, payload: Dict[str, Any]) -> bool:
    """Send a JSON frame, returning False if the client has gone away.

    A disconnect here is expected (see _DISCONNECT_EXC_NAMES) and never raises.
    """
    try:
        await websocket.send_json(payload)
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_disconnect(exc):
            logger.debug("client disconnected before send: %s", exc)
        else:
            logger.warning("websocket send failed: %s", exc)
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Tool schema declarations (sent to Vertex AI Live in LiveConnectConfig)
# ──────────────────────────────────────────────────────────────────────────────

_TOOLS: list[dict[str, Any]] = [
    {
        "function_declarations": [
            {
                "name": "process_note",
                "description": (
                    "Process a spoken operational note from the restaurant staff. "
                    "Returns a human-readable summary of what Roba understood and "
                    "what signal or action will be created."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The verbatim transcription of the spoken note.",
                        }
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "confirm_plan",
                "description": "Apply a pending plan that the user has confirmed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_id": {"type": "string", "description": "The plan_id to apply."}
                    },
                    "required": ["plan_id"],
                },
            },
            {
                "name": "cancel_plan",
                "description": "Cancel a pending plan that the user rejected.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_id": {"type": "string", "description": "The plan_id to cancel."}
                    },
                    "required": ["plan_id"],
                },
            },
            {
                "name": "mark_batch_cooked",
                "description": "Record that the cook has finished cooking a batch.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {
                            "type": "string",
                            "description": "The dish name that was cooked.",
                        },
                        "actual_qty": {
                            "type": "number",
                            "description": "How many portions were actually made.",
                        },
                    },
                    "required": ["item_name", "actual_qty"],
                },
            },
            {
                "name": "report_waste",
                "description": (
                    "Report that food was thrown away. If the cause is unclear, "
                    "ask the cook a follow-up question before calling this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string"},
                        "qty": {"type": "number"},
                        "cause": {
                            "type": "string",
                            "enum": ["overproduction", "spoilage", "prep_error"],
                        },
                    },
                    "required": ["item_name", "qty", "cause"],
                },
            },
            {
                "name": "request_competitor_call",
                "description": (
                    "Request an approval-gated call to a competitor or supplier. "
                    "Only available to the manager role."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Name of the competitor or supplier."},
                        "counterparty_type": {
                            "type": "string",
                            "enum": ["competitor", "supplier"],
                        },
                        "purpose": {"type": "string"},
                    },
                    "required": ["target", "counterparty_type", "purpose"],
                },
            },
            {
                "name": "get_kitchen_status",
                "description": (
                    "Look up the CURRENT, live state of the restaurant to answer a factual question. "
                    "Use this before answering ANY question about whether a dish's batch was prepared "
                    "or cooked, whether a batch should be cooked, how many portions were made, what "
                    "has been approved or is pending, forecasts, or inventory levels. "
                    "Never guess or answer from memory — always call this tool first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dish": {
                            "type": "string",
                            "description": (
                                "Optional: the dish name or number (e.g. '3', '#3', 'Margherita', 'dish 3'). "
                                "Provide this when the question is about a specific dish."
                            ),
                        },
                        "topic": {
                            "type": "string",
                            "enum": ["all", "batches", "approvals", "forecast", "inventory"],
                            "description": "What aspect to focus on. Default: 'all'.",
                        },
                    },
                    "required": [],
                },
            },
            # ── granular read-only data lookups ─────────────────────────────
            # get_kitchen_status is the broad "live state" tool; these return
            # focused, fresh data (with names and units) for specific domains.
            {
                "name": "get_inventory",
                "description": (
                    "Look up current on-hand inventory. Returns ingredient names, "
                    "quantities, and units (g, ml, or each). Use this to answer "
                    "'how many/much X do we have'. Pass item_name to filter to one "
                    "ingredient (e.g. 'tomato'). Always state the unit in your answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {
                            "type": "string",
                            "description": "Optional ingredient name to filter by, e.g. 'tomato'.",
                        }
                    },
                },
            },
            {
                "name": "get_forecast",
                "description": (
                    "Look up the latest demand forecasts (expected quantities per "
                    "menu item and daypart). Pass item_name to filter to one dish."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {
                            "type": "string",
                            "description": "Optional menu item name to filter by.",
                        }
                    },
                },
            },
            {
                "name": "get_competitors",
                "description": (
                    "Look up competitor profiles (rating, price tier, distance) and "
                    "recent market-intelligence observations (opportunities/threats)."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_reviews",
                "description": "Look up recent customer reviews and derived insights.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_staff",
                "description": "Look up the staff roster and which stations each person covers.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_signals",
                "description": (
                    "Look up the currently-active operational signals on the bus "
                    "(constraints, demand forecasts, alerts, etc.)."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_batches",
                "description": (
                    "Look up upcoming and in-progress production batches (planned "
                    "quantities, status, serve windows)."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        ]
    }
]

# System instructions per role.
_SYSTEM_INSTRUCTIONS: Dict[str, str] = {
    "manager": (
        "You are Roba, the AI operations desk for this restaurant. "
        "You are a TWO-WAY interface: you BOTH answer questions about current restaurant state "
        "AND record operational updates from the manager. "
        "\n\nANSWERING QUESTIONS: For any factual question about current state — whether a batch "
        "was cooked, what is approved or pending, demand forecasts, inventory, competitor intel — "
        "ALWAYS call a read tool first, then answer from its result. Never guess. "
        "Use get_kitchen_status for batch/approval/overall state; for a focused lookup you may "
        "call get_inventory, get_forecast, get_competitors, get_reviews, get_staff, get_signals, "
        "or get_batches. When reporting an inventory quantity, always include its unit (g, ml, or "
        "each) and never read a raw number as an item count. "
        "\n\nRECORDING UPDATES: When the manager reports something (a constraint, a decision, a "
        "competitor note), call process_note with the exact transcription. "
        "Always confirm back in plain language what action will be taken and ask for confirmation "
        "in confirm-first mode before applying it. "
        "For outbound calls to competitors or suppliers, use request_competitor_call to create "
        "an approval-gated request. "
        "\n\nBATCH STATUS VOCABULARY (use these terms exactly): "
        "state=awaiting_approval means the batch is not yet cleared to cook; "
        "state=ready_to_cook means it is approved and should be cooked now; "
        "state=cooked means it has already been prepared (status=ready in the system); "
        "state=skipped means the batch decision was to skip it. "
        "The system never uses 'prepping' or 'served' as batch states. "
        "\n\nKeep responses concise and actionable."
    ),
    "cook": (
        "You are Roba, the AI kitchen desk. "
        "You are a TWO-WAY interface: you BOTH answer questions about batch state and "
        "AND record what the cook has done. "
        "\n\nANSWERING QUESTIONS: For any question about whether a batch was cooked, "
        "what needs to be cooked, what is approved or pending — ALWAYS call get_kitchen_status "
        "first, then answer from its result. Never guess. For focused lookups you may also call "
        "get_inventory, get_batches, get_forecast, or get_signals. Always include units (g, ml, "
        "each) when reporting an inventory quantity. "
        "\n\nRECORDING UPDATES: When the cook reports completing a batch, call mark_batch_cooked. "
        "When they report throwing food away, call report_waste. "
        "If the cause of waste is unclear, always ask before reporting. "
        "\n\nBATCH STATUS VOCABULARY: "
        "ready_to_cook = approved and should be cooked now; "
        "cooked = already prepared (status=ready); "
        "awaiting_approval = waiting for manager sign-off; "
        "skipped = decided not to cook. "
        "\n\nKeep responses brief and kitchen-friendly — one or two sentences max."
    ),
}


async def live_bridge(
    websocket: Any,
    voice_processor: Any,
    role: str = "manager",
    mode: str = "confirm",
) -> None:
    """Bridge a client WebSocket to a Vertex AI Live session.

    Spawns two tasks:
    1. ``_client_to_gemini``: reads from the client WS and writes to the Live session.
    2. ``_gemini_to_client``: reads from the Live session and writes to the client WS.
    Tool calls from the model are executed server-side and results fed back.

    Exits cleanly on client disconnect, session end, or any unrecoverable error.
    """
    from . import vertex

    if not vertex.vertex_available():
        await _safe_send_json(websocket, {"type": "unavailable", "reason": "no_gcp_project"})
        return

    try:
        from google.genai import types as _gtypes
        client = vertex.build_genai_client()
    except ImportError:
        await _safe_send_json(websocket, {"type": "unavailable", "reason": "genai_not_installed"})
        return
    except RuntimeError as exc:
        logger.warning("Vertex AI Live unavailable: %s", exc)
        await _safe_send_json(websocket, {"type": "unavailable", "reason": str(exc)})
        return

    system_instruction = _SYSTEM_INSTRUCTIONS.get(role, _SYSTEM_INSTRUCTIONS["manager"])

    # Append live restaurant context to the system prompt.
    try:
        context = voice_processor._restaurant_context_for_prompt()
        system_instruction = system_instruction + f"\n\nRestaurant context:\n{context}"
    except Exception:  # noqa: BLE001
        pass

    live_config = _gtypes.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_instruction,
        tools=_TOOLS,  # type: ignore[arg-type]
        input_audio_transcription=_gtypes.AudioTranscriptionConfig(),
        output_audio_transcription=_gtypes.AudioTranscriptionConfig(),
    )

    # ``client.aio.live.connect(...)`` only builds the async context manager;
    # the real TCP/TLS handshake happens at ``__aenter__``.  Enter it manually
    # so the timeout wraps ONLY the handshake.  (A ``async with asyncio.timeout``
    # around the whole ``async with connect_ctx as session`` body would also
    # time-bound the entire conversation, killing every session after
    # _CONNECT_TIMEOUT_S — which is exactly the "always times out" bug.)
    connect_ctx = client.aio.live.connect(model=GEMINI_LIVE_MODEL, config=live_config)
    try:
        async with asyncio.timeout(_CONNECT_TIMEOUT_S):
            session = await connect_ctx.__aenter__()
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("Vertex AI Live session connect timed out")
        await _safe_send_json(websocket, {"type": "unavailable", "reason": "connect_timeout"})
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vertex AI Live connect failed: %s", exc)
        await _safe_send_json(websocket, {"type": "unavailable", "reason": str(exc)})
        return

    try:
        # If the client already went away during the handshake, exit quietly.
        if not await _safe_send_json(
            websocket, {"type": "connected", "model": GEMINI_LIVE_MODEL}
        ):
            return

        task_c2g = asyncio.create_task(
            _client_to_gemini(websocket, session, voice_processor),
            name="voice_live_c2g",
        )
        task_g2c = asyncio.create_task(
            _gemini_to_client(websocket, session, voice_processor, role, mode),
            name="voice_live_g2c",
        )
        done, pending = await asyncio.wait(
            [task_c2g, task_g2c], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for t in done:
            exc = t.exception()
            if exc:
                logger.warning("voice live task raised: %s", exc)
    except Exception as exc:  # noqa: BLE001
        if _is_disconnect(exc):
            logger.info("voice live client disconnected: %s", exc)
        else:
            logger.exception("Vertex AI Live session error: %s", exc)
            await _safe_send_json(websocket, {"type": "error", "message": str(exc)})
    finally:
        try:
            await connect_ctx.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


async def _client_to_gemini(
    websocket: Any, session: Any, voice_processor: Any
) -> None:
    """Read frames from the browser WS and relay to the Vertex AI Live session.

    Binary frames → PCM16 audio (send_realtime_input).
    JSON control frames:
      end_of_turn   → audio_stream_end signal to the session.
      text_input    → send_client_content text turn.
      confirm_plan  → execute voice_processor.confirm and return tool_result.
      cancel_plan   → execute voice_processor.cancel and return tool_result.
    """
    from google.genai import types as _gtypes

    try:
        while True:
            try:
                data = await websocket.receive()
            except Exception:
                return  # client disconnected

            if "bytes" in data and data["bytes"]:
                await session.send_realtime_input(
                    media=_gtypes.Blob(
                        data=data["bytes"],
                        mime_type="audio/pcm;rate=16000",
                    )
                )
            elif "text" in data and data["text"]:
                try:
                    msg = json.loads(data["text"])
                except json.JSONDecodeError:
                    continue
                msg_type = msg.get("type")
                if msg_type == "end_of_turn":
                    # Signal that the audio stream for this turn is done.
                    await session.send_realtime_input(audio_stream_end=True)
                elif msg_type == "text_input":
                    text = str(msg.get("text") or "")
                    if text:
                        await session.send_client_content(
                            turns={"parts": [{"text": text}]},
                            turn_complete=True,
                        )
                elif msg_type in ("confirm_plan", "cancel_plan"):
                    # The client Confirm/Cancel buttons send these frames; execute
                    # the plan action and relay the result back to the browser so
                    # the plan card can clear and the transcript can update.
                    plan_id = str(msg.get("plan_id") or "")
                    if plan_id:
                        try:
                            if msg_type == "confirm_plan":
                                result = await asyncio.to_thread(
                                    voice_processor.confirm, plan_id
                                )
                                tool = "confirm_plan"
                            else:
                                result = await asyncio.to_thread(
                                    voice_processor.cancel, plan_id
                                )
                                tool = "cancel_plan"
                            await _safe_send_json(
                                websocket,
                                {"type": "tool_result", "tool": tool, "result": result},
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("voice %s failed: %s", msg_type, exc)
    except asyncio.CancelledError:
        pass


class _TurnBuffer:
    """Accumulates streamed transcription so we emit one complete line per turn.

    Vertex AI Live streams transcription as many small deltas.  Rather than
    forward each as its own bubble, we buffer per role and flush a single
    finished line at the turn boundary (final-only display).
    """

    def __init__(self) -> None:
        self.user: list[str] = []
        self.roba: list[str] = []

    def take(self, role: str) -> str:
        parts = self.user if role == "user" else self.roba
        text = "".join(parts).strip()
        if role == "user":
            self.user = []
        else:
            self.roba = []
        return text


async def _flush_transcript(websocket: Any, role: str, buffers: "_TurnBuffer") -> None:
    """Emit the buffered text for ``role`` as one final transcript line."""
    text = buffers.take(role)
    if text:
        await _safe_send_json(
            websocket,
            {"type": "transcript", "role": role, "text": text, "final": True},
        )


async def _gemini_to_client(
    websocket: Any,
    session: Any,
    voice_processor: Any,
    role: str,
    mode: str,
) -> None:
    """Read from the Vertex AI Live session and relay audio/events to the browser WS.

    session.receive() yields chunks until turn_complete, then raises
    StopAsyncIteration.  We wrap it in a ``while True`` loop for multi-turn.
    Each chunk is inspected for:
      chunk.data  → binary audio PCM16 @ 24kHz → send to browser as bytes
      chunk.text  → text content → send as transcript frame
      chunk.server_content.input_transcription  → what the user said
      chunk.server_content.output_transcription → what Roba said
      chunk.tool_call.function_calls  → execute server-side, return results
    """
    from google.genai import types as _gtypes

    buffers = _TurnBuffer()
    try:
        while True:
            try:
                async with asyncio.timeout(_RESPONSE_TIMEOUT_S):
                    async for chunk in session.receive():
                        await _handle_chunk(
                            chunk, websocket, session, voice_processor,
                            role, mode, _gtypes, buffers,
                        )
            except asyncio.TimeoutError:
                # No model output for a while — a silent user is normal in a
                # voice session, so keep listening instead of tearing it down.
                logger.debug("Vertex AI Live idle (no output in %.0fs)", _RESPONSE_TIMEOUT_S)
                continue
            except StopAsyncIteration:
                # Normal end of a turn — loop for the next one.
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_disconnect(exc):
                    # The Live peer (or client) closed the session normally.
                    logger.info("Vertex AI Live session closed: %s", exc)
                    return
                logger.exception("Vertex AI Live receive error: %s", exc)
                await _safe_send_json(websocket, {"type": "error", "message": str(exc)})
                return
    except asyncio.CancelledError:
        pass


async def _handle_chunk(
    chunk: Any,
    websocket: Any,
    session: Any,
    voice_processor: Any,
    role: str,
    mode: str,
    _gtypes: Any,
    buffers: "_TurnBuffer",
) -> None:
    """Process one LiveServerMessage chunk."""
    # Binary audio (shortcut .data property concatenates all inline_data parts).
    if chunk.data:
        try:
            await websocket.send_bytes(chunk.data)
        except Exception:
            return

    sc = chunk.server_content
    if sc is not None:
        # Accumulate streamed transcription; we emit one complete line per turn
        # (final-only) rather than forwarding every partial fragment.
        if sc.input_transcription and sc.input_transcription.text:
            buffers.user.append(sc.input_transcription.text)

        # Roba's words come either as output_transcription (AUDIO mode) or
        # chunk.text (TEXT-modality fallback).
        roba_text = None
        if sc.output_transcription and sc.output_transcription.text:
            roba_text = sc.output_transcription.text
        elif chunk.text:
            roba_text = chunk.text
        if roba_text:
            # The user's turn is over once Roba starts replying — flush it first
            # so the user line appears as a complete sentence before Roba's.
            await _flush_transcript(websocket, "user", buffers)
            buffers.roba.append(roba_text)

        # Turn boundaries: flush finished lines, and signal turn completion so
        # the client can stop showing "speaking" and re-arm.
        if getattr(sc, "generation_complete", False):
            await _flush_transcript(websocket, "roba", buffers)
        if getattr(sc, "interrupted", False):
            # Barge-in: tell the browser to stop playback and resume listening.
            await _flush_transcript(websocket, "user", buffers)
            await _flush_transcript(websocket, "roba", buffers)
            await _safe_send_json(websocket, {"type": "interrupted"})
        if getattr(sc, "turn_complete", False):
            await _flush_transcript(websocket, "user", buffers)
            await _flush_transcript(websocket, "roba", buffers)
            await _safe_send_json(websocket, {"type": "turn_complete"})

    # Tool calls from the model.
    if chunk.tool_call:
        for fn_call in (chunk.tool_call.function_calls or []):
            result = await _execute_tool(fn_call, voice_processor, role, mode)
            # Return the result to Gemini so it can speak the outcome.
            try:
                await session.send_tool_response(
                    function_responses=_gtypes.FunctionResponse(
                        id=fn_call.id,
                        name=fn_call.name,
                        response=result,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("send_tool_response failed: %s", exc)
            # Also tell the browser UI so it can render the plan card etc.
            try:
                await websocket.send_json({
                    "type": "tool_result",
                    "tool": fn_call.name,
                    "result": result,
                })
            except Exception:
                pass


async def _execute_tool(
    fn_call: Any,
    voice_processor: Any,
    role: str,
    mode: str,
) -> Dict[str, Any]:
    """Execute a Vertex AI Live tool call server-side and return the result dict."""
    name = str(fn_call.name or "")
    args = dict(fn_call.args or {})
    logger.debug("voice_live tool call: %s(%s)", name, args)

    try:
        # ── read-only lookups ───────────────────────────────────────────────
        # All run synchronous DB queries; offload to a worker thread so the
        # audio relay loop is never blocked.
        if name == "get_kitchen_status":
            return await asyncio.to_thread(
                voice_processor.kitchen_status,
                dish=args.get("dish") or None,
                topic=str(args.get("topic", "all")),
            )
        if name == "get_inventory":
            return await asyncio.to_thread(
                voice_processor.query_inventory, args.get("item_name") or None
            )
        if name == "get_forecast":
            return await asyncio.to_thread(
                voice_processor.query_forecast, args.get("item_name") or None
            )
        if name == "get_competitors":
            return await asyncio.to_thread(voice_processor.query_competitors)
        if name == "get_reviews":
            return await asyncio.to_thread(voice_processor.query_reviews)
        if name == "get_staff":
            return await asyncio.to_thread(voice_processor.query_staff)
        if name == "get_signals":
            return await asyncio.to_thread(voice_processor.query_signals)
        if name == "get_batches":
            return await asyncio.to_thread(voice_processor.query_batches)

        # ── write / action tools ────────────────────────────────────────────
        # voice_processor.plan() runs a synchronous LLM extraction; offload it
        # too so it can't stall the event loop (and the audio stream).
        if name == "process_note":
            result = await asyncio.to_thread(
                voice_processor.plan, args.get("text", ""), role=role, mode=mode
            )
            return {
                "plan_id": result.get("plan_id"),
                "human_readable": result.get("human_readable", ""),
                "status": result.get("status", "pending"),
                "requires_approval": result.get("requires_approval", False),
                "clarification": result.get("clarification"),
            }

        if name == "confirm_plan":
            return await asyncio.to_thread(
                voice_processor.confirm, str(args.get("plan_id", ""))
            )

        if name == "cancel_plan":
            return await asyncio.to_thread(
                voice_processor.cancel, str(args.get("plan_id", ""))
            )

        if name == "mark_batch_cooked":
            item_name = str(args.get("item_name", ""))
            qty = float(args.get("actual_qty", 0))
            text = f"I cooked the {item_name} batch, made {qty:.0f}"
            result = await asyncio.to_thread(
                voice_processor.plan, text, role="cook", mode="auto"
            )
            return {"status": "applied", "summary": result.get("summary", "")}

        if name == "report_waste":
            item_name = str(args.get("item_name", ""))
            qty = float(args.get("qty", 0))
            cause = str(args.get("cause", "overproduction"))
            text = f"I threw away {qty:.0f} {item_name} because of {cause}"
            result = await asyncio.to_thread(
                voice_processor.plan, text, role="cook", mode="auto"
            )
            return {"status": "applied", "summary": result.get("summary", "")}

        if name == "request_competitor_call":
            target = str(args.get("target", ""))
            purpose = str(args.get("purpose", ""))
            text = f"call {target}: {purpose}"
            result = await asyncio.to_thread(
                voice_processor.plan, text, role="manager", mode="confirm"
            )
            return {"status": result.get("status", "pending"), "plan_id": result.get("plan_id")}

    except Exception as exc:  # noqa: BLE001
        logger.exception("voice_live tool %s failed: %s", name, exc)
        return {"error": str(exc)}

    return {"error": f"Unknown tool: {name}"}
