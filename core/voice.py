"""Voice intake pipeline (§11).

``VoiceProcessor.process`` runs the full §11 pipeline on a piece of transcribed
text:

1. **Extract** a structured fact with the LLM using the §11 extraction schema
   (``intent, entity_type, entity_ref, attribute, value, effective_window?,
   confidence``). When the LLM is unavailable (canned fallback) or unsure, a
   best-effort regex parser recovers the obvious intents (e.g. a "leave"
   keyword → ``set_leave``).
2. **Route** the fact into typed operational signals.
3. **Persist** a ``user_facts`` audit row.
4. **Optionally emit** the legacy ``USER_FACT`` compatibility signal.
5. **Return** ``{extracted, routes, resulting_writes, signal_id}``.

Voice is ``core`` infrastructure and no longer owns domain-table writes. Track
agents consume the routed signals and apply their own deterministic changes.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from .clock import DAY_CLOSE_OFFSET, DAY_OPEN_OFFSET, SECONDS_PER_DAY
from .config import EVENT_MULT, VOICE_DEFAULT_MODE, VOICE_EMIT_LEGACY_USER_FACT
from .llm import CANNED_NOTE
from .module_capabilities import capability_prompt_context
from .models import (
    ApprovalRequest,
    Forecast,
    Batch,
    BatchDefinition,
    Competitor,
    CompetitorObservation,
    Forecast,
    Ingredient,
    InventoryLevel,
    MenuItem,
    Recipe,
    RecipeLine,
    Review,
    ReviewInsight,
    Staff,
    StaffStation,
    Station,
    Supplier,
    UserFact,
    VoicePlan,
    WasteEvent,
)
from .signals import SignalType

logger = logging.getLogger(__name__)

# Map from target_modules string → agent name used in the orchestrator.
_MODULE_TO_AGENT: Dict[str, str] = {
    "track_a.forecaster": "forecaster",
    "track_a.staff": "staff",
    "track_a.review": "review",
    "track_a.competitor": "competitor",
    "track_b.ledger": "ledger",
    "track_b.optimizer": "optimizer",
    "track_b.market_spectator": "market_spectator",
}

# Roles that can use cook-specific intents.
COOK_ROLES = {"cook", "kitchen"}

# §11 extraction schema (passed to the LLM as JSON mode).
EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entity_type": {"type": "string"},
        "entity_ref": {"type": "string"},
        "attribute": {"type": "string"},
        "value": {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "object"},
                {"type": "array"},
            ]
        },
        "effective_window": {"type": "object"},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "confidence"],
}

# Valid intents (§11).
INTENTS = {
    "add_menu_item", "edit_menu_item", "set_recipe", "add_inventory_count",
    "record_receipt", "set_attendance", "set_leave", "add_event",
    "set_supplier_price", "set_competitor", "add_review",
    "set_operational_constraint", "other",
}

_NUMBER_RE = r"(\d+(?:\.\d+)?)"
_UNIT_WORDS = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg",
    "kilograms": "kg", "g": "g", "gram": "g", "grams": "g",
    "l": "ml", "liter": "ml", "liters": "ml", "litre": "ml", "litres": "ml",
    "ml": "ml", "each": "each", "unit": "each", "units": "each",
    "piece": "each", "pieces": "each",
}


def _redact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of payload suitable for plan storage (strip large blobs)."""
    redacted = {}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > 200:
            redacted[k] = v[:200] + "…"
        else:
            redacted[k] = v
    return redacted


def _route_human_summary(spec: Dict[str, Any], extracted: Dict[str, Any]) -> str:
    """Generate a 1-line human-readable summary for a route spec."""
    sig = spec.get("signal_type")
    sig_str = sig.value if hasattr(sig, "value") else str(sig)
    entity = extracted.get("entity_ref") or ""
    value = extracted.get("value")
    return f"{sig_str}: {entity} {value}".strip()


def _plan_human_readable(extracted: Dict[str, Any], routes: List[Dict[str, Any]]) -> str:
    """Build a friendly summary of what the plan will do."""
    intent = extracted.get("intent") or "other"
    entity = extracted.get("entity_ref") or ""
    window = extracted.get("effective_window")
    window_str = ""
    if isinstance(window, dict):
        start = window.get("start")
        end = window.get("end")
        if start is not None and end is not None:
            window_str = f" (window {start:.0f}–{end:.0f})"
    agents = sorted({a for r in routes for a in (r.get("target_agents") or [])})
    agents_str = f" → agents: {', '.join(agents)}" if agents else ""
    if intent == "add_event":
        return f"Record demand event '{entity}'{window_str}{agents_str}."
    if intent == "set_operational_constraint":
        return f"Set production constraint for '{entity}'{window_str}{agents_str}."
    if intent == "set_leave":
        return f"Mark '{entity}' as on leave{window_str}{agents_str}."
    if intent == "add_review":
        return f"Log customer feedback about '{entity}'{agents_str}."
    if intent == "set_competitor":
        return f"Log competitor note about '{entity}'{agents_str}."
    if intent == "record_receipt":
        return f"Record inventory receipt of '{entity}'{agents_str}."
    if intent == "set_supplier_price":
        return f"Update supplier pricing for '{entity}'{agents_str}."
    if routes:
        types_str = ", ".join({r.get("signal_type", "") for r in routes})
        return f"Emit {types_str}{agents_str}."
    return f"Intent '{intent}' for '{entity}'{agents_str}."


class VoiceProcessor:
    """STT text → LLM/regex extraction → typed routes + audit (§11).

    Stream B adds plan/confirm mode: ``plan`` computes what will happen and
    persists a ``VoicePlan`` row; ``confirm`` applies it.  ``process`` remains
    the original single-shot path (used by the legacy /api/voice/transcript).
    """

    def __init__(
        self,
        llm: Any,
        bus: Any,
        db_session_factory: Callable[[], Any],
        approvals: Any = None,
        calls: Any = None,
    ):
        self.llm = llm
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.approvals = approvals
        self.calls = calls

    def attach_approvals(self, approvals: Any) -> None:
        self.approvals = approvals

    def attach_calls(self, calls: Any) -> None:
        self.calls = calls

    # -- public API ---------------------------------------------------------

    def process(self, raw_text: str) -> Dict[str, Any]:
        """Run the full §11 pipeline; see module docstring for the steps."""
        extracted = self._extract(raw_text)
        routes = self._emit_routes(extracted, raw_text)
        resulting_writes = [
            f"signal:{route['signal_type']}:{route['signal_id']}"
            for route in routes
            if route.get("signal_id")
        ] or ["stored"]
        self._write_user_fact(raw_text, extracted, resulting_writes)
        legacy_signal_id = (
            self._emit_user_fact(extracted, raw_text)
            if VOICE_EMIT_LEGACY_USER_FACT
            else None
        )
        signal_id = legacy_signal_id or next(
            (route.get("signal_id") for route in routes if route.get("signal_id")),
            None,
        )
        return {
            "extracted": extracted,
            "routes": routes,
            "resulting_writes": resulting_writes,
            "signal_id": signal_id,
        }

    # -- Stream B: plan/confirm API ------------------------------------------

    def plan(
        self,
        raw_text: str,
        role: str = "manager",
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute a plan for the spoken input without emitting any signals.

        Returns a ``VoicePlan`` dict.  When ``mode="auto"`` emits immediately
        and returns the applied plan.

        Cook-specific intents (mark_batch_cooked, report_waste) are handled
        here if ``role`` is in ``COOK_ROLES``.
        """
        if mode is None:
            mode = VOICE_DEFAULT_MODE

        # Check for cook-specific intents first.
        if role in COOK_ROLES:
            cook_result = self._try_cook_intent(raw_text)
            if cook_result is not None:
                return self._save_and_maybe_apply(cook_result, raw_text, role, mode)

        extracted = self._extract(raw_text)
        specs = self._route_specs(extracted, raw_text)

        # Manager: check for call-request intent.
        call_plan = self._try_call_intent(extracted, raw_text, role)
        if call_plan is not None:
            return self._save_and_maybe_apply(call_plan, raw_text, role, mode)

        # Build the plan dict.
        route_summaries = []
        for spec in specs:
            modules = spec.get("target_modules") or []
            agents = [_MODULE_TO_AGENT[m] for m in modules if m in _MODULE_TO_AGENT]
            route_summaries.append({
                "signal_type": spec["signal_type"].value
                if hasattr(spec["signal_type"], "value") else str(spec["signal_type"]),
                "target_modules": modules,
                "target_agents": agents,
                "payload_preview": _redact_payload(spec.get("payload") or {}),
                "summary": _route_human_summary(spec, extracted),
            })

        human_readable = _plan_human_readable(extracted, route_summaries)
        plan_dict: Dict[str, Any] = {
            "role": role,
            "mode": mode,
            "summary": human_readable,
            "human_readable": human_readable,
            "routes": route_summaries,
            "requires_approval": False,
            "clarification": None,
        }
        return self._save_and_maybe_apply(plan_dict, raw_text, role, mode)

    def confirm(self, plan_id: str) -> Dict[str, Any]:
        """Apply a pending plan: emit its signals and mark it applied."""
        session = self.db_session_factory()
        try:
            row = session.get(VoicePlan, plan_id)
            if row is None:
                return {"error": f"Plan {plan_id} not found", "status": "not_found"}
            if row.status != "pending":
                return {"plan_id": plan_id, "status": row.status}
            plan = row.plan or {}
        finally:
            session.close()

        signal_ids: List[str] = []
        for route in plan.get("routes") or []:
            sig_type_str = route.get("signal_type")
            if not sig_type_str:
                continue
            # Resolve the signal type.
            try:
                sig_type = SignalType(sig_type_str)
            except ValueError:
                continue
            payload = route.get("payload_preview") or route.get("payload") or {}
            target_agents = route.get("target_agents") or None
            try:
                signal = self.bus.emit(
                    sig_type,
                    payload,
                    source="voice_plan",
                    target_agents=target_agents,
                )
                if signal is not None:
                    signal_ids.append(signal.signal_id)
            except Exception:  # noqa: BLE001
                logger.exception("voice plan confirm: failed to emit %s", sig_type_str)

        # Handle non-signal actions (e.g. batch cooked, call request).
        for action in plan.get("direct_actions") or []:
            try:
                self._apply_direct_action(action)
            except Exception:  # noqa: BLE001
                logger.exception("voice plan confirm: failed direct action %s", action)

        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            row = session.get(VoicePlan, plan_id)
            if row is not None:
                row.status = "applied"
                row.applied_at = now
                session.commit()
        finally:
            session.close()

        self._write_user_fact(
            plan.get("raw_text", ""),
            {"intent": "confirmed_plan", "plan_id": plan_id},
            [f"signal:{s}" for s in signal_ids] or ["applied"],
        )
        return {"plan_id": plan_id, "status": "applied", "signal_ids": signal_ids}

    def cancel(self, plan_id: str) -> Dict[str, Any]:
        """Cancel a pending plan."""
        session = self.db_session_factory()
        try:
            row = session.get(VoicePlan, plan_id)
            if row is None:
                return {"error": f"Plan {plan_id} not found"}
            row.status = "cancelled"
            session.commit()
        finally:
            session.close()
        return {"plan_id": plan_id, "status": "cancelled"}

    def clarify(self, plan_id: str, answer: str) -> Dict[str, Any]:
        """Re-plan with the clarification answer appended to the original text."""
        session = self.db_session_factory()
        try:
            row = session.get(VoicePlan, plan_id)
            if row is None:
                return {"error": f"Plan {plan_id} not found"}
            original_text = row.raw_text or ""
            role = row.role or "manager"
            mode = row.mode or VOICE_DEFAULT_MODE
            row.status = "superseded"
            session.commit()
        finally:
            session.close()
        combined = f"{original_text} [clarification: {answer}]"
        return self.plan(combined, role=role, mode=mode)

    # -- cook-specific intent detection (Stream B) ---------------------------

    def _try_cook_intent(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """Check if the text is a cook intent (batch cooked / waste report).

        Returns a partial plan dict with ``direct_actions`` if recognised,
        or None if it's not a cook intent.
        """
        low = raw_text.lower()

        # Batch cooked: "I cooked the margherita batch, made 18"
        cooked_phrases = (
            "i cooked", "we cooked", "batch done", "cooked the batch",
            "finished cooking", "finished batch", "batch is done",
            "batch cooked", "made the batch",
        )
        if any(ph in low for ph in cooked_phrases):
            batch = self._resolve_next_batch_from_text(low)
            qty = self._extract_qty_from_text(low)
            if batch is not None:
                return {
                    "role": "cook",
                    "summary": f"Mark batch #{batch['id']} ({batch['item_name']}) as cooked"
                               + (f", actual qty {qty}" if qty else ""),
                    "human_readable": (
                        f"Roba will record batch #{batch['id']} ({batch['item_name']}) "
                        f"as cooked"
                        + (f" with actual quantity {qty}" if qty else "")
                        + "."
                    ),
                    "routes": [],
                    "direct_actions": [{
                        "type": "batch_cooked",
                        "batch_id": batch["id"],
                        "menu_item_id": batch["menu_item_id"],
                        "actual_made_qty": qty or batch.get("planned_qty") or 0,
                        "planned_qty": batch.get("planned_qty"),
                    }],
                    "requires_approval": False,
                    "clarification": None,
                }
            # Can't resolve → return a clarification
            return {
                "role": "cook",
                "summary": "Mark a batch as cooked",
                "human_readable": "Which batch did you cook? Please say the dish name.",
                "routes": [],
                "direct_actions": [],
                "requires_approval": False,
                "clarification": {
                    "question": "Which dish batch did you cook?",
                    "options": self._upcoming_batch_names(),
                },
            }

        # Waste report: "threw away 6 tiramisus"
        waste_phrases = (
            "threw away", "thrown away", "wasted", "had to throw",
            "discarded", "waste", "in the bin", "in the trash",
        )
        if any(ph in low for ph in waste_phrases):
            qty = self._extract_qty_from_text(low)
            item_name = self._extract_dish_from_text(low)
            if qty is not None and item_name:
                return {
                    "role": "cook",
                    "summary": f"Report waste: {qty} {item_name}",
                    "human_readable": (
                        f"Roba will record waste of {qty} × {item_name}. "
                        "Was this from the batch you just cooked (overproduction) "
                        "or did an ingredient spoil / prep error?"
                    ),
                    "routes": [],
                    "direct_actions": [],
                    "requires_approval": False,
                    "clarification": {
                        "question": "Why was the food wasted?",
                        "options": [
                            {"value": "overproduction", "label": "From a batch (overproduction)"},
                            {"value": "spoilage", "label": "Ingredient spoiled"},
                            {"value": "prep_error", "label": "Prep error / dropped"},
                        ],
                        "pending_waste": {
                            "item_name": item_name,
                            "qty": qty,
                        },
                    },
                }
            # Partial info → ask
            return {
                "role": "cook",
                "summary": "Report food waste",
                "human_readable": "How much was wasted and what dish?",
                "routes": [],
                "direct_actions": [],
                "requires_approval": False,
                "clarification": {
                    "question": "What was thrown away and how much?",
                    "options": [],
                },
            }

        # "I threw away 6 tiramisus because they spoiled" — embedded cause
        for cause_phrase, waste_type in (
            ("overproduction", "overproduction"),
            ("from the batch", "overproduction"),
            ("too many", "overproduction"),
            ("spoil", "spoilage"),
            ("expir", "spoilage"),
            ("drop", "prep_error"),
            ("prep error", "prep_error"),
        ):
            if cause_phrase in low and any(ph in low for ph in waste_phrases):
                qty = self._extract_qty_from_text(low)
                item_name = self._extract_dish_from_text(low)
                if qty and item_name:
                    menu_item_id = self._resolve_menu_item_id(item_name)
                    return {
                        "role": "cook",
                        "summary": f"Report {waste_type} waste: {qty} {item_name}",
                        "human_readable": (
                            f"Roba will record {waste_type} waste of "
                            f"{qty} × {item_name} and update the demand model."
                        ),
                        "routes": [],
                        "direct_actions": [{
                            "type": "waste_event",
                            "menu_item_id": menu_item_id,
                            "qty": qty,
                            "waste_type": waste_type,
                            "reason": raw_text,
                        }],
                        "requires_approval": False,
                        "clarification": None,
                    }

        return None

    def _try_call_intent(
        self, extracted: Dict[str, Any], raw_text: str, role: str
    ) -> Optional[Dict[str, Any]]:
        """If the manager asks to 'call' a competitor/supplier, create an approval."""
        if role not in ("manager",):
            return None
        low = raw_text.lower()
        call_phrases = ("call ", "phone ", "ring ", "contact ")
        if not any(ph in low for ph in call_phrases):
            return None
        # Check for competitor/supplier keywords.
        counterparty_type = None
        counterparty_id = None
        if any(w in low for w in ("competitor", "rival", "restaurant", "pizza", "mario")):
            counterparty_type = "competitor"
            counterparty_id = self._resolve_counterparty_id("competitor", raw_text)
        elif any(w in low for w in ("supplier", "vendor", "farm", "distributor")):
            counterparty_type = "supplier"
            counterparty_id = self._resolve_counterparty_id("supplier", raw_text)
        if counterparty_type is None:
            return None

        return {
            "role": role,
            "summary": f"Request {counterparty_type} call",
            "human_readable": (
                f"Roba will create an approval request for an outbound "
                f"{counterparty_type} call. You will confirm it in the approvals inbox."
            ),
            "routes": [],
            "direct_actions": [{
                "type": "call_request",
                "counterparty_type": counterparty_type,
                "counterparty_id": counterparty_id,
                "purpose": raw_text,
            }],
            "requires_approval": True,
            "clarification": None,
        }

    def _apply_direct_action(self, action: Dict[str, Any]) -> None:
        """Execute a non-signal action from a confirmed plan."""
        kind = action.get("type")
        if kind == "batch_cooked":
            self._record_batch_cooked(
                batch_id=int(action["batch_id"]),
                menu_item_id=int(action["menu_item_id"]),
                actual_made_qty=float(action["actual_made_qty"]),
                planned_qty=action.get("planned_qty"),
            )
        elif kind == "waste_event":
            self._record_waste_event(
                menu_item_id=action.get("menu_item_id"),
                qty=float(action.get("qty") or 0.0),
                waste_type=str(action.get("waste_type") or "overproduction"),
                reason=str(action.get("reason") or ""),
            )
        elif kind == "call_request":
            if self.calls is not None:
                try:
                    self.calls.request(
                        agent="manager",
                        counterparty_type=str(action.get("counterparty_type") or "competitor"),
                        counterparty_id=int(action.get("counterparty_id") or 0),
                        purpose=str(action.get("purpose") or ""),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("voice plan: call request failed")

    def _record_batch_cooked(
        self,
        batch_id: int,
        menu_item_id: int,
        actual_made_qty: float,
        planned_qty: Optional[float] = None,
    ) -> None:
        """Advance a batch to 'cooked'/ready state and emit BATCH_PROGRESS."""
        from .signals import SignalType  # avoid circular
        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            batch = session.get(Batch, batch_id)
            if batch is None:
                return
            old_planned = float(batch.planned_qty or 0.0)
            batch.actual_made_qty = actual_made_qty
            batch.status = "ready"
            batch.cooked_at = now
            session.commit()
        finally:
            session.close()

        # Emit BATCH_PROGRESS so the forecaster and ledger can react.
        self.bus.emit(
            SignalType.BATCH_PROGRESS,
            {
                "batch_id": batch_id,
                "menu_item_id": menu_item_id,
                "actual_made_qty": actual_made_qty,
                "planned_qty": planned_qty or old_planned,
                "status": "cooked",
                "source": "cook",
            },
            source="voice_cook",
            target_agents=["forecaster", "ledger"],
        )

    def _record_waste_event(
        self,
        menu_item_id: Optional[int],
        qty: float,
        waste_type: str,
        reason: str = "",
    ) -> None:
        """Write a WasteEvent and emit WASTE_EVENT."""
        from .signals import SignalType  # avoid circular
        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            we = WasteEvent(
                waste_type=waste_type,
                ingredient_id=None,
                menu_item_id=menu_item_id,
                lot_id=None,
                qty=qty,
                unit="each",
                cost=None,
                reason=reason,
                sim_time=now,
                source="cook",
            )
            session.add(we)
            session.commit()
            session.refresh(we)
            we_id = we.id
        finally:
            session.close()

        self.bus.emit(
            SignalType.WASTE_EVENT,
            {
                "waste_event_id": we_id,
                "waste_type": waste_type,
                "menu_item_id": menu_item_id,
                "ingredient_id": None,
                "qty": qty,
                "unit": "each",
                "cost": None,
                "reason": reason,
                "source": "cook",
            },
            source="voice_cook",
            target_agents=["forecaster", "ledger"],
        )

    # -- plan persistence helpers -------------------------------------------

    def _save_and_maybe_apply(
        self,
        plan_dict: Dict[str, Any],
        raw_text: str,
        role: str,
        mode: str,
    ) -> Dict[str, Any]:
        """Persist the plan and apply immediately if mode='auto'."""
        plan_id = str(uuid.uuid4())
        now = float(self.bus.sim_time)
        plan_dict["plan_id"] = plan_id
        plan_dict.setdefault("raw_text", raw_text)

        status = "pending"
        if mode == "auto" and not plan_dict.get("requires_approval") and not plan_dict.get("clarification"):
            status = "applied"

        session = self.db_session_factory()
        try:
            row = VoicePlan(
                plan_id=plan_id,
                role=role,
                mode=mode,
                raw_text=raw_text,
                plan=plan_dict,
                status=status,
                created_at=now,
                applied_at=now if status == "applied" else None,
            )
            session.add(row)
            session.commit()
        finally:
            session.close()

        plan_dict["status"] = status
        plan_dict["plan_id"] = plan_id

        if status == "applied":
            # Apply immediately (auto mode).
            result = self.confirm(plan_id)
            plan_dict["signal_ids"] = result.get("signal_ids", [])

        return plan_dict

    # -- cook helper methods ------------------------------------------------

    def _resolve_next_batch_from_text(self, low: str) -> Optional[Dict[str, Any]]:
        """Find the most likely 'next approved/decided' batch from the text."""
        session = self.db_session_factory()
        try:
            # Look for a dish name in the text.
            items = session.query(MenuItem).filter(MenuItem.active == 1).all()
            matched_item = None
            for item in sorted(items, key=lambda i: len(i.name or ""), reverse=True):
                if (item.name or "").lower() in low:
                    matched_item = item
                    break
            if matched_item:
                batch = (
                    session.query(Batch)
                    .filter(
                        Batch.menu_item_id == matched_item.id,
                        Batch.status.in_(("decided", "approved")),
                        Batch.decision == "cook",
                    )
                    .order_by(Batch.decided_at.desc())
                    .first()
                )
            else:
                batch = (
                    session.query(Batch)
                    .filter(
                        Batch.status.in_(("decided", "approved")),
                        Batch.decision == "cook",
                    )
                    .order_by(Batch.decided_at.desc())
                    .first()
                )
            if batch is None:
                return None
            item_name = ""
            if batch.menu_item_id:
                mi = session.get(MenuItem, batch.menu_item_id)
                item_name = mi.name if mi else str(batch.menu_item_id)
            return {
                "id": int(batch.id),
                "menu_item_id": int(batch.menu_item_id or 0),
                "item_name": item_name,
                "planned_qty": float(batch.planned_qty or 0.0),
            }
        finally:
            session.close()

    def _upcoming_batch_names(self) -> List[str]:
        """Return names of the next few pending batches for a clarification list."""
        session = self.db_session_factory()
        try:
            batches = (
                session.query(Batch)
                .filter(Batch.status.in_(("decided", "approved")), Batch.decision == "cook")
                .order_by(Batch.decided_at.desc())
                .limit(5)
                .all()
            )
            names = []
            for b in batches:
                mi = session.get(MenuItem, b.menu_item_id) if b.menu_item_id else None
                names.append(mi.name if mi else f"Batch #{b.id}")
            return names
        finally:
            session.close()

    def _extract_qty_from_text(self, low: str) -> Optional[float]:
        """Extract a simple number from the text."""
        m = re.search(r"(\d+(?:\.\d+)?)", low)
        return float(m.group(1)) if m else None

    def _extract_dish_from_text(self, low: str) -> Optional[str]:
        """Try to find a dish name in the text by matching active menu items."""
        session = self.db_session_factory()
        try:
            items = session.query(MenuItem).filter(MenuItem.active == 1).all()
            for item in sorted(items, key=lambda i: len(i.name or ""), reverse=True):
                if (item.name or "").lower() in low:
                    return item.name
            return None
        finally:
            session.close()

    def _resolve_menu_item_id(self, ref: Optional[str]) -> Optional[int]:
        if not ref:
            return None
        session = self.db_session_factory()
        try:
            row = session.query(MenuItem).filter(MenuItem.name.ilike(str(ref))).first()
            if row is None:
                row = session.query(MenuItem).filter(MenuItem.name.ilike(f"{ref}%")).first()
            return int(row.id) if row is not None else None
        finally:
            session.close()

    def _resolve_counterparty_id(self, ctype: str, text: str) -> int:
        """Resolve a competitor or supplier id from text (best-effort, 0 if unknown)."""
        from .models import Competitor
        low = text.lower()
        session = self.db_session_factory()
        try:
            if ctype == "competitor":
                rows = session.query(Competitor).all()
                for row in sorted(rows, key=lambda r: len(r.name or ""), reverse=True):
                    if (row.name or "").lower() in low:
                        return int(row.id)
                # Return first competitor as fallback.
                first = session.query(Competitor).first()
                return int(first.id) if first else 0
            elif ctype == "supplier":
                rows = session.query(Supplier).all()
                for row in sorted(rows, key=lambda r: len(r.name or ""), reverse=True):
                    if (row.name or "").lower() in low:
                        return int(row.id)
                first = session.query(Supplier).first()
                return int(first.id) if first else 0
        finally:
            session.close()
        return 0

    # -- (1) extraction -----------------------------------------------------

    def _extract(self, raw_text: str) -> Dict[str, Any]:
        """Ask the LLM to extract the §11 schema; fall back to regex when the
        LLM is unavailable (canned) or returns an unusable result."""
        context = self._restaurant_context_for_prompt()
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract one operational fact from a restaurant manager's "
                    "spoken note. Use the provided menu, recipe, equipment, staff, "
                    "inventory, and active-constraint context as a semantic map, not "
                    "an exact string filter. Respond with JSON matching: intent, "
                    "entity_type, entity_ref, attribute, value, effective_window "
                    "(optional {start,end}), confidence (0..1). intent is one of: "
                    + ", ".join(sorted(INTENTS)) + ". For production constraints, "
                    "value should be an object with action, target_qty when relevant, "
                    "dependency_type, dependency_ref, affected_menu_item_ids, "
                    "affected_item_names, and reasoning. Category phrases must "
                    "cascade to active menu items in that category: 'desserts are "
                    "over for today' or 'no desserts left' affects dessert items such "
                    "as Tiramisu. Specific ingredient/modifier phrases stay narrow: "
                    "'no more bacon burgers' affects bacon-dependent items, not all "
                    "burgers. Equipment failures affect only items whose station, "
                    "category, item name, recipe, or equipment context depends on "
                    "that equipment. Restaurant shorthand such as over, done, "
                    "finished, 86, sold out, no more, or out of means production is "
                    "unavailable for the stated window."
                ),
            },
            {"role": "user", "content": f"Restaurant context:\n{context}\n\nSpoken note:\n{raw_text}"},
        ]
        # Voice needs a fast response — cap to 8 s so we degrade to regex/canned
        # quickly instead of leaving the user in "Thinking…" for 20+ seconds.
        # Guard with getattr so test stubs that don't expose timeout_s still work.
        _orig_timeout = getattr(self.llm, "timeout_s", None)
        if _orig_timeout is not None:
            self.llm.timeout_s = 8.0
        try:
            result = self.llm.complete(
                messages, json_schema=EXTRACTION_SCHEMA, max_tokens=400, use_site="voice"
            )
        finally:
            if _orig_timeout is not None:
                self.llm.timeout_s = _orig_timeout

        if not isinstance(result, dict):
            return self._regex_extract(raw_text)

        # LLM unavailable / unsure -> deterministic regex parse.
        intent = result.get("intent")
        if (
            result.get("note") == CANNED_NOTE
            or intent in (None, "", "other")
            or intent not in INTENTS
            or float(result.get("confidence") or 0.0) < 0.35
        ):
            regex = self._regex_extract(raw_text)
            # Prefer the regex result when it recognised a concrete intent.
            if regex.get("intent") != "other":
                return regex
            # Otherwise keep the (neutral) LLM dict but normalise its shape.
            return self._normalise(result, raw_text)

        return self._normalise(result, raw_text)

    def _normalise(self, extracted: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
        """Ensure every §11 field is present (defaults for missing keys)."""
        out = {
            "intent": extracted.get("intent") or "other",
            "entity_type": extracted.get("entity_type") or "",
            "entity_ref": extracted.get("entity_ref"),
            "attribute": extracted.get("attribute") or "",
            "value": extracted.get("value"),
            "effective_window": extracted.get("effective_window"),
            "confidence": float(extracted.get("confidence") or 0.0),
        }
        if out["intent"] == "set_operational_constraint" and not out["effective_window"]:
            low = raw_text.lower()
            out["effective_window"] = self._window_from_text(low) or self._default_constraint_window()
        if self._is_unavailable_constraint_shape(out):
            out["attribute"] = "production_unavailable"
            out["value"] = {
                "action": "halt_production",
                "target_qty": 0,
                "raw_value": out.get("value"),
                "raw_text": raw_text,
            }
        if out["intent"] == "add_event":
            try:
                numeric = float(out.get("value"))
            except (TypeError, ValueError):
                numeric = None
            raw_low = raw_text.lower()
            attendance_words = ("people", "person", "crowd", "guests", "attendees", "pax")
            if numeric is not None and (
                str(out.get("attribute") or "").lower() == "expected_attendance"
                or numeric > 10
                or any(word in raw_low for word in attendance_words)
            ):
                out["attribute"] = "expected_attendance"
                out["value"] = numeric
        if out["intent"] == "set_operational_constraint":
            out = self._enrich_operational_constraint(out, raw_text)
        return out

    def _restaurant_context_for_prompt(self) -> str:
        """Compact operational context for voice extraction.

        The prompt needs enough structure to reason about dependencies, but it
        must stay small because voice runs happen interactively.
        """
        session = self.db_session_factory()
        try:
            menu_rows = self._menu_dependency_rows(session)
            staff_rows = [
                {
                    "id": int(staff.id),
                    "name": staff.name,
                    "role": staff.role,
                    "active": bool(staff.active),
                }
                for staff in session.query(Staff).order_by(Staff.id.asc()).all()
            ][:20]
            ingredient_lookup = {
                int(ingredient.id): ingredient
                for ingredient in session.query(Ingredient).all()
            }
            inventory_rows = [
                {
                    "ingredient_id": int(level.ingredient_id),
                    "name": (
                        str(ingredient_lookup[int(level.ingredient_id)].name or "")
                        if int(level.ingredient_id) in ingredient_lookup
                        else str(level.ingredient_id)
                    ),
                    # on_hand is expressed in the ingredient's base unit (g | ml | each).
                    "unit": (
                        str(ingredient_lookup[int(level.ingredient_id)].base_unit or "")
                        if int(level.ingredient_id) in ingredient_lookup
                        else None
                    ),
                    "on_hand": float(level.on_hand_cached or 0.0),
                    "last_counted_qty": (
                        float(level.last_counted_qty)
                        if level.last_counted_qty is not None else None
                    ),
                }
                for level in session.query(InventoryLevel)
                .order_by(InventoryLevel.ingredient_id.asc())
                .all()
            ][:40]

            # --- batch board (last 6 sim-hours, capped at 15 rows) ---
            from . import kitchen as _kitchen
            raw_board = _kitchen.batch_board(session, now=float(self.bus.sim_time), window_sim_s=6*3600, limit=15)
            # Slim down batches for the prompt (omit sold_qty/wasted_qty/decided_at)
            slim_batches = [
                {k: b[k] for k in ("id","dish","decision","status","state","planned_qty","actual_made_qty","cook_by")}
                for b in raw_board["batches"]
            ]
            batch_board_ctx = {"clock": raw_board["clock"], "counts": raw_board["counts"], "recent_batches": slim_batches}

            # --- pending approvals ---
            approval_rows = (
                session.query(ApprovalRequest)
                .filter(ApprovalRequest.status == "pending")
                .order_by(ApprovalRequest.created_at.desc())
                .limit(15)
                .all()
            )
            pending_approvals = [
                {"id": ar.id, "type": ar.type, "title": ar.title, "ref_id": ar.ref_id, "urgency": ar.urgency}
                for ar in approval_rows
            ]

            # --- latest forecast per active item ---
            forecasts_raw = (
                session.query(Forecast)
                .order_by(Forecast.generated_at.desc())
                .limit(40)
                .all()
            )
            seen_items: set = set()
            forecast_ctx = []
            for f in forecasts_raw:
                if f.menu_item_id not in seen_items and len(forecast_ctx) < 20:
                    seen_items.add(f.menu_item_id)
                    # resolve name from menu_rows already fetched
                    dish_name = next((r.get("name") for r in menu_rows if r.get("id") == f.menu_item_id), f"Item #{f.menu_item_id}")
                    forecast_ctx.append({
                        "menu_item_id": f.menu_item_id,
                        "dish": dish_name,
                        "daypart": f.daypart,
                        "forecast_qty": f.forecast_qty,
                        "confidence": f.confidence,
                    })
        finally:
            session.close()

        active_constraints = []
        try:
            active_constraints = [
                {
                    "type": sig.type,
                    "source": sig.source,
                    "payload": sig.payload,
                    "expires_at": sig.expires_at,
                }
                for sig in self.bus.live(groups=["forecasting", "human"])[:20]
            ]
        except Exception:
            active_constraints = []

        payload = {
            "sim_time": float(self.bus.sim_time),
            "clock": raw_board["clock"],
            "menu": menu_rows[:80],
            "staff": staff_rows,
            "inventory": inventory_rows,
            "active_constraints": active_constraints,
            "batch_board": batch_board_ctx,
            "pending_approvals": pending_approvals,
            "forecasts": forecast_ctx,
            "module_capabilities": capability_prompt_context(),
            "guidance": {
                "specific_modifier_rule": "Ingredient/modifier words like bacon or mozzarella narrow the impact before category words like burger or pizza.",
                "category_rule": "Category words like dessert, desserts, pizza, pasta, or beverages cascade to every active menu item in that category.",
                "equipment_rule": "Equipment failures only affect items whose station, category, item name, or description indicates that equipment.",
                "hard_zero_rule": "Broken equipment, out-of-stock required ingredients, no-more, over, done, finished, and 86 instructions are hard production_unavailable constraints.",
                "inventory_units_rule": "Each inventory row has a 'name' and a 'unit' (g, ml, or each); 'on_hand' is the quantity in that unit. Always state the unit when reporting a quantity and never read 'on_hand' as a raw item count.",
                "status_vocabulary": (
                    "Batch state vocabulary (use EXACTLY these terms — do not invent others): "
                    "state=skipped: decision is 'skip', do not cook; "
                    "state=awaiting_approval: cook batch not yet cleared (status=decided); "
                    "state=ready_to_cook: cleared and should be cooked now (status=approved); "
                    "state=cooked: already prepared (status=ready, cooked_at set). "
                    "The word 'prepping' and 'served' are NOT used in this system."
                ),
            },
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    # -- read queries for the voice assistant -------------------------------
    #
    # These power the live voice assistant's read-only tools (get_inventory,
    # get_forecast, …).  Each returns plain JSON-able structures with names and
    # units so the model can answer questions accurately rather than guessing
    # from opaque ids.  They are deliberately small and self-contained (each
    # opens and closes its own session) so they can be dispatched from the
    # Gemini Live bridge in a worker thread without touching the event loop.

    def query_inventory(self, item_name: Optional[str] = None) -> Dict[str, Any]:
        """Current on-hand inventory with ingredient names and units.

        ``on_hand`` is in the ingredient's base unit (g | ml | each).  When
        ``item_name`` is given, rows whose name contains it (case-insensitive)
        are returned; if nothing matches, the full list is returned so the
        caller can reason about near-matches.
        """
        session = self.db_session_factory()
        try:
            ingredients = {int(ing.id): ing for ing in session.query(Ingredient).all()}
            all_rows: List[Dict[str, Any]] = []
            for level in (
                session.query(InventoryLevel)
                .order_by(InventoryLevel.ingredient_id.asc())
                .all()
            ):
                ing = ingredients.get(int(level.ingredient_id))
                all_rows.append({
                    "ingredient_id": int(level.ingredient_id),
                    "name": str(ing.name or "") if ing else str(level.ingredient_id),
                    "unit": (str(ing.base_unit or "") if ing and ing.base_unit else None),
                    "on_hand": float(level.on_hand_cached or 0.0),
                    "last_counted_qty": (
                        float(level.last_counted_qty)
                        if level.last_counted_qty is not None else None
                    ),
                    "par_level": float(level.par_level) if level.par_level is not None else None,
                    "reorder_point": (
                        float(level.reorder_point) if level.reorder_point is not None else None
                    ),
                })
        finally:
            session.close()

        rows = all_rows
        if item_name:
            needle = item_name.strip().lower()
            matched = [r for r in all_rows if needle in str(r["name"]).lower()]
            rows = matched if matched else all_rows
        return {"inventory": rows, "count": len(rows)}

    def query_forecast(self, item_name: Optional[str] = None) -> Dict[str, Any]:
        """Most recent demand forecasts, resolved to menu-item names."""
        session = self.db_session_factory()
        try:
            item_names = {
                int(mi.id): str(mi.name or "") for mi in session.query(MenuItem).all()
            }
            rows: List[Dict[str, Any]] = []
            for fc in session.query(Forecast).order_by(Forecast.generated_at.desc()).all():
                name = item_names.get(int(fc.menu_item_id), str(fc.menu_item_id))
                if item_name and item_name.strip().lower() not in name.lower():
                    continue
                rows.append({
                    "menu_item": name,
                    "daypart": fc.daypart,
                    "forecast_qty": int(fc.forecast_qty or 0),
                    "baseline_qty": (
                        float(fc.baseline_qty) if fc.baseline_qty is not None else None
                    ),
                    "confidence": float(fc.confidence) if fc.confidence is not None else None,
                    "window": fc.window,
                    "generated_at": (
                        float(fc.generated_at) if fc.generated_at is not None else None
                    ),
                })
                if len(rows) >= 40:
                    break
        finally:
            session.close()
        return {"sim_time": float(self.bus.sim_time), "forecasts": rows, "count": len(rows)}

    def query_competitors(self) -> Dict[str, Any]:
        """Competitor profiles plus recent market-intelligence observations."""
        session = self.db_session_factory()
        try:
            comp_objs = session.query(Competitor).order_by(Competitor.id.asc()).all()
            comp_names = {int(c.id): str(c.name or "") for c in comp_objs}
            competitors = [
                {
                    "name": c.name,
                    "platform": c.platform,
                    "rating": float(c.rating) if c.rating is not None else None,
                    "price_tier": c.price_tier,
                    "distance_km": float(c.distance_km) if c.distance_km is not None else None,
                    "is_open": bool(c.is_open),
                }
                for c in comp_objs
            ][:40]
            observations = [
                {
                    "competitor": (
                        comp_names.get(int(o.competitor_id))
                        if o.competitor_id is not None else None
                    ),
                    "signal_kind": o.signal_kind,
                    "direction": o.direction,
                    "impact_score": float(o.impact_score) if o.impact_score is not None else None,
                    "confidence": float(o.confidence) if o.confidence is not None else None,
                    "affected_menu_items": o.affected_menu_items,
                    "source_channel": o.source_channel,
                }
                for o in (
                    session.query(CompetitorObservation)
                    .order_by(CompetitorObservation.sim_time.desc())
                    .all()
                )
            ][:20]
        finally:
            session.close()
        return {"competitors": competitors, "observations": observations}

    def query_reviews(self) -> Dict[str, Any]:
        """Recent customer reviews and the insights derived from them."""
        session = self.db_session_factory()
        try:
            reviews = [
                {
                    "source": r.source,
                    "rating": float(r.rating) if r.rating is not None else None,
                    "text": r.text,
                    "sentiment": r.sentiment,
                    "dish_mentions": r.dish_mentions,
                }
                for r in session.query(Review).order_by(Review.sim_time.desc()).all()
            ][:20]
            insights = [
                {
                    "insight_type": i.insight_type,
                    "summary": i.summary,
                    "suggested_action": i.suggested_action,
                    "severity": i.severity,
                }
                for i in (
                    session.query(ReviewInsight)
                    .order_by(ReviewInsight.sim_time.desc())
                    .all()
                )
            ][:20]
        finally:
            session.close()
        return {"reviews": reviews, "insights": insights}

    def query_staff(self) -> Dict[str, Any]:
        """Staff roster with the stations each person can cover."""
        session = self.db_session_factory()
        try:
            station_names = {
                int(s.id): str(s.name or "") for s in session.query(Station).all()
            }
            cover_by_staff: Dict[int, List[str]] = {}
            for ss in session.query(StaffStation).all():
                cover_by_staff.setdefault(int(ss.staff_id), []).append(
                    station_names.get(int(ss.station_id), str(ss.station_id))
                )
            staff = [
                {
                    "name": s.name,
                    "role": s.role,
                    "skill_level": int(s.skill_level) if s.skill_level is not None else None,
                    "active": bool(s.active),
                    "stations": sorted(cover_by_staff.get(int(s.id), [])),
                }
                for s in session.query(Staff).order_by(Staff.id.asc()).all()
            ]
        finally:
            session.close()
        return {"staff": staff, "count": len(staff)}

    def query_signals(self) -> Dict[str, Any]:
        """All currently-live signals on the bus (constraints, forecasts, etc.)."""
        try:
            signals = [
                {
                    "type": sig.type,
                    "source": sig.source,
                    "priority": sig.priority,
                    "payload": sig.payload,
                    "expires_at": sig.expires_at,
                }
                for sig in self.bus.live()
            ][:40]
        except Exception:  # noqa: BLE001
            signals = []
        return {"sim_time": float(self.bus.sim_time), "signals": signals}

    def query_batches(self) -> Dict[str, Any]:
        """Upcoming / in-progress production batches, resolved to item names."""
        session = self.db_session_factory()
        try:
            item_names = {
                int(mi.id): str(mi.name or "") for mi in session.query(MenuItem).all()
            }
            rows: List[Dict[str, Any]] = []
            for b in (
                session.query(Batch)
                .filter(Batch.status.in_(["decided", "approved", "prepping", "ready"]))
                .order_by(Batch.decided_at.desc())
                .all()
            ):
                rows.append({
                    "menu_item": item_names.get(int(b.menu_item_id), str(b.menu_item_id)),
                    "decision": b.decision,
                    "status": b.status,
                    "planned_qty": int(b.planned_qty or 0),
                    "actual_made_qty": (
                        float(b.actual_made_qty) if b.actual_made_qty is not None else None
                    ),
                    "serve_window": b.serve_window,
                    "cooked_at": float(b.cooked_at) if b.cooked_at is not None else None,
                })
                if len(rows) >= 40:
                    break
        finally:
            session.close()
        return {"sim_time": float(self.bus.sim_time), "batches": rows, "count": len(rows)}
    def kitchen_status(self, *, dish: str | None = None, topic: str = "all") -> dict:
        """Live DB read for the get_kitchen_status tool.

        Returns a JSON-serialisable dict with a 'summary' key suitable for
        reading back to the staff member.
        """
        from . import kitchen as _kitchen
        session = self.db_session_factory()
        try:
            now = float(self.bus.sim_time)
            if dish:
                result = _kitchen.dish_status(session, dish, now=now)
                mi = result.get("menu_item") or {}
                ans = result.get("answer", {})
                name = mi.get("name", dish)
                mid = mi.get("id", "?")
                if not result["resolved"]:
                    summary = f"Could not find a dish matching '{dish}' in the menu."
                elif ans.get("prepared"):
                    made = ans.get("made_qty")
                    made_str = f"{made:.0f} made" if made is not None else "already made"
                    batches = result.get("batches", [])
                    planned = next((b["planned_qty"] for b in batches if b["state"]=="cooked"), None)
                    planned_str = f" (planned {planned:.0f})" if planned else ""
                    summary = f"Dish #{mid} ({name}): cooked — {made_str}{planned_str}."
                elif ans.get("should_cook"):
                    summary = f"Dish #{mid} ({name}): approved and should be cooked now."
                elif ans.get("awaiting_approval"):
                    summary = f"Dish #{mid} ({name}): batch is awaiting manager approval before cooking."
                else:
                    batches = result.get("batches", [])
                    if any(b["decision"] == "skip" for b in batches):
                        summary = f"Dish #{mid} ({name}): batch was decided to be skipped."
                    else:
                        summary = f"Dish #{mid} ({name}): no batch found in the current window."
                result["summary"] = summary
                return result
            else:
                board = _kitchen.batch_board(session, now=now, window_sim_s=6*3600, limit=40)
                c = board["counts"]
                parts = []
                if c["cooked"]: parts.append(f"{c['cooked']} cooked")
                if c["approved"]: parts.append(f"{c['approved']} ready to cook")
                if c["pending"]: parts.append(f"{c['pending']} awaiting approval")
                if c["skipped"]: parts.append(f"{c['skipped']} skipped")
                if topic in ("approvals", "all"):
                    approvals = session.query(ApprovalRequest).filter(ApprovalRequest.status == "pending").order_by(ApprovalRequest.created_at.desc()).limit(15).all()
                    board["pending_approvals"] = [
                        {"id": ar.id, "type": ar.type, "title": ar.title, "urgency": ar.urgency}
                        for ar in approvals
                    ]
                board["summary"] = ", ".join(parts) + "." if parts else "No batches in the current window."
                return board
        finally:
            session.close()

    def _enrich_operational_constraint(
        self,
        extracted: Dict[str, Any],
        raw_text: str,
    ) -> Dict[str, Any]:
        value = extracted.get("value")
        if not isinstance(value, dict):
            value = {"raw_value": value}
        else:
            value = dict(value)

        resolution = self._resolve_constraint_impact(extracted, raw_text)
        if resolution:
            value.update(resolution)
            if resolution.get("dependency_ref") and resolution.get("dependency_type") != "category":
                extracted["entity_ref"] = resolution["dependency_ref"]
            if resolution.get("dependency_type") and not extracted.get("entity_type"):
                extracted["entity_type"] = str(resolution["dependency_type"])

        if not value.get("raw_text"):
            value["raw_text"] = raw_text
        extracted["value"] = value
        return extracted

    def _resolve_constraint_impact(
        self,
        extracted: Dict[str, Any],
        raw_text: str,
    ) -> Dict[str, Any]:
        text = " ".join(
            str(part or "")
            for part in (
                raw_text,
                extracted.get("entity_ref"),
                extracted.get("attribute"),
                extracted.get("value"),
            )
        ).lower()
        tokens = self._target_tokens(text)
        if not tokens:
            return {}

        session = self.db_session_factory()
        try:
            menu_rows = self._menu_dependency_rows(session)
        finally:
            session.close()

        # Specific dependencies should win before broad labels. That keeps
        # "bacon burgers" scoped to bacon items and lets exact item names beat
        # generic equipment/category matches.
        ingredient_names = sorted(
            {
                str(ingredient).lower()
                for row in menu_rows
                for ingredient in row.get("ingredients", [])
                if ingredient
            },
            key=len,
            reverse=True,
        )
        matched_ingredient = self._best_dependency_match(text, ingredient_names, tokens)
        if matched_ingredient:
            affected = [
                row for row in menu_rows
                if self._dependency_name_matches(matched_ingredient, row.get("ingredients", []))
            ]
            return self._impact_payload("ingredient", matched_ingredient, affected)

        equipment_names = sorted(
            {
                str(equipment).lower()
                for row in menu_rows
                for equipment in row.get("equipment", [])
                if equipment
            },
            key=len,
            reverse=True,
        )
        item_match = self._best_item_match(text, menu_rows)
        if item_match is not None:
            return self._impact_payload("menu_item", item_match["name"], [item_match])

        matched_equipment = self._best_dependency_match(text, equipment_names, tokens)
        if matched_equipment:
            affected = [
                row for row in menu_rows
                if self._dependency_name_matches(matched_equipment, row.get("equipment", []))
            ]
            return self._impact_payload("equipment", matched_equipment, affected)

        categories = sorted(
            {str(row.get("category") or "").lower() for row in menu_rows if row.get("category")},
            key=len,
            reverse=True,
        )
        matched_category = self._best_dependency_match(text, categories, tokens)
        if matched_category:
            affected = [
                row for row in menu_rows
                if self._singular(str(row.get("category") or "")) == self._singular(matched_category)
            ]
            return self._impact_payload("category", matched_category, affected)

        return {}

    @staticmethod
    def _impact_payload(
        dependency_type: str,
        dependency_ref: str,
        affected: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not affected:
            return {}
        return {
            "dependency_type": dependency_type,
            "dependency_ref": dependency_ref,
            "affected_menu_item_ids": [int(row["id"]) for row in affected],
            "affected_item_names": [str(row["name"]) for row in affected],
            "reasoning": (
                f"Matched {dependency_type} '{dependency_ref}' against menu and recipe context."
            ),
        }

    def _menu_dependency_rows(self, session: Any) -> List[Dict[str, Any]]:
        ingredients_by_recipe: Dict[int, List[str]] = {}
        recipe_ids_by_item: Dict[int, List[int]] = {}
        ingredient_names = {
            int(ingredient.id): str(ingredient.name or "")
            for ingredient in session.query(Ingredient).all()
        }
        for recipe in session.query(Recipe).all():
            recipe_ids_by_item.setdefault(int(recipe.menu_item_id), []).append(int(recipe.id))
        for line in session.query(RecipeLine).all():
            ingredients_by_recipe.setdefault(int(line.recipe_id), []).append(
                ingredient_names.get(int(line.ingredient_id), str(line.ingredient_id))
            )
        station_names = {
            int(station.id): str(station.name or "")
            for station in session.query(Station).all()
        }
        rows = []
        for item in session.query(MenuItem).filter(MenuItem.active == 1).order_by(MenuItem.id.asc()).all():
            recipe_ids = recipe_ids_by_item.get(int(item.id), [])
            ingredients = [
                name
                for recipe_id in recipe_ids
                for name in ingredients_by_recipe.get(recipe_id, [])
                if name
            ]
            station = station_names.get(int(item.station_id or 0), "")
            rows.append(
                {
                    "id": int(item.id),
                    "name": item.name or "",
                    "category": item.category or "",
                    "station": station,
                    "ingredients": sorted(set(ingredients)),
                    "equipment": self._equipment_dependencies_for_item(item, station),
                }
            )
        return rows

    @staticmethod
    def _equipment_dependencies_for_item(item: MenuItem, station_name: str) -> List[str]:
        haystack = " ".join(
            str(value or "")
            for value in (item.name, item.category, item.description, station_name)
        ).lower()
        equipment: Set[str] = set()
        rules = {
            "pizza oven": ("pizza oven", "pizza"),
            "oven": ("oven", "baked", "bake"),
            "grill": ("grill", "burger", "steak", "kebab"),
            "fryer": ("fryer", "fried", "fries", "chips"),
            "pasta station": ("pasta",),
            "cold station": ("salad", "cold"),
            "bar": ("bar", "beverage", "drink", "coffee"),
        }
        for label, terms in rules.items():
            if any(term in haystack for term in terms):
                equipment.add(label)
        if station_name:
            equipment.add(station_name.lower())
        return sorted(equipment)

    @classmethod
    def _best_dependency_match(
        cls,
        text: str,
        candidates: List[str],
        tokens: List[str],
    ) -> Optional[str]:
        token_set = {cls._singular(token) for token in tokens if len(token) > 2}
        best: tuple[int, int, str] | None = None
        for candidate in candidates:
            candidate_low = candidate.lower().strip()
            if not candidate_low:
                continue
            candidate_tokens = {
                cls._singular(token)
                for token in re.findall(r"[a-z0-9]+", candidate_low)
                if len(token) > 2
            }
            if candidate_low in text:
                score = 100 + len(candidate_tokens)
            else:
                overlap = len(candidate_tokens.intersection(token_set))
                if overlap <= 0:
                    continue
                score = overlap
            tie_breaker = -len(candidate_low)
            if best is None or (score, tie_breaker) > (best[0], best[1]):
                best = (score, tie_breaker, candidate)
        return best[2] if best is not None else None

    @classmethod
    def _dependency_name_matches(cls, dependency: str, candidates: List[Any]) -> bool:
        target = cls._singular(str(dependency or ""))
        target_tokens = {
            cls._singular(token)
            for token in re.findall(r"[a-z0-9]+", target)
            if len(token) > 2
        }
        for candidate in candidates:
            candidate_low = str(candidate or "").lower()
            candidate_tokens = {
                cls._singular(token)
                for token in re.findall(r"[a-z0-9]+", candidate_low)
                if len(token) > 2
            }
            if target == cls._singular(candidate_low):
                return True
            if len(target_tokens) > 1 and target_tokens.issubset(candidate_tokens):
                return True
            if len(target_tokens) == 1 and target_tokens.intersection(candidate_tokens):
                return True
        return False

    @staticmethod
    def _best_item_match(text: str, menu_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for row in sorted(menu_rows, key=lambda entry: len(str(entry.get("name") or "")), reverse=True):
            name = str(row.get("name") or "").lower()
            if name and name in text:
                return row
        return None

    @staticmethod
    def _is_unavailable_constraint_shape(extracted: Dict[str, Any]) -> bool:
        if extracted.get("intent") != "set_operational_constraint":
            return False
        attribute = str(extracted.get("attribute") or "").lower()
        value = extracted.get("value")
        value_text = str(value).strip().lower()
        return attribute in {"availability", "available", "production_available"} and value_text in {
            "false",
            "0",
            "no",
            "none",
            "unavailable",
            "not available",
        }

    # -- regex fallback (§11 — best-effort for obvious intents) ------------

    def _regex_extract(self, raw_text: str) -> Dict[str, Any]:
        text = raw_text.strip()
        low = text.lower()

        # set_leave: "<Name> is on leave/sick ..." / "... day off ..."
        if (
            "leave" in low
            or "is off" in low
            or "day off" in low
            or "sick" in low
            or "off sick" in low
            or "absent" in low
            or "unavailable" in low
            or "missing" in low
        ):
            name = self._first_name(text)
            window = self._window_from_text(low)
            status = "sick" if "sick" in low else "leave"
            if self._looks_like_station_absence(low, name):
                return {
                    "intent": "set_operational_constraint",
                    "entity_type": "station_or_skill",
                    "entity_ref": self._constraint_target(text, low),
                    "attribute": "capacity_absence",
                    "value": {
                        "raw_text": text,
                        "all_qualified_staff": self._all_qualified_staff_absent(low),
                    },
                    "effective_window": window,
                    "confidence": 0.65,
                }
            return {
                "intent": "set_leave",
                "entity_type": "staff",
                "entity_ref": name,
                "attribute": status,
                "value": status,
                "effective_window": window,
                "confidence": 0.7,
            }

        # record_receipt: "received 20 kg of tomatoes from GreenFarm at 2 ..."
        if ("receiv" in low or "got" in low or "delivery" in low) and " of " in low:
            receipt = self._parse_receipt(text, low)
            if receipt is not None:
                return receipt

        # add_event: "there's a parade ... Monday"
        if any(w in low for w in ("parade", "festival", "event", "concert", "match", "holiday")):
            window = self._window_from_text(low)
            attendance = self._attendance_from_text(low)
            return {
                "intent": "add_event",
                "entity_type": "event",
                "entity_ref": self._event_label(low),
                "attribute": "expected_attendance" if attendance is not None else "demand_multiplier",
                "value": attendance if attendance is not None else EVENT_MULT,
                "effective_window": window,
                "confidence": 0.6,
            }

        contextual_constraint = self._contextual_unavailable_constraint(text, low)
        if contextual_constraint is not None:
            return self._normalise(contextual_constraint, raw_text)

        # set_operational_constraint: "no desserts possible" / "we cannot make tiramisu"
        if self._looks_like_unavailable_menu_constraint(low):
            target = self._unavailable_target(text, low)
            return self._normalise({
                "intent": "set_operational_constraint",
                "entity_type": "menu_item_or_category",
                "entity_ref": target,
                "attribute": "production_unavailable",
                "value": {
                    "action": "halt_production",
                    "target_qty": 0,
                    "raw_text": text,
                },
                "effective_window": self._window_from_text(low) or self._default_constraint_window(),
                "confidence": 0.7,
            }, raw_text)

        # set_operational_constraint: "desserts are overstocked" / "too much tiramisu"
        if self._looks_like_overstock_constraint(low):
            target = self._overstock_target(text, low)
            return self._normalise({
                "intent": "set_operational_constraint",
                "entity_type": "menu_item_or_category",
                "entity_ref": target,
                "attribute": "overstock",
                "value": {
                    "action": "reduce_forecast",
                    "target_qty": 0,
                    "raw_text": text,
                },
                "effective_window": self._window_from_text(low) or self._default_constraint_window(),
                "confidence": 0.65,
            }, raw_text)

        # add_inventory_count: "we have 12 kg of flour left" / "count ..."
        if "count" in low or ("we have" in low and " of " in low) or "stock of" in low:
            count = self._parse_count(text, low)
            if count is not None:
                return count

        # add_menu_item: "add a Margherita pizza for 12 dollars"
        if low.startswith("add ") and ("menu" in low or "for" in low or "pizza" in low or "$" in low or "dollar" in low):
            item = self._parse_menu_item(text, low)
            if item is not None:
                return item

        return {
            "intent": "other",
            "entity_type": "",
            "entity_ref": None,
            "attribute": "",
            "value": text,
            "effective_window": None,
            "confidence": 0.2,
        }

    # -- (2) route to typed signals ------------------------------------------

    def _emit_routes(self, extracted: Dict[str, Any], raw_text: str) -> List[Dict[str, Any]]:
        routes: List[Dict[str, Any]] = []
        for spec in self._route_specs(extracted, raw_text):
            route_id = str(uuid.uuid4())
            signal_type = spec["signal_type"]
            payload = spec["payload"]
            route: Dict[str, Any] = {
                "route_id": route_id,
                "signal_type": signal_type.value,
                "target_modules": spec["target_modules"],
                "payload": payload,
                "confidence": float(extracted.get("confidence") or payload.get("confidence") or 0.0),
                "status": "pending",
                "signal_id": None,
            }
            try:
                signal = self.bus.emit(
                    signal_type,
                    payload,
                    source="voice",
                    ttl=self._ttl_for_payload(payload),
                    dedup_key=spec.get("dedup_key"),
                )
            except Exception as exc:  # noqa: BLE001 - bad voice parse must not crash.
                route["status"] = "failed"
                route["error"] = f"{type(exc).__name__}: {exc}"
            else:
                route["status"] = "emitted" if signal is not None else "dropped"
                route["signal_id"] = signal.signal_id if signal is not None else None
            routes.append(route)
        return routes

    def _route_specs(self, extracted: Dict[str, Any], raw_text: str) -> List[Dict[str, Any]]:
        intent = str(extracted.get("intent") or "other")
        entity_ref = extracted.get("entity_ref")
        entity_ref_s = str(entity_ref or "").strip()
        value = extracted.get("value")
        value_dict = value if isinstance(value, dict) else {}
        window = extracted.get("effective_window")
        confidence = float(extracted.get("confidence") or 0.0)

        if intent == "record_receipt":
            ingredient_id = self._resolve_ingredient_id(entity_ref_s, value_dict.get("unit") or "each")
            supplier_name = value_dict.get("supplier")
            supplier_id = self._resolve_supplier_id(str(supplier_name)) if supplier_name else None
            payload = {
                "ingredient_id": ingredient_id,
                "ingredient_ref": entity_ref_s,
                "qty": float(value_dict.get("qty") or 0.0),
                "unit": str(value_dict.get("unit") or "each"),
                "supplier_id": supplier_id,
                "supplier_ref": supplier_name,
                "price": value_dict.get("price"),
                "raw_text": raw_text,
                "confidence": confidence,
            }
            return [{
                "signal_type": SignalType.INVENTORY_RECEIPT_REPORTED,
                "target_modules": ["track_b.ledger"],
                "payload": payload,
            }]

        if intent == "add_inventory_count":
            ingredient_id = self._resolve_ingredient_id(entity_ref_s, value_dict.get("unit") or "each")
            payload = {
                "ingredient_id": ingredient_id,
                "ingredient_ref": entity_ref_s,
                "qty": float(value_dict.get("qty") or 0.0),
                "unit": str(value_dict.get("unit") or "each"),
                "raw_text": raw_text,
                "confidence": confidence,
            }
            return [{
                "signal_type": SignalType.INVENTORY_COUNT_REPORTED,
                "target_modules": ["track_b.ledger"],
                "payload": payload,
            }]

        if intent in {"set_leave", "set_attendance"}:
            status = self._leave_status(extracted)
            payload = {
                "staff_id": self._resolve_staff_id(entity_ref_s),
                "staff_name": entity_ref_s or None,
                "station_id": None,
                "station_ref": None,
                "status": status,
                "window": window,
                "reason": str(extracted.get("attribute") or status),
                "raw_text": raw_text,
                "confidence": confidence,
            }
            return [{
                "signal_type": SignalType.STAFF_AVAILABILITY,
                "target_modules": ["track_a.staff"],
                "payload": payload,
            }]

        if intent == "add_event":
            payload = {
                "event_ref": entity_ref_s or "event",
                "event_kind": str(extracted.get("attribute") or "event"),
                "expected_attendance": (
                    float(value)
                    if extracted.get("attribute") == "expected_attendance"
                    and isinstance(value, (int, float))
                    else None
                ),
                "demand_multiplier": (
                    float(value)
                    if extracted.get("attribute") != "expected_attendance"
                    and isinstance(value, (int, float))
                    else None
                ),
                "affected_menu_item_ids": [],
                "affected_categories": [],
                "window": window,
                "raw_text": raw_text,
                "confidence": confidence,
            }
            return [{
                "signal_type": SignalType.DEMAND_EVENT,
                "target_modules": ["track_a.forecaster"],
                "payload": payload,
            }]

        if intent == "set_operational_constraint":
            affected = value_dict.get("affected_menu_item_ids") or []
            categories = []
            dep_type = str(value_dict.get("dependency_type") or extracted.get("entity_type") or "constraint")
            if dep_type == "category" and value_dict.get("dependency_ref"):
                categories = [str(value_dict["dependency_ref"])]
            action = str(value_dict.get("action") or "block")
            payload = {
                "constraint_ref": str(value_dict.get("dependency_ref") or entity_ref_s or "constraint"),
                "constraint_type": dep_type,
                "action": "reduce" if action == "reduce_forecast" else "block",
                "affected_menu_item_ids": [int(i) for i in affected],
                "affected_categories": categories,
                "window": window,
                "reason": raw_text,
                "raw_text": raw_text,
                "confidence": confidence,
            }
            return [{
                "signal_type": SignalType.PRODUCTION_CONSTRAINT,
                "target_modules": ["track_a.forecaster"],
                "payload": payload,
            }]

        if intent == "set_supplier_price":
            payload = {
                "supplier_id": self._resolve_supplier_id(entity_ref_s),
                "supplier_ref": entity_ref_s or "supplier",
                "ingredient_id": self._resolve_ingredient_id(str(value_dict.get("ingredient") or ""), "each"),
                "ingredient_ref": value_dict.get("ingredient"),
                "availability": value_dict.get("availability"),
                "price": value_dict.get("price"),
                "lead_time_days": value_dict.get("lead_time_days"),
                "raw_text": raw_text,
                "confidence": confidence,
            }
            return [{
                "signal_type": SignalType.SUPPLIER_CATALOG_NOTE,
                "target_modules": ["track_b.market_spectator"],
                "payload": payload,
            }]

        if intent == "add_review":
            return [{
                "signal_type": SignalType.CUSTOMER_FEEDBACK_NOTE,
                "target_modules": ["track_a.review"],
                "payload": {
                    "summary": str(value if value is not None else raw_text),
                    "dish_mentions": [entity_ref_s] if entity_ref_s else [],
                    "sentiment": None,
                    "severity": None,
                    "raw_text": raw_text,
                    "confidence": confidence,
                },
            }]

        if intent == "set_competitor":
            return [{
                "signal_type": SignalType.COMPETITOR_NOTE,
                "target_modules": ["track_a.competitor"],
                "payload": {
                    "summary": str(value if value is not None else raw_text),
                    "competitor_ref": entity_ref_s or None,
                    "affected_menu_item_ids": [],
                    "affected_categories": [],
                    "raw_text": raw_text,
                    "confidence": confidence,
                },
            }]

        return self._qualitative_inventory_routes(extracted, raw_text)

    def _qualitative_inventory_routes(
        self, extracted: Dict[str, Any], raw_text: str
    ) -> List[Dict[str, Any]]:
        low = raw_text.lower()
        if any(phrase in low for phrase in ("almost out", "nearly out", "running out", "low on")):
            ref = self._ingredient_phrase_from_text(raw_text)
            return [{
                "signal_type": SignalType.INGREDIENT_SHORTAGE_REPORTED,
                "target_modules": ["track_b.ledger", "track_b.optimizer"],
                "payload": {
                    "ingredient_id": self._resolve_ingredient_id(ref, "each"),
                    "ingredient_ref": ref or str(extracted.get("entity_ref") or "ingredient"),
                    "severity": "critical" if "out" in low else "low",
                    "qty": None,
                    "unit": None,
                    "raw_text": raw_text,
                    "confidence": max(float(extracted.get("confidence") or 0.0), 0.55),
                },
            }]
        if "expire" in low or "expires" in low or "expiring" in low:
            ref = self._ingredient_phrase_from_text(raw_text)
            return [{
                "signal_type": SignalType.EXPIRY_USE_PRIORITY,
                "target_modules": ["track_b.optimizer", "track_a.forecaster"],
                "payload": {
                    "ingredient_id": self._resolve_ingredient_id(ref, "each"),
                    "ingredient_ref": ref or str(extracted.get("entity_ref") or "ingredient"),
                    "lot_id": None,
                    "expiry": None,
                    "qty": None,
                    "desired_action": "use_up",
                    "raw_text": raw_text,
                    "confidence": max(float(extracted.get("confidence") or 0.0), 0.55),
                },
            }]
        return []

    def _ttl_for_payload(self, payload: Dict[str, Any]) -> Optional[float]:
        window = payload.get("window")
        if isinstance(window, dict) and window.get("end") is not None:
            try:
                return max(float(window["end"]) - float(self.bus.sim_time), 1.0)
            except (TypeError, ValueError):
                return None
        return None

    def _resolve_ingredient_id(self, ref: str, unit: str) -> Optional[int]:
        if not ref:
            return None
        session = self.db_session_factory()
        try:
            ingredient = self._resolve_ingredient(session, ref, unit, create=False)
            return int(ingredient.id) if ingredient is not None else None
        finally:
            session.close()

    def _resolve_supplier_id(self, ref: str) -> Optional[int]:
        if not ref:
            return None
        session = self.db_session_factory()
        try:
            supplier = self._resolve_supplier(session, ref, create=False)
            return int(supplier.id) if supplier is not None else None
        finally:
            session.close()

    def _resolve_staff_id(self, ref: str) -> Optional[int]:
        if not ref:
            return None
        session = self.db_session_factory()
        try:
            row = session.query(Staff).filter(Staff.name.ilike(ref)).first()
            if row is None:
                row = session.query(Staff).filter(Staff.name.ilike(f"{ref}%")).first()
            return int(row.id) if row is not None else None
        finally:
            session.close()

    def _ingredient_phrase_from_text(self, text: str) -> str:
        low = text.lower()
        for phrase in ("almost out of", "nearly out of", "running out of", "low on"):
            if phrase in low:
                tail = text[low.index(phrase) + len(phrase):].strip(" .,!;:")
                return tail.split(" and ")[0].strip()
        match = re.search(r"([A-Za-z][A-Za-z ]+?)\s+expir", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return str(self._constraint_target(text, low) or "").strip()

    @staticmethod
    def _looks_like_station_absence(low: str, name: Optional[str]) -> bool:
        absence = any(w in low for w in ("absent", "unavailable", "missing", "off sick", "sick"))
        operational = any(
            w in low
            for w in (
                "station", "counter", "line", "cook", "chef", "worker",
                "staff", "making", "make", "prep",
            )
        )
        # If a real named staff member was found, the structured attendance
        # path is more precise. Avoid treating sentence-openers ("The") as a
        # staff name.
        has_staff_name = bool(name and name.lower() not in {"the", "all", "every", "no"})
        return absence and operational and not has_staff_name

    @staticmethod
    def _all_qualified_staff_absent(low: str) -> bool:
        return any(
            phrase in low
            for phrase in (
                "all ", "every ", "no one", "nobody", "none of",
                "all the possible", "everyone",
            )
        )

    @staticmethod
    def _constraint_target(text: str, low: str) -> str:
        patterns = [
            r"(?:the\s+)?([A-Za-z0-9 '&-]+?)\s+station",
            r"(?:making|make|prep|prepping)\s+([A-Za-z0-9 '&-]+?)(?:\s+are|\s+is|\s+was|\s+were|\s+absent|$)",
            r"([A-Za-z0-9 '&-]+?)\s+(?:cook|chef|worker|staff)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                continue
            target = m.group(1).strip(" .,'\"")
            if target and target.lower() not in {"the", "all", "possible", "station"}:
                return target
        return low

    @staticmethod
    def _looks_like_unavailable_menu_constraint(low: str) -> bool:
        if VoiceProcessor._looks_like_overstock_constraint(low):
            return False
        unavailable = (
            "no more", "not possible", "impossible", "can't make", "cannot make",
            "cant make", "unable to make", "not available", "unavailable",
            "stop making", "halt", "pause", "out of", "ran out",
            "sold out", "over for today", "over tonight", "are over", "is over",
            "done for today", "done tonight", "are done", "is done",
            "finished for today", "finished tonight", "are finished", "is finished",
            "86", "eighty six",
        )
        food_context = (
            "dessert", "desert", "pizza", "pasta", "salad", "beverage",
            "dish", "item", "menu", "tiramisu", "burger",
        )
        no_possible = re.search(r"\bno\s+(?:more\s+)?[a-z0-9 '&-]+\s+possible\b", low) is not None
        return (no_possible or any(word in low for word in unavailable)) and any(
            word in low for word in food_context
        )

    @staticmethod
    def _unavailable_target(text: str, low: str) -> str:
        patterns = [
            r"no\s+(?:more\s+)?([A-Za-z0-9 '&-]+?)(?:\s+(?:possible|available|today|tonight|now|$)|$)",
            r"([A-Za-z0-9 '&-]+?)\s+(?:is|are|was|were)?\s*(?:not possible|impossible|unavailable|not available)",
            r"([A-Za-z0-9 '&-]+?)\s+(?:is|are|was|were)\s+(?:over|done|finished)(?:\s+for\s+(?:today|tonight))?",
            r"(?:can't|cannot|cant|unable to|stop|halt|pause)\s+(?:make|making|serve|serving|prep|prepping)?\s*([A-Za-z0-9 '&-]+)",
            r"(?:86|eighty\s+six)\s+([A-Za-z0-9 '&-]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                continue
            target = m.group(1).strip(" .,'\"")
            if target and target.lower() not in {"we", "have", "the", "our", "make", "making"}:
                return target
        for category in ("desserts", "deserts", "dessert", "desert", "pizza", "pasta", "salad", "beverage", "beverages"):
            if category in low:
                return category
        return low

    def _contextual_unavailable_constraint(self, text: str, low: str) -> Optional[Dict[str, Any]]:
        if self._looks_like_overstock_constraint(low):
            return None
        hard_zero_phrases = (
            "no more", "can't make", "cannot make", "cant make", "unable to make",
            "stop making", "stop serving", "not available", "unavailable",
            "sold out", "out of", "ran out", "broken", "not working", "down",
            "over for today", "over tonight", "are over", "is over",
            "done for today", "done tonight", "are done", "is done",
            "finished for today", "finished tonight", "are finished", "is finished",
            "86", "eighty six",
        )
        if not any(phrase in low for phrase in hard_zero_phrases):
            return None
        entity_type = "operational_dependency" if any(
            phrase in low for phrase in ("broken", "not working", "down")
        ) else "menu_item_or_dependency"
        target = self._unavailable_target(text, low)
        if target == low:
            target = self._target_phrase_for_constraint(text, low)
        return {
            "intent": "set_operational_constraint",
            "entity_type": entity_type,
            "entity_ref": target,
            "attribute": "production_unavailable",
            "value": {
                "action": "halt_production",
                "target_qty": 0,
                "raw_text": text,
            },
            "effective_window": self._window_from_text(low) or self._default_constraint_window(),
            "confidence": 0.72,
        }

    @staticmethod
    def _target_phrase_for_constraint(text: str, low: str) -> str:
        patterns = [
            r"(?:the\s+)?([A-Za-z0-9 '&-]+?)\s+(?:is|are|was|were)\s+(?:broken|down|not working)",
            r"(?:out of|ran out of)\s+([A-Za-z0-9 '&-]+?)(?:\s+(?:today|tonight|now|$)|$)",
            r"(?:no\s+more|sold out of)\s+([A-Za-z0-9 '&-]+?)(?:\s+(?:today|tonight|now|$)|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                target = match.group(1).strip(" .,'\"")
                if target:
                    return target
        return low

    @staticmethod
    def _target_tokens(phrase: str) -> List[str]:
        words = re.findall(r"[a-z0-9]+", phrase.lower())
        ignored = {
            "the", "all", "possible", "staff", "worker", "workers", "cook",
            "chef", "station", "making", "make", "are", "is", "was", "were",
            "absent", "unavailable", "available", "missing", "sick", "off",
            "for", "today", "tonight", "now", "more", "none", "cannot", "cant",
            "can't", "stop", "serve", "serving", "production", "halt", "out",
            "ran", "broken", "working", "down", "only", "one",
        }
        return [word for word in words if word not in ignored and len(word) > 2]

    @staticmethod
    def _looks_like_overstock_constraint(low: str) -> bool:
        overstock_words = (
            "overstock", "over-stock", "over stocked", "overstocked",
            "too much", "too many", "excess", "surplus", "overproduced",
            "over-produced",
        )
        production_words = ("forecast", "prep", "prepare", "make", "produce", "stock", "inventory")
        return any(word in low for word in overstock_words) and any(
            word in low for word in production_words
        )

    @staticmethod
    def _overstock_target(text: str, low: str) -> str:
        patterns = [
            r"(?:too much|too many|excess|surplus)\s+([A-Za-z0-9 '&-]+?)(?:\s+(?:stock|inventory|prep|prepared|today|for|$)|$)",
            r"([A-Za-z0-9 '&-]+?)\s+(?:is|are|was|were)\s+(?:overstocked|over-stocked|over stocked|overproduced|over-produced)",
            r"(?:overstocked|over-stocked|over stocked|overproduced|over-produced)\s+([A-Za-z0-9 '&-]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                continue
            target = m.group(1).strip(" .,'\"")
            if target and target.lower() not in {"we", "have", "the", "our"}:
                return target
        for category in ("desserts", "dessert", "pizza", "pasta", "salad", "beverage", "beverages"):
            if category in low:
                return category
        return low

    # -- regex helpers ------------------------------------------------------

    @staticmethod
    def _first_name(text: str) -> Optional[str]:
        m = re.match(r"\s*([A-Z][a-zA-Z]+)", text)
        if m:
            return m.group(1)
        m = re.search(r"([A-Z][a-zA-Z]+)\s+is\b", text)
        return m.group(1) if m else None

    @staticmethod
    def _event_label(low: str) -> str:
        for w in ("parade", "festival", "concert", "match", "holiday", "event"):
            if w in low:
                return w
        return "event"

    @staticmethod
    def _attendance_from_text(low: str) -> Optional[float]:
        match = re.search(
            _NUMBER_RE + r"\s*(?:people|person|guests?|attendees?|pax|crowd)",
            low,
            re.IGNORECASE,
        )
        if not match:
            return None
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            return None

    def _window_from_text(self, low: str) -> Optional[Dict[str, float]]:
        """Map common phrases ("next week", "this Monday", "today") to a
        sim-second window using the current sim day geometry (§6.1)."""
        now = float(self.bus.sim_time)
        day_number = int(now // SECONDS_PER_DAY)
        dow = day_number % 7  # 0 = Monday

        def day_start(d: int) -> float:
            return d * SECONDS_PER_DAY + DAY_OPEN_OFFSET

        def day_end(d: int) -> float:
            return d * SECONDS_PER_DAY + DAY_CLOSE_OFFSET

        def with_time_range(window: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
            time_range = self._time_range_from_text(low)
            if time_range is None:
                return window
            base_day = int(((window or {}).get("start", day_start(day_number))) // SECONDS_PER_DAY)
            start_s, end_s = time_range
            start = base_day * SECONDS_PER_DAY + start_s
            end = base_day * SECONDS_PER_DAY + end_s
            if end <= start:
                end += SECONDS_PER_DAY
            return {"start": float(start), "end": float(end)}

        if "next week" in low:
            days_to_mon = (7 - dow) if dow != 0 else 7
            mon = day_number + days_to_mon
            return {"start": day_start(mon), "end": day_end(mon + 6)}

        if "this week" in low:
            mon = day_number - dow
            return {"start": day_start(mon), "end": day_end(mon + 6)}

        # A named weekday ("this Monday", "on Friday").
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday"]
        for idx, name in enumerate(weekdays):
            if name in low:
                delta = (idx - dow) % 7
                # "next <weekday>" pushes a week out; a bare/"this" weekday that
                # already passed this week also rolls to the next occurrence.
                if "next" in low or delta == 0:
                    delta = delta or 7
                target = day_number + delta
                return with_time_range({"start": day_start(target), "end": day_end(target)})

        if "tomorrow" in low:
            return with_time_range({"start": day_start(day_number + 1), "end": day_end(day_number + 1)})
        if "today" in low:
            return with_time_range({"start": day_start(day_number), "end": day_end(day_number)})

        time_window = with_time_range(None)
        if time_window is not None:
            return time_window

        return None

    def _default_constraint_window(self) -> Dict[str, float]:
        now = float(self.bus.sim_time)
        day_number = int(now // SECONDS_PER_DAY)
        end = float(day_number * SECONDS_PER_DAY + DAY_CLOSE_OFFSET)
        if end <= now:
            end = float((day_number + 1) * SECONDS_PER_DAY + DAY_CLOSE_OFFSET)
        return {"start": now, "end": end}

    @staticmethod
    def _time_range_from_text(low: str) -> Optional[tuple[float, float]]:
        time_pattern = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
        range_match = re.search(
            r"(?:from|between)\s+" + time_pattern + r"\s*(?:to|and|-)\s*" + time_pattern,
            low,
            re.IGNORECASE,
        )
        if range_match:
            start = VoiceProcessor._parse_time_match(range_match, 1)
            end = VoiceProcessor._parse_time_match(range_match, 4)
            if start is not None and end is not None:
                return start, end

        start_match = re.search(
            r"(?:from|at|around)\s+" + time_pattern,
            low,
            re.IGNORECASE,
        )
        if start_match:
            start = VoiceProcessor._parse_time_match(start_match, 1)
            if start is not None:
                return start, float(DAY_CLOSE_OFFSET)
        return None

    @staticmethod
    def _parse_time_match(match: re.Match[str], offset: int) -> Optional[float]:
        try:
            hour = int(match.group(offset))
            minute = int(match.group(offset + 1) or 0)
        except (TypeError, ValueError):
            return None
        suffix = (match.group(offset + 2) or "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        if not suffix and hour < 8:
            hour += 12
        if hour > 23 or minute > 59:
            return None
        return float(hour * 3600 + minute * 60)

    def _parse_receipt(self, text: str, low: str) -> Optional[Dict[str, Any]]:
        m = re.search(
            _NUMBER_RE + r"\s*([a-zA-Z]+)?\s+of\s+([a-zA-Z ]+?)\s+from\s+([A-Za-z0-9 &'.-]+?)"
            r"(?:\s+(?:at|for)\s+\$?" + _NUMBER_RE + r")?(?:\s|$|\.)",
            text,
            re.IGNORECASE,
        )
        if not m:
            return None
        qty = float(m.group(1))
        unit = _UNIT_WORDS.get((m.group(2) or "").lower(), (m.group(2) or "each").lower())
        ingredient = m.group(3).strip()
        supplier = m.group(4).strip()
        price = float(m.group(5)) if m.group(5) else None
        # Trim trailing connective words that the loose supplier capture grabbed.
        supplier = re.sub(r"\s+(at|for)$", "", supplier, flags=re.IGNORECASE).strip()
        return {
            "intent": "record_receipt",
            "entity_type": "ingredient",
            "entity_ref": ingredient,
            "attribute": "receipt",
            "value": {
                "qty": qty, "unit": unit, "supplier": supplier, "price": price,
            },
            "effective_window": None,
            "confidence": 0.7,
        }

    def _parse_count(self, text: str, low: str) -> Optional[Dict[str, Any]]:
        m = re.search(
            _NUMBER_RE + r"\s*([a-zA-Z]+)?\s+of\s+([a-zA-Z ]+?)(?:\s+(?:left|remaining|in stock))?(?:\s|$|\.)",
            text,
            re.IGNORECASE,
        )
        if not m:
            return None
        qty = float(m.group(1))
        unit = _UNIT_WORDS.get((m.group(2) or "").lower(), (m.group(2) or "each").lower())
        ingredient = m.group(3).strip()
        return {
            "intent": "add_inventory_count",
            "entity_type": "ingredient",
            "entity_ref": ingredient,
            "attribute": "count",
            "value": {"qty": qty, "unit": unit},
            "effective_window": None,
            "confidence": 0.6,
        }

    def _parse_menu_item(self, text: str, low: str) -> Optional[Dict[str, Any]]:
        m = re.search(
            r"add\s+(?:a|an|the)?\s*([A-Za-z0-9 '&-]+?)\s+(?:for|at)\s+\$?" + _NUMBER_RE,
            text,
            re.IGNORECASE,
        )
        name = None
        price = None
        if m:
            name = m.group(1).strip()
            price = float(m.group(2))
        else:
            m2 = re.search(r"add\s+(?:a|an|the)?\s*([A-Za-z0-9 '&-]+)", text, re.IGNORECASE)
            if m2:
                name = m2.group(1).strip()
        if not name:
            return None
        # Strip a trailing "to the menu".
        name = re.sub(r"\s+to the menu$", "", name, flags=re.IGNORECASE).strip()
        return {
            "intent": "add_menu_item",
            "entity_type": "menu_item",
            "entity_ref": name,
            "attribute": "price",
            "value": price if price is not None else 0.0,
            "effective_window": None,
            "confidence": 0.6,
        }

    @staticmethod
    def _leave_status(extracted: Dict[str, Any]) -> str:
        """Resolve ``leave`` vs ``sick`` (vs ``present``) from the extracted
        fact's text fields (default ``leave``)."""
        blob = " ".join(
            str(extracted.get(k) or "") for k in ("attribute", "value", "intent")
        ).lower()
        if "sick" in blob:
            return "sick"
        if "present" in blob or "back" in blob:
            return "present"
        return "leave"

    # -- DB resolve / helpers ----------------------------------------------

    @staticmethod
    def _singular(name: str) -> str:
        n = name.strip().lower()
        if n.endswith("ies"):
            return n[:-3] + "y"
        if n.endswith("ses"):
            return n[:-2]
        if n.endswith("s") and not n.endswith("ss"):
            return n[:-1]
        return n

    def _resolve_ingredient(
        self, session: Any, name: Optional[str], unit: str, create: bool
    ) -> Optional[Ingredient]:
        if not name:
            return None
        target = self._singular(name)
        for ing in session.query(Ingredient).all():
            if self._singular(ing.name or "") == target:
                return ing
        if not create:
            return None
        base_unit = unit if unit in ("g", "ml", "each") else "g"
        ing = Ingredient(
            name=name.strip().title(),
            category="other",
            base_unit=base_unit,
            perishable=1,
            shelf_life_days=5.0,
            allergen_flags=[],
            weather_tags=[],
            notes="created via voice",
        )
        session.add(ing)
        session.flush()
        return ing

    def _resolve_supplier(
        self, session: Any, name: Optional[str], create: bool
    ) -> Optional[Supplier]:
        if not name:
            return None
        existing = (
            session.query(Supplier).filter(Supplier.name.ilike(name.strip())).first()
        )
        if existing is not None:
            return existing
        if not create:
            return None
        sup = Supplier(
            name=name.strip(),
            lead_time_days=2.0,
            reliability_score=0.9,
            min_order_value=0.0,
            contact="",
        )
        session.add(sup)
        session.flush()
        return sup

    # -- (3) persist the user fact -----------------------------------------

    def _write_user_fact(
        self, raw_text: str, extracted: Dict[str, Any], resulting_writes: List[str]
    ) -> int:
        session = self.db_session_factory()
        try:
            applied = 1 if resulting_writes and resulting_writes != ["stored"] else 0
            row = UserFact(
                raw_text=raw_text,
                source="voice",
                extracted=extracted,
                applied=applied,
                resulting_writes=resulting_writes,
                sim_time=float(self.bus.sim_time),
            )
            session.add(row)
            session.commit()
            return int(row.id)
        finally:
            session.close()

    # -- (4) emit USER_FACT -------------------------------------------------

    def _emit_user_fact(self, extracted: Dict[str, Any], raw_text: str) -> Optional[str]:
        window = extracted.get("effective_window")
        ttl = None
        if window and window.get("end") is not None:
            ttl = max(float(window["end"]) - float(self.bus.sim_time), 1.0)

        entity_ref = extracted.get("entity_ref")
        payload = {
            "intent": extracted.get("intent") or "other",
            "entity_type": extracted.get("entity_type") or "",
            "entity_ref": entity_ref if entity_ref is not None else "",
            "attribute": extracted.get("attribute") or "",
            "value": extracted.get("value"),
            "effective_window": window,
            "raw_text": raw_text,
        }
        signal = self.bus.emit(
            SignalType.USER_FACT, payload, source="voice", ttl=ttl
        )
        return signal.signal_id if signal is not None else None
