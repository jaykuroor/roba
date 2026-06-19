"""Smoke-test the configured LLM provider.

Run from the repo root:

    python scripts/llm_smoke.py

With GEMINI_API_KEY set, this performs live Gemini calls through google-genai.
Without keys, it verifies that each major app use-site reaches the canned
fallback cleanly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config
from core.llm import CANNED_NOTE, LLMProvider


VOICE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entity_type": {"type": "string"},
        "entity_ref": {"type": "string"},
        "attribute": {"type": "string"},
        "value": {"type": "string"},
        "effective_window": {"type": "object"},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "confidence"],
}

REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {"type": "string"},
        "summary": {"type": "string"},
        "suggested_action": {"type": "string"},
        "dish_mentions": {"type": "array"},
        "sentiment": {"type": "string"},
    },
    "required": [
        "severity",
        "summary",
        "suggested_action",
        "dish_mentions",
        "sentiment",
    ],
}

OUTCOME_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ingredient_id": {"type": "integer"},
        "agreed_price": {"type": "number"},
        "agreed_terms": {"type": "string"},
        "agreed": {"type": "boolean"},
    },
    "required": ["agreed"],
}

GENERATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "cuisine": {"type": "string"},
        "stations": {"type": "array"},
        "menu_items": {"type": "array"},
        "suppliers": {"type": "array"},
        "staff": {"type": "array"},
    },
    "required": ["menu_items"],
}

FORECAST_OPT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "item_adjustments": {"type": "array"},
        "global_notes": {"type": "array"},
        "memory_updates": {"type": "array"},
        "confidence": {"type": "number"},
    },
    "required": ["item_adjustments", "global_notes", "memory_updates"],
}


def run_case(
    llm: LLMProvider,
    name: str,
    messages: List[dict],
    use_site: str,
    schema: Optional[dict] = None,
    max_tokens: int = 400,
) -> Dict[str, Any]:
    result = llm.complete(
        messages,
        json_schema=schema,
        max_tokens=max_tokens,
        use_site=use_site,
    )
    canned = isinstance(result, dict) and result.get("note") == CANNED_NOTE
    ok = bool(result) and not canned
    if not os.getenv("GEMINI_API_KEY"):
        ok = canned or isinstance(result, str)
    return {"case": name, "ok": ok, "canned": canned, "result": result}


def main() -> int:
    llm = LLMProvider(fallback=["gemini", "canned"])
    cases = [
        run_case(
            llm,
            "call_turn_text",
            [
                {"role": "system", "content": "You are a concise supplier caller."},
                {"role": "user", "content": "Ask for a better tomato price."},
            ],
            "call_supplier",
            None,
            120,
        ),
        run_case(
            llm,
            "voice_extraction_json",
            [
                {
                    "role": "system",
                    "content": "Extract one restaurant operational fact as JSON.",
                },
                {"role": "user", "content": "Priya is sick tomorrow."},
            ],
            "voice",
            VOICE_SCHEMA,
        ),
        run_case(
            llm,
            "review_analysis_json",
            [
                {"role": "system", "content": "Analyze this review as JSON."},
                {"role": "user", "content": "Cold pasta, waited an hour."},
            ],
            "review",
            REVIEW_SCHEMA,
        ),
        run_case(
            llm,
            "call_outcome_json",
            [
                {"role": "system", "content": "Extract supplier call outcome as JSON."},
                {
                    "role": "user",
                    "content": "supplier agreed to 1.8 per kg for tomatoes",
                },
            ],
            "outcome_extraction",
            OUTCOME_SCHEMA,
        ),
        run_case(
            llm,
            "dataset_generation_json",
            [
                {
                    "role": "system",
                    "content": (
                        "Generate a small restaurant dataset qualitative layer as JSON."
                    ),
                },
                {"role": "user", "content": "Cuisine: italian. Two menu items."},
            ],
            "generation",
            GENERATION_SCHEMA,
            900,
        ),
        run_case(
            llm,
            "forecaster_optimization_json",
            [
                {
                    "role": "system",
                    "content": (
                        "Optimize this restaurant demand forecast as compact JSON. "
                        "Use concise user-facing reasons only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "{'weather': {'temperature_c': 7, 'condition': 'storm'}, "
                        "'items': [{'menu_item_id': 1, 'name': 'Margherita Pizza', "
                        "'baseline': 3, 'multipliers': {'weather': 1.1}}], "
                        "'memory': []}"
                    ),
                },
            ],
            "forecaster_optimization",
            FORECAST_OPT_SCHEMA,
            700,
        ),
    ]
    report = {
        "gemini_model": config.GEMINI_MODEL,
        "gemini_key_present": bool(os.getenv("GEMINI_API_KEY")),
        "request_count": llm.request_count,
        "cases": cases,
    }
    print(json.dumps(report, indent=2, default=str))
    return 0 if all(case["ok"] for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
