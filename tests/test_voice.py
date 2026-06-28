"""Tests for the voice intake pipeline (§11) — gates 2 & 3."""

import pytest

from core.llm import LLMProvider
from core.models import (
    Attendance,
    EventLog,
    InventoryLedger,
    InventoryLot,
    UserFact,
)
from core.seeding import Seeder
from core.signals import SignalType
from core.voice import VoiceProcessor
from track_a.agents.staff import StaffAgent
from track_b.agents.ledger import InventoryLedger as TrackBInventoryLedger


class FakeEventLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
        return {
            "intent": "add_event",
            "entity_type": "event",
            "entity_ref": "food fest",
            "attribute": "demand_multiplier",
            "value": "800",
            "effective_window": {},
            "confidence": 0.95,
        }


class FakeAvailabilityLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
        return {
            "intent": "set_operational_constraint",
            "entity_type": "menu_category",
            "entity_ref": "desserts",
            "attribute": "availability",
            "value": False,
            "effective_window": {},
            "confidence": 0.95,
        }


@pytest.fixture
def seeded(bus, session_factory, monkeypatch):
    """An in-memory DB loaded with the Bella's Kitchen preset + a voice
    processor whose LLM has no API keys (so it uses the regex fallback)."""
    for var in ("GOOGLE_CLOUD_PROJECT",):
        monkeypatch.delenv(var, raising=False)
    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    Seeder(llm, session_factory).load_preset("bellas_kitchen")
    bus.sim_time = 0.0  # day 0 = Monday
    return VoiceProcessor(llm, bus, session_factory), session_factory


def test_set_leave_writes_attendance(seeded):
    """Gate 2: voice routes leave to StaffAgent, which owns attendance writes."""
    voice, session_factory = seeded
    result = voice.process("Ansi is on leave the whole next week")

    assert result["extracted"]["intent"] == "set_leave"
    assert result["signal_id"] is not None
    assert result["routes"][0]["signal_type"] == SignalType.STAFF_AVAILABILITY.value
    assert any(
        w.startswith(f"signal:{SignalType.STAFF_AVAILABILITY.value}:")
        for w in result["resulting_writes"]
    )

    routed_signal = voice.bus.live(type=SignalType.STAFF_AVAILABILITY)[0]
    StaffAgent(voice.bus, session_factory).on_signal(routed_signal)

    session = session_factory()
    try:
        # Structured, queryable truth lives in the attendance table.
        attendance = session.query(Attendance).all()
        assert len(attendance) == 7  # Mon–Sun next week
        assert all(a.status == "leave" for a in attendance)
        assert all(a.daypart is None for a in attendance)
        days = sorted(a.date_sim_day for a in attendance)
        assert days == list(range(days[0], days[0] + 7))  # 7 consecutive days

        # Exactly one human-readable narrative row (display only).
        logs = session.query(EventLog).filter(EventLog.category == "attendance").all()
        assert len(logs) == 1
        assert "Ansi" in (logs[0].summary or "")

        assert session.query(UserFact).count() == 1
    finally:
        session.close()


def test_set_sick_writes_sick_status(seeded):
    """A 'sick' fact routes to StaffAgent, which records status='sick'."""
    voice, session_factory = seeded
    result = voice.process("Marco is off sick today")
    assert result["routes"][0]["signal_type"] == SignalType.STAFF_AVAILABILITY.value
    routed_signal = voice.bus.live(type=SignalType.STAFF_AVAILABILITY)[0]
    StaffAgent(voice.bus, session_factory).on_signal(routed_signal)

    session = session_factory()
    try:
        rows = session.query(Attendance).all()
        assert len(rows) == 1
        assert rows[0].status == "sick"
        # Resolved to the seeded staff member.
        assert rows[0].staff_id is not None
    finally:
        session.close()


def test_record_receipt_writes_lot_and_ledger(seeded):
    """Gate 3: voice routes a receipt to Track B's ledger for durable writes."""
    voice, session_factory = seeded
    result = voice.process(
        "We received 20 kg of tomatoes from GreenFarm at 2 dollars a kilo"
    )

    assert result["extracted"]["intent"] == "record_receipt"
    assert result["routes"][0]["signal_type"] == SignalType.INVENTORY_RECEIPT_REPORTED.value

    routed_signal = voice.bus.live(type=SignalType.INVENTORY_RECEIPT_REPORTED)[0]
    TrackBInventoryLedger(voice.bus, session_factory).on_signal(routed_signal)

    session = session_factory()
    try:
        lots = session.query(InventoryLot).filter(InventoryLot.qty_on_hand == 20.0).all()
        assert len(lots) == 1
        lot = lots[0]
        assert lot.purchase_price == 2.0

        receipts = (
            session.query(InventoryLedger)
            .filter(InventoryLedger.reason == "receipt", InventoryLedger.lot_id == lot.id)
            .all()
        )
        assert len(receipts) == 1
        assert receipts[0].delta_qty == 20.0
    finally:
        session.close()


def test_unrecognised_intent_stores_only(seeded):
    """An unrecognised fact writes the UserFact row only."""
    voice, session_factory = seeded
    result = voice.process("The new napkins look really nice today")
    assert result["resulting_writes"] == ["stored"]
    session = session_factory()
    try:
        assert session.query(UserFact).count() == 1
    finally:
        session.close()


def test_station_absence_is_stored_as_operational_constraint(seeded):
    voice, _session_factory = seeded
    result = voice.process("All the possible staff making pasta are absent today")

    assert result["extracted"]["intent"] == "set_operational_constraint"
    assert result["extracted"]["value"]["all_qualified_staff"] is True
    assert result["signal_id"] is not None


def test_overstock_voice_note_is_operational_forecast_constraint(seeded):
    voice, _session_factory = seeded
    result = voice.process("Desserts are overstocked today")

    assert result["extracted"]["intent"] == "set_operational_constraint"
    assert result["extracted"]["entity_ref"] == "Desserts"
    assert result["extracted"]["attribute"] == "overstock"
    assert result["extracted"]["value"]["action"] == "reduce_forecast"
    assert result["signal_id"] is not None


def test_no_more_desserts_gets_default_unavailable_window(seeded):
    voice, _session_factory = seeded
    voice.bus.sim_time = 38100.0

    result = voice.process("No desserts possible.")

    assert result["extracted"]["intent"] == "set_operational_constraint"
    assert result["extracted"]["entity_ref"] == "desserts"
    assert result["extracted"]["attribute"] == "production_unavailable"
    assert result["extracted"]["value"]["action"] == "halt_production"
    assert result["extracted"]["effective_window"] == {"start": 38100.0, "end": 82800.0}
    assert result["signal_id"] is not None


def test_desserts_are_over_maps_to_dessert_items(seeded):
    voice, _session_factory = seeded
    voice.bus.sim_time = 38100.0

    result = voice.process("Desserts are over for today.")

    value = result["extracted"]["value"]
    assert result["extracted"]["intent"] == "set_operational_constraint"
    assert result["extracted"]["entity_ref"] == "Desserts"
    assert result["extracted"]["attribute"] == "production_unavailable"
    assert value["dependency_type"] == "category"
    assert value["dependency_ref"] == "dessert"
    assert value["affected_menu_item_ids"] == [4]
    assert value["affected_item_names"] == ["Tiramisu"]
    assert result["extracted"]["effective_window"]["end"] == 82800.0


def test_llm_availability_false_normalises_to_production_unavailable(seeded):
    voice, _session_factory = seeded
    voice.llm = FakeAvailabilityLLM()
    voice.bus.sim_time = 29880.0

    result = voice.process("No desserts possible.")

    assert result["extracted"]["intent"] == "set_operational_constraint"
    assert result["extracted"]["entity_ref"] == "desserts"
    assert result["extracted"]["attribute"] == "production_unavailable"
    assert result["extracted"]["value"]["action"] == "halt_production"
    assert result["extracted"]["effective_window"] == {"start": 29880.0, "end": 82800.0}
    assert result["signal_id"] is not None


def test_event_attendance_is_not_stored_as_raw_multiplier(seeded):
    voice, _session_factory = seeded
    voice.llm = FakeEventLLM()

    result = voice.process("Food fest from 21:00 for 800 people")

    assert result["extracted"]["intent"] == "add_event"
    assert result["extracted"]["attribute"] == "expected_attendance"
    assert result["extracted"]["value"] == 800.0


def test_event_fallback_preserves_attendance_and_hour_range(seeded):
    voice, _session_factory = seeded

    result = voice.process("There is a food festival today for 800 people from 9 AM to 11 AM")

    assert result["extracted"]["intent"] == "add_event"
    assert result["extracted"]["attribute"] == "expected_attendance"
    assert result["extracted"]["value"] == 800.0
    assert result["extracted"]["effective_window"] == {"start": 32400.0, "end": 39600.0}


def test_pizza_oven_constraint_targets_only_pizza_oven_items(seeded):
    voice, _session_factory = seeded

    result = voice.process("The pizza oven is broken today.")

    value = result["extracted"]["value"]
    assert result["extracted"]["intent"] == "set_operational_constraint"
    assert result["extracted"]["attribute"] == "production_unavailable"
    assert value["dependency_type"] == "equipment"
    assert value["dependency_ref"] == "pizza oven"
    assert value["affected_menu_item_ids"] == [1]
    assert value["affected_item_names"] == ["Margherita Pizza"]


def test_no_more_bacon_burgers_targets_bacon_items_only(bus, session_factory, monkeypatch):
    for var in ("GOOGLE_CLOUD_PROJECT",):
        monkeypatch.delenv(var, raising=False)
    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    Seeder(llm, session_factory).load_preset("burger_joint")
    bus.sim_time = 38100.0
    voice = VoiceProcessor(llm, bus, session_factory)

    result = voice.process("No more bacon burgers for today.")

    value = result["extracted"]["value"]
    assert result["extracted"]["intent"] == "set_operational_constraint"
    assert result["extracted"]["attribute"] == "production_unavailable"
    assert value["dependency_type"] == "ingredient"
    assert value["dependency_ref"] == "bacon"
    assert value["affected_menu_item_ids"] == [2]
    assert value["affected_item_names"] == ["Bacon Burger"]
