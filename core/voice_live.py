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
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .config import GEMINI_LIVE_MODEL

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Per-call hint queues (populated by POST /api/calls/{id}/hint).
# Key = call_id.  The live bridge drains each queue and injects coaching hints
# as silent context messages to the Gemini session.
# ──────────────────────────────────────────────────────────────────────────────

_call_hint_queues: Dict[int, "asyncio.Queue[str]"] = {}


def register_call_session(call_id: int) -> "asyncio.Queue[str]":
    """Register a hint queue for an active call session. Returns the queue."""
    q: "asyncio.Queue[str]" = asyncio.Queue()
    _call_hint_queues[call_id] = q
    return q


def enqueue_call_hint(call_id: int, text: str) -> bool:
    """Enqueue a spectator coaching hint. Returns False if no session registered."""
    q = _call_hint_queues.get(call_id)
    if q is not None:
        q.put_nowait(text)
        return True
    return False


def unregister_call_session(call_id: int) -> None:
    """Remove the hint queue for a call session (called on bridge teardown)."""
    _call_hint_queues.pop(call_id, None)


_RESPONSE_TIMEOUT_S = 20.0
_CONNECT_TIMEOUT_S = 10.0
_RESUME_BACKOFF_S = (1.0, 2.0, 4.0)   # per-attempt wait before retrying a resume
_MAX_RESUME_FAILS = 3                  # consecutive resume failures before giving up

# Models the UI may select via the model= query param.
# These must be actual Vertex AI Live API model IDs (use `client.models.list()` to verify).
_ALLOWED_LIVE_MODELS = {
    "gemini-live-2.5-flash",               # half-cascade, reliable input transcription
    "gemini-live-2.5-flash-native-audio",  # native voice, GA on Vertex
}

# Hardcoded fallback — used when GEMINI_LIVE_MODEL env var contains an invalid name.
_FALLBACK_LIVE_MODEL = "gemini-live-2.5-flash"

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


@dataclass
class _BridgeState:
    """Mutable state shared between the two relay tasks and the reconnect loop."""
    handle: Optional[str] = field(default=None)  # latest session-resumption handle from Gemini
    client_gone: bool = field(default=False)       # browser WS closed → stop looping
    resume_wanted: bool = field(default=False)     # upstream session ended → reconnect


class _GoAway(Exception):
    """Raised inside _handle_chunk to signal an imminent Gemini session expiry (go_away frame)."""


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
                    "Pass item_name to filter to one dish. Pass daypart to filter (breakfast/lunch/dinner/etc). "
                    "IMPORTANT: if the user names a specific dish (e.g. 'cheeseburger forecast', "
                    "'how many fries do we expect'), you MUST set item_name to that dish's name."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {
                            "type": "string",
                            "description": (
                                "The dish name to scope results to. REQUIRED whenever the user asks "
                                "about a specific dish (e.g. 'Classic Cheeseburger', 'French Fries'). "
                                "Omit only when the user asks for the overall/whole-menu forecast."
                            ),
                        },
                        "daypart": {"type": "string", "description": "Optional daypart filter."},
                    },
                },
            },
            {
                "name": "forecast_demand",
                "description": (
                    "Generate an on-demand interval demand forecast for any time range and show "
                    "results in a card. Use this when the user asks about future demand beyond the "
                    "current daypart — e.g. 'what will next week look like?', 'how many portions "
                    "tomorrow?', 'forecast dinner tomorrow', 'show me the weekly outlook', "
                    "'how many cheeseburgers next week?' (→ set item_name='Classic Cheeseburger'). "
                    "Returns per-item quantities, day-by-day breakdown, and daypart totals. "
                    "range='daypart' forecasts a single daypart (specify daypart+day_offset). "
                    "range='day' or 'today' forecasts a full day. "
                    "range='week' forecasts the next 7 days. "
                    "range='custom' requires explicit start/end offsets in seconds from now. "
                    "IMPORTANT: if the user names a specific dish, you MUST set item_name."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "range": {
                            "type": "string",
                            "enum": ["daypart", "day", "today", "week", "custom"],
                            "description": "Time range to forecast.",
                        },
                        "daypart": {
                            "type": "string",
                            "enum": ["breakfast", "lunch", "afternoon", "dinner", "late"],
                            "description": "Daypart (required when range='daypart').",
                        },
                        "day_offset": {
                            "type": "integer",
                            "description": "0=today, 1=tomorrow, 2=day after, etc. Default 0.",
                        },
                        "start": {
                            "type": "number",
                            "description": "Seconds from now (range='custom' only).",
                        },
                        "end": {
                            "type": "number",
                            "description": "Seconds from now (range='custom' only).",
                        },
                        "item_name": {
                            "type": "string",
                            "description": (
                                "The dish name to scope the forecast to. REQUIRED whenever the user "
                                "asks about a specific dish — e.g. 'Classic Cheeseburger forecast for "
                                "the week' → item_name='Classic Cheeseburger'. Omit only when the "
                                "user asks for the overall/whole-menu forecast."
                            ),
                        },
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
                    "Use this when the manager says 'disable X', 'take X off the menu', etc. "
                    "Pass category to bulk-disable all active items in a category (e.g. 'pasta'). "
                    "Pass name_contains to bulk-disable all active items whose name contains that string."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string", "description": "The dish name to disable (single item)."},
                        "category": {"type": "string", "description": "Bulk: disable all active items in this category."},
                        "name_contains": {"type": "string", "description": "Bulk: disable all active items whose name contains this string."},
                        "reason": {"type": "string", "description": "Optional reason for the disable."},
                    },
                },
            },
            {
                "name": "enable_menu_item",
                "description": (
                    "Re-enable a disabled menu item. Clears all blocks (including manual and stock-based). "
                    "Pass category to bulk-enable all disabled items in a category. "
                    "Pass name_contains to bulk-enable all disabled items whose name contains that string."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string", "description": "The dish name to re-enable (single item)."},
                        "category": {"type": "string", "description": "Bulk: re-enable all disabled items in this category."},
                        "name_contains": {"type": "string", "description": "Bulk: re-enable all disabled items whose name contains this string."},
                    },
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
                    "Update one or more staff members' attendance status. The system will automatically "
                    "disable or re-enable dishes based on station coverage after the update. "
                    "Use for: 'head chef is sick', 'Marco is on leave today', 'Mark and Giulia are off', 'chef is back'. "
                    "staff_name_or_role accepts a single name, multiple names, or a role."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "staff_name_or_role": {
                            "type": "string",
                            "description": "One or more staff names (comma-separated or 'and'-separated, e.g. 'Marco and Giulia') or a role.",
                        },
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
                    "Always requires manager approval before it proceeds. "
                    "Use 'ingredient' when the user says 'call our suppliers for [X]' — "
                    "set ingredient to the ingredient name (e.g. 'tomato') and the system "
                    "resolves the default supplier automatically. "
                    "Use 'note' to pass any specific instructions for the AI caller."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": (
                                "Name of the competitor or supplier. May be a partial name, "
                                "a cuisine type (for competitors), or omitted when 'ingredient' is set."
                            ),
                        },
                        "counterparty_type": {"type": "string", "enum": ["competitor", "supplier"]},
                        "purpose": {
                            "type": "string",
                            "description": "Call purpose: 'negotiate', 'intel', 'onboarding', etc.",
                        },
                        "ingredient": {
                            "type": "string",
                            "description": (
                                "Optional ingredient name — the system will find the default supplier "
                                "for this ingredient. Use when the user says 'call our supplier for tomatoes'."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "Optional operator note for the AI caller, e.g. 'push hard on price', "
                                "'ask about bulk discounts'. Forwarded verbatim to the call agent."
                            ),
                        },
                    },
                    "required": ["counterparty_type", "purpose"],
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
    },
    {
        "function_declarations": [
            {
                "name": "consult_reasoner",
                "description": (
                    "Consult a superior reasoning model for complex trade-off decisions or recommendations. "
                    "Use when: (1) user asks 'what should I do?' or 'what do you recommend?'; "
                    "(2) multiple constraints conflict (low stock + unstaffed station); "
                    "(3) you face a trade-off with no obvious right answer. "
                    "Returns a decisive, actionable recommendation you can speak aloud."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The specific question or dilemma to reason about."},
                        "context": {"type": "string", "description": "Relevant facts: inventory levels, staff status, active dishes, etc."},
                    },
                    "required": ["question"],
                },
            }
        ]
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# System prompts
# ──────────────────────────────────────────────────────────────────────────────

_FEW_SHOTS = """
## Examples (utterance → tool call → spoken reply)

User: "How many tomatoes do we have?"
→ get_inventory(item_name="Tomato")
→ "We have 12,000 grams of tomatoes."

User: "Disable all pasta items."
→ disable_menu_item(category="pasta")
→ [confirm mode] "I'll disable all pasta dishes (Pasta Pomodoro and Spaghetti Carbonara) — a confirmation card is on screen."
→ [after confirm] "Done — Pasta Pomodoro and Spaghetti Carbonara have been disabled."
→ [auto mode] "Disabled all pasta dishes."

User: [holds button, says nothing / unclear mumble]
→ [NO tool call]
→ "What would you like me to do?"

User: "Disable Margherita Pizza."
→ disable_menu_item(item_name="Margherita Pizza")
→ [confirm mode] "I'll disable Margherita Pizza — a confirmation card is on screen."
→ [after confirm] "Margherita Pizza has been disabled."
→ [auto mode] "Margherita Pizza is now disabled."

User: "All the tomatoes have spoiled."
→ record_spoilage(ingredient_name="Tomato", all_stock=true)
→ [confirm mode] "I'll zero the Tomato stock and auto-disable affected dishes — a confirmation card is on screen."
→ [after applied] "Done. Tomato stock zeroed. Margherita Pizza, Pasta Pomodoro, and Bruschetta have been automatically disabled."

User: "The head chef is sick."
→ get_staff()  [to confirm who the head chef is]
→ set_staff_attendance(staff_name_or_role="head chef", status="sick")
→ [confirm mode] "I'll mark Marco (head chef) as sick — a confirmation card is on screen."
→ [after applied] "Marco is now marked sick. Giulia still covers Grill, so no dishes have been auto-disabled."

User: "Mark Marco and Giulia as on leave today."
→ set_staff_attendance(staff_name_or_role="Marco and Giulia", status="leave")
→ [confirm mode] "I'll mark Marco and Giulia as on leave — a confirmation card is on screen."
→ [after applied] "Marco and Giulia are now on leave. Grill station is unstaffed — Margherita Pizza and Garlic Bread have been automatically disabled."

User: "We're low on tomatoes and the pasta chef just left — what should I prioritize?"
→ consult_reasoner(question="Low on tomatoes and pasta chef absent — what should the manager prioritize?", context="Tomatoes below safety stock; Pasta station understaffed.")
→ "Given both constraints, I'd 86 the pasta dishes first since the station is short-staffed, then alert GreenFarm Produce for an emergency tomato order."

User: "What's selling most in the last 3 hours?"
→ get_pos_stats(window="3h")
→ "Top seller: Margherita Pizza with 24 orders, then Spaghetti Carbonara with 18."

User: "What are the tomato prices from all our suppliers?"
→ get_supplier_prices(ingredient_name="Tomato")
→ "GreenFarm Produce charges €0.004 per gram for Tomato (5 kg packs), currently in stock."

User: "What dish is the most hated right now?"
→ get_reviews(sort="most_hated")
→ "Based on recent reviews, Caesar Salad has the lowest ratings — customers say it was soggy and small."

User: "What will next week look like?"
→ forecast_demand(range="week")
→ "Here's the 7-day forecast — total 2,340 portions expected. Dinner is the busiest period each day." [ForecastCard shown]

User: "How many cheeseburgers do we expect at dinner tomorrow?"
→ forecast_demand(range="daypart", daypart="dinner", day_offset=1, item_name="Classic Cheeseburger")
→ "Tomorrow dinner forecast for Classic Cheeseburger: 48 portions expected." [ForecastCard shown]

User: "Classic cheeseburger forecast for the week"
→ forecast_demand(range="week", item_name="Classic Cheeseburger")
→ "Classic Cheeseburger: ~210 portions expected this week." [ForecastCard shown]

User: "How many fries do we go through in a week?"
→ forecast_demand(range="week", item_name="French Fries")
→ "French Fries: ~340 portions forecast this week." [ForecastCard shown]

User: "Show me today's full demand forecast."
→ forecast_demand(range="day")
→ "Today's forecast: 312 total portions across all dayparts." [ForecastCard shown]

User: "What's the Margherita Pizza forecast for the week?" [even if the dish is currently 86'd]
→ forecast_demand(range="week", item_name="Margherita Pizza")
→ [if result.item.active is false] "Margherita Pizza is currently 86'd (tomato stockout). If reinstated, we'd expect about 84 portions this week — roughly €756 in potential revenue."
→ [if result.item.active is true] "Margherita Pizza: ~84 portions expected this week, about €756 revenue." [ForecastCard shown]

RULE — dish-named forecast: If the user names ANY specific dish in a forecast question, you MUST
pass item_name=<that dish's name> to forecast_demand or get_forecast. Never omit item_name when
a dish is named. Only omit it for whole-menu / "all dishes" queries.

User: "How much will we lose if Marco is on leave this week?"
→ consult_reasoner(question="How much revenue will we lose this week if Marco is on leave?", context="Need revenue-at-risk from Marco's sole-covered dishes based on current forecasts.")
→ "Marco solely covers the Pizza station. At ~84 Margherita portions forecast this week (€9 each), that's roughly €756 at risk. Recommend covering his station or calling in a substitute."
"""

_SYSTEM_INSTRUCTIONS: Dict[str, str] = {
    "manager": (
        "You are Roba, the AI operations desk for this restaurant. "
        "You are a TWO-WAY interface: you answer questions AND record operational updates.\n\n"
        "CORE RULES:\n"
        "1. ALWAYS call a tool before answering any factual question. Never guess or answer from memory.\n"
        "2. TRUTHFULNESS: State ONLY what the tool result confirms. If a tool returns an error, say so — "
        "never claim an action succeeded without a successful tool result.\n"
        "3. CONCISENESS: Answer the specific question asked. Never recite entire inventory lists, "
        "whole forecasts, or long reports when a specific answer was requested.\n"
        "4. MISSING ARGS: If a tool returns {'need': ...}, ask the user for that specific piece of info.\n"
        "5. MODES:\n"
        "   CONFIRM MODE: Call the write tool (it stages the action and shows a confirmation card on-screen). "
        "Then speak exactly ONE short sentence: '[Action summary] — a confirmation card is on screen.' and wait. "
        "When the user says yes/confirm/go ahead, call confirm_plan(plan_id=...). "
        "Never ask a 'manager' or anyone else — the person speaking to you IS the authority.\n"
        "   AUTO MODE: Write tools apply immediately. Speak ONE short sentence of what was done. "
        "Never ask for confirmation.\n"
        "6. ACTIONS vs READS: If the user's request is an ACTION (disable, enable, set, mark, adjust, 86, "
        "turn off, remove), you MUST call the matching WRITE tool. Never respond to an action request by "
        "listing items — that is always wrong. If you receive an empty or unclear utterance, ask ONE short "
        "clarifying question. Do NOT recite inventory or the menu to fill silence.\n"
        "7. NEVER ask the user for information they already gave. If they say 'all the tomatoes spoiled', "
        "call record_spoilage(ingredient_name='Tomato', all_stock=True) immediately — do not ask 'which "
        "ingredient?' or 'how much?'. Infer all_stock=True from words like 'all', 'everything', 'the whole', "
        "'all of the', 'spoiled all', 'dropped all'.\n"
        "8. NEVER claim an ingredient doesn't exist or suggest alternatives unless get_inventory returned zero "
        "results. If the result has an exact or partial match, use it — do not narrate other rows as "
        "'alternatives' to the one asked about.\n\n"
        "TOOL GUIDE:\n"
        "• Inventory quantity → get_inventory(item_name=...)\n"
        "• Inventory expiry → get_inventory(sort='expiring_soonest')\n"
        "• Forecast → get_forecast(item_name=..., daypart=...)\n"
        "• Batch status → get_batches(status=..., dish=...)\n"
        "• What's disabled → get_menu(filter='disabled')\n"
        "• All menu items → get_menu(filter='all')\n"
        "• Sales / top sellers → get_pos_stats(window=..., item_name=...)\n"
        "• Competitor promos → get_competitors()\n"
        "• Review sentiment → get_reviews(sort='most_hated')\n"
        "• Staff presence → get_staff()\n"
        "• Supplier prices → get_supplier_prices(ingredient_name=...)\n"
        "• Spoilage (ingredient) → record_spoilage(ingredient_name=..., all_stock=...)\n"
        "• Disable/enable dish → disable_menu_item / enable_menu_item (supports category= or name_contains= for bulk)\n"
        "• Staff sick/leave → set_staff_attendance(staff_name_or_role=..., status=...)\n"
        "• Adjust stock → adjust_inventory(ingredient_name=..., set_to=... or delta=...)\n"
        "• Interval forecast → forecast_demand(range=week|day|daypart|custom, daypart=..., day_offset=..., item_name=...)\n"
        "  ↳ ALWAYS set item_name when the user names a specific dish.\n"
        "• Trigger agents → run_forecast / run_inventory_optimizer / run_competitor_scan / process_reviews\n"
        "• Outbound call → request_outbound_call(counterparty_type=supplier|competitor, purpose=negotiate|intel|onboarding, "
        "target=<name or partial>, ingredient=<ingredient name when calling supplier for specific item>, note=<operator instructions>)\n"
        "  Examples:\n"
        "    'call our suppliers for tomatoes and negotiate' → request_outbound_call(counterparty_type='supplier', "
        "purpose='negotiate', ingredient='tomato')\n"
        "    'call a competitor selling pizza' → request_outbound_call(counterparty_type='competitor', "
        "purpose='intel', target='pizza')\n"
        "    'call GreenFarm and push on price' → request_outbound_call(counterparty_type='supplier', "
        "purpose='negotiate', target='GreenFarm', note='push hard on price')\n"
        "• Complex trade-off → consult_reasoner(question=..., context=...)\n\n"
        "BATCH STATUS VOCABULARY: 'decided'=awaiting approval; 'approved'=ready to cook; "
        "'ready'=cooked; 'skipped'=decided to skip.\n\n"
        "ESCALATION — call consult_reasoner when:\n"
        "• You must choose between competing priorities (e.g. scarce ingredient AND unstaffed station)\n"
        "• The user asks 'what should I do?' / 'what do you recommend?' / 'what's the right call?'\n"
        "• You face a trade-off (raise price vs. 86 the dish; skip a batch vs. rush order)\n"
        "• The situation has multiple interacting constraints you cannot resolve with a single tool\n"
        "When using consult_reasoner, say a short filler ('Let me think on that...') before calling.\n\n"
        + _FEW_SHOTS
    ),
    # Call-bound roles: system instruction is built dynamically from call_personas
    # when call_id is set.  These static stubs serve as fallbacks only.
    "supplier_call": (
        "You are Roba, calling a supplier as the restaurant purchasing agent. "
        "Your goal is to negotiate a lower unit price and/or better delivery terms. "
        "You placed this call -- speak first with a professional greeting."
    ),
    "competitor_call": (
        "You are Roba, posing as a regular curious customer calling a restaurant. "
        "Learn about their popular dishes, prices, and specials without revealing "
        "that you represent a competing restaurant."
    ),
    "onboarding_call": (
        "You are Roba, welcoming a new potential supplier on their first call. "
        "Gather their full catalog: ingredients, prices, pack sizes, minimums, "
        "delivery charge, and lead time. You placed this call -- speak first."
    ),
    "cook": (
        "You are Roba, the AI kitchen desk. Concise kitchen-friendly replies (1-2 sentences max).\n\n"
        "CORE RULES:\n"
        "1. ALWAYS call a tool first. Never guess.\n"
        "2. TRUTHFULNESS: Say only what the tool confirms.\n"
        "3. CONCISENESS: Kitchen staff are busy. One or two sentences.\n"
        "4. MISSING ARGS: If a tool returns {'need': ...}, ask the user for that specific piece of info.\n"
        "5. MODES: CONFIRM MODE: stage the write tool and say '[summary] — a confirmation card is on screen.'. "
        "AUTO MODE: apply immediately and confirm in one sentence.\n"
        "6. ACTIONS vs READS: If the request is an ACTION (mark cooked, record waste, record spoilage), "
        "call the matching WRITE tool immediately. Never list items to fill silence — ask ONE short question.\n"
        "7. NEVER ask the user for information they already gave. If they say 'all the mozzarella went bad', "
        "call record_spoilage(ingredient_name='Mozzarella', all_stock=True) immediately.\n\n"
        "TOOL GUIDE:\n"
        "• Batch status → get_batches(status='approved,decided')\n"
        "• Did we cook X → get_batches(dish=..., status='ready')\n"
        "• Mark cooked → confirm_batch_cooked(dish_or_batch=..., actual_qty=...)\n"
        "• Waste (dish) → record_waste(item_name=..., qty=..., cause=...)\n"
        "• Ingredient spoiled → record_spoilage(ingredient_name=..., all_stock=...)\n"
        "• Inventory check → get_inventory(item_name=...)\n"
        "• Dish forecast → forecast_demand(range=week|day|daypart, item_name=...)\n"
        "  ↳ ALWAYS set item_name when the user names a specific dish.\n\n"
        "BATCH STATES: approved=cook now; decided=needs approval; ready=already cooked; skipped=skip.\n\n"
        + _FEW_SHOTS
    ),
}


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
    call_id: Optional[int] = None,
    calls: Optional[Any] = None,
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

    system_instruction = _SYSTEM_INSTRUCTIONS.get(role, _SYSTEM_INSTRUCTIONS["manager"])

    # For call-bound roles, build a richer dynamic persona from the DB.
    if call_id is not None and calls is not None and role in ("supplier_call", "competitor_call", "onboarding_call"):
        try:
            from . import call_personas as _cp  # noqa: PLC0415
            from .models import (  # noqa: PLC0415
                AppSettings as _AppSettings,
                Competitor as _Competitor,
                CompetitorOffer as _CO,
                Ingredient as _Ing,
                Supplier as _Supplier,
                SupplierCatalog as _SCat,
            )
            _call = calls._load(call_id)
            if _call is not None:
                _db_sess = voice_processor.db_session_factory()
                try:
                    # Load restaurant identity for supplier personas
                    _identity: dict = {"title": "the restaurant", "location": "", "phone": ""}
                    _app_cfg = _db_sess.get(_AppSettings, 1)
                    if _app_cfg is not None:
                        _identity = _app_cfg.get_identity()

                    _call_meta = _call.call_metadata or {}

                    if role == "supplier_call":
                        _sup = _db_sess.get(_Supplier, _call.counterparty_id)
                        _target_ing_id = _call_meta.get("ingredient_id")
                        if _target_ing_id:
                            _cat = (
                                _db_sess.query(_SCat)
                                .filter(
                                    _SCat.supplier_id == _call.counterparty_id,
                                    _SCat.ingredient_id == int(_target_ing_id),
                                )
                                .first()
                            )
                        else:
                            _cat = (
                                _db_sess.query(_SCat)
                                .filter(
                                    _SCat.supplier_id == _call.counterparty_id,
                                    _SCat.is_default == 1,
                                )
                                .first()
                            ) or (
                                _db_sess.query(_SCat)
                                .filter(_SCat.supplier_id == _call.counterparty_id)
                                .first()
                            )
                        _ing = _db_sess.get(_Ing, _cat.ingredient_id) if _cat else None
                        system_instruction = _cp.supplier_negotiation_prompt(
                            supplier_name=_sup.name if _sup else f"Supplier #{_call.counterparty_id}",
                            ingredient_name=_ing.name if _ing else "the ingredient",
                            current_price=float(_cat.current_price or 0.0) if _cat else 0.0,
                            unit=str(_cat.unit or "g") if _cat else "g",
                            restaurant_title=_identity["title"],
                            restaurant_location=_identity["location"],
                            note=str(_call_meta.get("note") or ""),
                        )
                    elif role == "competitor_call":
                        _comp = _db_sess.get(_Competitor, _call.counterparty_id)
                        _cuisine = ""
                        _dist = 0.0
                        if _comp:
                            if isinstance(_comp.cuisine, list) and _comp.cuisine:
                                _cuisine = _comp.cuisine[0]
                            elif isinstance(_comp.cuisine, str):
                                _cuisine = _comp.cuisine
                            _dist = float(_comp.distance_km or 0.0)
                        _offers = (
                            _db_sess.query(_CO)
                            .filter(_CO.competitor_id == _call.counterparty_id)
                            .all()
                        )
                        _known_dishes = [o.dish_name for o in _offers if o.dish_name] or None
                        system_instruction = _cp.competitor_intel_prompt(
                            competitor_name=_comp.name if _comp else f"Restaurant #{_call.counterparty_id}",
                            cuisine=_cuisine,
                            distance_km=_dist,
                            known_dishes=_known_dishes,
                            intel_goal=str(_call_meta.get("intel_goal") or ""),
                        )
                    elif role == "onboarding_call":
                        _sup = _db_sess.get(_Supplier, _call.counterparty_id)
                        system_instruction = _cp.supplier_onboarding_prompt(
                            supplier_name=_sup.name if _sup else f"Supplier #{_call.counterparty_id}",
                            phone=str(_sup.phone or "") if _sup else "",
                            restaurant_title=_identity["title"],
                            restaurant_location=_identity["location"],
                        )
                finally:
                    _db_sess.close()
        except Exception as _persona_exc:  # noqa: BLE001
            logger.warning("call_personas build failed: %s", _persona_exc)

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

    # Shared mutable state across reconnect iterations.
    bridge = _BridgeState()
    buffers = _TurnBuffer()
    first_connect = True
    fail_streak = 0

    while True:
        # Build (or rebuild) the config each iteration so the latest handle is used.
        # On first connect handle is None, which is equivalent to a fresh session.
        live_config = _gtypes.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=system_instruction,
            tools=_TOOLS,  # type: ignore[arg-type]
            input_audio_transcription=_gtypes.AudioTranscriptionConfig(),
            output_audio_transcription=_gtypes.AudioTranscriptionConfig(),
            realtime_input_config=realtime_input_config,
            # Enable resumption so the SDK can continue context across session limits.
            session_resumption=_gtypes.SessionResumptionConfig(handle=bridge.handle),
            # Prevent the context window from growing unbounded in long sessions.
            context_window_compression=_gtypes.ContextWindowCompressionConfig(
                sliding_window=_gtypes.SlidingWindow(),
            ),
        )

        connect_ctx = client.aio.live.connect(model=live_model, config=live_config)
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_S):
                session = await connect_ctx.__aenter__()
        except (TimeoutError, asyncio.TimeoutError):
            if first_connect:
                logger.warning("Vertex AI Live session connect timed out")
                await _safe_send_json(websocket, {"type": "unavailable", "reason": "connect_timeout"})
                return
            fail_streak += 1
            logger.warning("Vertex AI Live resume timed out (streak=%d)", fail_streak)
            if fail_streak >= _MAX_RESUME_FAILS:
                await _safe_send_json(websocket, {"type": "error", "message": "Voice session could not resume — please refresh."})
                return
            await asyncio.sleep(_RESUME_BACKOFF_S[min(fail_streak - 1, len(_RESUME_BACKOFF_S) - 1)])
            continue
        except Exception as exc:  # noqa: BLE001
            if first_connect:
                logger.warning("Vertex AI Live connect failed: %s", exc)
                await _safe_send_json(websocket, {"type": "unavailable", "reason": str(exc)})
                return
            fail_streak += 1
            logger.warning("Vertex AI Live resume connect failed: %s (streak=%d)", exc, fail_streak)
            if fail_streak >= _MAX_RESUME_FAILS:
                await _safe_send_json(websocket, {"type": "error", "message": f"Voice session could not resume: {exc}"})
                return
            await asyncio.sleep(_RESUME_BACKOFF_S[min(fail_streak - 1, len(_RESUME_BACKOFF_S) - 1)])
            continue

        # Connected (or resumed with prior context).
        if first_connect:
            if not await _safe_send_json(websocket, {"type": "connected", "model": live_model}):
                try:
                    await connect_ctx.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                return
            first_connect = False
        else:
            logger.info(
                "Vertex AI Live session resumed (handle=%s…)",
                (bridge.handle or "")[:20],
            )

        bridge.resume_wanted = False
        fail_streak = 0

        # For call-bound sessions, register the hint queue and inject an opener.
        if call_id is not None:
            hint_queue = register_call_session(call_id)
            try:
                await session.send_client_content(
                    turns={"parts": [{"text": "(Begin the call now. You called them -- speak first with a professional greeting and state your purpose.)"}]},
                    turn_complete=True,
                )
            except Exception as _oe:  # noqa: BLE001
                logger.warning("call opener inject failed: %s", _oe)
        else:
            hint_queue = None

        async def _drain_hints(s: Any, hq: Any) -> None:
            """Drain the hint queue and inject each hint as a silent coaching note."""
            try:
                while True:
                    hint = await hq.get()
                    try:
                        await s.send_client_content(
                            turns={"parts": [{"text": f"(Coaching note from your manager -- internalize this but do NOT say it aloud: {hint})"}]},
                            turn_complete=False,
                        )
                    except Exception as _he:  # noqa: BLE001
                        logger.warning("hint inject failed: %s", _he)
            except asyncio.CancelledError:
                pass

        try:
            task_c2g = asyncio.create_task(
                _client_to_gemini(websocket, session, voice_processor, voice_actions, buffers, bridge, call_id=call_id),
                name="voice_live_c2g",
            )
            task_g2c = asyncio.create_task(
                _gemini_to_client(websocket, session, voice_processor, role, mode, voice_actions, buffers, bridge, call_id=call_id, calls=calls),
                name="voice_live_g2c",
            )
            all_tasks: list = [task_c2g, task_g2c]
            if hint_queue is not None:
                task_hints = asyncio.create_task(
                    _drain_hints(session, hint_queue),
                    name="voice_live_hints",
                )
                all_tasks.append(task_hints)
            done, pending = await asyncio.wait(
                all_tasks, return_when=asyncio.FIRST_COMPLETED
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
                bridge.client_gone = True
            else:
                logger.exception("Vertex AI Live session error: %s", exc)
                await _safe_send_json(websocket, {"type": "error", "message": str(exc)})
        finally:
            try:
                await connect_ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

        # ── Decide whether to loop (resume) or exit. ──────────────────────
        if bridge.client_gone:
            logger.debug("voice live: browser disconnected, ending bridge")
            break
        if bridge.resume_wanted:
            logger.info(
                "voice live: upstream session ended cleanly, resuming (handle=%s…)",
                (bridge.handle or "none")[:20],
            )
            continue
        # Genuine error (already surfaced to the client) — stop the loop.
        break

    # ── Call teardown: end the call row and unregister hint queue. ────────────
    if call_id is not None:
        try:
            if calls is not None:
                _loaded = calls._load(call_id)
                if _loaded is not None and _loaded.status == "active":
                    calls.end_call(call_id)
        except Exception as _td_exc:  # noqa: BLE001
            logger.warning("call teardown end_call failed: %s", _td_exc)
        finally:
            unregister_call_session(call_id)


async def _client_to_gemini(
    websocket: Any, session: Any, voice_processor: Any, voice_actions: Optional[Any],
    buffers: "_TurnBuffer", bridge: "_BridgeState",
    *,
    call_id: Optional[int] = None,
) -> None:
    """Read frames from the browser WS and relay to the Live session."""
    from google.genai import types as _gtypes

    try:
        while True:
            try:
                data = await websocket.receive()
            except Exception:
                bridge.client_gone = True
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
                    buffers.start_user_turn()
                    await session.send_realtime_input(activity_start=_gtypes.ActivityStart())
                elif msg_type == "activity_end":
                    await session.send_realtime_input(activity_end=_gtypes.ActivityEnd())
                    # Do NOT flush/close the user turn here — input_transcription arrives
                    # after the audio boundary; finalize on generation_complete/turn_complete.
                elif msg_type == "end_of_turn":
                    await session.send_realtime_input(audio_stream_end=True)
                elif msg_type == "text_input":
                    text = str(msg.get("text") or "")
                    if text:
                        await session.send_client_content(
                            turns={"parts": [{"text": text}]},
                            turn_complete=True,
                        )
                elif msg_type == "spectator_hint":
                    # Manager on the same tab injects a coaching hint.
                    hint_text = str(msg.get("text") or "")
                    if hint_text and call_id is not None:
                        enqueue_call_hint(call_id, hint_text)
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
                            # Emit applied frame so the confirm card dismisses.
                            if isinstance(result, dict) and result.get("status") == "applied":
                                applied_frame = {
                                    "type": "applied",
                                    "tool": msg_type,
                                    "summary": str(result.get("summary", result.get("human_readable", "Done."))),
                                    "result": result,
                                }
                                await _safe_send_json(websocket, applied_frame)
                                await _speak_confirmation(session, "done")
                            elif isinstance(result, dict) and result.get("status") == "cancelled":
                                await _speak_confirmation(session, "cancelled")
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("voice %s failed: %s", msg_type, exc)
    except asyncio.CancelledError:
        pass


async def _speak_confirmation(session: Any, kind: str) -> None:
    """Inject a forced spoken acknowledgement after a confirm/cancel action."""
    if kind == "done":
        text = "(Action confirmed. Reply with exactly: 'Done.')"
    else:
        text = "(Action cancelled. Reply with exactly: 'Cancelled.')"
    try:
        await session.send_client_content(
            turns={"parts": [{"text": text}]},
            turn_complete=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("_speak_confirmation(%s) failed: %s", kind, exc)


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
        self._user_open: bool = False

    def new_user_turn(self) -> str:
        self._user_turn_id = str(uuid.uuid4())
        return self._user_turn_id

    def start_user_turn(self) -> None:
        """Called when user begins speaking (activity_start in PTT). Resets the user turn."""
        self.user = []
        self._user_turn_id = str(uuid.uuid4())
        self._user_open = True

    def close_user_turn(self) -> None:
        """Mark the user turn as closed (called after flush on activity_end or turn_complete)."""
        self._user_open = False

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


async def _flush_transcript(
    websocket: Any,
    role: str,
    buffers: "_TurnBuffer",
    *,
    call_id: Optional[int] = None,
    calls: Optional[Any] = None,
) -> None:
    """Emit the buffered text as a FINAL transcript line.

    When ``call_id`` is set, also mirrors the turn to ``calls.add_turn``:
    ``role="user"`` maps to ``"counterparty"`` (the human on the mic) and
    ``role="roba"`` maps to ``"agent"`` (Roba's spoken output).
    """
    text = buffers.take(role)
    if text:
        await _safe_send_json(websocket, {
            "type": "transcript",
            "role": role,
            "text": text,
            "turn_id": buffers.turn_id(role),
            "final": True,
        })
        if call_id is not None and calls is not None:
            call_role = "counterparty" if role == "user" else "agent"
            try:
                import asyncio as _asyncio  # noqa: PLC0415
                await _asyncio.to_thread(calls.add_turn, call_id, call_role, text)
            except Exception as _mt_exc:  # noqa: BLE001
                logger.warning("calls.add_turn mirror failed: %s", _mt_exc)


def _merge_transcript_chunk(buf: list, incoming: str) -> None:
    """Merge an incoming STT chunk into the running buffer.

    Input transcription from Gemini Live sends cumulative text, so each
    chunk often contains everything said so far. Detect this by checking
    whether the incoming text starts with (or equals) the current buffer
    content, and replace rather than append in that case.
    """
    incoming = incoming.strip()
    if not incoming:
        return
    current = "".join(buf).strip()
    if not current:
        buf.append(incoming)
        return
    if incoming.startswith(current):
        # Cumulative: the new frame includes everything said so far — replace.
        buf.clear()
        buf.append(incoming)
    elif current.startswith(incoming):
        # STT revision sent a shorter version of what's already accumulated —
        # keep the longer text (do not drop words already displayed).
        return
    else:
        # True delta: append normally.
        buf.append(" " + incoming)


async def _gemini_to_client(
    websocket: Any,
    session: Any,
    voice_processor: Any,
    role: str,
    mode: str,
    voice_actions: Optional[Any],
    buffers: "_TurnBuffer",
    bridge: "_BridgeState",
    *,
    call_id: Optional[int] = None,
    calls: Optional[Any] = None,
) -> None:
    """Read from the Vertex AI Live session and relay audio/events to the browser."""
    from google.genai import types as _gtypes

    try:
        while True:
            try:
                async with asyncio.timeout(_RESPONSE_TIMEOUT_S):
                    async for chunk in session.receive():
                        await _handle_chunk(
                            chunk, websocket, session, voice_processor,
                            role, mode, _gtypes, buffers, voice_actions, bridge,
                            call_id=call_id, calls=calls,
                        )
            except asyncio.TimeoutError:
                logger.debug("Vertex AI Live idle (no output in %.0fs)", _RESPONSE_TIMEOUT_S)
                continue
            except StopAsyncIteration:
                continue
            except asyncio.CancelledError:
                raise
            except _GoAway:
                # go_away already set bridge.resume_wanted — exit so the loop reconnects.
                logger.info("Vertex AI Live go_away received, exiting receive loop for resume")
                return
            except Exception as exc:  # noqa: BLE001
                if _is_disconnect(exc):
                    # Clean close from Gemini (session time-limit or normal end).
                    # Signal the reconnect loop instead of letting the WS drop.
                    logger.info("Vertex AI Live upstream session closed: %s — will resume", exc)
                    bridge.resume_wanted = True
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
    bridge: "_BridgeState",
    *,
    call_id: Optional[int] = None,
    calls: Optional[Any] = None,
) -> None:
    """Process one LiveServerMessage chunk."""
    # ── Session lifecycle events ─────────────────────────────────────────────
    if chunk.session_resumption_update:
        update = chunk.session_resumption_update
        if update.resumable and update.new_handle:
            bridge.handle = update.new_handle
            logger.debug("Vertex AI Live resumption handle updated (%s…)", update.new_handle[:16])

    if chunk.go_away:
        time_left = getattr(chunk.go_away, "time_left", "?")
        logger.info("Vertex AI Live go_away (time_left=%s) — rotating session", time_left)
        bridge.resume_wanted = True
        raise _GoAway()

    if chunk.data:
        try:
            await websocket.send_bytes(chunk.data)
        except Exception:
            return

    sc = chunk.server_content
    if sc is not None:
        # User STT partial — accumulate and emit partial frame.
        if sc.input_transcription and sc.input_transcription.text:
            if not buffers._user_open:
                # Conversation mode fallback (no activity_start/end frames):
                # start a turn on first chunk if none is open
                buffers.start_user_turn()
            logger.debug("user STT chunk: %r", sc.input_transcription.text)
            _merge_transcript_chunk(buffers.user, sc.input_transcription.text)
            await _emit_partial(websocket, "user", buffers)

        # Roba TTS text — accumulate and emit partial frame.
        roba_text = None
        if sc.output_transcription and sc.output_transcription.text:
            roba_text = sc.output_transcription.text
        elif chunk.text:
            roba_text = chunk.text
        if roba_text:
            if not buffers.roba:
                buffers.new_roba_turn()
            buffers.roba.append(roba_text)
            await _emit_partial(websocket, "roba", buffers)

        if getattr(sc, "generation_complete", False):
            # Do NOT flush the user turn here.  In half-cascade mode,
            # input_transcription chunks can arrive AFTER generation_complete
            # (especially on tool-call turns where the model fires this signal
            # as soon as it emits the function-call, before STT is fully
            # streamed).  Finalizing here would truncate the user bubble to
            # whatever text had arrived so far ("do we" instead of the full
            # utterance).  The user turn is finalized exclusively on
            # turn_complete (or interrupted for barge-in).
            await _flush_transcript(websocket, "roba", buffers, call_id=call_id, calls=calls)
        if getattr(sc, "interrupted", False):
            await _flush_transcript(websocket, "roba", buffers, call_id=call_id, calls=calls)
            await _safe_send_json(websocket, {"type": "interrupted"})
        if getattr(sc, "turn_complete", False):
            await _flush_transcript(websocket, "roba", buffers, call_id=call_id, calls=calls)
            # In conversation mode, finalize user turn here (PTT already finalized on activity_end)
            if buffers._user_open:
                await _flush_transcript(websocket, "user", buffers, call_id=call_id, calls=calls)
                buffers.close_user_turn()
            await _safe_send_json(websocket, {"type": "turn_complete"})

    if chunk.tool_call:
        # Capture the accumulated user STT so we can recover a missing item_name below.
        # STT chunks may still be arriving (half-cascade) so this is best-effort; it is
        # strictly additive — only used when the model omitted the arg entirely.
        user_text = "".join(buffers.user)
        for fn_call in (chunk.tool_call.function_calls or []):
            result = await _execute_tool(fn_call, voice_processor, role, mode, voice_actions, user_text=user_text)
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
            # Emit plan_preview or applied frames so the frontend card updates.
            if isinstance(result, dict) and result.get("status") == "pending":
                plan_id = str(result.get("plan_id", ""))
                human_readable = str(result.get("human_readable", ""))
                plan_preview_frame = {
                    "type": "plan_preview",
                    "plan": {
                        "plan_id": plan_id,
                        "human_readable": human_readable,
                        "summary": human_readable,
                        "routes": [{"summary": human_readable, "target_agents": ["menu"]}],
                    },
                }
                await _safe_send_json(websocket, plan_preview_frame)
            elif isinstance(result, dict) and result.get("status") == "applied":
                applied_frame = {
                    "type": "applied",
                    "tool": fn_call.name,
                    "summary": str(result.get("summary", result.get("human_readable", "Done."))),
                    "result": result,
                }
                await _safe_send_json(websocket, applied_frame)
                if fn_call.name in ("confirm_plan", "cancel_plan"):
                    await _speak_confirmation(session, "done")


def _infer_item_name_from_text(user_text: str, va: Any) -> Optional[str]:
    """Scan user_text for a single menu-item name match and return it (or None).

    Used as a server-side safety net when the live model omits item_name from a
    forecast tool call even though the user named a specific dish.  We only auto-fill
    when exactly ONE distinct dish is found to avoid mis-scoping multi-dish utterances.

    Two-pass matching (either pass can match; exact wins):
      1. Full item name is contained in the utterance (e.g. "classic cheeseburger" in text).
      2. Any significant word (≥4 chars) from the utterance is contained in an item name
         (e.g. "fries" in "French Fries", "cheeseburger" in "Classic Cheeseburger").
    If either pass yields more than one distinct item, we return None (ambiguous).
    """
    if not user_text or va is None:
        return None
    try:
        import re  # noqa: PLC0415
        from .models import MenuItem  # noqa: PLC0415
        session = va.db_session_factory()
        try:
            items = session.query(MenuItem.id, MenuItem.name).all()
        finally:
            session.close()
        text_lower = user_text.lower()
        # Significant tokens from the utterance (≥4 chars, letters only)
        tokens = [t for t in re.split(r"\W+", text_lower) if len(t) >= 4]
        matched: list[str] = []
        for _, item_name in items:
            if not item_name:
                continue
            name_lower = item_name.lower()
            hit = (
                # Pass 1: full name appears in utterance
                name_lower in text_lower
                # Pass 2: any utterance word is a substring of the item name
                or any(tok in name_lower for tok in tokens)
            )
            if hit and item_name not in matched:
                matched.append(item_name)
        if len(matched) == 1:
            logger.debug("voice_live item_name fallback: inferred %r from %r", matched[0], user_text)
            return matched[0]
    except Exception as exc:  # noqa: BLE001
        logger.debug("voice_live item_name fallback error: %s", exc)
    return None


async def _execute_tool(
    fn_call: Any,
    voice_processor: Any,
    role: str,
    mode: str,
    voice_actions: Optional[Any],
    *,
    user_text: str = "",
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
            # Server-side fallback: recover item_name from the user utterance when the
            # model omitted it despite the user naming a dish.
            item_name_gf = args.get("item_name") or None
            if not item_name_gf and user_text:
                item_name_gf = _infer_item_name_from_text(user_text, va)
            if va:
                return await asyncio.to_thread(
                    va.get_forecast,
                    item_name=item_name_gf,
                    daypart=args.get("daypart") or None,
                )
            return await asyncio.to_thread(
                voice_processor.query_forecast, item_name_gf
            )

        if name == "forecast_demand":
            # Server-side fallback: recover item_name from the user utterance when the
            # model omitted it despite the user naming a dish.
            item_name_fd = args.get("item_name") or None
            if not item_name_fd and user_text:
                item_name_fd = _infer_item_name_from_text(user_text, va)
            if va:
                day_offset = args.get("day_offset")
                return await asyncio.to_thread(
                    va.forecast_demand,
                    range=args.get("range") or "day",
                    daypart=args.get("daypart") or None,
                    day_offset=int(day_offset) if day_offset is not None else 0,
                    start=args.get("start") or None,
                    end=args.get("end") or None,
                    item_name=item_name_fd,
                )
            return {"error": "forecast_demand requires voice_actions"}

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
                category=args.get("category") or None,
                name_contains=args.get("name_contains") or None,
                mode=mode,
            )

        if name == "enable_menu_item":
            if va is None:
                return {"error": "VoiceActions not available"}
            return await asyncio.to_thread(
                va.enable_menu_item,
                item_name=str(args.get("item_name", "")),
                category=args.get("category") or None,
                name_contains=args.get("name_contains") or None,
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
                counterparty_type=str(args.get("counterparty_type", "competitor")),
                purpose=str(args.get("purpose", "")),
                target=str(args.get("target", "")),
                ingredient=str(args.get("ingredient", "")),
                note=str(args.get("note", "")),
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

        if name == "consult_reasoner":
            if va:
                return await asyncio.to_thread(
                    va.consult_reasoner,
                    question=str(args.get("question", "")),
                    context=args.get("context"),
                )
            return {"error": "VoiceActions not available"}

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
