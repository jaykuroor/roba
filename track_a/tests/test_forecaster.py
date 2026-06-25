import pytest

import track_a.forecast_jobs as forecast_jobs_module
from core.models import (
    ApprovalRequest,
    Batch,
    DemandForecasterMemory,
    EventLog,
    Forecast,
    ForecastAdjustment,
    ForecastJob,
    ForecastOverride,
    ForecastTrace,
    MenuItem,
    Signal,
    SimSettings,
)
from core.approvals import ApprovalsHub
from core.llm import CANNED_NOTE
from core.signals import SignalType
from track_a.agents.forecaster import LLM_AUTHORITY_FORECAST, DemandForecaster
from track_a.forecast_jobs import DETERMINISTIC_FORECAST, LLM_FINALIZER, ForecastJobRunner


class FakeSuggestionLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
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
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
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
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
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


class FakeDessertTargetForecastLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
        return {
            "item_adjustments": [
                {
                    "menu_item_id": 3,
                    "forecast": 9,
                    "reason": "Dessert demand is normally strong.",
                    "confidence": 0.86,
                }
            ],
            "global_notes": [],
            "memory_updates": [],
            "confidence": 0.86,
        }


class FakeFinalizerAcceptLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
        return {
            "item_final_forecasts": [
                {
                    "menu_item_id": 1,
                    "final_qty": 4,
                    "confidence": 0.91,
                    "decision": "accept_deterministic",
                    "changed": False,
                    "reason": "Deterministic recommendation is well supported.",
                    "evidence": ["stable deterministic context"],
                }
            ],
            "global_notes": [],
            "memory_updates": [],
            "confidence": 0.91,
        }


class FakeFinalizerUnjustifiedChangeLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
        return {
            "item_final_forecasts": [
                {
                    "menu_item_id": 1,
                    "final_qty": 99,
                    "confidence": 0.95,
                    "decision": "adjust",
                    "changed": True,
                    "reason": "Unexplained demand surge.",
                    "evidence": [],
                }
            ],
            "global_notes": [],
            "memory_updates": [],
            "confidence": 0.95,
        }


class FakeMaterialFinalizerLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
        self.calls += 1
        return {
            "item_final_forecasts": [
                {
                    "menu_item_id": 1,
                    "final_qty": 7,
                    "confidence": 0.88,
                    "decision": "adjust",
                    "changed": True,
                    "reason": "Competitor signal materially changes pizza demand.",
                    "evidence": ["COMPETITOR_INTEL popular_dishes includes Margherita Pizza"],
                }
            ],
            "global_notes": [],
            "memory_updates": [],
            "confidence": 0.88,
        }


class FakeCannedForecastLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
        return {"note": CANNED_NOTE}


class FakeHardZeroIgnoringLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **_kwargs):
        return {
            "item_final_forecasts": [
                {
                    "menu_item_id": 1,
                    "final_qty": 9,
                    "confidence": 0.9,
                    "decision": "adjust",
                    "changed": True,
                    "reason": "Demand is high despite the stockout.",
                    "evidence": ["demand signal"],
                }
            ],
            "global_notes": [],
            "memory_updates": [],
            "confidence": 0.9,
        }


class FailingForecastLLM:
    def complete(self, *_args, **_kwargs):
        raise AssertionError("regular forecasts must not call the remote LLM")


class RecordingForecaster:
    def __init__(self):
        self.calls = []

    def run_forecast(self, *_args, **_kwargs):
        self.calls.append((_args, _kwargs))
        return []


class ExplodingLock:
    def __enter__(self):
        raise AssertionError("forecast computation must not hold DB_LOCK")

    def __exit__(self, *_args):
        return False


def test_auto_mode_normal_forecast_skips_llm_without_material_context(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FailingForecastLLM())
    agent.set_auto_mode(True)
    agent.run_forecast("interval")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored is not None
        assert "llm_finalizer" not in stored.multipliers
        assert "llm_fallback" not in stored.multipliers
    finally:
        session.close()


def test_material_competitor_signal_routes_to_llm_authority(bus, session_factory, seeded):
    bus.emit(
        SignalType.COMPETITOR_INTEL,
        {
            "competitor_id": 1,
            "popular_dishes": ["Margherita Pizza"],
            "price_points": {},
            "method": "call",
            "call_id": 1,
        },
        source="test",
    )
    llm = FakeMaterialFinalizerLLM()
    agent = DemandForecaster(bus, session_factory, llm=llm)
    agent.set_auto_mode(True)
    agent.run_forecast("interval")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        trace = session.query(ForecastTrace).filter(ForecastTrace.forecast_id == stored.id).one()
        assert llm.calls == 1
        assert stored.forecast_qty == 7
        assert stored.multipliers["llm_final"] == 1.0
        assert trace.trace["llm_final_decision"]["decision"] == "adjust"
    finally:
        session.close()


def test_signal_handler_enqueues_llm_authority_job_for_material_signal(bus, session_factory, seeded):
    calls = []
    agent = DemandForecaster(bus, session_factory, llm=FakeMaterialFinalizerLLM())
    agent.set_auto_mode(True)
    agent.set_forecast_job_enqueue(lambda kind, reason: calls.append((kind, reason)))
    signal = bus.emit(
        SignalType.COMPETITOR_UPDATE,
        {"competitor_id": 1, "is_open": False, "offers_changed": False, "summary": "closed"},
        source="test",
    )

    agent.on_signal(signal)

    assert calls == [(LLM_AUTHORITY_FORECAST, "signal:COMPETITOR_UPDATE")]


def test_forecast_job_runner_releases_clock_lock_while_forecasting(
    bus, session_factory, seeded, monkeypatch
):
    forecaster = RecordingForecaster()
    runner = ForecastJobRunner(bus, session_factory, forecaster, approvals=None)
    runner._finish = lambda *_args, **_kwargs: None
    monkeypatch.setattr(forecast_jobs_module.db, "DB_LOCK", ExplodingLock())

    runner._run_deterministic({"job_id": "job-1", "trigger_reason": "manual"})

    assert len(forecaster.calls) == 1


def test_llm_authority_fallback_publishes_deterministic_trace(bus, session_factory, seeded):
    bus.emit(
        SignalType.COMPETITOR_INTEL,
        {
            "competitor_id": 1,
            "popular_dishes": ["Margherita Pizza"],
            "price_points": {},
            "method": "call",
            "call_id": 1,
        },
        source="test",
    )
    agent = DemandForecaster(bus, session_factory, llm=FakeCannedForecastLLM())
    agent.set_auto_mode(True)
    agent.run_forecast("interval")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        trace = session.query(ForecastTrace).filter(ForecastTrace.forecast_id == stored.id).one()
        assert stored.forecast_qty == 5
        assert stored.multipliers["llm_fallback"] == 1.0
        assert trace.trace["llm_final_decision"]["decision"] == "fallback_deterministic"
    finally:
        session.close()


def test_llm_authority_does_not_overwrite_hard_zero(bus, session_factory, seeded):
    bus.emit(
        SignalType.STOCKOUT_RISK,
        {"ingredient_id": 1, "on_hand": 1.0, "projected_runout": 30000.0, "affected_items": [1]},
        source="test",
    )
    agent = DemandForecaster(bus, session_factory, llm=FakeHardZeroIgnoringLLM())
    agent.set_auto_mode(True)
    agent.run_forecast("interval")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.forecast_qty == 0
        assert stored.multipliers["availability"] == 0.0
        assert stored.multipliers["llm_finalizer"] == 1.0
    finally:
        session.close()


def test_material_signal_keeps_later_interval_runs_llm_authoritative(bus, session_factory, seeded):
    bus.emit(
        SignalType.COMPETITOR_INTEL,
        {
            "competitor_id": 1,
            "popular_dishes": ["Margherita Pizza"],
            "price_points": {},
            "method": "call",
            "call_id": 1,
        },
        source="test",
    )
    llm = FakeMaterialFinalizerLLM()
    agent = DemandForecaster(bus, session_factory, llm=llm)
    agent.set_auto_mode(True)
    agent.run_forecast("interval")
    agent.run_forecast("interval")

    session = session_factory()
    try:
        latest = (
            session.query(Forecast)
            .filter(Forecast.menu_item_id == 1)
            .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
            .first()
        )
        assert llm.calls == 2
        assert latest.forecast_qty == 7
        assert latest.multipliers["llm_final"] == 1.0
    finally:
        session.close()


def test_forecast_applies_multipliers_and_explains(bus, session_factory, seeded):
    bus.emit(
        SignalType.DEMAND_EVENT,
        {
            "event_ref": "parade",
            "event_kind": "demand_multiplier",
            "expected_attendance": None,
            "demand_multiplier": 1.35,
            "affected_menu_item_ids": [],
            "affected_categories": [],
            "window": {"start": 28800.0, "end": 39600.0},
            "raw_text": "parade today",
            "confidence": 0.9,
        },
        source="test",
    )
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.baseline_qty == 10.0
        assert stored.multipliers["settings_demand"] == pytest.approx(0.396)
        assert stored.multipliers["event"] == 1.35
        assert stored.multipliers["weather"] == 1.18
        assert stored.forecast_qty == 6
        assert stored.confidence > 0
    finally:
        session.close()


def test_competitor_market_opportunity_lifts_matched_item(bus, session_factory, seeded):
    bus.emit(
        SignalType.COMPETITOR_MARKET_SIGNAL,
        {
            "signal_kind": "competitor_offline",
            "source_channel": "aggregator",
            "platform": "swiggy",
            "competitor_id": 1,
            "affected_menu_items": [1],
            "affected_categories": ["pizza"],
            "direction": "opportunity",
            "impact_score": 0.20,
            "confidence": 1.0,
            "window": {"start": 28800.0, "end": 39600.0},
            "evidence": ["Mario is offline"],
            "raw": {"is_open": False},
        },
        source="test",
        ttl=10800.0,
    )
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        pizza = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        pasta = session.query(Forecast).filter(Forecast.menu_item_id == 2).first()
        assert pizza.multipliers["competitor_market"] > 1.0
        assert pasta.multipliers["competitor_market"] == pytest.approx(1.0)
        trace = session.query(ForecastTrace).filter(ForecastTrace.forecast_id == pizza.id).one()
        competitor_adjustment = next(
            entry for entry in trace.trace["adjustments"]
            if entry["key"] == "competitor_market"
        )
        assert competitor_adjustment["evidence"][0]["signal_kind"] == "competitor_offline"
    finally:
        session.close()


def test_competitor_market_threat_suppresses_matched_item(bus, session_factory, seeded):
    bus.emit(
        SignalType.COMPETITOR_MARKET_SIGNAL,
        {
            "signal_kind": "promo_started",
            "source_channel": "aggregator",
            "platform": "zomato",
            "competitor_id": 1,
            "affected_menu_items": [1],
            "affected_categories": ["pizza"],
            "direction": "threat",
            "impact_score": 0.20,
            "confidence": 1.0,
            "window": {"start": 28800.0, "end": 39600.0},
            "evidence": ["Mario started a pizza discount"],
            "raw": {"discount_pct": 20},
        },
        source="test",
        ttl=10800.0,
    )
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        pizza = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert pizza.multipliers["competitor_market"] < 1.0
    finally:
        session.close()


def test_large_event_value_is_treated_as_attendance_not_multiplier(bus, session_factory, seeded):
    bus.emit(
        SignalType.DEMAND_EVENT,
        {
            "event_ref": "food fest",
            "event_kind": "expected_attendance",
            "expected_attendance": 800,
            "demand_multiplier": None,
            "affected_menu_item_ids": [],
            "affected_categories": [],
            "window": {"start": 28800.0, "end": 39600.0},
            "raw_text": "Food fest from 09:00 for 800 people",
            "confidence": 0.9,
        },
        source="test",
    )
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.multipliers["event"] == pytest.approx(1.8)
        assert stored.forecast_qty < 100
        log = next(
            row for row in session.query(EventLog).filter(EventLog.category == "forecast").all()
            if row.detail.get("forecast_id") == stored.id
        )
        assert "food fest attendance 800" in log.detail["explanations"]["event"]
        assert "Station and qualified-staff capacity" not in str(log.detail["explanations"].values())
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
        assert stored.multipliers["settings_demand"] == pytest.approx(2.474)
        assert stored.forecast_qty == 29
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
        assert stored.multipliers["settings_demand"] == pytest.approx(0.396)
        assert stored.forecast_qty == 2
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


def test_hard_constraints_preserve_latent_demand_in_trace(bus, session_factory, seeded):
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
        logs = {
            row.detail["menu_item_id"]: row.detail
            for row in session.query(EventLog).filter(EventLog.category == "forecast").all()
            if isinstance(row.detail, dict) and row.detail.get("menu_item_id") in {1, 2}
        }

        stockout_final = logs[1]["trace"]["final"]
        assert logs[1]["forecast_qty"] == 0
        assert stockout_final["zero_reason"] == "availability_blocked"
        assert stockout_final["latent_demand_qty"] > 0
        assert stockout_final["constrained_raw_qty"] == 0

        staff_final = logs[2]["trace"]["final"]
        assert logs[2]["forecast_qty"] == 0
        assert staff_final["zero_reason"] == "staff_unavailable"
        assert staff_final["latent_demand_qty"] > 0
        assert staff_final["constrained_raw_qty"] == 0
    finally:
        session.close()


def test_batch_decision_ignores_stale_forecast_window(bus, session_factory, seeded):
    session = session_factory()
    try:
        session.add(
            Forecast(
                menu_item_id=1,
                window={"start": 28800.0, "end": 39600.0},
                daypart="breakfast",
                forecast_qty=20.0,
                baseline_qty=10.0,
                multipliers={"event": 2.0},
                confidence=0.9,
                generated_at=28800.0,
                trigger_reason="stale",
            )
        )
        session.commit()
    finally:
        session.close()

    bus.sim_time = 34200.0
    agent = DemandForecaster(bus, session_factory)
    agent.decide_batches("test")

    session = session_factory()
    try:
        decision = session.query(Batch).filter(Batch.menu_item_id == 1).one()
        assert decision.decision == "skip"
        log = next(
            row for row in session.query(EventLog).filter(EventLog.category == "batch").all()
            if row.detail.get("menu_item_id") == 1
        )
        assert log.detail["forecast_id"] is None
        assert "no current-window forecast" in log.detail["reasons"]
    finally:
        session.close()


def test_forecast_emits_run_trace_for_explanations(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        signal = next(
            row for row in session.query(Signal).filter(Signal.type == SignalType.DEMAND_FORECAST.value).all()
            if row.payload.get("menu_item_id") == 1
        )
        assert signal.payload["run_id"].startswith("fr:28800:breakfast")
        assert signal.payload["trace"]["run_id"] == signal.payload["run_id"]
        assert signal.payload["trace"]["scope"]["menu_item_id"] == 1
        assert signal.payload["trace"]["adjustments"]

        log = next(
            row for row in session.query(EventLog).filter(EventLog.category == "forecast").all()
            if row.detail.get("forecast_id") == stored.id
        )
        assert log.detail["trace"]["summary"]
        assert log.detail["trace"]["final"]["production_recommendation_qty"] == stored.forecast_qty
    finally:
        session.close()


def test_forecast_writes_durable_trace_ledger(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory)
    agent.run_forecast("test")

    session = session_factory()
    try:
        forecast_ids = {row.id for row in session.query(Forecast).all()}
        traces = session.query(ForecastTrace).all()
        assert {row.forecast_id for row in traces} == forecast_ids

        trace = next(row for row in traces if row.menu_item_id == 1)
        assert trace.run_id.startswith("fr:28800:breakfast")
        assert trace.trace["scope"]["menu_item_id"] == 1
        assert trace.summary == trace.trace["summary"]

        adjustments = (
            session.query(ForecastAdjustment)
            .filter(ForecastAdjustment.forecast_id == trace.forecast_id)
            .all()
        )
        keys = {row.modifier_key for row in adjustments}
        assert {"settings_demand", "weather", "recent_velocity"}.issubset(keys)
        assert all(row.run_id == trace.run_id for row in adjustments)
    finally:
        session.close()


def test_forecaster_suggestions_use_llm(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeSuggestionLLM())
    agent.run_forecast("test")
    result = agent.generate_suggestions()
    assert result["summary"] == "resize_batches"
    assert result["suggestions"][0]["action"] == "resize"


def test_llm_finalizer_accepts_deterministic_recommendation(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeFinalizerAcceptLLM())
    agent.run_forecast("manual", optimize=True)

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.forecast_qty == 5
        assert stored.multipliers["llm_finalizer"] == 1.0
        trace = session.query(ForecastTrace).filter(ForecastTrace.forecast_id == stored.id).one()
        assert trace.trace["deterministic_recommendation"]["forecast_qty"] == 5
        assert trace.trace["llm_final_decision"]["decision"] == "accept_deterministic"
    finally:
        session.close()


def test_llm_finalizer_rejects_unjustified_material_change(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeFinalizerUnjustifiedChangeLLM())
    agent.run_forecast("manual", optimize=True)

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.forecast_qty == 5
        assert stored.multipliers["llm_finalizer"] == 1.0
    finally:
        session.close()


def test_regular_forecast_skips_llm_finalizer(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FailingForecastLLM())
    agent.run_forecast("manual")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored is not None
        assert "llm_finalizer" not in stored.multipliers
    finally:
        session.close()


def test_manual_llm_optimization_writes_memory(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeOptimizerLLM())
    agent.optimize_forecast("test")

    session = session_factory()
    try:
        stored = session.query(Forecast).filter(Forecast.menu_item_id == 1).first()
        assert stored.multipliers["weather"] == 2.0
        assert stored.forecast_qty == 8
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


def test_manual_llm_target_persists_as_active_override(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeTargetForecastLLM())
    agent.optimize_forecast()

    session = session_factory()
    try:
        override = session.query(ForecastOverride).filter(ForecastOverride.menu_item_id == 1).one()
        assert override.operation == "set_target"
        assert override.value == {"qty": 4}
        assert override.status == "active"
        assert override.valid_until == 39600.0
    finally:
        session.close()

    agent.run_forecast("interval")

    session = session_factory()
    try:
        latest = (
            session.query(Forecast)
            .filter(Forecast.menu_item_id == 1)
            .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
            .first()
        )
        assert latest.forecast_qty == 4
        assert latest.trigger_reason == "interval"

        log = next(
            row for row in session.query(EventLog).filter(EventLog.category == "forecast").all()
            if row.detail.get("forecast_id") == latest.id
        )
        assert log.detail["multipliers"]["authority_override"] == 1.0
        assert log.detail["trace"]["summary"] == "Storm conditions soften dine-in demand."
    finally:
        session.close()


def test_production_constraint_hard_zeroes_matching_category(bus, session_factory, seeded):
    session = session_factory()
    try:
        session.add(
            MenuItem(
                id=3,
                name="Tiramisu",
                category="dessert",
                station_id=1,
                dine_in_price=8.0,
                online_price=9.0,
                prep_time_min=4.0,
                is_batchable=0,
                active=1,
                weather_tags=["cold"],
                description="Mascarpone dessert",
            )
        )
        settings = session.get(SimSettings, 1)
        settings.dish_mix_weights = {"1": 2.0, "2": 1.0, "3": 2.0}
        session.commit()
    finally:
        session.close()

    signal = bus.emit(
        SignalType.PRODUCTION_CONSTRAINT,
        {
            "constraint_ref": "dessert",
            "constraint_type": "category",
            "action": "block",
            "affected_menu_item_ids": [],
            "affected_categories": ["dessert"],
            "window": {"start": 28800.0, "end": 39600.0},
            "reason": "Desserts are overstocked today",
            "raw_text": "Desserts are overstocked today",
            "confidence": 0.9,
        },
        source="voice",
        ttl=10800.0,
    )

    agent = DemandForecaster(bus, session_factory)
    agent.on_signal(signal)
    agent.run_forecast("manual")

    session = session_factory()
    try:
        latest = (
            session.query(Forecast)
            .filter(Forecast.menu_item_id == 3)
            .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
            .first()
        )
        assert latest.trigger_reason == "manual"
        assert latest.forecast_qty == 0
        assert latest.multipliers["production_constraint"] == 0.0
    finally:
        session.close()


def test_production_constraint_uses_typed_signal_trace(bus, session_factory, seeded):
    session = session_factory()
    try:
        session.add(
            MenuItem(
                id=3,
                name="Tiramisu",
                category="dessert",
                station_id=1,
                dine_in_price=8.0,
                online_price=9.0,
                prep_time_min=4.0,
                is_batchable=0,
                active=1,
                weather_tags=["cold"],
                description="Mascarpone dessert",
            )
        )
        settings = session.get(SimSettings, 1)
        settings.dish_mix_weights = {"1": 2.0, "2": 1.0, "3": 2.0}
        session.commit()
    finally:
        session.close()

    signal = bus.emit(
        SignalType.PRODUCTION_CONSTRAINT,
        {
            "constraint_ref": "dessert",
            "constraint_type": "category",
            "action": "block",
            "affected_menu_item_ids": [],
            "affected_categories": ["dessert"],
            "window": {"start": 28800.0, "end": 39600.0},
            "reason": "No more desserts possible",
            "raw_text": "No more desserts possible",
            "confidence": 0.9,
        },
        source="voice",
        ttl=10800.0,
    )

    agent = DemandForecaster(bus, session_factory)
    agent.on_signal(signal)
    agent.run_forecast("manual")

    session = session_factory()
    try:
        latest = (
            session.query(Forecast)
            .filter(Forecast.menu_item_id == 3)
            .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
            .first()
        )
        assert latest.trigger_reason == "manual"
        assert latest.forecast_qty == 0
        assert latest.multipliers["production_constraint"] == 0.0

        log = next(
            row for row in session.query(EventLog).filter(EventLog.category == "forecast").all()
            if row.detail.get("forecast_id") == latest.id
        )
        assert log.detail["trace"]["final"]["zero_reason"] == "production_constraint"
        assert "No more desserts possible" in log.detail["trace"]["summary"]
    finally:
        session.close()


def test_production_constraint_survives_later_llm_target_override(bus, session_factory, seeded):
    session = session_factory()
    try:
        session.add(
            MenuItem(
                id=3,
                name="Tiramisu",
                category="dessert",
                station_id=1,
                dine_in_price=8.0,
                online_price=9.0,
                prep_time_min=4.0,
                is_batchable=0,
                active=1,
                weather_tags=["cold"],
                description="Mascarpone dessert",
            )
        )
        settings = session.get(SimSettings, 1)
        settings.dish_mix_weights = {"1": 2.0, "2": 1.0, "3": 2.0}
        session.commit()
    finally:
        session.close()

    signal = bus.emit(
        SignalType.PRODUCTION_CONSTRAINT,
        {
            "constraint_ref": "dessert",
            "constraint_type": "category",
            "action": "block",
            "affected_menu_item_ids": [],
            "affected_categories": ["dessert"],
            "window": {"start": 28800.0, "end": 39600.0},
            "reason": "No desserts possible",
            "raw_text": "No desserts possible",
            "confidence": 0.9,
        },
        source="voice",
        ttl=10800.0,
    )

    agent = DemandForecaster(bus, session_factory, llm=FakeDessertTargetForecastLLM())
    agent.on_signal(signal)
    agent.optimize_forecast("manual")
    agent.run_forecast("manual")

    session = session_factory()
    try:
        latest = (
            session.query(Forecast)
            .filter(Forecast.menu_item_id == 3)
            .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
            .first()
        )
        assert latest.forecast_qty == 0
        assert latest.multipliers["production_constraint"] == 0.0
        assert latest.multipliers.get("llm_target") is None
    finally:
        session.close()


def test_llm_finalizer_job_creates_approval_not_override(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeTargetForecastLLM())
    approvals = ApprovalsHub(bus, session_factory)
    runner = ForecastJobRunner(bus, session_factory, agent, approvals)

    job = runner.enqueue(LLM_FINALIZER, trigger_reason="manual_llm_review", requested_by="user")
    runner._run_job(job["job_id"])

    session = session_factory()
    try:
        approvals_rows = session.query(ApprovalRequest).filter(
            ApprovalRequest.type == "forecast_override_proposal"
        ).all()
        assert len(approvals_rows) == 1
        assert approvals_rows[0].payload["menu_item_id"] == 1
        assert approvals_rows[0].payload["operation"] == "set_target"
        assert session.query(ForecastOverride).count() == 0
        assert session.query(Forecast).count() == 0

        stored_job = session.query(ForecastJob).filter(ForecastJob.job_id == job["job_id"]).one()
        assert stored_job.status == "succeeded"
        assert stored_job.result["needs_approval"] is True
    finally:
        session.close()


def test_approved_llm_proposal_persists_override_and_reforecasts(bus, session_factory, seeded):
    agent = DemandForecaster(bus, session_factory, llm=FakeTargetForecastLLM())
    approvals = ApprovalsHub(bus, session_factory)
    runner = ForecastJobRunner(bus, session_factory, agent, approvals)
    bus.subscribe(SignalType.APPROVAL_RESOLVED, runner.on_approval_resolved)

    job = runner.enqueue(LLM_FINALIZER, trigger_reason="manual_llm_review", requested_by="user")
    runner._run_job(job["job_id"])

    session = session_factory()
    try:
        approval = session.query(ApprovalRequest).filter(
            ApprovalRequest.type == "forecast_override_proposal"
        ).one()
        approval_id = approval.id
    finally:
        session.close()

    approvals.approve(approval_id)

    session = session_factory()
    try:
        override = session.query(ForecastOverride).filter(ForecastOverride.menu_item_id == 1).one()
        assert override.source == "llm"
        assert override.authority == "approved_llm"
        assert override.operation == "set_target"
        assert override.value == {"qty": 4}
        deterministic_job = (
            session.query(ForecastJob)
            .filter(ForecastJob.kind == DETERMINISTIC_FORECAST, ForecastJob.status == "queued")
            .one()
        )
        deterministic_job_id = deterministic_job.job_id
    finally:
        session.close()

    runner._run_job(deterministic_job_id)

    session = session_factory()
    try:
        latest = (
            session.query(Forecast)
            .filter(Forecast.menu_item_id == 1)
            .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
            .first()
        )
        assert latest.forecast_qty == 4
        assert latest.multipliers["authority_override"] == 1.0
    finally:
        session.close()
