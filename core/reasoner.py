"""Background reasoning model — consults a superior LLM for complex trade-off decisions.

Uses Gemini 2.5 Pro on Vertex AI (same client as the rest of the stack).
Called by VoiceActions.consult_reasoner() via the voice agent's consult_reasoner tool,
and by the daily batch advisor (Forecaster.suggest_day_batches) for structured suggestions.
"""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert restaurant operations advisor. "
    "You receive a specific question from a restaurant manager (via a voice assistant) "
    "and an OPERATIONAL SNAPSHOT containing real current data including:\n"
    "  • dishes: per-dish forecast_qty, price, and revenue_estimate (= forecast_qty × price)\n"
    "  • staff: who is present/on-leave/sick, which dishes each person solely covers "
    "(sole_cover_dishes_at_risk), and their stations\n"
    "  • stations: current coverage status\n"
    "  • low_stock_ingredients: ingredients at or below safety stock\n\n"
    "RULES:\n"
    "1. When asked about revenue impact, loss, or 'what if X is on leave': COMPUTE "
    "the total from the snapshot — sum forecast_qty × price for the absent staffer's "
    "sole_cover_dishes_at_risk. State the euro amount explicitly.\n"
    "2. When asked about a specific dish's forecast: read forecast_qty and revenue_estimate "
    "directly from the dishes list.\n"
    "3. Give ONE decisive answer in 2-3 sentences. Be concrete: name dishes, staff, and "
    "calculated amounts. Do not hedge with 'it depends' — use the snapshot numbers to make "
    "a definitive call."
)


def consult(
    question: str,
    context: Optional[str] = None,
    timeout_s: float = 15.0,
) -> Dict[str, Any]:
    """Call the reasoner model and return {recommendation, rationale}.

    Falls back gracefully if Vertex AI is unavailable.
    """
    try:
        from .vertex import build_genai_client, vertex_available
        from .config import GEMINI_REASONER_MODEL

        if not vertex_available():
            return {
                "recommendation": "I'm unable to consult the reasoning model right now — please make the call based on current data.",
                "rationale": "Vertex AI unavailable",
            }

        client = build_genai_client()
        prompt_parts = [f"Question: {question}"]
        if context:
            prompt_parts.append(f"Context:\n{context}")
        prompt_parts.append("Give ONE decisive recommendation (2-3 sentences, concrete, actionable):")
        prompt = "\n\n".join(prompt_parts)

        import concurrent.futures

        def _call():
            return client.models.generate_content(
                model=GEMINI_REASONER_MODEL,
                contents=prompt,
                config={"system_instruction": _SYSTEM_PROMPT, "temperature": 0.3},
            )

        # Run synchronously with timeout (VoiceActions is called via asyncio.to_thread)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_call)
            try:
                resp = future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError:
                return {
                    "recommendation": "The reasoning model timed out — please decide based on current data.",
                    "rationale": "timeout",
                }

        text = ""
        if resp and resp.text:
            text = str(resp.text).strip()
        if not text:
            return {
                "recommendation": "No recommendation could be generated — please decide based on current data.",
                "rationale": "empty_response",
            }

        return {"recommendation": text, "rationale": "gemini_2.5_pro"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("consult_reasoner failed: %s", exc)
        return {
            "recommendation": "I hit an error consulting the reasoning model. Please decide based on the available data.",
            "rationale": str(exc),
        }


_BATCH_ADVISOR_SYSTEM = """\
You are an expert restaurant operations AI for a simulation environment.

Your job is to analyse today's batch cook schedule and identify REAL opportunities
to improve profitability or cut waste — such as adding a missing peak-hour batch,
changing a batch time to match demand, or adjusting quantities.

Rules:
- ONLY suggest changes when there is a clear, quantifiable opportunity (cost saving,
  revenue gain, or waste reduction). If the current schedule is already optimal, return
  an empty proposals list.
- Do NOT suggest trivial or cosmetic changes.
- For each proposal include: type, menu_item_id, dish_name, target_window_start (sim-seconds),
  target_qty (integer), forecast_demand (number expected in that window),
  projected_benefit_description (concrete £/unit estimate where possible), and reasoning.
- proposal types: "add_batch" | "retime" | "requantify"
- Return valid JSON only. No markdown fences, no prose outside the JSON.
"""

_BATCH_ADVISOR_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["add_batch", "retime", "requantify"]},
                    "menu_item_id": {"type": "integer"},
                    "dish_name": {"type": "string"},
                    "target_window_start": {"type": "number"},
                    "target_qty": {"type": "integer"},
                    "current_qty": {"type": "integer"},
                    "forecast_demand": {"type": "number"},
                    "projected_benefit_description": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["type", "menu_item_id", "dish_name", "target_window_start",
                             "target_qty", "forecast_demand", "projected_benefit_description", "reasoning"],
            },
        },
        "schedule_assessment": {"type": "string"},
    },
    "required": ["proposals"],
}


def suggest_batch_changes(
    context: Dict[str, Any],
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Ask Gemini 2.5 Pro to analyse today's batch schedule and propose changes.

    Returns a dict with keys:
        proposals: list of proposal dicts (may be empty if schedule is optimal)
        schedule_assessment: brief prose summary
        source: "gemini_2.5_pro" | "vertex_unavailable" | "error"

    Designed to be called via asyncio.to_thread from async code.
    """
    try:
        from .vertex import build_genai_client, vertex_available
        from .config import GEMINI_REASONER_MODEL

        if not vertex_available():
            logger.info("suggest_batch_changes: Vertex AI unavailable, skipping")
            return {"proposals": [], "schedule_assessment": "Vertex AI unavailable.", "source": "vertex_unavailable"}

        client = build_genai_client()
        prompt = (
            "Analyse the following restaurant batch schedule and demand context. "
            "Return your proposals as JSON matching the required schema.\n\n"
            f"Context:\n{json.dumps(context, indent=2, default=str)}"
        )

        import concurrent.futures

        def _call():
            return client.models.generate_content(
                model=GEMINI_REASONER_MODEL,
                contents=prompt,
                config={
                    "system_instruction": _BATCH_ADVISOR_SYSTEM,
                    "temperature": 0.2,
                    "response_mime_type": "application/json",
                    "response_schema": _BATCH_ADVISOR_SCHEMA,
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_call)
            try:
                resp = future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError:
                logger.warning("suggest_batch_changes: LLM timed out")
                return {"proposals": [], "schedule_assessment": "Reasoning model timed out.", "source": "timeout"}

        if not resp or not resp.text:
            return {"proposals": [], "schedule_assessment": "Empty response.", "source": "empty"}

        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            logger.warning("suggest_batch_changes: non-JSON response: %s", resp.text[:200])
            return {"proposals": [], "schedule_assessment": "Could not parse response.", "source": "parse_error"}

        data.setdefault("proposals", [])
        data.setdefault("schedule_assessment", "")
        data["source"] = "gemini_2.5_pro"
        return data

    except Exception as exc:  # noqa: BLE001
        logger.warning("suggest_batch_changes failed: %s", exc)
        return {"proposals": [], "schedule_assessment": str(exc), "source": "error"}


_DEMAND_EVENT_SYSTEM = """\
You are the demand-signal synthesizer for a restaurant operations AI. The restaurant manager has made a spoken statement about an upcoming event or demand change. Your job is to convert this into a precise demand forecast signal for the planning system.

TODAY: Day {sim_day} ({weekday_name}). Days are numbered from 0; day-of-week: 0=Monday, 1=Tuesday, ..., 6=Sunday.

Your output specifies a time window as DAY OFFSETS FROM TODAY (not absolute dates):
  - day_from: integer >= 0 (0 = today, 1 = tomorrow, 7 = next Monday if today is Monday, etc.)
  - day_to: integer >= day_from (inclusive, for a multi-day event)
  - start_hour: optional integer 0-23 (hour the event starts; omit = whole day from open)
  - end_hour: optional integer 0-23 (hour the event ends; omit = whole day till close)

DEMAND MULTIPLIER RULES:
  - "parade", "festival", "marathon", "street fair", "concert nearby", "major event": 1.5-1.8x
  - "huge event", "stadium event nearby", "crowd of 5000+": 1.8-2.0x
  - "quiet week", "bad weather", "road closure reducing access": 0.6-0.8x
  - "slight uptick", "small local event": 1.2-1.4x
  - Specify demand_multiplier (float) OR expected_attendance (integer); not both.

EXAMPLES:
1. "There's going to be a parade next week from Monday to Wednesday" (today = Friday, day 4):
   -> day_from=3 (next Monday = 3 days away), day_to=5 (next Wednesday), demand_multiplier=1.6, event_ref="parade-next-week", event_kind="parade"

2. "There's a concert tonight from 6pm to 10pm" (today = any day):
   -> day_from=0, day_to=0, start_hour=18, end_hour=22, demand_multiplier=1.4, event_ref="concert-tonight", event_kind="concert"

3. "It's going to be a slow week, road closures" (today = Monday):
   -> day_from=0, day_to=6, demand_multiplier=0.7, event_ref="road-closure-week", event_kind="access_disruption"

4. "Big football match Saturday" (today = Wednesday):
   -> day_from=3, day_to=3, demand_multiplier=1.7, event_ref="football-saturday", event_kind="sports_event"

5. "We're expecting about 800 people at the park fair this weekend" (today = Thursday):
   -> day_from=2, day_to=3, expected_attendance=800, event_ref="park-fair-weekend", event_kind="fair"

RULES:
- ALWAYS use day offsets (day_from, day_to), never absolute dates or sim-seconds.
- "next week" when today is Mon-Fri means the following Mon-Sun (7-13 days away for Mon, etc.). Compute correctly from weekday_name.
- "this weekend" = nearest Saturday and Sunday.
- "tomorrow" = day_from=1, day_to=1.
- If the statement is ambiguous, pick the most reasonable interpretation and set confidence < 0.7.
- If there is no clear event (e.g. "what's the weather"), return an error field instead.
- event_ref should be a short kebab-case slug (no spaces).
"""

_DEMAND_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "event_ref": {"type": "string"},
        "event_kind": {"type": "string"},
        "day_from": {"type": "integer"},
        "day_to": {"type": "integer"},
        "start_hour": {"type": "integer"},
        "end_hour": {"type": "integer"},
        "demand_multiplier": {"type": "number"},
        "expected_attendance": {"type": "number"},
        "affected_categories": {"type": "array", "items": {"type": "string"}},
        "affected_menu_item_ids": {"type": "array", "items": {"type": "integer"}},
        "confidence": {"type": "number"},
        "raw_text": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["event_ref", "event_kind", "day_from", "day_to", "confidence", "raw_text"],
}


def synthesize_demand_event(
    raw_text: str,
    sim_day: int,
    weekday_name: str,
    sim_context: dict,
    timeout_s: float = 20.0,
) -> dict:
    """Convert a manager's spoken event statement into a demand forecast signal.

    Returns a dict matching _DEMAND_EVENT_SCHEMA, or {"error": "...", "raw_text": raw_text}
    on failure. Designed to be called via asyncio.to_thread from async code.
    """
    try:
        from .vertex import build_genai_client, vertex_available
        from .config import GEMINI_REASONER_MODEL

        if not vertex_available():
            logger.info("synthesize_demand_event: Vertex AI unavailable, skipping")
            return {"error": "synthesis unavailable (Vertex AI not configured)", "raw_text": raw_text}

        client = build_genai_client()
        system = _DEMAND_EVENT_SYSTEM.format(sim_day=sim_day, weekday_name=weekday_name)
        context_str = json.dumps(sim_context, default=str) if sim_context else "{}"
        prompt = (
            f"Manager statement: {raw_text!r}\n\n"
            f"Current ops snapshot (compact): {context_str}\n\n"
            "Convert the manager's statement into a demand event signal. "
            "Return JSON matching the required schema."
        )

        import concurrent.futures

        def _call():
            return client.models.generate_content(
                model=GEMINI_REASONER_MODEL,
                contents=prompt,
                config={
                    "system_instruction": system,
                    "temperature": 0.15,
                    "response_mime_type": "application/json",
                    "response_schema": _DEMAND_EVENT_SCHEMA,
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_call)
            try:
                resp = future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError:
                logger.warning("synthesize_demand_event: LLM timed out")
                return {"error": "synthesis timed out", "raw_text": raw_text}

        if not resp or not resp.text:
            return {"error": "empty response from synthesizer", "raw_text": raw_text}

        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            logger.warning("synthesize_demand_event: non-JSON response: %s", resp.text[:200])
            return {"error": "could not parse synthesizer response", "raw_text": raw_text}

        # Validate day offsets
        day_from = data.get("day_from", 0)
        day_to = data.get("day_to", 0)
        if not isinstance(day_from, int) or day_from < 0:
            data["day_from"] = 0
        if not isinstance(day_to, int) or day_to < data.get("day_from", 0):
            data["day_to"] = data.get("day_from", 0)
        data.setdefault("raw_text", raw_text)
        return data

    except Exception as exc:  # noqa: BLE001
        logger.warning("synthesize_demand_event failed: %s", exc)
        return {"error": "synthesis unavailable", "raw_text": raw_text}
