"""Approval handlers — act on resolutions for Track B's own types (02 §B4.5).

The approval **queue, inbox UI, ``/approvals/*`` endpoints, TTL expiry, and the
``APPROVAL_RESOLVED`` emit all live in core** (§19.4, §23). Track B only
subscribes to ``APPROVAL_RESOLVED`` (group ``procurement``) and, on
``decision="approved"``, executes its own request types:
``purchase_order`` → ``procurement.place(po)``; ``promo`` →
``optimizer.activate_promo(promo_id)``. ``outbound_call`` resolutions are
handled by the core call subsystem, not here; any other type is ignored.
"""

from __future__ import annotations

from typing import Any

from core.signals import SignalType

OWN_TYPES = {"purchase_order", "promo"}


class ApprovalHandlers:
    """Subscribes to ``APPROVAL_RESOLVED`` and dispatches Track B's own types."""

    def __init__(self, bus: Any, procurement: Any, optimizer: Any):
        self.bus = bus
        self.procurement = procurement
        self.optimizer = optimizer
        bus.subscribe(SignalType.APPROVAL_RESOLVED, self.on_resolved)

    def on_resolved(self, signal: Any) -> None:
        payload = signal.payload or {}
        approval_type = payload.get("type")
        if approval_type not in OWN_TYPES:
            return  # outbound_call (core) / other — not ours
        if payload.get("decision") != "approved":
            return  # rejected: the PO/promo simply stays unplaced/unactivated

        ref_id = payload.get("ref_id")
        if ref_id is None:
            return
        if approval_type == "purchase_order":
            self.procurement.place(int(ref_id))
        elif approval_type == "promo":
            self.optimizer.activate_promo(int(ref_id))
