"""Tests for the approval handlers (02 §B9 / §B4.5)."""

from types import SimpleNamespace

from core.signals import SignalType
from track_b.approval.handlers import ApprovalHandlers


class _FakeProcurement:
    def __init__(self):
        self.placed = []

    def place(self, po_id):
        self.placed.append(po_id)


class _FakeOptimizer:
    def __init__(self):
        self.activated = []

    def activate_promo(self, promo_id):
        self.activated.append(promo_id)


def _signal(approval_type, decision, ref_id):
    return SimpleNamespace(
        type=SignalType.APPROVAL_RESOLVED.value,
        payload={"approval_id": 1, "type": approval_type, "decision": decision, "ref_id": ref_id, "payload": {}},
    )


def test_approved_purchase_order_dispatches_to_procurement(bus):
    procurement = _FakeProcurement()
    optimizer = _FakeOptimizer()
    handlers = ApprovalHandlers(bus, procurement, optimizer)

    handlers.on_resolved(_signal("purchase_order", "approved", 42))
    assert procurement.placed == [42]
    assert optimizer.activated == []


def test_approved_promo_dispatches_to_optimizer(bus):
    procurement = _FakeProcurement()
    optimizer = _FakeOptimizer()
    handlers = ApprovalHandlers(bus, procurement, optimizer)

    handlers.on_resolved(_signal("promo", "approved", 7))
    assert optimizer.activated == [7]
    assert procurement.placed == []


def test_rejected_does_nothing(bus):
    procurement = _FakeProcurement()
    optimizer = _FakeOptimizer()
    handlers = ApprovalHandlers(bus, procurement, optimizer)

    handlers.on_resolved(_signal("purchase_order", "rejected", 42))
    handlers.on_resolved(_signal("promo", "rejected", 7))
    assert procurement.placed == []
    assert optimizer.activated == []


def test_non_track_b_types_ignored(bus):
    procurement = _FakeProcurement()
    optimizer = _FakeOptimizer()
    handlers = ApprovalHandlers(bus, procurement, optimizer)

    handlers.on_resolved(_signal("outbound_call", "approved", 99))
    handlers.on_resolved(_signal("menu_change", "approved", 100))
    assert procurement.placed == []
    assert optimizer.activated == []


def test_wired_via_bus_subscribe(bus, session_factory):
    """The handler reacts to a real APPROVAL_RESOLVED emitted on the bus, not
    just direct calls — proving the bus.subscribe wiring works end-to-end."""
    procurement = _FakeProcurement()
    optimizer = _FakeOptimizer()
    ApprovalHandlers(bus, procurement, optimizer)

    bus.emit(
        SignalType.APPROVAL_RESOLVED,
        {"approval_id": 1, "type": "purchase_order", "decision": "approved", "ref_id": 5, "payload": {}},
        source="human",
    )
    assert procurement.placed == [5]
