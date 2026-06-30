"""Vertex AI Live API bridge for the Roba voice interface.

Architecture:
  Browser ⟷ our WS /ws/voice/live?role=<role>&mode=<mode>&mic_mode=<mic_mode>&model=<model>
           ⟷ Vertex AI Live session (google-genai SDK, vertexai=True)

The browser sends binary PCM16 @16kHz audio frames and optional JSON control
frames.  We relay audio to the Live session and forward audio output + transcript
events back.

Tool calls from the model are dispatched to ``VoiceActions`` — the single
deterministic dispatch seam.  The old ``process_note`` / ``VoiceProcessor.plan``
double-LLM path is NOT used for live voice; it remains only behind the text REST
endpoints.

Transcript streaming:
  Each turn gets a stable ``turn_id``.  Partial transcript frames are emitted
  as the model speaks/listens (cumulative text, same turn_id, final=False).
  A final flush emits final=True so the frontend can freeze the bubble.

Authentication: service-account JSON at ``roba.json`` (repo root) or the path in
``GOOGLE_APPLICATION_CREDENTIALS``, falling back to Application Default Credentials.
Handled by ``core.vertex.build_genai_client()``.

Model selection:
  • Default: ``GEMINI_LIVE_MODEL`` from config / env.
  • Override: ``model`` query param on the WS URL (validated against allowlist).
  • Allowed models listed in ``_ALLOWED_LIVE_MODELS``.

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
import uuid
from typing import Any, Dict, Optional

from .config import GEMINI_LIVE_MODEL

logger = logging.getLogger(__name__)

_RESPONSE_TIMEOUT_S = 20.0
_CONNECT_TIMEOUT_S = 10.0

# Models the UI may select via the model= query param.
# These must be actual Vertex AI Live API model IDs (use `client.models.list()` to verify).
_ALLOWED_LIVE_MODELS = {
    "gemini-live-2.5-flash-native-audio",   # native voice, GA on Vertex
}

# Hardcoded fallback — used when GEMINI_LIVE_MODEL env var contains an invalid name.
_FALLBACK_LIVE_MODEL = "gemini-live-2.5-flash-native-audio"

_DISCONNECT_EXC_NAMES = {
    "WebSocketDisconnect",
    "ClientDisconnected",
    "ConnectionClosed",
    "ConnectionClosedOK",
    "ConnectionClosedError",
}


def _is_disconnect(exc: BaseException) -> bool:
    if type(exc).__name__ in _DISCONNECT_EXC_NAMES:
        return True
    return getattr(exc, "code", None) == 1000


async def _safe_send_json(websocket: Any, payload: Dict[str, Any]) -> bool:
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
# Tool schema declarations
# ──────────────────────────────────────────────────────────────────────────────

_TOOLS: list[dict[str, Any]] = [
    {
        "function_declarations": [
            # ── Read tools (never require confirmation) ────────────────────
            {
                "name": "get_inventory",
                "description": (
                    "Look up current on-hand inventory with ingredient names, quantities, "
                    "and units (g, ml, or each). Use this for ANY question about inventory "
                    "quantities. Pass item_name to filter to one ingredient. "
                    "Pass sort='expiring_soonest' to find items expiring earliest. "
                    "ALWAYS state the unit (g/ml/each) in your answer — never read raw numbers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string", "description": "Optional ingredient name filter, e.g. 'tomato'."},
                        "sort": {"type": "string", "enum": ["expiring_soonest"], "description": "Sort mode."},
                    },
                },
            },
            {
                "name": "get_forecast",
                "description": (
                    "Look up demand forecasts (expected quantities per menu item and daypart). "
                    "If no forecast exists, automatically runs the forecaster and returns fresh values. "
                    "Pass item_name to filter to one dish. Pass daypart to filter (breakfast/lunch/dinner/etc)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string", "description": "Optional menu item filter."},
                        "daypart": {"type": "string", "description": "Optional daypart filter."},
                    },
                },
            },
            {
                "name": "get_batches",
                "description": (
                    "Look up production batches. Use to answer: 'what batches are scheduled', "
                    "'has X been cooked', 'what is approved but not confirmed'. "
                    "status can be comma-separated, e.g. 'approved,decided' for upcoming. "
                    "Batch states: approved=ready to cook, decided=awaiting approval, ready=cooked, skipped=skipped."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Filter by status (decided/approved/ready/skipped), comma-separated."},
                        "dish": {"type": "string", "description": "Optional dish name filter."},
                    },
                },
            },
            {
                "name": "get_menu",
                "description": (
                    "Look up menu items. Use filter='disabled' (default) to see which items are "
                    "currently off and why, or filter='all' for all items."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filter": {"type": "string", "enum": ["disabled", "all"]},
                    },
                },
            },
            {
                "name": "get_pos_stats",
                "description": (
                    "Look up POS sales data. Use for: 'what is selling most in the last 3 hours', "
                    "'how many margheritas since 4pm', 'top items today'. "
                    "window examples: '3h', '30m', '1d'. Pass item_name to filter to one dish."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "window": {"type": "string", "description": "Time window, e.g. '3h', '30m', '1d'."},
                        "item_name": {"type": "string", "description": "Optional dish filter."},
                    },
                    "required": ["window"],
                },
            },
            {
                "name": "get_competitors",
                "description": "Look up competitor profiles and recent market-intelligence observations (promotions, threats, opportunities).",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_reviews",
                "description": (
                    "Look up recent customer reviews and insights. "
                    "Pass sort='most_hated' to find the most negatively-reviewed dishes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sort": {"type": "string", "enum": ["most_hated"]},
                    },
                },
            },
            {
                "name": "get_staff",
                "description": "Look up the staff roster, roles, and which stations each person covers.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_supplier_prices",
                "description": (
                    "Look up supplier prices and availability for ingredients. "
                    "Pass ingredient_name to filter, e.g. 'tomato' returns all supplier prices for tomatoes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ingredient_name": {"type": "string", "description": "Optional ingredient filter."},
                    },
                },
            },
            {
                "name": "get_signals",
                "description": "Look up currently-active operational signals (constraints, alerts, demand forecasts).",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_kitchen_status",
                "description": (
                    "Broad live-state lookup. Use for overall kitchen overview, "
                    "pending approvals, or when a more specific tool isn't the right fit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dish": {"type": "string", "description": "Optional dish name or number."},
                        "topic": {"type": "string", "enum": ["all", "batches", "approvals", "forecast", "inventory"]},
                    },
                },
            },

            # ── Write tools (confirm/auto governed; outbound call always staged) ──
            {
                "name": "disable_menu_item",
                "description": (
                    "Disable (turn off) a menu item so it cannot be ordered. "
                    "This is a manual sticky disable — it stays off until explicitly re-enabled. "
                    "Use this when the manager says 'disable X', 'take X off the menu', etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string", "description": "The dish name to disable."},
                        "reason": {"type": "string", "description": "Optional reason for the disable."},
                    },
                    "required": ["item_name"],
                },
            },
            {
                "name": "enable_menu_item",
                "description": "Re-enable a disabled menu item. Clears all blocks (including manual and stock-based).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string", "description": "The dish name to re-enable."},
                    },
                    "required": ["item_name"],
                },
            },
            {
                "name": "adjust_inventory",
                "description": (
                    "Add to or set the inventory quantity for an ingredient. "
                    "Use set_to to set an exact quantity, or delta to add (positive) or subtract (negative). "
                    "Example: 'we just got 5kg of tomatoes' → delta=5000, unit='g', ingredient_name='tomato'. "
                    "Always specify the unit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ingredient_name": {"type": "string"},
                        "set_to": {"type": "number", "description": "Set on_hand to exactly this amount."},
                        "delta": {"type": "number", "description": "Add/subtract this amount (positive=add, negative=remove)."},
                        "unit": {"type": "string", "description": "Unit, e.g. 'g', 'ml', 'each'."},
                        "reason": {"type": "string"},
                    },
                    "required": ["ingredient_name"],
                },
            },
            {
                "name": "record_spoilage",
                "description": (
                    "Mark an INGREDIENT (not a dish) as spoiled. Reduces inventory, logs waste for the "
                    "forecaster and optimizer, and automatically disables dishes that needed that ingredient. "
                    "Use for: 'all tomatoes are spoiled', 'the mozzarella went bad', '2kg of flour spoiled'. "
                    "Set all_stock=true when EVERYTHING of that ingredient is gone."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ingredient_name": {"type": "string", "description": "The spoiled ingredient, e.g. 'tomato'."},
                        "qty": {"type": "number", "description": "How much spoiled (in the ingredient's base unit). Omit when all_stock=true."},
                        "all_stock": {"type": "boolean", "description": "True if ALL of this ingredient has spoiled."},
                    },
                    "required": ["ingredient_name"],
                },
            },
            {
                "name": "confirm_batch_cooked",
                "description": (
                    "Record that a batch has been cooked. Use for: 'I cooked the margherita batch', "
                    "'batch done, made 18'. Provide the dish name or batch id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dish_or_batch": {"type": "string", "description": "Dish name or batch id (e.g. '#5' or 'Margherita')."},
                        "actual_qty": {"type": "number", "description": "How many were actually made (optional, defaults to planned qty)."},
                    },
                    "required": ["dish_or_batch"],
                },
            },
            {
                "name": "record_waste",
                "description": (
                    "Record that a dish/batch was thrown away (overproduction, prep error, etc.). "
                    "For INGREDIENT spoilage, use record_spoilage instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string"},
                        "qty": {"type": "number"},
                        "cause": {"type": "string", "enum": ["overproduction", "spoilage", "prep_error"]},
                    },
                    "required": ["item_name", "qty", "cause"],
                },
            },
            {
                "name": "set_staff_attendance",
                "description": (
                    "Update a staff member's attendance status. The system will automatically "
                    "disable or re-enable dishes based on station coverage after the update. "
                    "Use for: 'head chef is sick', 'Marco is on leave today', 'chef is back'. "
                    "staff_name_or_role can be a name ('Marco') or a role ('head chef', 'cook')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "staff_name_or_role": {"type": "string", "description": "Staff name or role."},
                        "status": {"type": "string", "enum": ["sick", "leave", "present"]},
                        "daypart": {"type": "string", "description": "Optional daypart restriction."},
                    },
                    "required": ["staff_name_or_role", "status"],
                },
            },
            {
                "name": "run_forecast",
                "description": "Trigger the demand forecaster to generate fresh forecasts.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "run_inventory_optimizer",
                "description": "Trigger the inventory optimizer to run a fresh analysis.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "run_competitor_scan",
                "description": "Trigger a competitor market poll to get the latest competitor data.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "process_reviews",
                "description": "Process unprocessed customer reviews and generate insights.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "request_outbound_call",
                "description": (
                    "Request an approval-gated outbound call to a competitor or supplier. "
                    "Always requires manager approval before it proceeds."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Name of the competitor or supplier."},
                        "counterparty_type": {"type": "string", "enum": ["competitor", "supplier"]},
                        "purpose": {"type": "string"},
                    },
                    "required": ["target", "counterparty_type", "purpose"],
                },
            },
            {
                "name": "confirm_plan",
                "description": "Apply a pending action that the user has confirmed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_id": {"type": "string"},
                    },
                    "required": ["plan_id"],
                },
            },
            {
                "name": "cancel_plan",
                "description": "Cancel a pending action that the user rejected.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_id": {"type": "string"},
                    },
                    "required": ["plan_id"],
                },
            },
        ]
    }
]


# ──────────────────────────────────────────────────────────────────────────────
# System prompts
# ──────────────────────────────────────────────────────────────────────────────

_FEW_SHOTS = """
## Examples

[AUTO MODE — act immediately, no permission needed]

User: "How many tomatoes do we have?"
→ get_inventory(item_name="tomato")
→ "4,200 grams."

User: "All tomatoes spoiled."
→ record_spoilage(ingredient_name="tomato", all_stock=true)
→ "Done — tomatoes zeroed. Margherita and Bruschetta auto-disabled."

User: "Disable Margherita."
→ disable_menu_item(item_name="Margherita Pizza")
→ "Margherita disabled."

User: "Head chef is leaving sick."
→ set_staff_attendance(staff_name_or_role="head chef", status="sick")
→ "Marco marked sick. Pasta station unstaffed — Carbonara and Tagliatelle disabled."

[CONFIRM MODE — call the tool, speak human_readable verbatim, wait]

User: "Disable Margherita."
→ disable_menu_item(item_name="Margherita Pizza")
→ tool returns {requires_approval: true, human_readable: "Disable Margherita Pizza?"}
→ "Disable Margherita Pizza?"   ← speak ONLY this, nothing more

User: "Head chef is leaving sick."
→ set_staff_attendance(staff_name_or_role="head chef", status="sick")
→ tool returns {requires_approval: true, human_readable: "Mark Marco as sick?"}
→ "Mark Marco as sick?"

User: "What's selling most in the last 3 hours?"
→ get_pos_stats(window="3h")
→ "Margherita Pizza — 24 orders. Tiramisu second with 18."

User: "Tomato prices from all suppliers?"
→ get_supplier_prices(ingredient_name="tomato")
→ "FreshFarms €2.50/kg, MedSupply €2.20/kg."
"""

_BASE_RULES = """\
CORE RULES:
1. ALWAYS call a tool before answering any factual question. Never guess.
2. TRUTHFULNESS: State ONLY what the tool result confirms. If a tool returns an error, say so — never claim an action succeeded without a successful tool result.
3. CONCISENESS: Answer the specific question asked. Never recite entire lists when a specific answer was requested.
4. MISSING ARGS: If a tool returns {"need": ...}, ask the user for that one piece of info only.
"""

_AUTO_MODE_RULE = """\
MODE = AUTO: Call write tools immediately without asking permission. After the tool returns, \
briefly state what was done. Never say "I'll do X" before doing it — just do it and confirm.
"""

_CONFIRM_MODE_RULE = """\
MODE = CONFIRM: Call the write tool. If it returns requires_approval=true, \
speak ONLY the human_readable field verbatim as your reply — nothing else, no preamble, \
no "manager", no "approval", no formalities. It's already a short question. \
Wait for the human to say yes/no, then call confirm_plan or cancel_plan accordingly.
"""

def _make_prompt(role: str, mode: str) -> str:
    mode_rule = _AUTO_MODE_RULE if mode == "auto" else _CONFIRM_MODE_RULE
    if role == "cook":
        return (
            "You are Roba, the AI kitchen desk. Concise kitchen-friendly replies — one or two sentences.\n\n"
            + _BASE_RULES
            + mode_rule + "\n"
            "TOOL GUIDE:\n"
            "• Batch status → get_batches(status='approved,decided')\n"
            "• Did we cook X → get_batches(dish=..., status='ready')\n"
            "• Mark cooked → confirm_batch_cooked(dish_or_batch=..., actual_qty=...)\n"
            "• Waste (dish) → record_waste(item_name=..., qty=..., cause=...)\n"
            "• Ingredient spoiled → record_spoilage(ingredient_name=..., all_stock=...)\n"
            "• Inventory check → get_inventory(item_name=...)\n\n"
            "BATCH STATES: approved=cook now; decided=needs approval; ready=already cooked; skipped=skip.\n\n"
            + _FEW_SHOTS
        )
    return (
        "You are Roba, the AI operations desk for this restaurant — you answer questions AND record updates.\n\n"
        + _BASE_RULES
        + mode_rule + "\n"
        "TOOL GUIDE:\n"
        "• Inventory quantity → get_inventory(item_name=...)\n"
        "• Inventory expiry → get_inventory(sort='expiring_soonest')\n"
        "• Forecast → get_forecast(item_name=..., daypart=...)\n"
        "• Batch status → get_batches(status=..., dish=...)\n"
        "• What's disabled → get_menu(filter='disabled')\n"
        "• Sales / top sellers → get_pos_stats(window=..., item_name=...)\n"
        "• Competitor promos → get_competitors()\n"
        "• Review sentiment → get_reviews(sort='most_hated')\n"
        "• Staff presence → get_staff()\n"
        "• Supplier prices → get_supplier_prices(ingredient_name=...)\n"
        "• Spoilage (ingredient) → record_spoilage(ingredient_name=..., all_stock=...)\n"
        "• Disable/enable dish → disable_menu_item / enable_menu_item\n"
        "• Staff sick/leave → set_staff_attendance(staff_name_or_role=..., status=...)\n"
        "• Adjust stock → adjust_inventory(ingredient_name=..., set_to=... or delta=...)\n"
        "• Trigger agents → run_forecast / run_inventory_optimizer / run_competitor_scan / process_reviews\n"
        "• Outbound call → request_outbound_call (always staged for approval)\n\n"
        "BATCH STATUS VOCABULARY: 'decided'=awaiting approval; 'approved'=ready to cook; "
        "'ready'=cooked; 'skipped'=decided to skip.\n\n"
        + _FEW_SHOTS
    )


# ──────────────────────────────────────────────────────────────────────────────
# Bridge
# ──────────────────────────────────────────────────────────────────────────────

async def live_bridge(
    websocket: Any,
    voice_processor: Any,
    role: str = "manager",
    mode: str = "confirm",
    mic_mode: str = "ptt",
    model: Optional[str] = None,
    voice_actions: Optional[Any] = None,
) -> None:
    """Bridge a client WebSocket to a Vertex AI Live session.

    Parameters
    ----------
    voice_processor:
        Legacy VoiceProcessor — used for read delegates when voice_actions is None.
    voice_actions:
        VoiceActions instance.  If None, falls back to voice_processor methods.
    model:
        Live model override (validated against _ALLOWED_LIVE_MODELS).
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

    # Resolve the model — validate against the allowlist so a misconfigured
    # GEMINI_LIVE_MODEL env var doesn't silently produce a "model not found" error.
    if GEMINI_LIVE_MODEL in _ALLOWED_LIVE_MODELS:
        live_model = GEMINI_LIVE_MODEL
    else:
        logger.warning(
            "GEMINI_LIVE_MODEL=%r is not in the allowed list; falling back to %r",
            GEMINI_LIVE_MODEL, _FALLBACK_LIVE_MODEL,
        )
        live_model = _FALLBACK_LIVE_MODEL
    if model and model in _ALLOWED_LIVE_MODELS:
        live_model = model

    system_instruction = _make_prompt(role, mode)

    # Slim injected context: just key numbers so the model uses tools for details.
    try:
        slim_ctx = _build_slim_context(voice_processor)
        system_instruction = system_instruction + f"\n\nCurrent context (use tools for full data):\n{slim_ctx}"
    except Exception:  # noqa: BLE001
        pass

    realtime_input_config: Optional[Any] = None
    if mic_mode == "ptt":
        realtime_input_config = _gtypes.RealtimeInputConfig(
            automatic_activity_detection=_gtypes.AutomaticActivityDetection(disabled=True),
        )

    live_config = _gtypes.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_instruction,
        tools=_TOOLS,  # type: ignore[arg-type]
        input_audio_transcription=_gtypes.AudioTranscriptionConfig(),
        output_audio_transcription=_gtypes.AudioTranscriptionConfig(),
        realtime_input_config=realtime_input_config,
    )

    connect_ctx = client.aio.live.connect(model=live_model, config=live_config)
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
        if not await _safe_send_json(
            websocket, {"type": "connected", "model": live_model}
        ):
            return

        task_c2g = asyncio.create_task(
            _client_to_gemini(websocket, session, voice_processor, voice_actions),
            name="voice_live_c2g",
        )
        task_g2c = asyncio.create_task(
            _gemini_to_client(websocket, session, voice_processor, role, mode, voice_actions),
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
    websocket: Any, session: Any, voice_processor: Any, voice_actions: Optional[Any]
) -> None:
    """Read frames from the browser WS and relay to the Live session."""
    from google.genai import types as _gtypes

    try:
        while True:
            try:
                data = await websocket.receive()
            except Exception:
                return

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
                if msg_type == "activity_start":
                    await session.send_realtime_input(activity_start=_gtypes.ActivityStart())
                elif msg_type == "activity_end":
                    await session.send_realtime_input(activity_end=_gtypes.ActivityEnd())
                elif msg_type == "end_of_turn":
                    await session.send_realtime_input(audio_stream_end=True)
                elif msg_type == "text_input":
                    text = str(msg.get("text") or "")
                    if text:
                        await session.send_client_content(
                            turns={"parts": [{"text": text}]},
                            turn_complete=True,
                        )
                elif msg_type in ("confirm_plan", "cancel_plan"):
                    # Confirm/Cancel buttons from the browser UI.
                    plan_id = str(msg.get("plan_id") or "")
                    if plan_id:
                        try:
                            if msg_type == "confirm_plan":
                                if voice_actions is not None:
                                    result = await asyncio.to_thread(
                                        voice_actions.execute_pending, plan_id
                                    )
                                else:
                                    result = await asyncio.to_thread(
                                        voice_processor.confirm, plan_id
                                    )
                            else:
                                if voice_actions is not None:
                                    result = await asyncio.to_thread(
                                        voice_actions.cancel_pending, plan_id
                                    )
                                else:
                                    result = await asyncio.to_thread(
                                        voice_processor.cancel, plan_id
                                    )
                            await _safe_send_json(
                                websocket,
                                {"type": "tool_result", "tool": msg_type, "result": result},
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("voice %s failed: %s", msg_type, exc)
    except asyncio.CancelledError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Transcript buffering with partial streaming
# ──────────────────────────────────────────────────────────────────────────────

class _TurnBuffer:
    """Accumulates and streams transcript per turn.

    Generates a stable ``turn_id`` per turn and emits cumulative partial frames
    as text arrives, then a final frame at the turn boundary.  The frontend can
    do in-place replacement by (role, turn_id).
    """

    def __init__(self) -> None:
        self.user: list[str] = []
        self.roba: list[str] = []
        self._user_turn_id: str = str(uuid.uuid4())
        self._roba_turn_id: str = str(uuid.uuid4())

    def new_user_turn(self) -> str:
        self._user_turn_id = str(uuid.uuid4())
        return self._user_turn_id

    def new_roba_turn(self) -> str:
        self._roba_turn_id = str(uuid.uuid4())
        return self._roba_turn_id

    def turn_id(self, role: str) -> str:
        return self._user_turn_id if role == "user" else self._roba_turn_id

    def cumulative(self, role: str) -> str:
        parts = self.user if role == "user" else self.roba
        return "".join(parts).strip()

    def take(self, role: str) -> str:
        text = self.cumulative(role)
        if role == "user":
            self.user = []
        else:
            self.roba = []
        return text


async def _emit_partial(websocket: Any, role: str, buffers: "_TurnBuffer") -> None:
    """Emit a partial (in-progress) transcript frame for in-place display."""
    text = buffers.cumulative(role)
    if text:
        await _safe_send_json(websocket, {
            "type": "transcript",
            "role": role,
            "text": text,
            "turn_id": buffers.turn_id(role),
            "final": False,
        })


async def _flush_transcript(websocket: Any, role: str, buffers: "_TurnBuffer") -> None:
    """Emit the buffered text as a FINAL transcript line."""
    text = buffers.take(role)
    if text:
        await _safe_send_json(websocket, {
            "type": "transcript",
            "role": role,
            "text": text,
            "turn_id": buffers.turn_id(role),
            "final": True,
        })


async def _gemini_to_client(
    websocket: Any,
    session: Any,
    voice_processor: Any,
    role: str,
    mode: str,
    voice_actions: Optional[Any],
) -> None:
    """Read from the Vertex AI Live session and relay audio/events to the browser."""
    from google.genai import types as _gtypes

    buffers = _TurnBuffer()
    try:
        while True:
            try:
                async with asyncio.timeout(_RESPONSE_TIMEOUT_S):
                    async for chunk in session.receive():
                        await _handle_chunk(
                            chunk, websocket, session, voice_processor,
                            role, mode, _gtypes, buffers, voice_actions,
                        )
            except asyncio.TimeoutError:
                logger.debug("Vertex AI Live idle (no output in %.0fs)", _RESPONSE_TIMEOUT_S)
                continue
            except StopAsyncIteration:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_disconnect(exc):
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
    voice_actions: Optional[Any],
) -> None:
    """Process one LiveServerMessage chunk."""
    if chunk.data:
        try:
            await websocket.send_bytes(chunk.data)
        except Exception:
            return

    sc = chunk.server_content
    if sc is not None:
        # User STT partial — accumulate and emit partial frame.
        if sc.input_transcription and sc.input_transcription.text:
            if not buffers.user:
                buffers.new_user_turn()
            buffers.user.append(sc.input_transcription.text)
            await _emit_partial(websocket, "user", buffers)

        # Roba TTS text — accumulate and emit partial frame.
        roba_text = None
        if sc.output_transcription and sc.output_transcription.text:
            roba_text = sc.output_transcription.text
        elif chunk.text:
            roba_text = chunk.text
        if roba_text:
            # Flush user first so user line appears complete before roba's.
            await _flush_transcript(websocket, "user", buffers)
            if not buffers.roba:
                buffers.new_roba_turn()
            buffers.roba.append(roba_text)
            await _emit_partial(websocket, "roba", buffers)

        if getattr(sc, "generation_complete", False):
            await _flush_transcript(websocket, "roba", buffers)
        if getattr(sc, "interrupted", False):
            await _flush_transcript(websocket, "user", buffers)
            await _flush_transcript(websocket, "roba", buffers)
            await _safe_send_json(websocket, {"type": "interrupted"})
        if getattr(sc, "turn_complete", False):
            await _flush_transcript(websocket, "user", buffers)
            await _flush_transcript(websocket, "roba", buffers)
            await _safe_send_json(websocket, {"type": "turn_complete"})

    if chunk.tool_call:
        for fn_call in (chunk.tool_call.function_calls or []):
            result = await _execute_tool(fn_call, voice_processor, role, mode, voice_actions)
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
    voice_actions: Optional[Any],
) -> Dict[str, Any]:
    """Execute a tool call via VoiceActions (primary) or VoiceProcessor (fallback reads)."""
    name = str(fn_call.name or "")
    args = dict(fn_call.args or {})
    logger.debug("voice_live tool call: %s(%s)", name, args)

    va = voice_actions  # may be None in tests / legacy mode

    try:
        # ── Read tools ────────────────────────────────────────────────────
        if name == "get_inventory":
            if va:
                return await asyncio.to_thread(
                    va.get_inventory,
                    item_name=args.get("item_name") or None,
                    sort=args.get("sort") or None,
                )
            return await asyncio.to_thread(
                voice_processor.query_inventory, args.get("item_name") or None
            )

        if name == "get_forecast":
            if va:
                return await asyncio.to_thread(
                    va.get_forecast,
                    item_name=args.get("item_name") or None,
                    daypart=args.get("daypart") or None,
                )
            return await asyncio.to_thread(
                voice_processor.query_forecast, args.get("item_name") or None
            )

        if name == "get_batches":
            if va:
                return await asyncio.to_thread(
                    va.get_batches,
                    status=args.get("status") or None,
                    dish=args.get("dish") or None,
                )
            return await asyncio.to_thread(voice_processor.query_batches)

        if name == "get_menu":
            if va:
                return await asyncio.to_thread(va.get_menu, filter=args.get("filter", "disabled"))
            return {"error": "get_menu not available in legacy mode"}

        if name == "get_pos_stats":
            if va:
                return await asyncio.to_thread(
                    va.get_pos_stats,
                    window=str(args.get("window", "3h")),
                    item_name=args.get("item_name") or None,
                )
            return {"error": "get_pos_stats not available in legacy mode"}

        if name == "get_competitors":
            return await asyncio.to_thread(voice_processor.query_competitors)

        if name == "get_reviews":
            if va:
                return await asyncio.to_thread(va.get_reviews, sort=args.get("sort") or None)
            return await asyncio.to_thread(voice_processor.query_reviews)

        if name == "get_staff":
            return await asyncio.to_thread(voice_processor.query_staff)

        if name == "get_supplier_prices":
            if va:
                return await asyncio.to_thread(
                    va.get_supplier_prices,
                    ingredient_name=args.get("ingredient_name") or None,
                )
            return {"error": "get_supplier_prices not available in legacy mode"}

        if name == "get_signals":
            return await asyncio.to_thread(voice_processor.query_signals)

        if name == "get_kitchen_status":
            return await asyncio.to_thread(
                voice_processor.kitchen_status,
                dish=args.get("dish") or None,
                topic=str(args.get("topic", "all")),
            )

        # ── Write tools (mode governed) ───────────────────────────────────
        if name == "disable_menu_item":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.disable_menu_item,
                item_name=str(args.get("item_name", "")),
                reason=str(args.get("reason", "voice request")),
                mode=mode,
            )

        if name == "enable_menu_item":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.enable_menu_item,
                item_name=str(args.get("item_name", "")),
                mode=mode,
            )

        if name == "adjust_inventory":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.adjust_inventory,
                ingredient_name=str(args.get("ingredient_name", "")),
                set_to=args.get("set_to"),
                delta=args.get("delta"),
                unit=args.get("unit") or None,
                reason=str(args.get("reason", "voice adjustment")),
                mode=mode,
            )

        if name == "record_spoilage":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.record_spoilage,
                ingredient_name=str(args.get("ingredient_name", "")),
                qty=args.get("qty"),
                all_stock=bool(args.get("all_stock", False)),
                mode=mode,
            )

        if name == "confirm_batch_cooked":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.confirm_batch_cooked,
                dish_or_batch=str(args.get("dish_or_batch", "")),
                actual_qty=args.get("actual_qty"),
                mode=mode,
            )

        if name == "record_waste":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.record_waste,
                item_name=str(args.get("item_name", "")),
                qty=float(args.get("qty", 0)),
                cause=str(args.get("cause", "overproduction")),
                mode=mode,
            )

        if name == "set_staff_attendance":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.set_staff_attendance,
                staff_name_or_role=str(args.get("staff_name_or_role", "")),
                status=str(args.get("status", "sick")),
                daypart=args.get("daypart") or None,
                mode=mode,
            )

        if name == "run_forecast":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(va.run_forecast)

        if name == "run_inventory_optimizer":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(va.run_inventory_optimizer)

        if name == "run_competitor_scan":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(va.run_competitor_scan)

        if name == "process_reviews":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(va.process_reviews)

        if name == "request_outbound_call":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.request_outbound_call,
                target=str(args.get("target", "")),
                counterparty_type=str(args.get("counterparty_type", "competitor")),
                purpose=str(args.get("purpose", "")),
            )

        if name == "confirm_plan":
            plan_id = str(args.get("plan_id", ""))
            if va is not None:
                return await asyncio.to_thread(va.execute_pending, plan_id)
            return await asyncio.to_thread(voice_processor.confirm, plan_id)

        if name == "cancel_plan":
            plan_id = str(args.get("plan_id", ""))
            if va is not None:
                return await asyncio.to_thread(va.cancel_pending, plan_id)
            return await asyncio.to_thread(voice_processor.cancel, plan_id)

    except Exception as exc:  # noqa: BLE001
        logger.exception("voice_live tool %s failed: %s", name, exc)
        return {"error": str(exc)}

    return {"error": f"Unknown tool: {name}"}


# ──────────────────────────────────────────────────────────────────────────────
# Slim context builder (replaces the giant blob)
# ──────────────────────────────────────────────────────────────────────────────

def _build_slim_context(voice_processor: Any) -> str:
    """Build a minimal context string; the model uses tools for details."""
    import json as _json
    try:
        bus = voice_processor.bus
        now = float(bus.sim_time)
        h = int(now // 3600) % 24
        m = int((now % 3600) // 60)
        clock = f"{h:02d}:{m:02d}"

        from .models import MenuItem, Attendance
        from core.clock import SECONDS_PER_DAY
        from track_a.agents.forecaster import current_daypart
        daypart = current_daypart(now)
        day = int(now // SECONDS_PER_DAY)

        session = voice_processor.db_session_factory()
        try:
            menu_count = session.query(MenuItem).filter(MenuItem.active == 1).count()
            disabled_count = session.query(MenuItem).filter(MenuItem.active == 0).count()
            from .models import Staff
            staff_total = session.query(Staff).filter(Staff.active == 1).count()
            staff_out = session.query(Attendance).filter(
                Attendance.date_sim_day == day,
                Attendance.status.in_(["sick", "leave"]),
            ).count()
        finally:
            session.close()

        return _json.dumps({
            "clock": clock,
            "daypart": daypart,
            "menu_active": menu_count,
            "menu_disabled": disabled_count,
            "staff_available": staff_total - staff_out,
            "note": "Use tools for detailed data — inventory levels, forecasts, batches, etc.",
        })
    except Exception:  # noqa: BLE001
        return "{}"
