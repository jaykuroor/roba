"""Unit tests for the signal bus (§14): dedup, sweep, and payload validation."""

import json

import pytest

from core.models import Signal, SignalDelivery
from core.clock import SimClock
from core.orchestrator import Orchestrator
from core.signals import LowStockPayload, SignalType


def _low_stock(on_hand=5.0, projected_runout=50000.0):
    return {
        "ingredient_id": 1,
        "on_hand": on_hand,
        "threshold": 10.0,
        "projected_runout": projected_runout,
        "unit": "g",
    }


def test_emit_dedup_refreshes_single_row(bus, session_factory):
    """Same dedup_key, changed payload -> one row with refreshed expires_at."""
    bus.emit(SignalType.LOW_STOCK, _low_stock(on_hand=5.0),
             source="test", dedup_key="low:tomato", now=100.0)
    bus.emit(SignalType.LOW_STOCK, _low_stock(on_hand=3.0),
             source="test", dedup_key="low:tomato", now=200.0)

    session = session_factory()
    try:
        rows = session.query(Signal).filter(Signal.dedup_key == "low:tomato").all()
    finally:
        session.close()

    assert len(rows) == 1
    assert rows[0].payload["on_hand"] == 3.0
    assert rows[0].expires_at == 200.0 + 14400.0  # LOW_STOCK TTL = 4h


def test_sweep_expires_live_signals(bus):
    """Two live signals are both expired by a sweep past their TTL."""
    bus.emit(SignalType.WASTE_EVENT,
             {"waste_type": "spoilage", "qty": 1.0, "unit": "g",
              "cost": 1.0, "reason": "x"},
             source="t", dedup_key="k1", now=10.0)
    bus.emit(SignalType.WASTE_EVENT,
             {"waste_type": "spoilage", "qty": 2.0, "unit": "g",
              "cost": 2.0, "reason": "y"},
             source="t", dedup_key="k2", now=10.0)

    assert len(bus.live()) == 2
    bus.sweep(now=99999.0)
    assert bus.live() == []


def test_subscribe_fires_callback_on_emit(bus):
    """A subscriber for a type is invoked (synchronously) with the written
    signal on every emit of that type, and not for other types."""
    received = []
    bus.subscribe(SignalType.LOW_STOCK, lambda sig: received.append(sig))

    # An emit of a different type must not trigger the LOW_STOCK subscriber.
    bus.emit(SignalType.WASTE_EVENT,
             {"waste_type": "spoilage", "qty": 1.0, "unit": "g",
              "cost": 1.0, "reason": "x"}, source="t", now=1.0)
    assert received == []

    signal = bus.emit(SignalType.LOW_STOCK, _low_stock(), source="t", now=2.0)
    assert len(received) == 1
    assert received[0].signal_id == signal.signal_id
    assert received[0].type == SignalType.LOW_STOCK.value


def test_multiple_subscribers_all_fire_and_isolate_failures(bus, session_factory):
    """Multiple subscribers per type all fire in order; a raising subscriber
    does not stop siblings or break the emit."""
    order = []
    bus.subscribe(SignalType.LOW_STOCK, lambda _s: order.append("a"))
    bus.subscribe(SignalType.LOW_STOCK, lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe(SignalType.LOW_STOCK, lambda _s: order.append("c"))

    signal = bus.emit(SignalType.LOW_STOCK, _low_stock(), source="t", now=1.0)

    assert signal is not None  # emit still succeeds
    assert order == ["a", "c"]  # the failing subscriber is isolated

    session = session_factory()
    try:
        deliveries = (
            session.query(SignalDelivery)
            .filter(SignalDelivery.signal_id == signal.signal_id)
            .order_by(SignalDelivery.id.asc())
            .all()
        )
    finally:
        session.close()
    assert [row.status for row in deliveries] == ["ack", "failed", "ack"]
    assert all(row.delivery_kind == "subscriber" for row in deliveries)


def test_subscribe_is_idempotent(bus):
    """Registering the same callback twice must still dispatch it once."""
    fired = []

    def callback(signal):
        fired.append(signal.signal_id)

    bus.subscribe(SignalType.LOW_STOCK, callback)
    bus.subscribe(SignalType.LOW_STOCK, callback)

    signal = bus.emit(SignalType.LOW_STOCK, _low_stock(), source="t", now=1.0)

    assert fired == [signal.signal_id]


def test_orchestrator_records_dead_letter_when_unrouted(bus, session_factory):
    """No matching agent capability becomes an auditable dead-letter row."""
    orchestrator = Orchestrator(SimClock(session_factory, bus), bus, session_factory)
    signal = bus.emit(SignalType.LOW_STOCK, _low_stock(), source="t", now=1.0)

    orchestrator.on_signal(signal)

    session = session_factory()
    try:
        row = (
            session.query(SignalDelivery)
            .filter(
                SignalDelivery.signal_id == signal.signal_id,
                SignalDelivery.delivery_kind == "dead_letter",
            )
            .one()
        )
    finally:
        session.close()
    assert row.status == "unrouted"
    assert row.consumer == "orchestrator"


def test_dedup_refresh_does_not_refire_subscribers(bus, session_factory):
    """A dedup-refresh (same key, changed payload) updates the live signal in
    place but does NOT re-invoke subscribers — reactors fire once per genuine
    emit, never on a refresh (§14.3)."""
    fired = []
    bus.subscribe(SignalType.LOW_STOCK, lambda s: fired.append(s.payload["on_hand"]))

    # New insert -> fires.
    bus.emit(SignalType.LOW_STOCK, _low_stock(on_hand=5.0),
             source="t", dedup_key="k", now=1.0)
    # Same dedup_key, materially changed payload -> refresh path, no re-fire.
    bus.emit(SignalType.LOW_STOCK, _low_stock(on_hand=3.0),
             source="t", dedup_key="k", now=2.0)

    assert fired == [5.0]

    # The single live row still got the refreshed payload (broadcast/state path
    # is unaffected — only the reactor dispatch is gated).
    session = session_factory()
    try:
        rows = session.query(Signal).filter(Signal.dedup_key == "k").all()
    finally:
        session.close()
    assert len(rows) == 1
    assert rows[0].payload["on_hand"] == 3.0


def test_identical_dedup_emit_does_not_fire_subscribers(bus):
    """A materially-identical dedup emit is a no-op and fires no subscribers."""
    fired = []
    bus.subscribe(SignalType.LOW_STOCK, lambda s: fired.append(s))
    bus.emit(SignalType.LOW_STOCK, _low_stock(on_hand=5.0),
             source="t", dedup_key="k", now=1.0)
    bus.emit(SignalType.LOW_STOCK, _low_stock(on_hand=5.0),
             source="t", dedup_key="k", now=2.0)
    assert len(fired) == 1  # only the first (new-insert) emit


def test_distinct_emits_each_fire_subscribers(bus):
    """Distinct emits (no dedup_key) each fire subscribers — the
    APPROVAL_RESOLVED case, where every resolution is a separate event."""
    fired = []
    bus.subscribe(SignalType.LOW_STOCK, lambda s: fired.append(s.signal_id))
    a = bus.emit(SignalType.LOW_STOCK, _low_stock(), source="t", now=1.0)
    b = bus.emit(SignalType.LOW_STOCK, _low_stock(), source="t", now=2.0)
    assert fired == [a.signal_id, b.signal_id]
    assert a.signal_id != b.signal_id


def test_valid_dict_payload_passes_through_unchanged(bus):
    """A valid dict payload is stored verbatim."""
    payload = _low_stock()
    signal = bus.emit(SignalType.LOW_STOCK, dict(payload),
                      source="t", dedup_key="ok", now=1.0)
    assert signal.payload == payload


def test_model_instance_payload_accepted(bus):
    """A pydantic model instance is accepted and stored as its dict form."""
    payload = _low_stock()
    signal = bus.emit(SignalType.LOW_STOCK, LowStockPayload(**payload),
                      source="t", dedup_key="model", now=1.0)
    assert signal.payload == payload


def test_missing_required_field_raises(bus):
    """A payload missing a required field raises ValueError naming type+field."""
    bad = {"ingredient_id": 1, "on_hand": 5.0, "threshold": 10.0, "unit": "g"}
    with pytest.raises(ValueError) as exc:
        bus.emit(SignalType.LOW_STOCK, bad, source="t", now=1.0)
    message = str(exc.value)
    assert "LOW_STOCK" in message
    assert "projected_runout" in message


@pytest.mark.parametrize("sig_type, payload", [
    (
        SignalType.STOCKOUT_RISK,
        {"ingredient_id": 7, "on_hand": 2.0, "projected_runout": 3000.0,
         "affected_items": [10, 11, 12]},
    ),
    (
        SignalType.STOCKOUT_RISK,
        {"ingredient_id": 7, "on_hand": 2.0, "projected_runout": 3000.0,
         "affected_items": []},
    ),
    (
        SignalType.DEMAND_FORECAST,
        {"menu_item_id": 3, "window": {"start": 0.0, "end": 3600.0},
         "daypart": "lunch", "qty": 12.0, "baseline": 10.0,
         "multipliers": {"weather": 1.1, "event": 1.35}, "confidence": 0.8},
    ),
    (
        SignalType.DEMAND_FORECAST,
        {"menu_item_id": 3, "window": {"start": 0.0, "end": 3600.0},
         "daypart": "lunch", "qty": 12.0, "baseline": 10.0,
         "multipliers": {}, "confidence": 0.8},
    ),
])
def test_nested_payload_round_trips_json_native(bus, session_factory, sig_type, payload):
    """Payloads with nested dicts/lists store as JSON-native and round-trip."""
    signal = bus.emit(sig_type, dict(payload), source="t",
                      dedup_key=None, now=1.0)

    session = session_factory()
    try:
        stored = session.get(Signal, signal.signal_id).payload
    finally:
        session.close()

    assert stored == payload
    # The stored dict must be serializable with the stdlib encoder (no custom).
    assert json.loads(json.dumps(stored)) == payload


def test_wrong_type_field_raises(bus):
    """A payload with a wrong-typed field raises ValueError naming type+field."""
    bad = {"ingredient_id": "not-an-int", "on_hand": 5.0, "threshold": 10.0,
           "projected_runout": 1.0, "unit": "g"}
    with pytest.raises(ValueError) as exc:
        bus.emit(SignalType.LOW_STOCK, bad, source="t", now=1.0)
    message = str(exc.value)
    assert "LOW_STOCK" in message
    assert "ingredient_id" in message
