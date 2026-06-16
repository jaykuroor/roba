"""Procurement service (02 §B4.4).

Turns Optimizer reorders into POs: create ``purchase_orders`` (+ lines); if
the total is over :data:`core.config.APPROVAL_PO_THRESHOLD` route through the
core approval queue (``approvals.create``) and wait, else auto-place. Once
placed, registers a delivery-deadline trigger at ``expected_delivery`` that
marks the PO ``delivered`` and hands it to the Ledger (the only inventory
writer) via :meth:`InventoryLedger.receive`. Emits ``REORDER_PLACED`` on
placement (auto or post-approval).

This is a service, not a signal-subscribing agent (02 §B1) — Optimizer calls
it directly to create a PO, and the approval handlers call :meth:`place` on
``APPROVAL_RESOLVED(type=purchase_order, decision=approved)``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import config
from core.events import log_event as core_log_event
from core.models import EventLog, PurchaseOrder, PurchaseOrderLine, Supplier
from core.signals import SignalType


class Procurement:
    """PO lifecycle: proposed → (approval) → placed → delivered → receive."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Any,
        orchestrator: Any,
        ledger: Any,
        approvals: Any = None,
        ws_broadcast: Any = None,
        name: str = "procurement",
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.orchestrator = orchestrator
        self.ledger = ledger
        self.approvals = approvals
        self.ws_broadcast = ws_broadcast
        self.name = name

    def attach_approvals(self, approvals: Any) -> None:
        self.approvals = approvals

    # -- helpers (mirrors BaseAgent's conveniences; Procurement is a service,
    #    not a signal-subscribing agent, so it does not subclass BaseAgent) --

    @property
    def sim_time(self) -> float:
        return self.bus.sim_time

    def emit(self, type_: Any, payload: Dict[str, Any], **kwargs: Any) -> Any:
        return self.bus.emit(type_, payload, source=self.name, **kwargs)

    def broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)

    def log_event(self, category: str, summary: str, detail: Optional[Any] = None) -> EventLog:
        session = self.db_session_factory()
        try:
            row = core_log_event(session, self.sim_time, category, self.name, summary, detail)
            session.expunge(row)
        finally:
            session.close()
        self.broadcast(
            "event_logged",
            {"event": {"id": row.id, "sim_time": row.sim_time, "category": row.category,
                       "actor": row.actor, "summary": row.summary, "detail": row.detail}},
        )
        return row

    # -- create (§18.8 / §B4.4) ----------------------------------------------

    def create_po(
        self,
        supplier_id: int,
        lines: List[Dict[str, Any]],
        created_by: str = "optimizer",
    ) -> PurchaseOrder:
        """Create a PO (+ lines); auto-place or route to approval (§18.8)."""
        now = self.sim_time
        total = sum(float(l["qty"]) * float(l["unit_price"] or 0.0) for l in lines)

        session = self.db_session_factory()
        try:
            po = PurchaseOrder(
                supplier_id=supplier_id,
                status="proposed",
                created_at=now,
                expected_delivery=None,
                total_cost=total,
                created_by=created_by,
                approval_id=None,
            )
            session.add(po)
            session.flush()
            for line in lines:
                session.add(
                    PurchaseOrderLine(
                        po_id=po.id,
                        ingredient_id=line["ingredient_id"],
                        qty=float(line["qty"]),
                        unit=line.get("unit") or "each",
                        unit_price=float(line["unit_price"] or 0.0),
                        line_total=float(line["qty"]) * float(line["unit_price"] or 0.0),
                    )
                )
            session.commit()
            session.refresh(po)
            po_id = po.id
            session.expunge(po)
        finally:
            session.close()

        if total > config.APPROVAL_PO_THRESHOLD and self.approvals is not None:
            approval = self.approvals.create(
                type="purchase_order",
                title=f"Purchase order #{po_id} (${total:.2f})",
                summary=f"PO #{po_id}: {len(lines)} line(s), total ${total:.2f}, supplier {supplier_id}.",
                payload={"po_id": po_id, "lines": lines, "total": total},
                ref_id=po_id,
            )
            session = self.db_session_factory()
            try:
                po = session.get(PurchaseOrder, po_id)
                po.approval_id = approval.id
                session.commit()
                session.refresh(po)
                session.expunge(po)
            finally:
                session.close()
            self.log_event(
                "po_pending_approval",
                f"PO #{po_id} (${total:.2f}) requires approval (over threshold).",
                {"po_id": po_id, "total": total},
            )
        else:
            self._place(po_id)

        return po

    # -- place (auto or post-approval) ---------------------------------------

    def place(self, po_id: int) -> None:
        """Place a PO that was awaiting approval (called by approval handlers)."""
        self._place(po_id)

    def _place(self, po_id: int) -> None:
        now = self.sim_time
        session = self.db_session_factory()
        try:
            po = session.get(PurchaseOrder, po_id)
            if po is None or po.status not in ("proposed",):
                return
            supplier = session.get(Supplier, po.supplier_id)
            lead_days = float(supplier.lead_time_days or 1.0) if supplier is not None else 1.0
            expected_delivery = now + lead_days * 86400.0
            po.status = "placed"
            po.expected_delivery = expected_delivery
            session.commit()

            lines = (
                session.query(PurchaseOrderLine)
                .filter(PurchaseOrderLine.po_id == po_id)
                .all()
            )
            line_payload = [{"ingredient_id": l.ingredient_id, "qty": l.qty} for l in lines]
            total = po.total_cost
            supplier_id = po.supplier_id
        finally:
            session.close()

        self.orchestrator.register(
            "deadline",
            lambda: self._deliver(po_id),
            due_at=expected_delivery,
            name=f"po_delivery_{po_id}",
        )

        self.emit(
            SignalType.REORDER_PLACED,
            {
                "po_id": po_id,
                "supplier_id": supplier_id,
                "lines": line_payload,
                "total": total,
                "eta": expected_delivery,
            },
        )
        self.log_event(
            "po_placed",
            f"PO #{po_id} placed with supplier {supplier_id}, ETA {expected_delivery:.0f} (total ${total:.2f}).",
            {"po_id": po_id, "supplier_id": supplier_id, "eta": expected_delivery},
        )

    # -- delivery (§B4.4) -----------------------------------------------------

    def _deliver(self, po_id: int) -> None:
        session = self.db_session_factory()
        try:
            po = session.get(PurchaseOrder, po_id)
            if po is None or po.status != "placed":
                return
            po.status = "delivered"
            session.commit()
        finally:
            session.close()

        self.ledger.receive(po_id)
        self.log_event(
            "po_delivered", f"PO #{po_id} delivered.", {"po_id": po_id}
        )
