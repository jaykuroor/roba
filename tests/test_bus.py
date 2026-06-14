"""Unit tests for the signal bus (§14): dedup, sweep, and payload validation."""

import json

import pytest

from core.models import Signal
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
