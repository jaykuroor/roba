from core.models import (
    Call,
    CompetitorIntel,
    CompetitorMenuSnapshot,
    CompetitorObservation,
    CompetitorOffer,
    CompetitorProbeResult,
)
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


def test_passive_monitor_flags_offer_changes(bus, session_factory, seeded):
    agent = CompetitorAgent(bus, session_factory)

    first = agent.passive_monitor()
    assert first[0]["offers_changed"] is False

    session = session_factory()
    try:
        offer = session.get(CompetitorOffer, 1)
        offer.price = 9.99
        offer.updated_at = 123.0
        session.commit()
    finally:
        session.close()

    second = agent.passive_monitor()
    mario = next(row for row in second if row["competitor_id"] == 1)
    assert mario["offers_changed"] is True


def test_poll_aggregators_persists_and_emits_market_signals(bus, session_factory, seeded):
    bus.sim_time = 34200.0
    agent = CompetitorAgent(bus, session_factory)

    observations = agent.poll_aggregators()

    assert observations
    assert any(row["signal_kind"] in {"eta_spike", "promo_started", "item_sold_out"} for row in observations)
    live = bus.live(type=SignalType.COMPETITOR_MARKET_SIGNAL)
    assert live

    session = session_factory()
    try:
        assert session.query(CompetitorObservation).count() == len(observations)
    finally:
        session.close()


def test_refresh_menu_records_snapshot_and_offer_history(bus, session_factory, seeded):
    agent = CompetitorAgent(bus, session_factory)

    result = agent.refresh_menu(1)

    assert result["competitor_id"] == 1
    assert result["compliance"]["robots_checked"] is True

    session = session_factory()
    try:
        assert session.query(CompetitorMenuSnapshot).count() == 1
        assert session.query(CompetitorOffer).filter(CompetitorOffer.competitor_id == 1).count() >= 1
    finally:
        session.close()


def test_probe_persists_result_and_market_signal(bus, session_factory, seeded):
    bus.sim_time = 39600.0
    agent = CompetitorAgent(bus, session_factory)

    result = agent.run_probe(1)

    assert result["estimated_wait_min"] >= 0
    assert result["observations"]
    assert bus.live(type=SignalType.COMPETITOR_MARKET_SIGNAL)

    session = session_factory()
    try:
        assert session.query(CompetitorProbeResult).count() == 1
    finally:
        session.close()
