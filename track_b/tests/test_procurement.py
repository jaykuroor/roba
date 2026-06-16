"""Tests for the Procurement service (02 §B9 / §B4.4): proposed -> approved ->
placed -> delivered -> receive."""

import pytest

from core import config
from core.models import PurchaseOrder, PurchaseOrderLine, Supplier
from core.orchestrator import Orchestrator
from core.signals import SignalType
from track_b.procurement.procurement import Procurement


class _FakeApprovals:
    def __init__(self):
        self.created = []
        self._next_id = 1

    def create(self, type, title, summary, payload=None, urgency="normal", ref_id=None):
        from types import SimpleNamespace
        approval_id = self._next_id
        self._next_id += 1
        self.created.append({"type": type, "ref_id": ref_id})
        return SimpleNamespace(id=approval_id)


class _FakeLedger:
    def __init__(self):
        self.received = []

    def receive(self, po_id):
        self.received.append(po_id)


def _seed_supplier(session_factory, lead_time_days=2.0):
    session = session_factory()
    try:
        sup = Supplier(name="GreenFarm", lead_time_days=lead_time_days, reliability_score=0.9,
                        min_order_value=0.0, contact="")
        session.add(sup)
        session.commit()
        return sup.id
    finally:
        session.close()


class _Clock:
    """Minimal clock stub: only what Orchestrator needs at construction time."""

    def attach_orchestrator(self, orchestrator):
        pass


@pytest.fixture
def orchestrator(bus, session_factory):
    return Orchestrator(_Clock(), bus, session_factory)


def test_auto_place_below_threshold(bus, session_factory, orchestrator):
    supplier_id = _seed_supplier(session_factory, lead_time_days=2.0)
    ledger = _FakeLedger()
    proc = Procurement(bus, session_factory, orchestrator, ledger, approvals=_FakeApprovals())

    bus.sim_time = 0.0
    total = 10.0  # well under APPROVAL_PO_THRESHOLD
    po = proc.create_po(
        supplier_id=supplier_id,
        lines=[{"ingredient_id": 1, "qty": 10.0, "unit": "g", "unit_price": total / 10.0}],
    )

    session = session_factory()
    try:
        row = session.get(PurchaseOrder, po.id)
        assert row.status == "placed"
        assert row.expected_delivery == pytest.approx(2.0 * 86400.0)
    finally:
        session.close()

    placed = bus.live(type=SignalType.REORDER_PLACED)
    assert len(placed) == 1
    assert placed[0].payload["po_id"] == po.id


def test_over_threshold_routes_to_approval_and_waits(bus, session_factory, orchestrator):
    supplier_id = _seed_supplier(session_factory)
    ledger = _FakeLedger()
    approvals = _FakeApprovals()
    proc = Procurement(bus, session_factory, orchestrator, ledger, approvals=approvals)

    bus.sim_time = 0.0
    big_qty = (config.APPROVAL_PO_THRESHOLD / 1.0) + 100.0
    po = proc.create_po(
        supplier_id=supplier_id,
        lines=[{"ingredient_id": 1, "qty": big_qty, "unit": "g", "unit_price": 1.0}],
    )

    session = session_factory()
    try:
        row = session.get(PurchaseOrder, po.id)
        assert row.status == "proposed"
        assert row.approval_id is not None
    finally:
        session.close()

    assert len(approvals.created) == 1
    assert approvals.created[0]["type"] == "purchase_order"
    assert approvals.created[0]["ref_id"] == po.id
    assert bus.live(type=SignalType.REORDER_PLACED) == []


def test_place_after_approval_then_delivery_calls_ledger(bus, session_factory, orchestrator):
    supplier_id = _seed_supplier(session_factory, lead_time_days=1.0)
    ledger = _FakeLedger()
    approvals = _FakeApprovals()
    proc = Procurement(bus, session_factory, orchestrator, ledger, approvals=approvals)

    bus.sim_time = 0.0
    big_qty = (config.APPROVAL_PO_THRESHOLD / 1.0) + 100.0
    po = proc.create_po(
        supplier_id=supplier_id,
        lines=[{"ingredient_id": 1, "qty": big_qty, "unit": "g", "unit_price": 1.0}],
    )

    proc.place(po.id)

    session = session_factory()
    try:
        row = session.get(PurchaseOrder, po.id)
        assert row.status == "placed"
        expected_delivery = row.expected_delivery
    finally:
        session.close()
    assert bus.live(type=SignalType.REORDER_PLACED)

    # Advance the clock past the delivery deadline and run the orchestrator's
    # tick-internal deadline firing directly (no full clock loop needed).
    bus.sim_time = expected_delivery + 1.0
    orchestrator._fire_deadline_triggers(bus.sim_time, jumped=False, window_start=None)

    session = session_factory()
    try:
        row = session.get(PurchaseOrder, po.id)
        assert row.status == "delivered"
    finally:
        session.close()
    assert ledger.received == [po.id]
