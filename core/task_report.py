"""LLM interpreter for a cook's *written* task report.

The cook types a single free-text sentence about a kitchen task ("did it but ran
10 min late", "couldn't — the stove is broken").  This module has Roba (Gemini)
interpret it into a structured outcome so the written path is as smart as the
voice one: it decides done vs not-done, writes a clean note, grades severity, and
— only when genuinely ambiguous — asks ONE clarifying question.

Mirrors :func:`core.reasoner.consult`: uses ``core.vertex`` directly, runs in a
worker thread with a timeout, and degrades gracefully (recording the raw text as
not-done at medium severity) whenever Vertex AI is unavailable or errors.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_VALID_OUTCOME = {"done", "not_done"}
_VALID_SEVERITY = {"low", "medium", "high"}

_SYSTEM_PROMPT = """\
You are Roba, an AI kitchen desk. A cook has typed a short report about a kitchen
checklist task. Interpret it and return ONLY JSON (no markdown) with fields:

  outcome:            "done" | "not_done"
  note:               a concise, clean one-line summary of what the cook reported
  severity:           "low" | "medium" | "high"   (how much the manager should care)
  needs_clarification: boolean
  question:           a single short question, or null

Rules:
- Decide outcome from meaning: "did it, just late" → done; "couldn't / didn't /
  ran out / broken" → not_done.
- severity (only really matters for not_done):
    high   = food-safety or equipment failure, or something blocking service
             (e.g. broken stove/fridge, temperature breach, no hot water).
    medium = a task skipped but recoverable soon.
    low    = minor or cosmetic.
- Set needs_clarification=true and ask ONE short question when the report has NO
  concrete detail about what actually happened — e.g. bare words like "problem",
  "issue", "couldn't", "didn't", "no" with nothing else. In that case you cannot
  judge severity, so ask what happened. Otherwise needs_clarification=false and
  question=null.
- When there IS a concrete reason, decide — do not ask for details you can
  reasonably infer.
"""


def _fallback(text: str) -> Dict[str, Any]:
    return {
        "outcome": "not_done",
        "note": (text or "").strip() or "Reported not done.",
        "severity": "medium",
        "needs_clarification": False,
        "question": None,
    }


def _coerce(data: Dict[str, Any], text: str, *, allow_question: bool) -> Dict[str, Any]:
    outcome = str(data.get("outcome") or "").strip().lower()
    if outcome not in _VALID_OUTCOME:
        outcome = "not_done"
    severity = str(data.get("severity") or "").strip().lower()
    if severity not in _VALID_SEVERITY:
        severity = "medium" if outcome == "not_done" else "low"
    note = str(data.get("note") or "").strip() or (text or "").strip()
    needs = bool(data.get("needs_clarification")) and allow_question
    question = data.get("question")
    question = str(question).strip() if (needs and question) else None
    if needs and not question:
        needs = False
    return {
        "outcome": outcome,
        "note": note,
        "severity": severity,
        "needs_clarification": needs,
        "question": question,
    }


def interpret(
    task: Dict[str, Any],
    text: str,
    prior_qa: Optional[List[Dict[str, str]]] = None,
    timeout_s: float = 12.0,
) -> Dict[str, Any]:
    """Interpret a cook's written task report → structured outcome.

    ``task`` supplies context (title/category/details). ``prior_qa`` is a list of
    ``{"q":..., "a":...}`` from an earlier clarification round; when present the
    model MUST decide (no second question).  Never raises — degrades to the
    raw-text fallback.
    """
    # Once we've already asked once, force a decision (no repeat questions).
    allow_question = not prior_qa
    try:
        from .vertex import build_genai_client, vertex_available
        from .config import GEMINI_REASONER_MODEL

        if not vertex_available():
            return _fallback(text)

        client = build_genai_client()
        lines = [
            f"Task: {task.get('title', '')}",
            f"Category: {task.get('category', '')}",
        ]
        details = task.get("details") or []
        if details:
            lines.append("Checklist: " + "; ".join(str(d) for d in details))
        lines.append(f'Cook wrote: "{text.strip()}"')
        for qa in (prior_qa or []):
            lines.append(f"Earlier — you asked: {qa.get('q','')}  cook answered: {qa.get('a','')}")
        if not allow_question:
            lines.append("You have already asked once. DECIDE now; needs_clarification must be false.")
        prompt = "\n".join(lines)

        import concurrent.futures

        def _call():
            return client.models.generate_content(
                model=GEMINI_REASONER_MODEL,
                contents=prompt,
                config={
                    "system_instruction": _SYSTEM_PROMPT,
                    "temperature": 0.2,
                    "response_mime_type": "application/json",
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_call)
            try:
                resp = future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError:
                return _fallback(text)

        raw = (resp.text or "").strip() if resp else ""
        if not raw:
            return _fallback(text)
        # Tolerate accidental ```json fences.
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else raw
        data = json.loads(raw)
        return _coerce(data, text, allow_question=allow_question)
    except Exception as exc:  # noqa: BLE001
        logger.warning("task_report.interpret failed: %s", exc)
        return _fallback(text)


def _demo() -> None:
    """Self-check: coercion + fallback are robust without a live LLM."""
    # Fallback path (Vertex unavailable in test env → interpret returns fallback).
    r = interpret({"title": "Preheat grill", "category": "opening"}, "stove is broken")
    assert r["outcome"] == "not_done" and r["severity"] in _VALID_SEVERITY, r
    assert r["needs_clarification"] is False, r
    # Coercion clamps bad values and drops a question once we've already asked.
    c = _coerce({"outcome": "weird", "severity": "urgent", "needs_clarification": True,
                 "question": "which station?"}, "x", allow_question=False)
    assert c["outcome"] == "not_done" and c["severity"] == "medium" and c["needs_clarification"] is False, c
    c2 = _coerce({"outcome": "done", "needs_clarification": True, "question": "when?"}, "did it late",
                 allow_question=True)
    assert c2["outcome"] == "done" and c2["needs_clarification"] is True and c2["question"] == "when?", c2
    print("task_report demo OK")


if __name__ == "__main__":
    _demo()
