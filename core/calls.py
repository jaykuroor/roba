"""The interactive call subsystem (§8).

Two agents make outbound calls — Market Spectator (supplier negotiation) and
Competitor Intelligence (undercover research) — through this one subsystem. In
the demo the presenter plays the other party by voice.

Lifecycle (§8.2):
  ``request`` → create a ``calls`` row (``requested``) + an ``outbound_call``
  approval → human approve/reject (core emits ``APPROVAL_RESOLVED``) →
  this subsystem (subscribed to those resolutions) starts the call:
  ``clock.freeze_for_call`` + ``CALL_STARTED`` → turn loop (``add_turn`` /
  ``generate_agent_turn``) → ``end_call`` (or ``auto_resolve`` when the
  presenter declines roleplay) which parses the outcome (§8.5), stores
  ``calls.outcome``, emits ``CALL_OUTCOME`` and restores the clock.

Hard rules (§6.3 / §8.5):
- Only **one** ``status='active'`` call at a time. A second ``request`` while
  one is active still queues an approval card (noted "waiting for current call
  to end"); the call is started only once the active call ends.
- The subsystem owns **zero** track-domain tables. It only fills
  ``calls.outcome`` and emits ``CALL_OUTCOME`` — the initiating agent persists
  its own domain record on receipt.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from .llm import CANNED_NOTE
from . import call_personas
from .models import Call, Competitor, CompetitorOffer, Supplier, SupplierCatalog
from .signals import SignalType

logger = logging.getLogger(__name__)

WAITING_NOTE = "waiting for current call to end"

# Canned counterparty lines (used by ``auto_resolve`` when the presenter
# declines to roleplay, §8.2). Scripted persona, one per counterparty type.
_CANNED_COUNTERPARTY = {
    "supplier": "Alright, I can come down a little on the unit price for you.",
    "competitor": "Honestly, our most popular dish is the house special.",
}


class CallSubsystem:
    """Interactive, approval-gated outbound calls (§8)."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        clock: Any,
        llm: Any,
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.clock = clock
        self.llm = llm
        self.approvals: Optional[Any] = None
        # Calls approved while another is active wait here, FIFO (§6.3).
        self._pending_starts: List[int] = []
        # Clock state captured at freeze time, restored on call end, per call.
        self._clock_restore: Dict[int, Tuple[str, float]] = {}
        # Optional WS broadcast sink ``fn(event, payload)``; None in tests.
        self.ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None

    # -- wiring -------------------------------------------------------------

    def attach_approvals(self, approvals: Any) -> None:
        """Wire the approvals hub for creating ``outbound_call`` approvals, and
        subscribe to ``APPROVAL_RESOLVED`` on the bus — the single dispatch path
        for resolutions (§8.2). The callback filters to ``outbound_call``."""
        self.approvals = approvals
        self.bus.subscribe(SignalType.APPROVAL_RESOLVED, self._on_approval_resolved)

    def set_ws_broadcast(self, fn: Callable[[str, Dict[str, Any]], Any]) -> None:
        """Wire the sink the subsystem pushes ``call_*`` events to."""
        self.ws_broadcast = fn

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)

    # -- request (§8.2) -----------------------------------------------------

    def start_direct(
        self,
        agent: str,
        counterparty_type: str,
        counterparty_id: int,
        purpose: str,
    ) -> Call:
        """Start a call immediately without an approval card (user-initiated).

        Creates the ``Call`` row as ``active`` straight away, freezes the clock
        and emits ``CALL_STARTED``.  No ``ApprovalRequest`` is created.
        """
        return self.request(
            agent=agent,
            counterparty_type=counterparty_type,
            counterparty_id=counterparty_id,
            purpose=purpose,
            require_approval=False,
        )

    def request(
        self,
        agent: str,
        counterparty_type: str,
        counterparty_id: int,
        purpose: str,
        require_approval: bool = True,
    ) -> Call:
        """Create a call.

        When ``require_approval=True`` (default): creates a ``requested`` call +
        an ``outbound_call`` approval card, emits ``CALL_REQUEST`` (§8.2).

        When ``require_approval=False``: bypasses the approval card; the call is
        immediately set ``active`` and ``CALL_STARTED`` is emitted directly.
        Use this path for user-initiated calls from the UI.

        If a call is already active, the call is queued (FIFO, §6.3).
        """
        # require_approval=False path: skip the approval card entirely
        if not require_approval:
            call_mode = self.clock.current_state().get("call_mode") or "freeze"
            session = self.db_session_factory()
            try:
                call = Call(
                    agent=agent,
                    counterparty_type=counterparty_type,
                    counterparty_id=counterparty_id,
                    purpose=purpose,
                    status="requested",
                    approval_id=None,
                    transcript=[],
                    outcome=None,
                    started_at=None,
                    ended_at=None,
                    clock_action=call_mode,
                )
                session.add(call)
                session.commit()
                session.refresh(call)
                call_id = call.id
                session.expunge(call)
            finally:
                session.close()
            # Go straight to active — bypassing approval card
            self._set_status(call_id, "approved")
            self._start_call(call_id)
            return self._load(call_id) or call  # type: ignore[return-value]

        if self.approvals is None:
            raise RuntimeError("CallSubsystem.approvals not attached")

        now = float(self.bus.sim_time)
        call_mode = self.clock.current_state().get("call_mode") or "freeze"
        waiting = self._has_active_call()

        session = self.db_session_factory()
        try:
            call = Call(
                agent=agent,
                counterparty_type=counterparty_type,
                counterparty_id=counterparty_id,
                purpose=purpose,
                status="requested",
                approval_id=None,
                transcript=[],
                outcome=None,
                started_at=None,
                ended_at=None,
                clock_action=call_mode,
            )
            session.add(call)
            session.commit()
            session.refresh(call)
            call_id = call.id
            session.expunge(call)
        finally:
            session.close()

        title = f"Outbound call: {agent} → {counterparty_type} #{counterparty_id}"
        summary = purpose or ""
        approval_payload: Dict[str, Any] = {"call_id": call_id}
        if waiting:
            summary = f"{summary} ({WAITING_NOTE})".strip()
            approval_payload["note"] = WAITING_NOTE

        approval = self.approvals.create(
            type="outbound_call",
            title=title,
            summary=summary,
            payload=approval_payload,
            urgency="normal",
            ref_id=call_id,
        )

        session = self.db_session_factory()
        try:
            row = session.get(Call, call_id)
            row.approval_id = approval.id
            session.commit()
            session.refresh(row)
            session.expunge(row)
            call = row
        finally:
            session.close()

        self.bus.emit(
            SignalType.CALL_REQUEST,
            {
                "call_id": call_id,
                "agent": agent,
                "counterparty_type": counterparty_type,
                "counterparty_id": counterparty_id,
                "purpose": purpose,
            },
            source="calls",
        )
        return call

    # -- approval resolution (§8.2) ----------------------------------------

    def _on_approval_resolved(self, signal: Any) -> None:
        """Handle an ``APPROVAL_RESOLVED`` bus signal for ``outbound_call`` only."""
        payload = signal.payload or {}
        if payload.get("type") != "outbound_call":
            return
        inner = payload.get("payload") or {}
        call_id = inner.get("call_id")
        if call_id is None:
            call_id = payload.get("ref_id")
        if not call_id:
            return

        decision = payload.get("decision")
        if decision == "approved":
            self._set_status(int(call_id), "approved")
            self._start_call(int(call_id))
        elif decision == "rejected":
            self._set_status(int(call_id), "rejected")

    # -- start (§8.2 / §6.3) -----------------------------------------------

    def _start_call(self, call_id: int) -> Optional[Call]:
        """Set the call ``active``, freeze the clock and emit ``CALL_STARTED``.

        Honours the single-active-call rule: if another call is active this one
        is queued (FIFO) and started only once that call ends (§6.3)."""
        if self._has_active_call(exclude_id=call_id):
            if call_id not in self._pending_starts:
                self._pending_starts.append(call_id)
            return None

        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is None:
                return None
            call.status = "active"
            call.started_at = now
            session.commit()
            session.refresh(call)
            session.expunge(call)
        finally:
            session.close()

        # CALL_FROZEN (or 0.1× when call_mode=slow) — captured for restore.
        self._clock_restore[call_id] = self.clock.freeze_for_call()

        self.bus.emit(
            SignalType.CALL_STARTED, {"call_id": call_id}, source="calls"
        )
        # The orchestrator's WS broadcast switches the frontend to ROLEPLAY.
        self._broadcast("call_started", {"call": self._to_dict(call)})
        return call

    # -- turns (§8.3) -------------------------------------------------------

    def add_turn(self, call_id: int, role: str, text: str) -> Dict[str, Any]:
        """Append ``{role, text, sim_ts}`` to ``calls.transcript`` and stream a
        ``call_turn`` WS event (§8.3)."""
        sim_ts = float(self.bus.sim_time)
        turn = {"role": role, "text": text, "sim_ts": sim_ts}

        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is None:
                return turn
            transcript = list(call.transcript or [])
            transcript.append(turn)
            call.transcript = transcript
            session.commit()
        finally:
            session.close()

        self._broadcast(
            "call_turn", {"call_id": call_id, "role": role, "text": text}
        )
        return turn

    def generate_agent_turn(self, call_id: int) -> str:
        """Generate the agent's next utterance via the LLM with a tight
        per-counterparty system prompt (§8.3), append it and return the text."""
        call = self._load(call_id)
        if call is None:
            return ""

        if call.counterparty_type == "supplier":
            use_site = "call_supplier"
            system = self._supplier_system_prompt(call)
        else:
            use_site = "call_competitor"
            system = self._competitor_system_prompt(call)

        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
        for turn in (call.transcript or []):
            role = "assistant" if turn.get("role") == "agent" else "user"
            messages.append({"role": role, "content": str(turn.get("text", ""))})

        result = self.llm.complete(messages, max_tokens=200, use_site=use_site)
        text = result if isinstance(result, str) else str(result)
        self.add_turn(call_id, "agent", text)
        return text

    # -- end (§8.5) ---------------------------------------------------------

    def end_call(self, call_id: int) -> Optional[Dict[str, Any]]:
        """Complete the call: parse + store the outcome, emit ``CALL_OUTCOME``
        and restore the clock (§8.5)."""
        return self._finalize(call_id, "completed")

    def auto_resolve(self, call_id: int) -> Optional[Dict[str, Any]]:
        """Simulate a counterpart reply with a canned persona, then complete the
        call as ``auto_resolved`` (§8.2 fallback when roleplay is declined)."""
        call = self._load(call_id)
        if call is None:
            return None
        if call.status != "active":
            self._start_call(call_id)

        canned_line = _CANNED_COUNTERPARTY.get(
            call.counterparty_type, "Thanks for calling."
        )
        self.add_turn(call_id, "counterparty", canned_line)
        self.generate_agent_turn(call_id)
        return self._finalize(call_id, "auto_resolved")

    def _finalize(self, call_id: int, status: str) -> Optional[Dict[str, Any]]:
        """Shared completion path for ``end_call`` / ``auto_resolve``."""
        outcome = self._extract_outcome(call_id)
        now = float(self.bus.sim_time)

        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is None:
                return None
            call.status = status
            call.ended_at = now
            session.commit()
            session.refresh(call)
            session.expunge(call)
        finally:
            session.close()

        self.bus.emit(
            SignalType.CALL_OUTCOME,
            {
                "call_id": call_id,
                "counterparty_type": call.counterparty_type,
                "outcome": outcome if isinstance(outcome, dict) else {},
            },
            source="calls",
        )

        # Restore the clock to its pre-call state/speed (§6.3).
        restore = self._clock_restore.pop(call_id, None)
        if restore is not None:
            self.clock.unfreeze_from_call(*restore)

        self._broadcast("call_ended", {"call": self._to_dict(call), "outcome": outcome})

        # A second call that was queued while this one ran can now start.
        self._start_next_pending()
        return outcome

    def _extract_outcome(self, call_id: int) -> Optional[Dict[str, Any]]:
        """Send the transcript to the LLM with the §8.5 schema and store the
        parsed result in ``calls.outcome``. Writes **no** track tables — the
        initiating agent persists its own record on ``CALL_OUTCOME``. A canned
        / unusable parse stores ``null`` (safe no-op)."""
        call = self._load(call_id)
        if call is None:
            return None

        schema = self._outcome_schema(call.counterparty_type)
        transcript_text = "\n".join(
            f"{t.get('role')}: {t.get('text')}" for t in (call.transcript or [])
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract the structured outcome of a phone call from its "
                    "transcript. Respond only with JSON matching the requested "
                    "fields. If nothing usable was agreed/learned, return empty "
                    "values."
                ),
            },
            {"role": "user", "content": transcript_text},
        ]
        result = self.llm.complete(
            messages, json_schema=schema, max_tokens=300, use_site="outcome_extraction"
        )

        if not isinstance(result, dict) or result.get("note") == CANNED_NOTE:
            outcome: Optional[Dict[str, Any]] = None
        else:
            outcome = result

        session = self.db_session_factory()
        try:
            row = session.get(Call, call_id)
            if row is not None:
                row.outcome = outcome
                session.commit()
        finally:
            session.close()
        return outcome

    # -- queue / active-call helpers (§6.3) --------------------------------

    def _start_next_pending(self) -> None:
        """Start the next queued call, if any (only one becomes active)."""
        if self._has_active_call():
            return
        while self._pending_starts:
            next_id = self._pending_starts.pop(0)
            started = self._start_call(next_id)
            if started is not None:
                break

    def _has_active_call(self, exclude_id: Optional[int] = None) -> bool:
        session = self.db_session_factory()
        try:
            query = session.query(Call).filter(Call.status == "active")
            if exclude_id is not None:
                query = query.filter(Call.id != exclude_id)
            return query.first() is not None
        finally:
            session.close()

    # -- prompts / schemas (§8.3 / §8.5) -----------------------------------

    def _supplier_system_prompt(self, call: Call) -> str:
        """Market Spectator goal: lower the unit price / better terms (§8.3).
        Delegates to call_personas.supplier_negotiation_prompt for a richer,
        worked-example prompt shared by both text and live-voice calls.
        """
        supplier_name = f"Supplier #{call.counterparty_id}"
        ingredient_name = "the target ingredient"
        current_price = 0.0
        unit = "unit"
        session = self.db_session_factory()
        try:
            from .models import Ingredient as _Ingredient  # noqa: PLC0415
            supplier = session.get(Supplier, call.counterparty_id)
            if supplier is not None:
                supplier_name = supplier.name
            cat = (
                session.query(SupplierCatalog)
                .filter(SupplierCatalog.supplier_id == call.counterparty_id)
                .first()
            )
            if cat is not None:
                current_price = float(cat.current_price or 0.0)
                unit = str(cat.unit or "unit")
                ing = session.get(_Ingredient, cat.ingredient_id)
                if ing is not None:
                    ingredient_name = ing.name
        finally:
            session.close()
        return call_personas.supplier_negotiation_prompt(
            supplier_name=supplier_name,
            ingredient_name=ingredient_name,
            current_price=current_price,
            unit=unit,
        )

    def _competitor_system_prompt(self, call: Call) -> str:
        """Competitor Intelligence persona (§8.1 / §8.3).
        Delegates to call_personas.competitor_intel_prompt so text calls and
        live-voice calls share the same rich, example-backed persona.
        """
        competitor_name = f"the restaurant"
        cuisine = ""
        distance_km = 0.0
        session = self.db_session_factory()
        try:
            competitor = session.get(Competitor, call.counterparty_id)
            if competitor is not None:
                competitor_name = competitor.name
                if isinstance(competitor.cuisine, list) and competitor.cuisine:
                    cuisine = competitor.cuisine[0]
                elif isinstance(competitor.cuisine, str):
                    cuisine = competitor.cuisine
                distance_km = float(competitor.distance_km or 0.0)
        finally:
            session.close()
        return call_personas.competitor_intel_prompt(
            competitor_name=competitor_name,
            cuisine=cuisine,
            distance_km=distance_km,
        )

    @staticmethod
    def _outcome_schema(counterparty_type: str) -> Dict[str, Any]:
        """The §8.5 JSON schema for the outcome, by counterparty type."""
        if counterparty_type == "supplier":
            return {
                "type": "object",
                "properties": {
                    "ingredient_id": {"type": "integer"},
                    "agreed_price": {"type": "number"},
                    "agreed_terms": {"type": "string"},
                    "agreed": {"type": "boolean"},
                },
                "required": ["agreed"],
            }
        return {
            "type": "object",
            "properties": {
                "popular_dishes": {"type": "array"},
                "price_points": {"type": "object"},
            },
            "required": ["popular_dishes"],
        }

    # -- low-level helpers --------------------------------------------------

    def _set_status(self, call_id: int, status: str) -> None:
        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is not None:
                call.status = status
                session.commit()
        finally:
            session.close()

    def _load(self, call_id: int) -> Optional[Call]:
        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is not None:
                session.expunge(call)
            return call
        finally:
            session.close()

    @staticmethod
    def _to_dict(call: Call) -> Dict[str, Any]:
        return {
            "id": call.id,
            "agent": call.agent,
            "counterparty_type": call.counterparty_type,
            "counterparty_id": call.counterparty_id,
            "purpose": call.purpose,
            "status": call.status,
            "approval_id": call.approval_id,
            "transcript": call.transcript,
            "outcome": call.outcome,
            "started_at": call.started_at,
            "ended_at": call.ended_at,
            "clock_action": call.clock_action,
        }
