from core.models import Batch, Forecast
from core.signals import SignalType
from track_a.agents.forecaster import DemandForecaster


def test_forecast_applies_multipliers_and_explains(bus, session_factory, seeded):
    bus.emit(
        SignalType.USER_FACT,
        {
            "intent": "add_event",
            "entity_type": "event",
            "entity_ref": "parade",
            "attribute": "demand_multiplier",
            "value": 1.35,
            "effective_window": {"start": 28800.0, "end": 39600.0},
            "raw_text": "parade today",
        },
        source="test",
    )
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.baseline_qty == 10.0
        assert stored.multipliers["event"] == 1.35
        assert stored.multipliers["weather"] == 1.1
        assert stored.forecast_qty > stored.baseline_qty
        assert stored.confidence > 0
    finally:
        session.close()


def test_batch_skip_truth_table_for_stockout_and_staff(bus, session_factory, seeded):
    bus.emit(
        SignalType.STOCKOUT_RISK,
        {"ingredient_id": 1, "on_hand": 1.0, "projected_runout": 30000.0, "affected_items": [1]},
        source="test",
    )
    bus.emit(
        SignalType.STAFF_COVERAGE,
        {"station_id": 2, "covered": False, "affected_items": [2], "shortfall": 1.0},
        source="test",
        ttl=3600.0,
    )
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        decisions = {row.menu_item_id: row.decision for row in session.query(Batch).all()}
        assert decisions[1] == "skip"
        assert decisions[2] == "skip"
    finally:
        session.close()
