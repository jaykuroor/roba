import pytest

from core.models import Batch, DemandForecasterMemory, Forecast, SimSettings
from core.signals import SignalType
from track_a.agents.forecaster import DemandForecaster


class FakeSuggestionLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
        return {
            "summary": "resize_batches",
            "suggestions": [
                {
                    "action": "resize",
                    "menu_item_id": 1,
                    "reason": "demand above baseline",
                }
            ],
        }


class FakeOptimizerLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
        if use_site == "forecaster_optimization":
            return {
                "item_adjustments": [
                    {
                        "menu_item_id": 1,
                        "multipliers": {"weather": 2.0},
                        "hard_override_qty": None,
                        "reason": "Cold rain strongly favors pizza.",
                        "confidence": 0.9,
                    }
                ],
                "global_notes": [],
                "memory_updates": [],
                "confidence": 0.9,
            }
        return {"suggestions": [], "summary": "no_change"}


class FakeTargetForecastLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
        return {
            "item_adjustments": [
                {
                    "menu_item_id": 1,
                    "forecast": 3.6,
                    "reason": "Storm conditions soften dine-in demand.",
                    "confidence": 0.82,
                }
            ],
            "global_notes": ["Storm impact applied to the forecast."],
            "memory_updates": ["Cold storm pattern lowered demand for this run."],
            "confidence": 0.82,
        }


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
        assert stored.multipliers["settings_demand"] == 1.2
        assert stored.multipliers["event"] == 1.35
        assert stored.multipliers["weather"] == 1.18
        assert stored.forecast_qty > stored.baseline_qty
        assert stored.confidence > 0
    finally:
        session.close()


def test_forecast_reflects_sim_settings_over_historical_baseline(bus, session_factory, seeded):
    session = session_factory()
    try:
        settings = session.get(SimSettings, 1)
        settings.base_orders_per_day = 200
        settings.velocity = 1.0
        settings.dish_mix_weights = {"1": 3.0, "2": 1.0}
        settings.daypart_curve = {"breakfast": 0.5}
        session.commit()
    finally:
        session.close()

    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.baseline_qty == 10.0
        assert stored.multipliers["settings_demand"] == pytest.approx(7.5)
        assert stored.forecast_qty == 89
    finally:
        session.close()


def test_forecast_prorates_to_remaining_window(bus, session_factory, seeded):
    bus.sim_time = 34200.0  # 09:30, halfway through the 08:00-11:00 breakfast window.

    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.window == {"start": 34200.0, "end": 39600.0}
        assert stored.baseline_qty == 5.0
        assert stored.multipliers["settings_demand"] == pytest.approx(1.2)
        assert stored.forecast_qty == 7
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


def test_forecaster_suggestions_use_llm(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeSuggestionLLM())
    agent.run_forecast("test")
    result = agent.generate_suggestions()
    assert result["summary"] == "resize_batches"
    assert result["suggestions"][0]["action"] == "resize"


def test_manual_llm_optimization_writes_memory(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeOptimizerLLM())
    agent.optimize_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.multipliers["weather"] == 2.0
        assert stored.forecast_qty == 24
        memories = session.query(DemandForecasterMemory).all()
        assert any(row.source == "llm" for row in memories)
    finally:
        session.close()


def test_manual_llm_optimization_accepts_direct_forecast_targets(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeTargetForecastLLM())
    agent.optimize_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.forecast_qty == 4
        assert stored.multipliers["llm_target"] == 1.0
        memories = session.query(DemandForecasterMemory).all()
        assert any("Cold storm pattern" in str(row.insight) for row in memories)
    finally:
        session.close()
