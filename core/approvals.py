"""Core approvals hub (§19.4).

Approvals are human-in-the-loop infrastructure owned by ``core`` (the queue +
inbox + ``/approvals/*`` endpoints). *Any* component creates an
``APPROVAL_REQUEST``; on approve / reject ``core`` updates the
``approval_requests`` row and emits ``APPROVAL_RESOLVED``; the owning side acts
on its own types (PO / promo → Track B handlers; ``outbound_call`` → the core
call subsystem).

This module owns:

- ``create`` — write a ``pending`` ``approval_requests`` row, emit
  ``APPROVAL_REQUEST`` and broadcast ``approval_created``.
- ``approve`` / ``reject`` — resolve a row, emit ``APPROVAL_RESOLVED`` and
  broadcast ``approval_resolved``.
- ``expire_pending`` — expire stale pending rows (TTL 6h, §15); the
  orchestrator calls it each tick.

Resolution dispatch is uniform: ``APPROVAL_RESOLVED`` is emitted on the bus and
nothing else. Reactors (the call subsystem for ``outbound_call``, Track B's
PO / promo handlers) subscribe to it via ``bus.subscribe`` (§8.2 / §19.4).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from .models import ApprovalRequest
from .signals import SignalType

logger = logging.getLogger(__name__)

# Pending approvals expire after 6h sim-time (the §15 APPROVAL_REQUEST TTL).
APPROVAL_TTL_SIM_S = 21600.0

# Types with NO reactor acting on the decision — approve/reject is meaningless
# for them; they are informational notices the manager *acknowledges*
# (docs/fable/approvals.md). Everything else is a real decision: a reactor
# fires on approval (PO placed, promo activated, call dialed, batch cooked,
# forecast override applied).
NOTICE_TYPES = {"kitchen_task", "staff_shift"}


def kind_for(approval_type: Optional[str]) -> str:
    """"notice" (acknowledge-only) or "decision" (approve/reject)."""
    return "notice" if approval_type in NOTICE_TYPES else "decision"


class ApprovalsHub:
    """The core approval queue + dispatch (§19.4)."""

    def __init__(self, bus: Any, db_session_factory: Callable[[], Any]):
        self.bus = bus
        self.db_session_factory = db_session_factory
        # Optional WS broadcast sink ``fn(event, payload)``, wired by the API
        # layer; a no-op (None) in tests / headless runs.
        self.ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None

    # -- WS wiring ----------------------------------------------------------

    def set_ws_broadcast(self, fn: Callable[[str, Dict[str, Any]], Any]) -> None:
        """Wire the sink the hub pushes ``approval_*`` events to."""
        self.ws_broadcast = fn

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)

    # -- create -------------------------------------------------------------

    def create(
        self,
        type: str,
        title: str,
        summary: str,
        payload: Optional[Dict[str, Any]] = None,
        urgency: str = "normal",
        ref_id: Optional[int] = None,
    ) -> ApprovalRequest:
        """Write a ``pending`` approval row, emit ``APPROVAL_REQUEST`` and
        broadcast ``approval_created`` (§19.4)."""
        now = float(self.bus.sim_time)
        payload = dict(payload or {})

        session = self.db_session_factory()
        try:
            row = ApprovalRequest(
                type=type,
                title=title,
                summary=summary,
                payload=payload,
                urgency=urgency,
                status="pending",
                created_at=now,
                resolved_at=None,
                resolved_by=None,
                ref_id=ref_id,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
        finally:
            session.close()

        self.bus.emit(
            SignalType.APPROVAL_REQUEST,
            {
                "approval_id": row.id,
                "type": type,
                "title": title,
                "summary": summary,
                "payload": payload,
                "urgency": urgency,
            },
            source="human",
        )
        self._broadcast("approval_created", {"approval": self._to_dict(row)})
        return row

    # -- update (notices escalate in place) ---------------------------------

    def update(
        self,
        approval_id: int,
        *,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        urgency: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[ApprovalRequest]:
        """Update a *pending* row in place and broadcast ``approval_updated``.

        Used by escalating notices (e.g. an overdue kitchen task climbing
        severity tiers, or flipping to "done late") so the inbox shows one
        evolving entry per underlying fact instead of a pile of duplicates.
        No bus signal is emitted — this is not a new request and must not
        re-fire reactors.
        """
        session = self.db_session_factory()
        try:
            row = session.get(ApprovalRequest, approval_id)
            if row is None or row.status != "pending":
                return None
            if title is not None:
                row.title = title
            if summary is not None:
                row.summary = summary
            if urgency is not None:
                row.urgency = urgency
            if payload is not None:
                row.payload = dict(payload)
            session.commit()
            session.refresh(row)
            session.expunge(row)
        finally:
            session.close()

        self._broadcast("approval_updated", {"approval": self._to_dict(row)})
        return row

    # -- resolve ------------------------------------------------------------

    def approve(
        self, approval_id: int, resolved_by: str = "human"
    ) -> Optional[ApprovalRequest]:
        """Resolve an approval as ``approved`` (§19.4)."""
        return self._resolve(approval_id, "approved", resolved_by)

    def reject(
        self, approval_id: int, resolved_by: str = "human"
    ) -> Optional[ApprovalRequest]:
        """Resolve an approval as ``rejected`` (§19.4)."""
        return self._resolve(approval_id, "rejected", resolved_by)

    def _resolve(
        self, approval_id: int, decision: str, resolved_by: str
    ) -> Optional[ApprovalRequest]:
        now = float(self.bus.sim_time)
        new_status = "approved" if decision == "approved" else "rejected"

        session = self.db_session_factory()
        try:
            row = session.get(ApprovalRequest, approval_id)
            if row is None:
                return None
            row.status = new_status
            row.resolved_at = now
            row.resolved_by = resolved_by
            session.commit()
            session.refresh(row)
            session.expunge(row)
        finally:
            session.close()

        resolved_payload = {
            "approval_id": row.id,
            "type": row.type,
            "decision": decision,
            "ref_id": row.ref_id if row.ref_id is not None else 0,
            "payload": dict(row.payload or {}),
        }
        # Emitting on the bus is the single dispatch path: every reactor
        # (call subsystem, Track B handlers) subscribes via ``bus.subscribe``.
        # Intentionally NO dedup_key — each resolution is a distinct event, so
        # it always takes the bus's new-insert path and fires reactors exactly
        # once (a dedup-refresh would never re-fire them anyway).
        self.bus.emit(SignalType.APPROVAL_RESOLVED, resolved_payload, source="human")
        self._broadcast("approval_resolved", {"approval": self._to_dict(row)})
        return row

    # -- expiry (§15 — 6h TTL) ---------------------------------------------

    def expire_pending(self, now_sim: float) -> int:
        """Expire pending rows older than the 6h TTL. Returns the count expired.

        Called by the orchestrator each tick (§19.4)."""
        session = self.db_session_factory()
        try:
            count = (
                session.query(ApprovalRequest)
                .filter(
                    ApprovalRequest.status == "pending",
                    ApprovalRequest.created_at.isnot(None),
                    ApprovalRequest.created_at + APPROVAL_TTL_SIM_S < now_sim,
                )
                .update({ApprovalRequest.status: "expired"}, synchronize_session=False)
            )
            session.commit()
            return int(count or 0)
        finally:
            session.close()

    def register(self, orchestrator: Any) -> Any:
        """Register the per-tick ``expire_pending`` sweep (§19.4)."""
        # ~once per tick at 1× (60 × 0.25 sim-s); the orchestrator catches up
        # across larger jumps. Expiry is idempotent so over-firing is harmless.
        return orchestrator.register(
            "interval",
            lambda: self.expire_pending(self.bus.sim_time),
            interval_sim_s=60.0 * 0.25,
            name="approvals_expire",
        )

    # -- serialization ------------------------------------------------------

    @staticmethod
    def _to_dict(row: ApprovalRequest) -> Dict[str, Any]:
        return {
            "id": row.id,
            "type": row.type,
            "kind": kind_for(row.type),
            "title": row.title,
            "summary": row.summary,
            "payload": row.payload,
            "urgency": row.urgency,
            "status": row.status,
            "created_at": row.created_at,
            "resolved_at": row.resolved_at,
            "resolved_by": row.resolved_by,
            "ref_id": row.ref_id,
        }
