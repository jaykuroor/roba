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
from core.models import (
    EventLog,
    InventoryOptimizerMemory,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierCatalog,
    SupplierTerm,
)
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
        approval_policy: Any = None,
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.orchestrator = orchestrator
        self.ledger = ledger
        self.approvals = approvals
        self.ws_broadcast = ws_broadcast
        self.name = name
        self.approval_policy = approval_policy

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

    # -- pricing helpers ------------------------------------------------------

    @staticmethod
    def _term_live(t: Any, now: float) -> bool:
        """A SupplierTerm still in effect (mirrors terms.apply_supplier_terms)."""
        if t.expiry_kind == "date" and t.expires_at is not None:
            return float(t.expires_at) >= now
        if t.expiry_kind == "orders":
            return (t.remaining_orders or 0) > 0
        return True

    def _live_terms(self, session: Any, supplier_id: int, term_type: str) -> List[Any]:
        now = self.sim_time
        rows = (
            session.query(SupplierTerm)
            .filter(
                SupplierTerm.supplier_id == supplier_id,
                SupplierTerm.status == "active",
                SupplierTerm.term_type == term_type,
            )
            .all()
        )
        return [t for t in rows if self._term_live(t, now)]

    def _free_delivery_applies(self, supplier_id: int, goods_value: float) -> bool:
        """True when a live free_delivery term's spend threshold is met."""
        session = self.db_session_factory()
        try:
            return any(
                goods_value >= float(t.min_order_value or 0.0)
                for t in self._live_terms(session, supplier_id, "free_delivery")
            )
        finally:
            session.close()

    def _discounted_goods_total(
        self, supplier_id: int, lines: List[Dict[str, Any]]
    ) -> float:
        """Goods subtotal with per-item quantity discounts and supplier volume
        discount applied — mirrors the optimizer's landed-cost basis.

        1. Per-item: if the catalog row for (supplier, ingredient) has a
           ``discount`` tier whose ``min_qty`` is met, the line is billed at the
           tier ``unit_price`` (the lowest qualifying).
        2. Supplier-level: the highest ``volume_discount`` tier whose
           ``min_value`` the (post-item-discount) subtotal meets rebates
           ``discount_pct`` % off the whole order.
        """
        session = self.db_session_factory()
        try:
            subtotal = 0.0
            for l in lines:
                qty = float(l["qty"])
                unit_price = float(l["unit_price"] or 0.0)
                cat = (
                    session.query(SupplierCatalog)
                    .filter(
                        SupplierCatalog.supplier_id == supplier_id,
                        SupplierCatalog.ingredient_id == l["ingredient_id"],
                    )
                    .first()
                )
                eff_price = unit_price
                tiers = (getattr(cat, "discount", None) or []) if cat is not None else []
                for tier in tiers:
                    min_qty = float(tier.get("min_qty") or 0.0)
                    tier_price = float(tier.get("unit_price") or unit_price)
                    if qty >= min_qty > 0 and tier_price < eff_price:
                        eff_price = tier_price
                subtotal += qty * eff_price

            supplier = session.get(Supplier, supplier_id)
            vd = (getattr(supplier, "volume_discount", None) or []) if supplier else []
            best_pct = 0.0
            for tier in vd:
                min_value = float(tier.get("min_value") or 0.0)
                pct = float(tier.get("discount_pct") or 0.0)
                if subtotal >= min_value > 0 and pct > best_pct:
                    best_pct = pct
            # Negotiated threshold_discount terms act as extra volume tiers so the
            # billed PO total matches the plan the optimizer costed against.
            for t in self._live_terms(session, supplier_id, "threshold_discount"):
                mov = float(t.min_order_value or 0.0)
                pct = float(t.value or 0.0) * 100.0
                if subtotal >= mov > 0 and pct > best_pct:
                    best_pct = pct
            return subtotal * (1.0 - best_pct / 100.0)
        finally:
            session.close()

    # -- create (§18.8 / §B4.4) ----------------------------------------------

    def create_po(
        self,
        supplier_id: int,
        lines: List[Dict[str, Any]],
        created_by: str = "optimizer",
        planned_delivery: Optional[float] = None,
        delivery_charge: float = 0.0,
        urgency: Optional[str] = None,
    ) -> PurchaseOrder:
        """Create a PO (+ lines); auto-place or route to approval (§18.8).

        ``planned_delivery`` — if provided, ``_place`` uses it as the
        ``expected_delivery`` (clamped to ≥ now) instead of recomputing from
        ``now + lead_days``.  Prevents the plan's promised delivery date from
        drifting when the order is actually executed (A4).

        ``delivery_charge`` — fixed delivery fee for this supplier order; added
        to ``total_cost`` so PO totals reflect true landed cost.

        ``urgency`` — propagated from the source PlannedOrder rows:
        ``"uncoverable"`` if any source row was uncoverable, ``"at_risk"`` if
        any was late/expedited, else ``None``.  Stored on the PO so the UI can
        show the label on the Ordered section permanently.

        Supplier ``volume_discount`` tiers and per-item catalog ``discount``
        tiers are applied to ``total_cost`` so the stored PO total equals the
        true landed cost the optimizer minimises against.
        """
        now = self.sim_time
        goods_total = self._discounted_goods_total(supplier_id, lines)
        raw_goods = sum(float(l["qty"]) * float(l["unit_price"] or 0.0) for l in lines)
        delivery = float(delivery_charge or 0.0)
        # Honour a live free-delivery promo: waive the fee when the order value
        # clears the promo's spend threshold (same gate the plan MILP used).
        if delivery > 0 and self._free_delivery_applies(supplier_id, raw_goods):
            delivery = 0.0
        total = goods_total + delivery

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
                urgency=urgency or None,
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
                        planned_order_id=line.get("planned_order_id"),
                    )
                )
            session.commit()
            session.refresh(po)
            po_id = po.id
            session.expunge(po)
        finally:
            session.close()

        # Use the runtime-configurable threshold if available, else fall back to config.
        _threshold = (
            self.approval_policy.approval_threshold
            if self.approval_policy is not None
            else float(config.APPROVAL_PO_THRESHOLD)
        )
        if total > _threshold and self.approvals is not None:
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
            self._place(po_id, planned_delivery=planned_delivery)

        return po

    # -- place (auto or post-approval) ---------------------------------------

    def place(self, po_id: int) -> None:
        """Place a PO that was awaiting approval (called by approval handlers)."""
        self._place(po_id)

    def _place(self, po_id: int, planned_delivery: Optional[float] = None) -> None:
        now = self.sim_time
        session = self.db_session_factory()
        try:
            po = session.get(PurchaseOrder, po_id)
            if po is None or po.status not in ("proposed",):
                return
            supplier = session.get(Supplier, po.supplier_id)
            lead_days = float(supplier.lead_time_days or 1.0) if supplier is not None else 1.0
            # A4: Use the plan's promised delivery date when provided so the PO's
            # expected_delivery matches what the plan showed.  Clamp to ≥ now so the
            # deadline trigger fires at a valid future time.
            if planned_delivery is not None:
                expected_delivery = max(float(planned_delivery), now)
            else:
                expected_delivery = now + lead_days * 86400.0
            po.status = "placed"
            po.expected_delivery = expected_delivery

            lines = (
                session.query(PurchaseOrderLine)
                .filter(PurchaseOrderLine.po_id == po_id)
                .all()
            )

            # Decrement order-bounded SupplierTerms — but only those this order
            # actually USES (goods value ≥ the term's spend threshold).  A small
            # order below a promo's MOV gets none of its benefit and must not
            # burn one of its remaining uses.
            po_goods = sum(float(l.line_total or 0.0) for l in lines)
            order_terms = (
                session.query(SupplierTerm)
                .filter(
                    SupplierTerm.supplier_id == po.supplier_id,
                    SupplierTerm.status == "active",
                    SupplierTerm.expiry_kind == "orders",
                )
                .all()
            )
            for ot in order_terms:
                if po_goods < float(ot.min_order_value or 0.0):
                    continue  # order didn't qualify for this promo — keep its uses
                if ot.remaining_orders is not None and ot.remaining_orders > 0:
                    ot.remaining_orders -= 1
                    if ot.remaining_orders <= 0:
                        ot.status = "expired"

            session.commit()
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

    # -- cross-sweep consolidation (WS4) ------------------------------------

    def add_lines_to_po(
        self,
        po_id: int,
        lines: List[Dict[str, Any]],
        created_by: str = "optimizer",
    ) -> bool:
        """Append new lines to an existing PO without adding a second delivery fee.

        Used when ``execute_due_planned_orders`` finds an open PO for the same
        (supplier_id, delivery_day) so all items going to the same supplier on
        the same day share one consolidated delivery.

        Re-computes ``total_cost`` as goods_subtotal (with discounts) plus the
        original delivery charge already embedded in the PO.  Returns ``True``
        when the lines were successfully appended, ``False`` on error or if the
        PO no longer exists / is already delivered/cancelled.
        """
        session = self.db_session_factory()
        try:
            po = session.get(PurchaseOrder, po_id)
            if po is None or po.status in ("delivered", "cancelled"):
                return False
            supplier_id = int(po.supplier_id)

            # Derive the existing delivery charge from the current total_cost by
            # subtracting the goods portion.  Re-add it unchanged so the delivery fee
            # is only ever charged once regardless of how many sweeps add lines.
            existing_lines = (
                session.query(PurchaseOrderLine)
                .filter(PurchaseOrderLine.po_id == po_id)
                .all()
            )
            existing_goods = sum(float(l.line_total or 0.0) for l in existing_lines)
            delivery_charge = float(po.total_cost or 0.0) - existing_goods

            for line in lines:
                session.add(
                    PurchaseOrderLine(
                        po_id=po_id,
                        ingredient_id=line["ingredient_id"],
                        qty=float(line["qty"]),
                        unit=line.get("unit") or "each",
                        unit_price=float(line["unit_price"] or 0.0),
                        line_total=float(line["qty"]) * float(line["unit_price"] or 0.0),
                        planned_order_id=line.get("planned_order_id"),
                    )
                )

            # Recompute total cost: all goods (existing + new) with discounts + delivery.
            all_lines = [
                {"ingredient_id": l.ingredient_id, "qty": float(l.qty), "unit_price": float(l.unit_price or 0.0)}
                for l in existing_lines
            ] + lines
            new_goods = self._discounted_goods_total(supplier_id, all_lines)
            # Merged lines may now clear a free-delivery promo threshold.
            raw_goods = sum(float(l["qty"]) * float(l["unit_price"] or 0.0) for l in all_lines)
            if delivery_charge > 0 and self._free_delivery_applies(supplier_id, raw_goods):
                delivery_charge = 0.0
            po.total_cost = new_goods + delivery_charge

            session.commit()
            return True
        except Exception:  # noqa: BLE001
            session.rollback()
            return False
        finally:
            session.close()

    # -- delivery (§B4.4) -----------------------------------------------------

    def _deliver(self, po_id: int) -> None:
        now = self.sim_time
        session = self.db_session_factory()
        try:
            po = session.get(PurchaseOrder, po_id)
            if po is None or po.status != "placed":
                return
            supplier_id = po.supplier_id
            expected = float(po.expected_delivery or now)
            po.status = "delivered"
            session.commit()
        finally:
            session.close()

        self.ledger.receive(po_id)
        self.log_event(
            "po_delivered", f"PO #{po_id} delivered.", {"po_id": po_id}
        )

        # Track late deliveries for sourcing reliability scoring.
        late_by = now - expected
        if late_by > 3600.0:  # >1 sim-hour grace period
            self._record_supplier_reliability(supplier_id, late_by, now)

    def _record_supplier_reliability(
        self, supplier_id: int, late_by: float, now: float
    ) -> None:
        """Update InventoryOptimizerMemory with rolling late-delivery stats."""
        scope_ref = str(supplier_id)
        session = self.db_session_factory()
        try:
            existing = (
                session.query(InventoryOptimizerMemory)
                .filter(
                    InventoryOptimizerMemory.scope_type == "supplier",
                    InventoryOptimizerMemory.scope_ref == scope_ref,
                )
                .first()
            )
            if existing is not None and isinstance(existing.insight, dict):
                prev = existing.insight
                late_count = int(prev.get("late_delivery_count", 0)) + 1
                total_late = float(prev.get("total_late_seconds", 0.0)) + late_by
                # Reliability score: simple exponential decay — each late delivery
                # reduces reliability by ~10%, min 0.1.
                reliability = max(0.1, float(prev.get("reliability_score", 1.0)) * 0.90)
                existing.insight = {
                    "late_delivery_count": late_count,
                    "total_late_seconds": total_late,
                    "last_late_at": now,
                    "reliability_score": round(reliability, 3),
                }
                existing.last_seen_at = now
            else:
                session.add(InventoryOptimizerMemory(
                    scope_type="supplier",
                    scope_ref=scope_ref,
                    insight={
                        "late_delivery_count": 1,
                        "total_late_seconds": late_by,
                        "last_late_at": now,
                        "reliability_score": 0.90,
                    },
                    evidence=None,
                    confidence=0.9,
                    created_at=now,
                    last_seen_at=now,
                    valid_until=None,
                    source="procurement",
                ))
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()
