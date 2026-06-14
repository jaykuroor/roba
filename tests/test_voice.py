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
from core.voice import VoiceProcessor


@pytest.fixture
def seeded(bus, session_factory, monkeypatch):
    """An in-memory DB loaded with the Bella's Kitchen preset + a voice
    processor whose LLM has no API keys (so it uses the regex fallback)."""
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    llm = LLMProvider()
    llm._sleep = lambda *_a, **_k: None
    Seeder(llm, session_factory).load_preset("bellas_kitchen")
    bus.sim_time = 0.0  # day 0 = Monday
    return VoiceProcessor(llm, bus, session_factory), session_factory


def test_set_leave_writes_attendance(seeded):
    """Gate 2: a leave fact extracts intent set_leave and writes 7 structured
    attendance rows (Mon–Sun next week) plus one display-only event_log row."""
    voice, session_factory = seeded
    result = voice.process("Ansi is on leave the whole next week")

    assert result["extracted"]["intent"] == "set_leave"
    assert result["signal_id"] is not None
    assert any(w.startswith("attendance:") for w in result["resulting_writes"])
    assert any(w.startswith("event_log:") for w in result["resulting_writes"])

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
    """A 'sick' fact records status='sick' in attendance."""
    voice, session_factory = seeded
    voice.process("Marco is off sick today")
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
    """Gate 3: a receipt fact writes an InventoryLot + a receipt ledger row."""
    voice, session_factory = seeded
    result = voice.process(
        "We received 20 kg of tomatoes from GreenFarm at 2 dollars a kilo"
    )

    assert result["extracted"]["intent"] == "record_receipt"

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
