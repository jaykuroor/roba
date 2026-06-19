from core.models import Call, CompetitorIntel
from core.signals import SignalType
from track_a.agents.competitor import CompetitorAgent


def test_discovery_selection_and_call_outcome_write(bus, session_factory, seeded):
    agent = CompetitorAgent(bus, session_factory)
    targets = agent.discover_targets()
    assert [target["id"] for target in targets] == [1]

    session = session_factory()
    try:
        call = Call(agent="competitor_intel", counterparty_type="competitor", counterparty_id=1, purpose="ask favourite dish", status="completed", approval_id=None, transcript=[], outcome=None, started_at=0.0, ended_at=1.0, clock_action="freeze")
        session.add(call)
        session.commit()
        call_id = call.id
    finally:
        session.close()

    signal = bus.emit(
        SignalType.CALL_OUTCOME,
        {"call_id": call_id, "counterparty_type": "competitor", "outcome": {"popular_dishes": ["Margherita Pizza"], "price_points": {"Margherita Pizza": 11.5}}},
        source="calls",
    )
    row = agent.handle_call_outcome(signal)
    assert row is not None
    assert agent.map_popular_to_menu_item("Margherita Pizza") == 1

    session = session_factory()
    try:
        assert session.query(CompetitorIntel).count() == 1
    finally:
        session.close()
