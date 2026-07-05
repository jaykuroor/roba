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
from .models import Call, Competitor, CompetitorOffer, Ingredient, Supplier, SupplierCatalog, SupplierTerm
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
        note: str = "",
        ingredient_id: Optional[int] = None,
        intel_goal: str = "",
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
            note=note,
            ingredient_id=ingredient_id,
            intel_goal=intel_goal,
        )

    def request(
        self,
        agent: str,
        counterparty_type: str,
        counterparty_id: int,
        purpose: str,
        require_approval: bool = True,
        note: str = "",
        ingredient_id: Optional[int] = None,
        intel_goal: str = "",
    ) -> Call:
        """Create a call.

        When ``require_approval=True`` (default): creates a ``requested`` call +
        an ``outbound_call`` approval card, emits ``CALL_REQUEST`` (§8.2).

        When ``require_approval=False``: bypasses the approval card; the call is
        immediately set ``active`` and ``CALL_STARTED`` is emitted directly.
        Use this path for user-initiated calls from the UI.

        If a call is already active, the call is queued (FIFO, §6.3).
        """
        # Build optional per-call metadata
        _call_meta: Optional[Dict[str, Any]] = None
        if note or ingredient_id or intel_goal:
            _call_meta = {}
            if note:
                _call_meta["note"] = note
            if ingredient_id:
                _call_meta["ingredient_id"] = ingredient_id
            if intel_goal:
                _call_meta["intel_goal"] = intel_goal

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
                    call_metadata=_call_meta,
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
                call_metadata=_call_meta,
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

        # Persist SupplierTerm cards from post-call extraction (new array format).
        # For supplier calls the schema now returns {"updates": [...]}.
        # Any terms already captured live by record_supplier_update are skipped (dedupe).
        if isinstance(outcome, dict) and call.counterparty_type == "supplier":
            updates = outcome.get("updates")
            if isinstance(updates, list) and updates:
                try:
                    self._process_extracted_updates(call, updates)
                except Exception as _exc:  # noqa: BLE001
                    logger.warning("extracted supplier updates processing failed: %s", _exc)
            # Backward-compat: old single-price schema (text calls, legacy)
            elif outcome.get("agreed") and outcome.get("agreed_price") is not None:
                try:
                    self._create_call_price_change(call, outcome)
                except Exception as _exc:  # noqa: BLE001
                    logger.warning("call_price ManagerChange creation failed: %s", _exc)

        # Restore the clock to its pre-call state/speed (§6.3).
        restore = self._clock_restore.pop(call_id, None)
        if restore is not None:
            self.clock.unfreeze_from_call(*restore)

        self._broadcast("call_ended", {"call": self._to_dict(call), "outcome": outcome})

        # A second call that was queued while this one ran can now start.
        self._start_next_pending()
        return outcome

    def _create_call_price_change(self, call: Call, outcome: Dict[str, Any]) -> None:
        """Create a ManagerChange(kind='call_price') so the agreed price surfaces
        as a desk card for the manager to apply/revert (§8.5 / workstream E)."""
        from .models import (  # noqa: PLC0415
            ManagerChange as _MC,
            Supplier as _Sup,
            SupplierCatalog as _SCat,
            Ingredient as _Ing,
        )
        agreed_price = float(outcome["agreed_price"])
        meta = call.call_metadata or {}

        session = self.db_session_factory()
        try:
            sup = session.get(_Sup, call.counterparty_id)
            sup_name = sup.name if sup else f"Supplier #{call.counterparty_id}"

            # Find the relevant catalog row
            target_ing_id = meta.get("ingredient_id")
            if target_ing_id:
                cat = (
                    session.query(_SCat)
                    .filter(
                        _SCat.supplier_id == call.counterparty_id,
                        _SCat.ingredient_id == int(target_ing_id),
                    )
                    .first()
                )
            else:
                cat = (
                    session.query(_SCat)
                    .filter(
                        _SCat.supplier_id == call.counterparty_id,
                        _SCat.is_default == 1,
                    )
                    .first()
                ) or (
                    session.query(_SCat)
                    .filter(_SCat.supplier_id == call.counterparty_id)
                    .first()
                )

            if cat is None:
                return

            ing = session.get(_Ing, cat.ingredient_id)
            ing_name = ing.name if ing else f"ingredient #{cat.ingredient_id}"
            current_price = float(cat.current_price or 0.0)

            cat_unit = cat.unit or "g"
            # Build human-readable price strings (per kg for display)
            if cat_unit == "g":
                before_disp = f"€{current_price * 1000:.2f}/kg"
                after_disp = f"€{agreed_price * 1000:.2f}/kg"
            else:
                before_disp = f"€{current_price:.2f}/{cat_unit}"
                after_disp = f"€{agreed_price:.2f}/{cat_unit}"

            change = _MC(
                kind="call_price",
                status="pending",
                summary=f"Price for {ing_name} ({sup_name}): {before_disp} → {after_disp}",
                created_at=float(self.bus.sim_time),
                details={
                    "before": {"price": current_price, "unit": cat_unit},
                    "after": {"price": agreed_price, "unit": cat_unit},
                    "rationale": outcome.get("agreed_terms") or "Negotiated price agreed on call",
                    "call_id": call.id,
                    "catalog_id": cat.id,
                    "ingredient_id": cat.ingredient_id,
                    "supplier_id": call.counterparty_id,
                    "ingredient_name": ing_name,
                    "supplier_name": sup_name,
                },
            )
            session.add(change)
            session.commit()

            self._broadcast(
                "manager_change",
                {"kind": "call_price", "id": change.id, "ingredient": ing_name, "supplier": sup_name},
            )
        finally:
            session.close()

    def _extract_outcome(self, call_id: int) -> Optional[Dict[str, Any]]:
        """Send the transcript to the LLM with the §8.5 schema and store the
        parsed result in ``calls.outcome``. Writes **no** track tables — the
        initiating agent persists its own record on ``CALL_OUTCOME``. A canned
        / unusable parse stores ``null`` (safe no-op).

        For supplier calls, uses a hardened extraction prompt with:
        - explicit verbatim-grounding rules (no paraphrase, no inference)
        - deterministic amount reporting (LLM reports raw number, Python normalises)
        - low temperature (0.2) to minimise hallucination
        - structured array schema covering discounts, scope (all/ingredient), and expiry
        """
        call = self._load(call_id)
        if call is None:
            return None

        schema = self._outcome_schema(call.counterparty_type)
        transcript_text = "\n".join(
            f"{t.get('role')}: {t.get('text')}" for t in (call.transcript or [])
        )

        if call.counterparty_type == "supplier":
            system_prompt = (
                "You extract ALL supplier updates from a call transcript. "
                "The caller is a supplier phoning the restaurant.\n\n"
                "CRITICAL RULES — follow exactly:\n"
                "1. NEVER invent or infer values. Extract ONLY what the caller explicitly stated.\n"
                "2. VERBATIM QUOTE: verbatim_quote must be the caller's exact words. Never paraphrase.\n"
                "3. DISCOUNTS vs PRICES: '20% off' = update_type=discount, amount_kind=percent_discount, amount=20. "
                "A price like '€4.20 per kg' = update_type=price_change, amount_kind=absolute_price, amount=4.20, unit=per_kg.\n"
                "4. DO NOT DO MATH. Report amount exactly as stated. '20%' → amount=20.0, NOT 0.20. "
                "The system normalises amounts in code — never convert.\n"
                "5. SCOPE: 'all items' / 'everything' / 'all products' / 'whole range' → scope=all. "
                "Named item → scope=ingredient, ingredient_name=<exact name stated>.\n"
                "6. EXPIRY: No expiry stated → expiry_kind=none. "
                "'for 2 orders' → expiry_kind=for_n_orders, expires_hint='for 2 orders'. "
                "'until end of month' → expiry_kind=until_date, expires_hint='until end of month'.\n"
                "7. CONFIDENCE: 1.0 = caller stated it clearly. 0.5 = somewhat ambiguous. "
                "0.0 = you'd be guessing. Do not include items below 0.5.\n"
                "8. If nothing was stated, return {\"updates\": []}.\n"
            )
        else:
            system_prompt = (
                "You extract the structured outcome of a phone call from its transcript. "
                "Respond only with JSON matching the requested fields. "
                "If nothing usable was agreed/learned, return empty values."
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript_text},
        ]

        # Low temperature for supplier extraction to minimise hallucination.
        temp = 0.2 if call.counterparty_type == "supplier" else None
        max_tok = 600 if call.counterparty_type == "supplier" else 300
        result = self.llm.complete(
            messages, json_schema=schema, max_tokens=max_tok,
            use_site="outcome_extraction", temperature=temp,
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

    def _get_identity(self) -> Dict[str, str]:
        """Return the restaurant identity from AppSettings (with fallbacks)."""
        from .models import AppSettings as _AppSettings  # noqa: PLC0415
        session = self.db_session_factory()
        try:
            settings = session.get(_AppSettings, 1)
            if settings is not None:
                return settings.get_identity()
        except Exception:  # noqa: BLE001
            pass
        finally:
            session.close()
        return {"title": "the restaurant", "location": "", "phone": ""}

    def _supplier_system_prompt(self, call: Call) -> str:
        """Market Spectator goal: lower the unit price / better terms (§8.3).
        Delegates to call_personas.supplier_negotiation_prompt for a richer,
        worked-example prompt shared by both text and live-voice calls.
        """
        from .models import Ingredient as _Ingredient  # noqa: PLC0415
        supplier_name = f"Supplier #{call.counterparty_id}"
        ingredient_name = "the target ingredient"
        current_price = 0.0
        unit = "g"
        meta = call.call_metadata or {}
        note = str(meta.get("note") or "")
        target_ingredient_id = meta.get("ingredient_id")

        session = self.db_session_factory()
        try:
            supplier = session.get(Supplier, call.counterparty_id)
            if supplier is not None:
                supplier_name = supplier.name

            # Prefer the specifically-targeted ingredient if provided
            if target_ingredient_id:
                cat = (
                    session.query(SupplierCatalog)
                    .filter(
                        SupplierCatalog.supplier_id == call.counterparty_id,
                        SupplierCatalog.ingredient_id == int(target_ingredient_id),
                    )
                    .first()
                )
            else:
                # Fall back to the default (is_default=1) catalog row
                cat = (
                    session.query(SupplierCatalog)
                    .filter(
                        SupplierCatalog.supplier_id == call.counterparty_id,
                        SupplierCatalog.is_default == 1,
                    )
                    .first()
                ) or (
                    session.query(SupplierCatalog)
                    .filter(SupplierCatalog.supplier_id == call.counterparty_id)
                    .first()
                )

            if cat is not None:
                current_price = float(cat.current_price or 0.0)
                unit = str(cat.unit or "g")
                ing = session.get(_Ingredient, cat.ingredient_id)
                if ing is not None:
                    ingredient_name = ing.name
        finally:
            session.close()

        identity = self._get_identity()
        return call_personas.supplier_negotiation_prompt(
            supplier_name=supplier_name,
            ingredient_name=ingredient_name,
            current_price=current_price,
            unit=unit,
            restaurant_title=identity["title"],
            restaurant_location=identity["location"],
            note=note,
        )

    def _competitor_system_prompt(self, call: Call) -> str:
        """Competitor Intelligence persona (§8.1 / §8.3).
        Delegates to call_personas.competitor_intel_prompt so text calls and
        live-voice calls share the same rich, example-backed persona.
        """
        competitor_name = "the restaurant"
        cuisine = ""
        distance_km = 0.0
        known_dishes: List[str] = []
        meta = call.call_metadata or {}
        intel_goal = str(meta.get("intel_goal") or "")

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
            # Load known dishes from competitor_offers
            from .models import CompetitorOffer as _CO  # noqa: PLC0415
            offers = (
                session.query(_CO)
                .filter(_CO.competitor_id == call.counterparty_id)
                .all()
            )
            known_dishes = [o.dish_name for o in offers if o.dish_name]
        finally:
            session.close()
        return call_personas.competitor_intel_prompt(
            competitor_name=competitor_name,
            cuisine=cuisine,
            distance_km=distance_km,
            known_dishes=known_dishes or None,
            intel_goal=intel_goal,
        )

    @staticmethod
    def _outcome_schema(counterparty_type: str) -> Dict[str, Any]:
        """The §8.5 JSON schema for the outcome, by counterparty type.

        Supplier schema: array of structured updates so discounts, scope (all/ingredient),
        and expiry are captured faithfully without hallucination.
        Competitor schema: unchanged (popular_dishes / price_points).
        """
        if counterparty_type == "supplier":
            return {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "description": "All pricing/discount/availability/lead-time updates the caller stated.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "update_type": {
                                    "type": "string",
                                    "description": "price_change | discount | availability | lead_time | other",
                                },
                                "scope": {
                                    "type": "string",
                                    "description": "'all' if applies to all items from this supplier, 'ingredient' if specific.",
                                },
                                "ingredient_name": {
                                    "type": "string",
                                    "description": "Exact ingredient name as stated (omit when scope=all).",
                                },
                                "amount": {
                                    "type": "number",
                                    "description": "Numeric value exactly as stated (20 for '20%', 4.20 for '€4.20'). Do NOT convert.",
                                },
                                "amount_kind": {
                                    "type": "string",
                                    "description": "absolute_price | percent_discount | percent_increase | availability_status | days",
                                },
                                "unit": {
                                    "type": "string",
                                    "description": "Unit as stated, e.g. 'per_kg', 'per_g', 'per_litre', 'each'. Omit if not applicable.",
                                },
                                "effective": {
                                    "type": "string",
                                    "description": "'immediate' or 'from_date'.",
                                },
                                "expiry_kind": {
                                    "type": "string",
                                    "description": "'none' (permanent), 'until_date', 'for_n_orders'.",
                                },
                                "expires_hint": {
                                    "type": "string",
                                    "description": "Natural-language expiry as stated, e.g. 'end of month', 'for 2 orders'. Omit if none.",
                                },
                                "verbatim_quote": {
                                    "type": "string",
                                    "description": "The caller's EXACT words stating this update. Never paraphrase.",
                                },
                                "confidence": {
                                    "type": "number",
                                    "description": "0.0–1.0 confidence this was clearly stated (not inferred).",
                                },
                            },
                            "required": ["update_type", "scope", "amount_kind", "verbatim_quote", "confidence"],
                        },
                    },
                },
                "required": ["updates"],
            }
        return {
            "type": "object",
            "properties": {
                "popular_dishes": {"type": "array"},
                "price_points": {"type": "object"},
            },
            "required": ["popular_dishes"],
        }

    # -- supplier-term helpers (live capture + extraction) ------------------

    def _fuzzy_match_ingredient(
        self, session: Any, supplier_id: int, ingredient_name: str
    ) -> Optional[int]:
        """Return the ingredient_id for a given name, fuzzy-matching against the
        supplier's catalog. Returns None when no match is found or name is blank."""
        if not ingredient_name:
            return None
        needle = ingredient_name.strip().lower()
        cats = (
            session.query(SupplierCatalog, Ingredient)
            .join(Ingredient, SupplierCatalog.ingredient_id == Ingredient.id)
            .filter(SupplierCatalog.supplier_id == supplier_id)
            .all()
        )
        # Exact match first
        for cat, ing in cats:
            if ing.name and ing.name.strip().lower() == needle:
                return int(cat.ingredient_id)
        # Substring / prefix match
        for cat, ing in cats:
            if ing.name and (needle in ing.name.strip().lower() or ing.name.strip().lower() in needle):
                return int(cat.ingredient_id)
        return None

    @staticmethod
    def _normalise_term_value(
        amount: float, amount_kind: str, unit: str
    ) -> Tuple[str, float]:
        """Return (term_type, normalised_value) from the raw LLM-reported amount.

        All math is deterministic Python — the LLM reports amounts exactly as
        stated (e.g. 20 for '20%') and we convert here.

        For price_override, value is normalised to per-gram to match
        SupplierCatalog.current_price semantics.
        For discount, value is a fraction (0.0–1.0).
        """
        kind = (amount_kind or "").strip().lower()
        u = (unit or "per_kg").strip().lower()
        if kind in ("percent_discount",):
            # Clamp to sane range (0-100 %)
            frac = max(0.0, min(1.0, float(amount) / 100.0))
            return "discount", frac
        if kind in ("percent_increase",):
            frac = max(0.0, float(amount) / 100.0)
            return "discount", -frac   # negative discount = price increase term
        # price_change / absolute_price
        price = float(amount)
        if u in ("per_kg", "kg"):
            return "price_override", price / 1000.0
        if u in ("per_100g",):
            return "price_override", price / 100.0
        if u in ("per_litre", "litre", "per_l", "l"):
            return "price_override", price / 1000.0
        if u in ("per_g", "g", "gram"):
            return "price_override", price
        # each / unit / pack — treat as-is (non-weight items)
        return "price_override", price

    @staticmethod
    def _parse_expiry(
        expiry_kind: str, expires_hint: str, n: Any, now: float
    ) -> Tuple[str, Optional[float], Optional[int]]:
        """Return (expiry_kind, expires_at, remaining_orders) from hint text.

        Python does all date arithmetic; the LLM only supplies the raw text and
        an enum tag.  On parse failure defaults to 'none' (permanent).
        """
        kind = (expiry_kind or "none").strip().lower()
        hint = (expires_hint or "").strip().lower()

        if kind in ("none", "permanent", ""):
            return "none", None, None

        if kind == "for_n_orders":
            try:
                count = int(float(str(n or 0)))
            except (ValueError, TypeError):
                # Try to extract a number from the hint text
                import re as _re
                m = _re.search(r"(\d+)", hint)
                count = int(m.group(1)) if m else 1
            return "orders", None, max(1, count)

        if kind == "until_date":
            # Parse common natural-language hints to sim-second offsets from now.
            import re as _re
            SECONDS_PER_DAY = 86400.0
            m_days = _re.search(r"(\d+)\s*day", hint)
            m_weeks = _re.search(r"(\d+)\s*week", hint)
            m_months = _re.search(r"(\d+)\s*month", hint)
            if m_months:
                offset = float(m_months.group(1)) * 30 * SECONDS_PER_DAY
            elif m_weeks:
                offset = float(m_weeks.group(1)) * 7 * SECONDS_PER_DAY
            elif m_days:
                offset = float(m_days.group(1)) * SECONDS_PER_DAY
            elif "end of month" in hint or "month end" in hint:
                offset = 30 * SECONDS_PER_DAY
            elif "end of week" in hint or "friday" in hint:
                offset = 7 * SECONDS_PER_DAY
            elif "next week" in hint:
                offset = 7 * SECONDS_PER_DAY
            elif "next month" in hint:
                offset = 30 * SECONDS_PER_DAY
            else:
                # Unknown text — store as 90-day expiry (long default, manager can adjust)
                offset = 90 * SECONDS_PER_DAY
            return "date", now + offset, None

        return "none", None, None

    def _create_supplier_term(
        self,
        *,
        call: Any,
        term_type: str,
        value: float,
        scope: str,
        ingredient_id: Optional[int],
        unit_basis: str,
        effective_at: float,
        expiry_kind: str,
        expires_at: Optional[float],
        remaining_orders: Optional[int],
        verbatim_quote: str,
    ) -> Optional["SupplierTerm"]:
        """Upsert a SupplierTerm and create a ManagerChange desk card.

        Deduplicates against existing terms for the same call to prevent double-capture
        when both the live tool and post-call extraction fire for the same update.
        Returns the term row, or None if a duplicate was found.
        """
        from .models import ManagerChange as _MC  # noqa: PLC0415

        now = effective_at
        session = self.db_session_factory()
        try:
            # Deduplicate: same call, same type, same scope, same ingredient
            existing = (
                session.query(SupplierTerm)
                .filter(
                    SupplierTerm.source_call_id == call.id,
                    SupplierTerm.term_type == term_type,
                    SupplierTerm.scope == scope,
                    SupplierTerm.ingredient_id == ingredient_id,
                )
                .first()
            )
            if existing is not None:
                return None  # already captured by live tool

            # Resolve ingredient + supplier names for summary display
            sup = session.get(Supplier, call.counterparty_id)
            sup_name = sup.name if sup else f"Supplier #{call.counterparty_id}"
            ing_name = "all items"
            if ingredient_id is not None:
                ing = session.get(Ingredient, ingredient_id)
                ing_name = ing.name if ing else f"ingredient #{ingredient_id}"

            # Build human-readable summary
            if term_type == "discount":
                pct = round(abs(value) * 100, 1)
                direction = "increase" if value < 0 else "discount"
                if scope == "all":
                    summary = f"{pct}% {direction} on all items from {sup_name}"
                else:
                    summary = f"{pct}% {direction} on {ing_name} ({sup_name})"
            else:  # price_override
                # Convert per-gram back to per-kg for display
                disp_price = value * 1000
                if scope == "all":
                    summary = f"New price on all items from {sup_name}: €{disp_price:.2f}/kg"
                else:
                    summary = f"New price for {ing_name} ({sup_name}): €{disp_price:.2f}/kg"

            if expiry_kind == "orders":
                summary += f" (for {remaining_orders} order{'s' if (remaining_orders or 1) != 1 else ''})"
            elif expiry_kind == "date" and expires_at:
                days_left = max(1, round((expires_at - now) / 86400))
                summary += f" (expires in ~{days_left} days)"
            else:
                summary += " (permanent)"

            term = SupplierTerm(
                supplier_id=call.counterparty_id,
                ingredient_id=ingredient_id,
                term_type=term_type,
                value=value,
                unit_basis=unit_basis,
                scope=scope,
                effective_at=effective_at,
                expiry_kind=expiry_kind,
                expires_at=expires_at,
                remaining_orders=remaining_orders,
                status="pending",
                source_call_id=call.id,
                verbatim_quote=verbatim_quote,
                created_at=now,
            )
            session.add(term)
            session.flush()

            # Create ManagerChange desk card
            details: Dict[str, Any] = {
                "term_type": term_type,
                "value": value,
                "scope": scope,
                "supplier_id": call.counterparty_id,
                "supplier_name": sup_name,
                "ingredient_id": ingredient_id,
                "ingredient_name": ing_name,
                "expiry_kind": expiry_kind,
                "expires_at": expires_at,
                "remaining_orders": remaining_orders,
                "verbatim_quote": verbatim_quote,
                "call_id": call.id,
                "supplier_term_id": term.id,
            }
            change = _MC(
                kind="supplier_term",
                status="pending",
                summary=summary,
                created_at=now,
                details=details,
            )
            session.add(change)
            session.flush()
            term.manager_change_id = change.id
            session.commit()

            self._broadcast(
                "manager_change",
                {
                    "kind": "supplier_term",
                    "id": change.id,
                    "supplier": sup_name,
                    "ingredient": ing_name,
                    "term_type": term_type,
                },
            )
            return term
        finally:
            session.close()

    def record_supplier_update_sync(
        self,
        call_id: int,
        *,
        update_type: str,
        scope: str,
        ingredient_name: str = "",
        amount: float,
        amount_kind: str,
        unit: str = "",
        effective: str = "immediate",
        expiry: str = "none",
        date_text: str = "",
        n: Any = None,
        verbatim_quote: str,
    ) -> Dict[str, Any]:
        """Synchronous handler for the ``record_supplier_update`` live voice tool.

        Called from ``_execute_tool`` in voice_live.py when the AI agent records a
        supplier update mid-call. Creates a SupplierTerm + ManagerChange card and
        returns a short confirmation for the agent to speak back.

        All numeric normalisation is deterministic Python; the LLM reports the raw
        values and we convert here.
        """
        call = self._load(call_id)
        if call is None:
            return {"error": "call not found"}

        now = float(self.bus.sim_time)

        # Resolve ingredient_id
        session = self.db_session_factory()
        try:
            ingredient_id: Optional[int] = None
            resolved_scope = (scope or "all").strip().lower()
            if resolved_scope == "ingredient" and ingredient_name:
                ingredient_id = self._fuzzy_match_ingredient(
                    session, call.counterparty_id, ingredient_name
                )
                if ingredient_id is None:
                    logger.info(
                        "record_supplier_update: could not match ingredient %r for supplier %s",
                        ingredient_name, call.counterparty_id,
                    )
        finally:
            session.close()

        # Normalise amount → (term_type, value)
        term_type, value = self._normalise_term_value(amount, amount_kind, unit)

        # Parse expiry
        expiry_kind_str, expires_at, remaining_orders = self._parse_expiry(
            expiry, date_text, n, now
        )

        # Create SupplierTerm + ManagerChange
        term = self._create_supplier_term(
            call=call,
            term_type=term_type,
            value=value,
            scope=resolved_scope,
            ingredient_id=ingredient_id,
            unit_basis=(unit or "").strip(),
            effective_at=now,
            expiry_kind=expiry_kind_str,
            expires_at=expires_at,
            remaining_orders=remaining_orders,
            verbatim_quote=(verbatim_quote or "").strip(),
        )

        if term is None:
            return {"status": "duplicate", "message": "This update was already recorded for this call."}

        # Build spoken confirmation
        if term_type == "discount":
            pct = round(abs(value) * 100, 1)
            direction = "increase" if value < 0 else "discount"
            if resolved_scope == "all":
                summary = f"{pct}% {direction} on all items"
            else:
                summary = f"{pct}% {direction} on {ingredient_name or 'that item'}"
        else:
            disp = value * 1000
            if resolved_scope == "all":
                summary = f"new price of €{disp:.2f}/kg on all items"
            else:
                summary = f"new price of €{disp:.2f}/kg on {ingredient_name or 'that item'}"

        if expiry_kind_str == "orders":
            summary += f" for {remaining_orders} order{'s' if (remaining_orders or 1) != 1 else ''}"
        elif expiry_kind_str == "date":
            summary += " until the stated date"
        else:
            summary += " permanently"

        return {
            "status": "recorded",
            "message": f"Recorded: {summary}. A change card has been created for manager review.",
        }

    def _process_extracted_updates(self, call: Any, updates: List[Dict[str, Any]]) -> None:
        """Create SupplierTerm rows for any updates found by post-call extraction
        that were NOT already captured live by record_supplier_update."""
        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            for update in updates:
                confidence = float(update.get("confidence") or 0.0)
                if confidence < 0.5:
                    continue  # drop low-confidence extractions

                scope = (update.get("scope") or "all").strip().lower()
                ingredient_name = update.get("ingredient_name") or ""
                ingredient_id: Optional[int] = None
                if scope == "ingredient" and ingredient_name:
                    ingredient_id = self._fuzzy_match_ingredient(
                        session, call.counterparty_id, ingredient_name
                    )

                amount_kind = update.get("amount_kind") or ""
                amount = float(update.get("amount") or 0.0)
                unit = update.get("unit") or ""
                term_type, value = self._normalise_term_value(amount, amount_kind, unit)

                expiry_kind_raw = update.get("expiry_kind") or "none"
                expires_hint = update.get("expires_hint") or ""
                expiry_kind_str, expires_at, remaining_orders = self._parse_expiry(
                    expiry_kind_raw, expires_hint, None, now
                )

                verbatim_quote = update.get("verbatim_quote") or ""

                self._create_supplier_term(
                    call=call,
                    term_type=term_type,
                    value=value,
                    scope=scope,
                    ingredient_id=ingredient_id,
                    unit_basis=unit.strip(),
                    effective_at=now,
                    expiry_kind=expiry_kind_str,
                    expires_at=expires_at,
                    remaining_orders=remaining_orders,
                    verbatim_quote=verbatim_quote,
                )
        finally:
            session.close()

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
