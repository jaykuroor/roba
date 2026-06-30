"""Background reasoning model — consults a superior LLM for complex trade-off decisions.

Uses Gemini 2.5 Pro on Vertex AI (same client as the rest of the stack).
Called by VoiceActions.consult_reasoner() via the voice agent's consult_reasoner tool.
"""
from __future__ import annotations
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert restaurant operations advisor. "
    "You receive a specific question from a restaurant manager (via a voice assistant) "
    "and relevant context about current inventory, staff, and menu state. "
    "Your job: give ONE decisive, actionable recommendation in 2-3 sentences that the "
    "voice assistant can read aloud. Be concrete — name specific dishes, ingredients, or "
    "staff actions. Do not hedge with 'it depends' — make a call."
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
