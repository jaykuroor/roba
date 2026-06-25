"""Demand Forecaster agent.

This module owns the Track A forecasting stack:

1. deterministic baseline and multiplier calculation,
2. operational constraint resolution from typed live signals,
3. optional LLM multiplier/batch optimization,
4. integer forecast emission over the Signal Bus,
5. durable forecaster memory for the MVP dashboard.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core import config
from core.agent_base import BaseAgent
from core.clock import DAY_CLOSE_OFFSET, DAY_OPEN_OFFSET, SECONDS_PER_DAY
from core.llm import CANNED_NOTE
from core.models import (
    Batch,
    BatchDefinition,
    Competitor,
    DemandForecasterMemory,
    Forecast,
    ForecastAdjustment,
    ForecastOverride,
    ForecastTrace,
    MenuItem,
    OrderLine,
    Signal,
    SimSettings,
    WeatherLog,
)
from core.pos_simulator import WINDOW_SECONDS, active_injections
from core.signals import SignalType


def _hhmm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 3600 + int(m) * 60


DAYPART_SECONDS = {
    name: (_hhmm(start), _hhmm(end), weight)
    for name, (start, end, weight) in config.DAYPARTS.items()
}


LLM_AUTHORITY_FORECAST = "llm_authority_forecast"

MATERIAL_SIGNAL_TYPES = {
    SignalType.DEMAND_EVENT.value,
    SignalType.PRODUCTION_CONSTRAINT.value,
    SignalType.STAFF_AVAILABILITY.value,
    SignalType.INGREDIENT_SHORTAGE_REPORTED.value,
    SignalType.EXPIRY_USE_PRIORITY.value,
    SignalType.CUSTOMER_FEEDBACK_NOTE.value,
    SignalType.COMPETITOR_NOTE.value,
    SignalType.STAFF_COVERAGE.value,
    SignalType.COMPETITOR_UPDATE.value,
    SignalType.COMPETITOR_INTEL.value,
    SignalType.COMPETITOR_MARKET_SIGNAL.value,
    SignalType.REVIEW_INSIGHT.value,
    SignalType.WEATHER_UPDATE.value,
    SignalType.MENU_TOGGLE.value,
    SignalType.STOCKOUT_RISK.value,
}


class DemandForecaster(BaseAgent):
    """Rolling item forecasts, LLM optimization, memory, and batch decisions."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        formatter: Optional[Any] = None,
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        llm: Optional[Any] = None,
    ):
        super().__init__(bus, db_session_factory, "track_a.forecaster")
        self.formatter = formatter
        self.ws_broadcast = ws_broadcast
        self.llm = llm
        self.llm_auto_mode = bool(config.LLM_FORECAST_AUTO_MODE)
        self.forecast_job_enqueue: Optional[Callable[[str, str], Any]] = None
        self.subscribe(["forecasting"])

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            lambda: self._enqueue_or_run_forecast("deterministic_forecast", "interval"),
            interval_sim_s=config.FORECAST_INTERVAL_SIM_S,
            name="track_a_forecast_interval",
        )
        orchestrator.register(
            "interval",
            self.generate_suggestions,
            interval_sim_s=config.SUGGESTION_INTERVAL_SIM_S,
            name="track_a_forecast_suggestions",
        )

    def on_signal(self, signal: Signal) -> None:
        if signal.type in {
            SignalType.WASTE_EVENT.value,
            SignalType.STAFF_COVERAGE.value,
            SignalType.COMPETITOR_UPDATE.value,
            SignalType.COMPETITOR_INTEL.value,
            SignalType.COMPETITOR_MARKET_SIGNAL.value,
            SignalType.REVIEW_INSIGHT.value,
            SignalType.WEATHER_UPDATE.value,
            SignalType.DEMAND_EVENT.value,
            SignalType.PRODUCTION_CONSTRAINT.value,
            SignalType.STAFF_AVAILABILITY.value,
            SignalType.INGREDIENT_SHORTAGE_REPORTED.value,
            SignalType.EXPIRY_USE_PRIORITY.value,
            SignalType.CUSTOMER_FEEDBACK_NOTE.value,
            SignalType.COMPETITOR_NOTE.value,
            SignalType.MENU_TOGGLE.value,
            SignalType.STOCKOUT_RISK.value,
        }:
            kind = (
                LLM_AUTHORITY_FORECAST
                if self.llm_auto_mode and self._signal_requires_llm_authority(signal)
                else "deterministic_forecast"
            )
            self._enqueue_or_run_forecast(kind, f"signal:{signal.type}")

    def set_forecast_job_enqueue(self, fn: Callable[[str, str], Any]) -> None:
        self.forecast_job_enqueue = fn

    def _enqueue_or_run_forecast(self, kind: str, trigger_reason: str) -> Any:
        if self.forecast_job_enqueue is not None:
            return self.forecast_job_enqueue(kind, trigger_reason)
        if kind == LLM_AUTHORITY_FORECAST:
            return self.run_forecast(trigger_reason, optimize=True, optimize_batches=False)
        if kind == "llm_finalizer":
            return self.optimize_forecast(trigger_reason)
        return self.run_forecast(trigger_reason, optimize=False)

    def set_auto_mode(self, enabled: bool) -> Dict[str, Any]:
        self.llm_auto_mode = bool(enabled)
        self._remember(
            "global",
            "auto_mode",
            {"title": "LLM auto mode changed", "enabled": self.llm_auto_mode},
            {"source": "ui"},
            1.0,
            "deterministic",
            valid_for=SECONDS_PER_DAY,
        )
        return {"enabled": self.llm_auto_mode}

    def _should_use_llm_authority(self, live: Iterable[Signal]) -> bool:
        if not self.llm_auto_mode:
            return False
        return any(self._signal_requires_llm_authority(signal) for signal in live)

    @staticmethod
    def _signal_requires_llm_authority(signal: Signal) -> bool:
        if signal.type not in MATERIAL_SIGNAL_TYPES:
            return False
        payload = signal.payload or {}
        if signal.type in {
            SignalType.DEMAND_EVENT.value,
            SignalType.PRODUCTION_CONSTRAINT.value,
            SignalType.STAFF_AVAILABILITY.value,
            SignalType.INGREDIENT_SHORTAGE_REPORTED.value,
            SignalType.EXPIRY_USE_PRIORITY.value,
            SignalType.CUSTOMER_FEEDBACK_NOTE.value,
            SignalType.COMPETITOR_NOTE.value,
        }:
            return True
        if signal.type == SignalType.STAFF_COVERAGE.value:
            return payload.get("covered") is False
        if signal.type == SignalType.COMPETITOR_UPDATE.value:
            return bool(payload.get("offers_changed") or payload.get("is_open") is False)
        return True

    # ------------------------------------------------------------------
    # Public forecast and batch API
    # ------------------------------------------------------------------

    def run_forecast(
        self,
        trigger_reason: str = "manual",
        optimize: bool = False,
        optimize_batches: Optional[bool] = None,
    ) -> List[Forecast]:
        """Forecast every active menu item and emit integer demand signals."""
        now = float(self.bus.sim_time)
        daypart, window = current_window(now)
        live = self.bus.live()
        rows: List[Forecast] = []
        row_summaries: List[Dict[str, Any]] = []
        after_commit: List[Tuple[str, Any]] = []
        run_id = forecast_run_id(now, daypart, window, trigger_reason)
        llm_fallback = False

        session = self.db_session_factory()
        try:
            items = (
                session.query(MenuItem)
                .filter(MenuItem.active == 1)
                .order_by(MenuItem.id.asc())
                .all()
            )
            prepared = [
                self._prepare_item(session, item, daypart, window, live)
                for item in items
            ]
            for prepared_item in prepared:
                self._finalize_deterministic_recommendation(session, prepared_item, daypart, window, now)

            authority_required = bool(optimize) or self._should_use_llm_authority(live)
            llm_plan = self._llm_plan(session, prepared, live, daypart, window) if authority_required else {}
            llm_fallback = bool(authority_required and not llm_plan)
            if llm_plan:
                self._queue_llm_plan_memory(llm_plan, after_commit)

            for prepared_item in prepared:
                item = prepared_item["item"]
                baseline = float(prepared_item["baseline"])
                multipliers = dict(prepared_item["multipliers"])
                explanations = dict(prepared_item["explanations"])
                hard_override = prepared_item.get("hard_override")
                deterministic_recommendation = dict(prepared_item["deterministic_recommendation"])
                llm_decision = self._final_decision_for(llm_plan, item.id)
                llm_adjustment = {} if llm_decision else self._adjustment_for(llm_plan, item.id)
                used_llm_decision = bool(llm_decision or llm_adjustment)

                if llm_fallback:
                    multipliers["llm_fallback"] = 1.0
                    explanations["llm_fallback"] = (
                        "LLM authority was requested but unavailable or invalid; "
                        "deterministic forecast published as fallback."
                    )

                if llm_decision:
                    hard_override = self._apply_llm_final_decision(
                        item.id,
                        daypart,
                        window,
                        multipliers,
                        explanations,
                        hard_override,
                        deterministic_recommendation,
                        llm_decision,
                        after_commit,
                        persist_override=bool(optimize and trigger_reason == "manual"),
                    )
                elif llm_adjustment:
                    hard_override = self._apply_llm_adjustment(
                        item.id,
                        daypart,
                        window,
                        multipliers,
                        explanations,
                        hard_override,
                        llm_adjustment,
                        after_commit,
                        persist_override=bool(optimize and trigger_reason == "manual"),
                    )

                raw_qty = self._raw_qty(baseline, multipliers)
                qty = int(hard_override) if hard_override is not None else nearest_int(raw_qty)
                qty = max(0, qty)
                latent_qty = self._latent_demand_qty(baseline, multipliers, hard_override)

                confidence = confidence_from(multipliers)
                final_confidence = (llm_decision or llm_adjustment or {}).get("confidence")
                if final_confidence is not None:
                    confidence = min(confidence, float(final_confidence or 0.85))

                trace = self._forecast_trace(
                    run_id=run_id,
                    item=item,
                    daypart=daypart,
                    window=window,
                    baseline=baseline,
                    multipliers=multipliers,
                    explanations=explanations,
                    raw_qty=raw_qty,
                    latent_qty=latent_qty,
                    forecast_qty=qty,
                    confidence=confidence,
                    hard_override=hard_override,
                    optimized=used_llm_decision,
                    trigger_reason=trigger_reason,
                    deterministic_recommendation=deterministic_recommendation,
                    finalizer_decision=(
                        llm_decision
                        or llm_adjustment
                        or (
                            {
                                "decision": "fallback_deterministic",
                                "reason": explanations["llm_fallback"],
                            }
                            if llm_fallback
                            else None
                        )
                    ),
                    live_signals=live,
                )
                forecast = Forecast(
                    menu_item_id=item.id,
                    window=window,
                    daypart=daypart,
                    forecast_qty=float(qty),
                    baseline_qty=round(baseline, 2),
                    multipliers=multipliers,
                    confidence=round(confidence, 3),
                    generated_at=now,
                    trigger_reason=(
                        "llm_manual"
                        if used_llm_decision and trigger_reason == "manual"
                        else trigger_reason
                    ),
                )
                session.add(forecast)
                session.flush()
                session.refresh(forecast)
                self._write_trace_ledger(session, forecast, run_id, item, daypart, window, trace, now)
                rows.append(forecast)
                row_summaries.append({"id": forecast.id, "qty": qty})

                forecast_payload = {
                    "menu_item_id": item.id,
                    "window": window,
                    "daypart": daypart,
                    "qty": qty,
                    "baseline": round(baseline, 2),
                    "multipliers": multipliers,
                    "confidence": round(confidence, 3),
                    "run_id": run_id,
                    "trace": trace,
                }
                log_detail = {
                    "run_id": run_id,
                    "forecast_id": forecast.id,
                    "menu_item_id": item.id,
                    "item_name": item.name,
                    "baseline": round(baseline, 2),
                    "raw_qty": round(raw_qty, 3),
                    "forecast_qty": qty,
                    "multipliers": multipliers,
                    "explanations": explanations,
                    "hard_override": hard_override,
                    "optimized": used_llm_decision,
                    "llm_fallback": llm_fallback,
                    "trigger": trigger_reason,
                    "trace": trace,
                }
                after_commit.extend(
                    [
                        (
                            "emit",
                            (
                                SignalType.DEMAND_FORECAST,
                                forecast_payload,
                                {
                                    "ttl": max(window["end"] - now, 1.0),
                                    "dedup_key": f"forecast:{item.id}:{int(window['start'])}",
                                },
                            ),
                        ),
                        (
                            "log",
                            (
                                "forecast",
                                f"Forecast {item.name}: {qty:d} for {daypart}",
                                log_detail,
                            ),
                        ),
                        (
                            "broadcast",
                            (
                                "forecast_updated",
                                {
                                    "forecast": self._forecast_to_dict(forecast),
                                    "item": self._item_to_dict(item),
                                    "reasoning": log_detail,
                                },
                            ),
                        ),
                    ]
                )

            session.commit()
        finally:
            session.close()

        self._run_after_commit(after_commit)
        self._remember_run_summary(row_summaries, optimize or (not llm_fallback and bool(llm_plan)), trigger_reason)
        self.decide_batches(
            trigger_reason,
            optimize=bool(optimize if optimize_batches is None else optimize_batches),
        )
        return rows

    def optimize_forecast(self, trigger_reason: str = "manual") -> List[Forecast]:
        return self.run_forecast(trigger_reason, optimize=True)

    def propose_llm_forecast_overrides(self, source_job_id: str) -> Dict[str, Any]:
        """Run the LLM finalizer as a reviewer and return approval proposals.

        This deliberately does not write forecast rows or overrides. The normal
        deterministic path stays authoritative until an operator approves a
        proposed durable change.
        """
        now = float(self.bus.sim_time)
        daypart, window = current_window(now)
        live = self.bus.live()
        run_id = forecast_run_id(now, daypart, window, f"llm_finalizer:{source_job_id}")
        after_commit: List[Tuple[str, Any]] = []
        proposals: List[Dict[str, Any]] = []
        accepted: List[Dict[str, Any]] = []

        session = self.db_session_factory()
        try:
            items = (
                session.query(MenuItem)
                .filter(MenuItem.active == 1)
                .order_by(MenuItem.id.asc())
                .all()
            )
            prepared = [
                self._prepare_item(session, item, daypart, window, live)
                for item in items
            ]
            for prepared_item in prepared:
                self._finalize_deterministic_recommendation(
                    session, prepared_item, daypart, window, now
                )

            llm_plan = self._llm_plan(session, prepared, live, daypart, window)
            if llm_plan:
                self._queue_llm_plan_memory(llm_plan, after_commit)

            for prepared_item in prepared:
                item = prepared_item["item"]
                deterministic = dict(prepared_item.get("deterministic_recommendation") or {})
                deterministic_qty = int(deterministic.get("forecast_qty") or 0)
                multipliers = dict(prepared_item.get("multipliers") or {})
                hard_override = prepared_item.get("hard_override")
                decision = self._final_decision_for(llm_plan, item.id)
                adjustment = {} if decision else self._adjustment_for(llm_plan, item.id)
                source_decision = decision or adjustment
                final_qty = self._proposed_final_qty(
                    source_decision,
                    deterministic_qty,
                    multipliers,
                    hard_override,
                    from_finalizer=bool(decision),
                )
                if final_qty is None or final_qty == deterministic_qty:
                    accepted.append(
                        {
                            "menu_item_id": int(item.id),
                            "item_name": item.name,
                            "deterministic_qty": deterministic_qty,
                            "decision": source_decision or {"decision": "accept_deterministic"},
                        }
                    )
                    continue

                proposals.append(
                    {
                        "menu_item_id": int(item.id),
                        "item_name": item.name,
                        "operation": "hard_zero_production" if final_qty == 0 else "set_target",
                        "qty": max(0, int(final_qty)),
                        "daypart": daypart,
                        "window": dict(window),
                        "reason": str(
                            source_decision.get("reason")
                            or "LLM finalizer proposed a durable forecast override."
                        ),
                        "evidence": source_decision.get("evidence") or source_decision,
                        "confidence": float(source_decision.get("confidence") or llm_plan.get("confidence") or 0.75),
                        "source_job_id": source_job_id,
                        "source_run_id": run_id,
                        "deterministic_qty": deterministic_qty,
                    }
                )
        finally:
            session.close()

        self._run_after_commit(after_commit)
        return {
            "run_id": run_id,
            "daypart": daypart,
            "window": window,
            "llm_plan": llm_plan if "llm_plan" in locals() else {},
            "accepted_deterministic": accepted,
            "proposals": proposals,
        }

    def decide_batches(
        self,
        trigger_reason: str = "manual",
        optimize: bool = False,
    ) -> List[Batch]:
        now = float(self.bus.sim_time)
        daypart, window = current_window(now)
        live = self.bus.live()
        rows: List[Batch] = []
        after_commit: List[Tuple[str, Any]] = []
        batch_plan = self._llm_batch_plan(daypart, window, live) if optimize else {}

        session = self.db_session_factory()
        try:
            definitions = session.query(BatchDefinition).order_by(BatchDefinition.id.asc()).all()
            for definition in definitions:
                if definition.dayparts and daypart not in definition.dayparts:
                    continue
                item = session.get(MenuItem, definition.menu_item_id)
                if item is None or not item.active:
                    continue

                forecast = (
                    session.query(Forecast)
                    .filter(Forecast.menu_item_id == item.id)
                    .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
                    .limit(24)
                    .all()
                )
                forecast = self._current_window_forecast(forecast, daypart, window, now)
                f_qty = int(round(float(forecast.forecast_qty if forecast is not None else 0.0)))
                reasons: List[str] = []
                available = not self._is_blocked_for_batch(item.id, definition.station_id, live, reasons)
                should_cook = f_qty >= int(definition.batch_size_min or 0) and available
                planned = self._round_batch_qty(f_qty, definition) if should_cook else 0
                decision = "cook" if should_cook else "skip"

                llm_decision = self._batch_adjustment_for(batch_plan, definition.id, item.id)
                if llm_decision:
                    decision, planned = self._apply_llm_batch_decision(
                        llm_decision, decision, planned, f_qty, definition, available
                    )
                    reasons.append(str(llm_decision.get("reason") or "LLM batch optimization"))

                if f_qty < int(definition.batch_size_min or 0):
                    reasons.append(f"forecast {f_qty:d} below min {int(definition.batch_size_min or 0):d}")
                if forecast is None:
                    reasons.append("no current-window forecast")
                if not available and not reasons:
                    reasons.append("operational constraint")
                if not reasons and decision == "cook":
                    reasons.extend([f"{daypart} forecast {f_qty:d}", "capacity available", "ingredients OK"])

                row = Batch(
                    batch_definition_id=definition.id,
                    menu_item_id=item.id,
                    decided_at=now,
                    serve_window=window,
                    decision=decision,
                    planned_qty=float(planned),
                    actual_made_qty=0.0,
                    sold_qty=0.0,
                    wasted_qty=0.0,
                    status="decided",
                    by="agent",
                )
                session.add(row)
                session.flush()
                session.refresh(row)
                rows.append(row)

                reason_text = ", ".join(reasons)
                signal_payload = {
                    "batch_definition_id": definition.id,
                    "menu_item_id": item.id,
                    "serve_window": window,
                    "decision": decision,
                    "qty": int(planned),
                    "by": "agent",
                }
                log_detail = {
                    "batch_id": row.id,
                    "menu_item_id": item.id,
                    "batch_definition_id": definition.id,
                    "forecast_id": forecast.id if forecast is not None else None,
                    "forecast_qty": f_qty,
                    "decision": decision,
                    "planned_qty": int(planned),
                    "reasons": reasons,
                    "optimized": bool(optimize),
                    "trigger": trigger_reason,
                }
                after_commit.extend(
                    [
                        (
                            "emit",
                            (
                                SignalType.BATCH_DECISION,
                                signal_payload,
                                {"dedup_key": f"batch:{definition.id}:{int(window['start'])}:{decision}"},
                            ),
                        ),
                        ("log", ("batch", f"{decision} {int(planned):d} {item.name}: {reason_text}", log_detail)),
                        (
                            "broadcast",
                            (
                                "batch_decided",
                                {"batch": self._batch_to_dict(row), "reason": reason_text},
                            ),
                        ),
                    ]
                )
            session.commit()
        finally:
            session.close()

        self._run_after_commit(after_commit)
        return rows

    def generate_suggestions(self) -> Dict[str, Any]:
        result = {"suggestions": [], "summary": "no_change"}
        if self.llm is not None:
            context = self._suggestion_context()
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You advise a restaurant demand forecaster. Return JSON "
                        "with summary and suggestions. Suggestions are optional "
                        "non-binding actions about add/remove/retime/resize "
                        "batches based on recent forecasts and batch results."
                    ),
                },
                {"role": "user", "content": str(context)},
            ]
            schema = {
                "type": "object",
                "properties": {
                    "suggestions": {"type": "array"},
                    "summary": {"type": "string"},
                },
                "required": ["suggestions", "summary"],
            }
            parsed = self.llm.complete(
                messages,
                json_schema=schema,
                max_tokens=500,
                use_site="forecaster_suggestion",
            )
            if isinstance(parsed, dict) and parsed.get("note") != CANNED_NOTE:
                suggestions = parsed.get("suggestions")
                result = {
                    "suggestions": suggestions if isinstance(suggestions, list) else [],
                    "summary": str(parsed.get("summary") or "no_change"),
                }
        self.log_event(
            "forecast",
            f"Batch suggestion scan: {result.get('summary', 'no_change')}",
            result,
        )
        return result

    # ------------------------------------------------------------------
    # Forecast preparation
    # ------------------------------------------------------------------

    def _prepare_item(
        self,
        session: Any,
        item: MenuItem,
        daypart: str,
        window: Dict[str, float],
        live: Iterable[Signal],
    ) -> Dict[str, Any]:
        baseline = self.baseline_qty(session, item.id, daypart, float(self.bus.sim_time))
        multipliers = self._deterministic_multipliers(session, item, baseline, daypart, window, live)
        explanations = self._base_explanations(multipliers, item, window, live)
        hard_override = self._apply_hard_constraints(
            session,
            item,
            daypart,
            window,
            multipliers,
            explanations,
            live,
        )
        return {
            "item": item,
            "baseline": baseline,
            "multipliers": multipliers,
            "explanations": explanations,
            "hard_override": hard_override,
        }

    def _finalize_deterministic_recommendation(
        self,
        session: Any,
        prepared_item: Dict[str, Any],
        daypart: str,
        window: Dict[str, float],
        now: float,
    ) -> None:
        item = prepared_item["item"]
        baseline = float(prepared_item["baseline"])
        multipliers = dict(prepared_item["multipliers"])
        explanations = dict(prepared_item["explanations"])
        hard_override = prepared_item.get("hard_override")

        active_override = self._active_override(session, item.id, daypart, window, now)
        if active_override is not None:
            hard_override = self._apply_forecast_override(
                active_override,
                multipliers,
                explanations,
                hard_override,
            )

        raw_qty = self._raw_qty(baseline, multipliers)
        qty = int(hard_override) if hard_override is not None else nearest_int(raw_qty)
        qty = max(0, qty)
        latent_qty = self._latent_demand_qty(baseline, multipliers, hard_override)
        confidence = confidence_from(multipliers)

        prepared_item["multipliers"] = multipliers
        prepared_item["explanations"] = explanations
        prepared_item["hard_override"] = hard_override
        prepared_item["deterministic_recommendation"] = {
            "raw_qty": round(raw_qty, 3),
            "forecast_qty": int(qty),
            "latent_demand_qty": round(latent_qty, 3),
            "confidence": round(confidence, 3),
            "hard_override": hard_override,
            "summary": self._top_trace_reason(
                [
                    {
                        "key": key,
                        "operation": self._modifier_operation(key, value),
                        "value": float(value),
                        "reason": explanations.get(key, key),
                    }
                    for key, value in multipliers.items()
                ],
                self._zero_reason(hard_override, multipliers),
            ),
        }

    @staticmethod
    def _raw_qty(baseline: float, multipliers: Dict[str, float]) -> float:
        raw_qty = float(baseline)
        for value in multipliers.values():
            raw_qty *= float(value)
        return raw_qty

    def baseline_qty(self, session: Any, item_id: int, daypart: str, now: float) -> float:
        _current_daypart, window = current_window(now)
        window_fraction = _window_fraction(daypart, window)
        current_dow = int(now // SECONDS_PER_DAY) % 7
        same_dow = self._history_average(session, item_id, daypart, current_dow)
        if same_dow > 0:
            return round(same_dow * window_fraction, 2)

        any_dow = self._history_average(session, item_id, daypart, None)
        if any_dow > 0:
            return round(any_dow * window_fraction, 2)

        item = session.get(MenuItem, item_id)
        if item is None:
            return 0.0
        return max(1.0, self._settings_projected_qty(session, item, daypart, now))

    def _deterministic_multipliers(
        self,
        session: Any,
        item: MenuItem,
        baseline: float,
        daypart: str,
        window: Dict[str, float],
        live: Iterable[Signal],
    ) -> Dict[str, float]:
        return {
            "settings_demand": round(self._settings_multiplier(session, item, baseline, daypart), 3),
            "event": round(self._event_multiplier(item, window, live), 3),
            "competitor_market": round(self._competitor_multiplier(session, item, live), 3),
            "review": round(self._review_multiplier(item, live), 3),
            "staff_coverage": round(self._staff_multiplier(item, live), 3),
            "weather": round(self._weather_multiplier(session, item), 3),
            "recent_velocity": round(self._velocity_multiplier(item.id, baseline, daypart), 3),
        }

    def _base_explanations(
        self,
        multipliers: Dict[str, float],
        item: MenuItem,
        window: Dict[str, float],
        live: Iterable[Signal],
    ) -> Dict[str, str]:
        labels = {
            "settings_demand": self._settings_explanation(multipliers.get("settings_demand", 1.0)),
            "event": self._event_explanation(multipliers.get("event", 1.0), window, live),
            "competitor_market": self._competitor_explanation(item, live),
            "review": "Recent review sentiment for this item.",
            "staff_coverage": self._staff_explanation(multipliers.get("staff_coverage", 1.0)),
            "weather": self._weather_explanation(item, multipliers.get("weather", 1.0)),
            "recent_velocity": self._velocity_explanation(multipliers.get("recent_velocity", 1.0)),
        }
        return {key: labels.get(key, key) for key in multipliers}

    # ------------------------------------------------------------------
    # Deterministic multiplier inputs
    # ------------------------------------------------------------------

    def _history_average(
        self,
        session: Any,
        item_id: int,
        daypart: str,
        day_of_week: Optional[int],
    ) -> float:
        start, end, _weight = DAYPART_SECONDS[daypart]
        per_day: Dict[int, float] = defaultdict(float)
        rows = (
            session.query(OrderLine)
            .filter(OrderLine.menu_item_id == item_id, OrderLine.status == "sold")
            .all()
        )
        for line in rows:
            tod = float(line.sim_time or 0.0) % SECONDS_PER_DAY
            if not (start <= tod < end):
                continue
            day = math.floor(float(line.sim_time or 0.0) / SECONDS_PER_DAY)
            if day_of_week is not None and day % 7 != day_of_week:
                continue
            per_day[day] += float(line.qty or 0.0)
        if not per_day:
            return 0.0
        return sum(per_day.values()) / len(per_day)

    def _settings_multiplier(
        self,
        session: Any,
        item: MenuItem,
        baseline: float,
        daypart: str,
    ) -> float:
        if baseline <= 0:
            return 1.0
        projected = self._settings_projected_qty(session, item, daypart, float(self.bus.sim_time))
        if projected <= 0:
            return 0.0
        return projected / baseline

    def _settings_projected_qty(
        self,
        session: Any,
        item: MenuItem,
        daypart: str,
        now: float,
    ) -> float:
        settings = session.get(SimSettings, 1)
        base = float(getattr(settings, "base_orders_per_day", None) or config.BASE_ORDERS_PER_DAY)
        velocity = float(getattr(settings, "velocity", None) or 1.0)
        daypart_weight = self._settings_daypart_weight(settings, daypart)
        _current_daypart, window = current_window(now)
        window_duration = max(
            0.0,
            float(window.get("end", 0.0)) - float(window.get("start", 0.0)),
        )

        weights = self._settings_item_weights(session, settings, now)
        total_weight = sum(weights.values())
        item_weight = weights.get(int(item.id), 0.0)
        share = item_weight / total_weight if total_weight > 0 else 0.0

        for inj in active_injections(getattr(settings, "anomaly_injections", None), now):
            mult = inj.get("velocity_mult")
            if mult is not None:
                velocity *= float(mult)

        expected_orders = base * velocity * daypart_weight * (window_duration / WINDOW_SECONDS)
        expected_lines_per_order = sum(
            float(qty) * float(weight)
            for qty, weight in config.LINES_PER_ORDER.items()
        )
        cancel_factor = 1.0 - float(config.CANCEL_RATE)
        return max(0.0, expected_orders * expected_lines_per_order * cancel_factor * share)

    @staticmethod
    def _settings_daypart_weight(settings: Optional[SimSettings], daypart: str) -> float:
        curve = getattr(settings, "daypart_curve", None) or {}
        default = DAYPART_SECONDS.get(daypart, ("", "", 0.2))[2]
        return float(curve.get(daypart, default))

    @staticmethod
    def _settings_item_weights(session: Any, settings: Optional[SimSettings], now: float) -> Dict[int, float]:
        active_items = session.query(MenuItem).filter(MenuItem.active == 1).all()
        active_ids = {int(item.id) for item in active_items}
        raw_weights = getattr(settings, "dish_mix_weights", None) or {}

        weights: Dict[int, float] = {}
        for raw_id, raw_weight in raw_weights.items():
            try:
                item_id = int(raw_id)
                weight = float(raw_weight)
            except (TypeError, ValueError):
                continue
            if item_id in active_ids and weight > 0:
                weights[item_id] = weight

        if not weights:
            weights = {item_id: 1.0 for item_id in active_ids}

        for inj in active_injections(getattr(settings, "anomaly_injections", None), now):
            skew = inj.get("dish_mix_skew")
            if not isinstance(skew, dict):
                continue
            for item_id in list(weights):
                factor = skew.get(str(item_id))
                if factor is not None:
                    weights[item_id] *= float(factor)
        return weights

    def _event_multiplier(self, item: MenuItem, window: Dict[str, float], live: Iterable[Signal]) -> float:
        mult = 1.0
        for sig in live:
            payload = sig.payload or {}
            if sig.type == SignalType.DEMAND_EVENT.value:
                event_window = payload.get("window")
                if event_window and not windows_overlap(window, event_window):
                    continue
                mult *= self._demand_event_multiplier(payload)
        return min(float(config.EVENT_STACK_MAX_MULT), mult)

    @staticmethod
    def _demand_event_multiplier(payload: Dict[str, Any]) -> float:
        if payload.get("demand_multiplier") is not None:
            try:
                return min(
                    float(config.EVENT_ATTENDANCE_MAX_MULT),
                    max(0.0, float(payload["demand_multiplier"])),
                )
            except (TypeError, ValueError):
                return float(config.EVENT_MULT)
        if payload.get("expected_attendance") is not None:
            try:
                attendance = float(payload["expected_attendance"])
            except (TypeError, ValueError):
                return float(config.EVENT_MULT)
            reference = max(float(config.EVENT_ATTENDANCE_REFERENCE), 1.0)
            attendance_ratio = max(0.0, min(1.0, attendance / reference))
            return round(
                1.0 + attendance_ratio * (float(config.EVENT_ATTENDANCE_MAX_MULT) - 1.0),
                3,
            )
        return float(config.EVENT_MULT)

    @staticmethod
    def _settings_explanation(value: float) -> str:
        if value > 1.05:
            return f"Simulation demand settings lift this item to {value:.2f}x its historical baseline."
        if value < 0.95:
            return f"Simulation demand settings reduce this item to {value:.2f}x its historical baseline."
        return "Simulation demand settings are aligned with the historical baseline."

    def _event_explanation(self, value: float, window: Dict[str, float], live: Iterable[Signal]) -> str:
        if abs(value - 1.0) < 0.01:
            return "No active event is changing this item."
        labels: List[str] = []
        for sig in live:
            payload = sig.payload or {}
            if sig.type == SignalType.DEMAND_EVENT.value:
                event_window = payload.get("window")
                if event_window and not windows_overlap(window, event_window):
                    continue
                name = str(payload.get("event_ref") or "event")
                attendance = payload.get("expected_attendance")
                if attendance is not None:
                    labels.append(f"{name} attendance {int(float(attendance)):d}")
                else:
                    labels.append(name)
        detail = "; ".join(labels[:2]) if labels else "active event"
        return f"{detail} changes demand to {value:.2f}x after attendance guardrails."

    @staticmethod
    def _staff_explanation(value: float) -> str:
        if value <= 0:
            return "Station has no remaining qualified staff."
        if value < 1:
            return f"Staff coverage limits prep capacity to {value:.2f}x."
        return "Staff coverage is available and is not changing demand."

    @staticmethod
    def _weather_explanation(item: MenuItem, value: float) -> str:
        if value > 1.01:
            return f"Weather conditions favor {item.name}, lifting demand to {value:.2f}x."
        if value < 0.99:
            return f"Weather conditions soften {item.name}, reducing demand to {value:.2f}x."
        return "Weather is neutral for this item."

    @staticmethod
    def _velocity_explanation(value: float) -> str:
        if value > 1.05:
            return f"Recent POS velocity is above expectation at {value:.2f}x."
        if value < 0.95:
            return f"Recent POS velocity is below expectation at {value:.2f}x."
        return "Recent POS velocity is near expectation."

    def _competitor_multiplier(
        self,
        session: Any,
        item: MenuItem,
        live: Iterable[Signal],
    ) -> float:
        value = 1.0
        name = (item.name or "").lower()
        item_category = str(item.category or "").lower()
        _daypart, forecast_window = current_window(float(self.bus.sim_time))
        for sig in live:
            payload = sig.payload or {}
            if sig.type == SignalType.COMPETITOR_MARKET_SIGNAL.value:
                signal_window = payload.get("window") or {}
                if signal_window and not windows_overlap(forecast_window, signal_window):
                    continue
                affinity = self._competitor_item_affinity(item, payload)
                if affinity <= 0:
                    continue
                competitor = (
                    session.get(Competitor, payload.get("competitor_id"))
                    if payload.get("competitor_id") is not None
                    else None
                )
                direction = str(payload.get("direction") or "watch").lower()
                sign = 1.0 if direction == "opportunity" else -1.0 if direction in {"threat", "drag"} else 0.0
                impact = max(0.0, min(0.30, float(payload.get("impact_score") or 0.0)))
                confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
                value *= (
                    1.0
                    + sign
                    * impact
                    * confidence
                    * self._signal_freshness(sig, half_life_sim_s=10800.0)
                    * self._competitor_proximity(competitor)
                    * self._competitor_cuisine_overlap(competitor, item_category)
                    * affinity
                )
            elif sig.type == SignalType.COMPETITOR_INTEL.value:
                dishes = [str(d).lower() for d in payload.get("popular_dishes") or []]
                if any(name in dish or dish in name for dish in dishes):
                    value *= 1.05
            elif sig.type == SignalType.COMPETITOR_UPDATE.value and payload.get("offers_changed"):
                summary = str(payload.get("summary") or "").lower()
                if item.category and str(item.category).lower() in summary:
                    value *= 0.97
        return min(1.6, max(0.7, value))

    @staticmethod
    def _competitor_item_affinity(item: MenuItem, payload: Dict[str, Any]) -> float:
        affected_items = {int(v) for v in (payload.get("affected_menu_items") or [])}
        if int(item.id) in affected_items:
            return 1.0
        categories = {str(v).lower() for v in (payload.get("affected_categories") or [])}
        item_category = str(item.category or "").lower()
        item_name = str(item.name or "").lower()
        if item_category and item_category in categories:
            return 0.8
        if item_name and any(category in item_name or item_name in category for category in categories):
            return 0.6
        return 0.3 if payload.get("competitor_id") is None and categories else 0.0

    @staticmethod
    def _competitor_proximity(competitor: Optional[Competitor]) -> float:
        if competitor is None:
            return 0.8
        radius = max(float(config.COMPETITOR_RADIUS_KM), 0.1)
        distance = max(float(competitor.distance_km or radius), 0.0)
        return min(1.0, max(0.2, 1.0 - (distance / radius) + 0.2))

    @staticmethod
    def _competitor_cuisine_overlap(competitor: Optional[Competitor], item_category: str) -> float:
        if competitor is None:
            return 0.85
        cuisine = {str(c).lower() for c in (competitor.cuisine or [])}
        if not cuisine:
            return 0.7
        if item_category and item_category in cuisine:
            return 1.0
        return 0.75

    def _signal_freshness(self, sig: Signal, half_life_sim_s: float) -> float:
        age = max(0.0, float(self.bus.sim_time) - float(sig.created_at or self.bus.sim_time))
        return max(0.25, 0.5 ** (age / max(half_life_sim_s, 1.0)))

    def _competitor_explanation(self, item: MenuItem, live: Iterable[Signal]) -> str:
        labels: List[str] = []
        for sig in live:
            payload = sig.payload or {}
            if sig.type == SignalType.COMPETITOR_MARKET_SIGNAL.value and self._competitor_item_affinity(item, payload) > 0:
                labels.append(str(payload.get("signal_kind") or "market signal").replace("_", " "))
            elif sig.type == SignalType.COMPETITOR_INTEL.value:
                dishes = [str(d).lower() for d in payload.get("popular_dishes") or []]
                name = str(item.name or "").lower()
                if any(name in dish or dish in name for dish in dishes):
                    labels.append("competitor favourite dish")
            elif sig.type == SignalType.COMPETITOR_UPDATE.value and payload.get("offers_changed"):
                labels.append("competitor offer change")
        if labels:
            return f"Competitor market signals affecting this item: {', '.join(labels[:3])}."
        return "No live competitor market signal is changing this item."

    def _review_multiplier(self, item: MenuItem, live: Iterable[Signal]) -> float:
        value = 1.0
        name = (item.name or "").lower()
        for sig in live:
            if sig.type != SignalType.REVIEW_INSIGHT.value:
                continue
            payload = sig.payload or {}
            mentions = [str(d).lower() for d in payload.get("dish_mentions") or []]
            if mentions and not any(name in d or d in name for d in mentions):
                continue
            severity = str(payload.get("severity") or "low").lower()
            summary = str(payload.get("summary") or "").lower()
            if "positive" in summary:
                value *= 1.05
            elif severity == "high":
                value *= 0.85
            elif severity == "medium":
                value *= 0.92
            else:
                value *= 0.98
        return value

    def _staff_multiplier(self, item: MenuItem, live: Iterable[Signal]) -> float:
        for sig in live:
            if sig.type != SignalType.STAFF_COVERAGE.value:
                continue
            payload = sig.payload or {}
            affected = payload.get("affected_items") or []
            if payload.get("covered") is False and (item.id in affected or item.station_id == payload.get("station_id")):
                return 0.0
        return 1.0

    def _weather_multiplier(self, session: Any, item: MenuItem) -> float:
        weather = session.query(WeatherLog).order_by(WeatherLog.sim_time.desc(), WeatherLog.id.desc()).first()
        if weather is None:
            return 1.0
        tags = {str(t).lower() for t in (item.weather_tags or [])}
        category = str(item.category or "").lower()
        condition = str(weather.condition or "").lower()
        temp_c = float(weather.temp_c or 0.0)
        cold = temp_c <= config.COLD_TEMP_C or condition in {"snow"}
        hot = temp_c >= config.HOT_TEMP_C

        if cold and tags.intersection({"ice_cream", "cold_drink", "cold", "salad"}):
            return 0.75
        if cold and ("comfort" in tags or category in {"pizza", "pasta", "burger", "main"}):
            return 1.18
        if hot and tags.intersection({"ice_cream", "cold_drink", "cold", "salad"}):
            return 1.2
        if condition in {"rain", "storm", "snow"} and "comfort" in tags:
            return 1.1
        if condition in {"rain", "storm"} and tags.intersection({"salad", "cold"}):
            return 0.9
        if condition == "clear" and tags.intersection({"salad", "cold"}):
            return 1.05
        return 1.0

    def _velocity_multiplier(self, item_id: int, baseline: float, daypart: str) -> float:
        if self.formatter is None:
            return 1.0
        rate = float(self.formatter.item_velocity(item_id) or 0.0)
        if rate <= 0:
            return 1.0
        start, end, _weight = DAYPART_SECONDS[daypart]
        daypart_len = max(end - start, 1)
        expected_recent = baseline * (config.VELOCITY_WINDOW_SIM_S / daypart_len)
        if expected_recent <= 0:
            return 1.0
        ratio = (rate * config.VELOCITY_WINDOW_SIM_S) / expected_recent
        low, high = config.VELOCITY_CLAMP
        return min(high, max(low, ratio))

    # ------------------------------------------------------------------
    # Operational constraints
    # ------------------------------------------------------------------

    def _apply_hard_constraints(
        self,
        session: Any,
        item: MenuItem,
        daypart: str,
        window: Dict[str, float],
        multipliers: Dict[str, float],
        explanations: Dict[str, str],
        live: Iterable[Signal],
    ) -> Optional[int]:
        hard_override: Optional[int] = None
        for sig in live:
            payload = sig.payload or {}
            if sig.type == SignalType.MENU_TOGGLE.value and payload.get("menu_item_id") == item.id:
                if payload.get("action") == "disable":
                    hard_override = 0
                    multipliers["availability"] = 0.0
                    explanations["availability"] = f"Menu disabled: {payload.get('reason') or 'inventory signal'}."
            elif sig.type == SignalType.STOCKOUT_RISK.value and item.id in (payload.get("affected_items") or []):
                hard_override = 0
                multipliers["availability"] = 0.0
                explanations["availability"] = f"Stockout risk on ingredient {payload.get('ingredient_id')}."
            elif sig.type == SignalType.PRODUCTION_CONSTRAINT.value:
                affected = payload.get("affected_menu_item_ids") or []
                categories = {str(cat).lower() for cat in (payload.get("affected_categories") or [])}
                category_match = str(item.category or "").lower() in categories
                if item.id in affected or category_match:
                    hard_override = 0
                    multipliers["production_constraint"] = 0.0
                    explanations["production_constraint"] = str(
                        payload.get("reason")
                        or f"Production constraint: {payload.get('constraint_ref')}"
                    )
            elif sig.type == SignalType.STAFF_COVERAGE.value:
                affected = payload.get("affected_items") or []
                if payload.get("covered") is False and (item.id in affected or item.station_id == payload.get("station_id")):
                    hard_override = 0
                    multipliers["staff_coverage"] = 0.0
                    explanations["staff_coverage"] = "Station has no remaining qualified staff."

        return hard_override

    @staticmethod
    def _is_blocked_for_batch(item_id: int, station_id: int, live: Iterable[Signal], reasons: List[str]) -> bool:
        blocked = False
        for sig in live:
            payload = sig.payload or {}
            if sig.type == SignalType.MENU_TOGGLE.value and payload.get("menu_item_id") == item_id and payload.get("action") == "disable":
                reasons.append(f"menu disabled: {payload.get('reason')}")
                blocked = True
            if sig.type == SignalType.STOCKOUT_RISK.value and item_id in (payload.get("affected_items") or []):
                reasons.append(f"stockout risk ingredient {payload.get('ingredient_id')}")
                blocked = True
            if sig.type == SignalType.STAFF_COVERAGE.value and payload.get("station_id") == station_id and payload.get("covered") is False:
                reasons.append("station unstaffed")
                blocked = True
        return blocked

    def _active_override(
        self,
        session: Any,
        item_id: int,
        daypart: str,
        window: Dict[str, float],
        now: float,
    ) -> Optional[ForecastOverride]:
        rows = (
            session.query(ForecastOverride)
            .filter(
                ForecastOverride.menu_item_id == item_id,
                ForecastOverride.daypart == daypart,
                ForecastOverride.status == "active",
            )
            .order_by(ForecastOverride.created_at.desc(), ForecastOverride.id.desc())
            .all()
        )
        active_rows: List[ForecastOverride] = []
        for row in rows:
            if float(row.valid_until or 0.0) <= now:
                row.status = "expired"
                continue
            if windows_overlap(row.window or {}, window):
                active_rows.append(row)
        if not active_rows:
            return None
        return sorted(active_rows, key=self._override_priority)[0]

    @staticmethod
    def _override_priority(override: ForecastOverride) -> Tuple[int, float, int]:
        operation = str(override.operation or "")
        source = str(override.source or "")
        authority = str(override.authority or "")
        if operation == "hard_zero_production":
            priority = 1
        elif authority == "user_instruction":
            priority = 2
        elif source == "llm":
            priority = 4
        else:
            priority = 3
        return (priority, -float(override.created_at or 0.0), -int(override.id or 0))

    @staticmethod
    def _apply_forecast_override(
        override: ForecastOverride,
        multipliers: Dict[str, float],
        explanations: Dict[str, str],
        hard_override: Optional[int],
    ) -> Optional[int]:
        reason = str(override.reason or "Forecast override is active.")
        operation = str(override.operation or "")
        source = str(override.source or "")
        value = override.value or {}
        if operation == "hard_zero_production":
            multipliers["authority_override"] = 0.0
            explanations["authority_override"] = reason
            return 0
        if operation == "set_target":
            if hard_override == 0 and DemandForecaster._has_zero_feasibility_constraint(multipliers):
                return hard_override
            try:
                qty = nearest_int(float(value.get("qty")))
            except (TypeError, ValueError, AttributeError):
                return hard_override
            multipliers["authority_override"] = 1.0
            explanations["authority_override"] = reason
            return max(0, qty)
        return hard_override

    def _persist_override(
        self,
        item_id: int,
        daypart: str,
        window: Dict[str, float],
        operation: str,
        value: Dict[str, Any],
        reason: str,
        source: str,
        authority: str,
        evidence: Dict[str, Any],
    ) -> ForecastOverride:
        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            active = (
                session.query(ForecastOverride)
                .filter(
                    ForecastOverride.menu_item_id == item_id,
                    ForecastOverride.daypart == daypart,
                    ForecastOverride.status == "active",
                )
                .all()
            )
            for row in active:
                if windows_overlap(row.window or {}, window):
                    if row.authority == "user_instruction" and authority != "user_instruction":
                        continue
                    row.status = "superseded"
            row = ForecastOverride(
                menu_item_id=item_id,
                daypart=daypart,
                window=window,
                operation=operation,
                value=value,
                reason=reason,
                source=source,
                authority=authority,
                status="active",
                created_at=now,
                valid_until=float(window.get("end", now)),
                evidence=evidence,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row
        finally:
            session.close()

    def persist_approved_llm_override(self, proposal: Dict[str, Any]) -> ForecastOverride:
        """Persist one operator-approved LLM forecast proposal."""
        item_id = int(proposal["menu_item_id"])
        operation = str(proposal.get("operation") or "set_target")
        qty = max(0, nearest_int(float(proposal.get("qty") or 0)))
        if operation not in {"set_target", "hard_zero_production"}:
            operation = "hard_zero_production" if qty == 0 else "set_target"
        value = {"qty": 0 if operation == "hard_zero_production" else qty}
        return self._persist_override(
            item_id,
            str(proposal.get("daypart") or current_daypart(float(self.bus.sim_time))),
            dict(proposal.get("window") or current_window(float(self.bus.sim_time))[1]),
            operation,
            value,
            str(proposal.get("reason") or "Approved LLM forecast proposal."),
            "llm",
            "approved_llm",
            {
                "approval_type": "forecast_override_proposal",
                "source_job_id": proposal.get("source_job_id"),
                "source_run_id": proposal.get("source_run_id"),
                "confidence": proposal.get("confidence"),
                "evidence": proposal.get("evidence"),
                "deterministic_qty": proposal.get("deterministic_qty"),
            },
        )

    # ------------------------------------------------------------------
    # LLM optimization
    # ------------------------------------------------------------------

    def _llm_plan(
        self,
        session: Any,
        prepared: List[Dict[str, Any]],
        live: Iterable[Signal],
        daypart: str,
        window: Dict[str, float],
    ) -> Dict[str, Any]:
        if self.llm is None:
            return {}
        material_context = self._material_forecast_context(prepared, live)
        context = {
            "sim_time": float(self.bus.sim_time),
            "daypart": daypart,
            "window": window,
            "instruction": (
                "For most items, accept deterministic_recommendation.forecast_qty exactly. "
                "Only change final_qty when material_context is true for that item or an "
                "active signal/constraint explicitly justifies it."
            ),
            "weather": self._weather_context(session),
            "temperature_thresholds_c": {
                "cold_lte": config.COLD_TEMP_C,
                "hot_gte": config.HOT_TEMP_C,
            },
            "multiplier_limits": {
                "min": config.LLM_MULTIPLIER_CLAMP[0],
                "max": config.LLM_MULTIPLIER_CLAMP[1],
            },
            "final_qty_guardrails": {
                "copy_deterministic_by_default": True,
                "never_override_zero_feasibility_constraints": True,
                "require_explicit_signal_for_material_changes": True,
                "large_change_without_material_context_is_invalid": True,
            },
            "material_context": material_context,
            "items": [self._prepared_context(entry) for entry in prepared],
            "live_signals": [self._signal_context(sig) for sig in live],
            "memory": self._memory_context(session),
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the final forecast decision layer for a restaurant simulation. "
                    "The deterministic model is usually accurate and should be treated as the "
                    "default expert recommendation. Your main job is to return the final forecast "
                    "in the required JSON format. For each item, copy "
                    "deterministic_recommendation.forecast_qty unless there is explicit evidence "
                    "from active constraints, voice instructions, menu/stockout/staff signals, "
                    "events, competitor/review signals, weather, recent velocity, or memory that "
                    "requires a change. Do not recalculate baseline math. Do not double-count "
                    "multipliers already present in deterministic_recommendation. Hard feasibility "
                    "zeros from voice, menu disable, stockout, or staff coverage are guardrails: "
                    "the final_qty must remain 0. If changing a non-zero forecast, keep the change "
                    "bounded and evidence-bound. Return JSON only with item_final_forecasts, "
                    "global_notes, memory_updates, and confidence. item_final_forecasts must contain "
                    "{menu_item_id, final_qty, confidence, decision, changed, reason, evidence}. "
                    "Use decision='accept_deterministic' when copying the recommendation."
                ),
            },
            {"role": "user", "content": str(context)},
        ]
        schema = {
            "type": "object",
            "properties": {
                "item_final_forecasts": {"type": "array"},
                "item_adjustments": {"type": "array"},
                "global_notes": {"type": "array"},
                "memory_updates": {"type": "array"},
                "confidence": {"type": "number"},
            },
            "required": ["global_notes", "memory_updates"],
        }
        try:
            result = self.llm.complete(
                messages,
                json_schema=schema,
                max_tokens=1200,
                use_site="forecaster_optimization",
                temperature=0.1,
            )
        except Exception:  # noqa: BLE001 - deterministic fallback is the contract.
            return {}
        if not isinstance(result, dict) or result.get("note") == CANNED_NOTE:
            return {}
        return result

    def _llm_batch_plan(
        self,
        daypart: str,
        window: Dict[str, float],
        live: Iterable[Signal],
    ) -> Dict[str, Any]:
        if self.llm is None:
            return {}
        session = self.db_session_factory()
        try:
            forecasts = (
                session.query(Forecast)
                .order_by(Forecast.generated_at.desc(), Forecast.id.desc())
                .limit(24)
                .all()
            )
            definitions = session.query(BatchDefinition).order_by(BatchDefinition.id.asc()).all()
            context = {
                "daypart": daypart,
                "window": window,
                "forecasts": [self._forecast_to_dict(row) for row in forecasts],
                "batch_definitions": [self._batch_definition_to_dict(row) for row in definitions],
                "live_signals": [self._signal_context(sig) for sig in live],
            }
        finally:
            session.close()
        messages = [
            {
                "role": "system",
                "content": (
                    "You may override restaurant batch decisions for maximum operational utility. "
                    "Return JSON with batch_adjustments only. Each adjustment may set decision "
                    "cook|skip and qty as an integer. Keep reasons concise."
                ),
            },
            {"role": "user", "content": str(context)},
        ]
        schema = {
            "type": "object",
            "properties": {"batch_adjustments": {"type": "array"}},
            "required": ["batch_adjustments"],
        }
        try:
            result = self.llm.complete(
                messages,
                json_schema=schema,
                max_tokens=600,
                use_site="forecaster_optimization",
                temperature=0.2,
            )
        except Exception:  # noqa: BLE001 - batch optimization is optional.
            return {}
        if not isinstance(result, dict) or result.get("note") == CANNED_NOTE:
            return {}
        return result

    @staticmethod
    def _current_window_forecast(
        forecasts: Iterable[Forecast],
        daypart: str,
        window: Dict[str, float],
        now: float,
    ) -> Optional[Forecast]:
        for forecast in forecasts:
            if float(forecast.generated_at or 0.0) > now:
                continue
            if forecast.daypart != daypart:
                continue
            if not forecast_window_matches(forecast.window or {}, window):
                continue
            return forecast
        return None

    @staticmethod
    def _adjustment_for(plan: Dict[str, Any], item_id: int) -> Dict[str, Any]:
        for entry in plan.get("item_adjustments") or []:
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("menu_item_id")) == int(item_id):
                    return entry
            except (TypeError, ValueError):
                continue
        return {}

    @staticmethod
    def _final_decision_for(plan: Dict[str, Any], item_id: int) -> Dict[str, Any]:
        for entry in plan.get("item_final_forecasts") or []:
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("menu_item_id")) == int(item_id):
                    return entry
            except (TypeError, ValueError):
                continue
        return {}

    def _apply_llm_final_decision(
        self,
        item_id: int,
        daypart: str,
        window: Dict[str, float],
        multipliers: Dict[str, float],
        explanations: Dict[str, str],
        hard_override: Optional[int],
        deterministic_recommendation: Dict[str, Any],
        decision: Dict[str, Any],
        after_commit: List[Tuple[str, Any]],
        persist_override: bool = False,
    ) -> Optional[int]:
        deterministic_qty = int(deterministic_recommendation.get("forecast_qty") or 0)
        reason = str(decision.get("reason") or "LLM finalizer accepted the deterministic forecast.")
        final_qty = self._validated_llm_final_qty(
            decision,
            deterministic_qty,
            multipliers,
            hard_override,
        )
        changed = final_qty != deterministic_qty

        if changed:
            hard_override = final_qty
            multipliers["llm_final"] = 1.0
            explanations["llm_final"] = reason
            if persist_override:
                operation = "hard_zero_production" if final_qty == 0 else "set_target"
                after_commit.append(
                    (
                        "forecast_override",
                        (
                            item_id,
                            daypart,
                            window,
                            operation,
                            {"qty": max(0, final_qty)},
                            reason,
                            "llm",
                            "approved_llm",
                            {"decision": decision, "trigger": "manual_finalizer"},
                        ),
                    )
                )
        else:
            multipliers["llm_finalizer"] = 1.0
            explanations["llm_finalizer"] = (
                reason
                if str(decision.get("decision") or "") != "accept_deterministic"
                else "LLM finalizer accepted the deterministic recommendation."
            )

        after_commit.append(
            (
                "remember",
                (
                    "menu_item",
                    str(item_id),
                    {
                        "title": "LLM final forecast decision",
                        "summary": explanations.get("llm_final", explanations.get("llm_finalizer", reason)),
                        "decision": decision,
                        "deterministic_recommendation": deterministic_recommendation,
                    },
                    {"menu_item_id": item_id},
                    float(decision.get("confidence") or deterministic_recommendation.get("confidence") or 0.8),
                    "llm",
                    SECONDS_PER_DAY,
                ),
            )
        )
        return hard_override

    def _validated_llm_final_qty(
        self,
        decision: Dict[str, Any],
        deterministic_qty: int,
        multipliers: Dict[str, float],
        hard_override: Optional[int],
    ) -> int:
        if hard_override == 0 and self._has_zero_feasibility_constraint(multipliers):
            return 0
        if (
            decision.get("changed") is False
            or str(decision.get("decision") or "") == "accept_deterministic"
        ):
            return max(0, deterministic_qty)
        try:
            proposed = nearest_int(float(decision.get("final_qty")))
        except (TypeError, ValueError):
            return max(0, deterministic_qty)
        proposed = max(0, proposed)
        if proposed == deterministic_qty:
            return proposed
        evidence = decision.get("evidence")
        if not evidence:
            return max(0, deterministic_qty)
        if not self._has_material_change_evidence(multipliers):
            return max(0, deterministic_qty)
        maximum = max(20, deterministic_qty + 8, nearest_int(max(deterministic_qty, 1) * 2.0))
        return min(proposed, maximum)

    @staticmethod
    def _has_zero_feasibility_constraint(multipliers: Dict[str, float]) -> bool:
        return any(
            float(multipliers.get(key, 1.0)) <= 0
            for key in (
                "availability",
                "production_constraint",
                "staff_coverage",
                "authority_override",
                "llm_override",
            )
        )

    @staticmethod
    def _has_material_change_evidence(multipliers: Dict[str, float]) -> bool:
        material_keys = {
            "availability",
            "production_constraint",
            "staff_coverage",
            "event",
            "competitor_market",
            "review",
            "weather",
            "recent_velocity",
            "authority_override",
        }
        for key, value in multipliers.items():
            if key in material_keys and abs(float(value) - 1.0) >= 0.03:
                return True
            if key.startswith("llm"):
                return True
        return False

    def _apply_llm_adjustment(
        self,
        item_id: int,
        daypart: str,
        window: Dict[str, float],
        multipliers: Dict[str, float],
        explanations: Dict[str, str],
        hard_override: Optional[int],
        adjustment: Dict[str, Any],
        after_commit: List[Tuple[str, Any]],
        persist_override: bool = False,
    ) -> Optional[int]:
        reason = str(adjustment.get("reason") or "LLM optimizer adjusted forecast.")
        raw_multipliers = adjustment.get("multipliers") or {}
        if isinstance(raw_multipliers, dict):
            low, high = config.LLM_MULTIPLIER_CLAMP
            for key, raw_value in raw_multipliers.items():
                try:
                    value = max(low, min(high, float(raw_value)))
                except (TypeError, ValueError):
                    continue
                multipliers[str(key)] = round(value, 3)
                explanations[str(key)] = reason
                if value in config.LLM_EXTREME_MULTIPLIERS:
                    after_commit.append(
                        (
                            "log",
                            (
                                "forecast",
                                f"Extreme LLM multiplier logged for item {item_id}: {key} x{value:.2f}",
                                {"menu_item_id": item_id, "key": key, "value": value, "reason": reason},
                            ),
                        )
                    )
        elif isinstance(raw_multipliers, (int, float)):
            low, high = config.LLM_MULTIPLIER_CLAMP
            value = max(low, min(high, float(raw_multipliers)))
            multipliers["llm_overall"] = round(value, 3)
            explanations["llm_overall"] = reason
            if value in config.LLM_EXTREME_MULTIPLIERS:
                after_commit.append(
                    (
                        "log",
                        (
                            "forecast",
                            f"Extreme LLM multiplier logged for item {item_id}: llm_overall x{value:.2f}",
                            {"menu_item_id": item_id, "key": "llm_overall", "value": value, "reason": reason},
                        ),
                    )
                )

        if adjustment.get("hard_override_qty") is not None:
            try:
                override = int(adjustment["hard_override_qty"])
            except (TypeError, ValueError):
                override = hard_override if hard_override is not None else -1
            if override == 0:
                hard_override = 0
                multipliers["llm_override"] = 0.0
                explanations["llm_override"] = reason
                if persist_override:
                    after_commit.append(
                        (
                            "forecast_override",
                            (
                                item_id,
                                daypart,
                                window,
                                "hard_zero_production",
                                {"qty": 0},
                                reason,
                                "llm",
                                "approved_llm",
                                {"adjustment": adjustment, "trigger": "manual_optimization"},
                            ),
                        )
                    )
        else:
            target_qty = self._target_qty_from_adjustment(adjustment)
            if target_qty is not None:
                if hard_override == 0 and self._has_zero_feasibility_constraint(multipliers):
                    return hard_override
                hard_override = max(0, target_qty)
                multipliers["llm_target"] = 1.0
                explanations["llm_target"] = reason
                if persist_override:
                    after_commit.append(
                        (
                            "forecast_override",
                            (
                                item_id,
                                daypart,
                                window,
                                "set_target",
                                {"qty": max(0, target_qty)},
                                reason,
                                "llm",
                                "approved_llm",
                                {"adjustment": adjustment, "trigger": "manual_optimization"},
                            ),
                        )
                    )
        after_commit.append(
            (
                "remember",
                (
                    "menu_item",
                    str(item_id),
                    {"title": "LLM forecast adjustment", "summary": reason, "adjustment": adjustment},
                    {"menu_item_id": item_id},
                    float(adjustment.get("confidence") or 0.8),
                    "llm",
                    SECONDS_PER_DAY,
                ),
            )
        )
        return hard_override

    @staticmethod
    def _target_qty_from_adjustment(adjustment: Dict[str, Any]) -> Optional[int]:
        for key in ("target_forecast_qty", "forecast_qty", "forecast", "qty", "override_qty"):
            if adjustment.get(key) is None:
                continue
            try:
                return nearest_int(float(adjustment[key]))
            except (TypeError, ValueError):
                continue
        return None

    def _proposed_final_qty(
        self,
        decision: Dict[str, Any],
        deterministic_qty: int,
        multipliers: Dict[str, float],
        hard_override: Optional[int],
        *,
        from_finalizer: bool,
    ) -> Optional[int]:
        if not decision:
            return None
        if hard_override == 0 and self._has_zero_feasibility_constraint(multipliers):
            return 0
        if from_finalizer:
            return self._validated_llm_final_qty(
                decision,
                deterministic_qty,
                multipliers,
                hard_override,
            )
        if decision.get("hard_override_qty") is not None:
            try:
                override = nearest_int(float(decision.get("hard_override_qty")))
            except (TypeError, ValueError):
                return None
            return max(0, override)
        target_qty = self._target_qty_from_adjustment(decision)
        if target_qty is None:
            return None
        if not self._has_material_change_evidence(multipliers):
            return None
        return max(0, target_qty)

    @staticmethod
    def _queue_llm_plan_memory(plan: Dict[str, Any], after_commit: List[Tuple[str, Any]]) -> None:
        for index, note in enumerate(plan.get("global_notes") or []):
            summary = note if isinstance(note, str) else str((note or {}).get("summary") or note)
            after_commit.append(
                (
                    "log",
                    (
                        "forecast",
                        f"LLM optimizer note: {summary}",
                        {"source": "llm", "index": index, "note": note},
                    ),
                )
            )
        for index, update in enumerate(plan.get("memory_updates") or []):
            if isinstance(update, dict):
                title = str(update.get("title") or "LLM learned context")
                summary = str(update.get("summary") or update.get("insight") or update)
                confidence = float(update.get("confidence") or plan.get("confidence") or 0.75)
            else:
                title = "LLM learned context"
                summary = str(update)
                confidence = float(plan.get("confidence") or 0.75)
            after_commit.append(
                (
                    "remember",
                    (
                        "global",
                        f"llm_plan:{index}",
                        {"title": title, "summary": summary, "update": update},
                        {"source": "forecaster_optimization"},
                        confidence,
                        "llm",
                        SECONDS_PER_DAY,
                    ),
                )
            )

    @staticmethod
    def _batch_adjustment_for(plan: Dict[str, Any], definition_id: int, item_id: int) -> Dict[str, Any]:
        for entry in plan.get("batch_adjustments") or []:
            if not isinstance(entry, dict):
                continue
            try:
                matches_definition = int(entry.get("batch_definition_id", -1)) == int(definition_id)
                matches_item = int(entry.get("menu_item_id", -1)) == int(item_id)
            except (TypeError, ValueError):
                continue
            if matches_definition or matches_item:
                return entry
        return {}

    def _apply_llm_batch_decision(
        self,
        adjustment: Dict[str, Any],
        current_decision: str,
        current_qty: int,
        forecast_qty: int,
        definition: BatchDefinition,
        available: bool,
    ) -> Tuple[str, int]:
        decision = str(adjustment.get("decision") or current_decision).lower()
        if decision not in {"cook", "skip"}:
            decision = current_decision
        if not available:
            decision = "skip"
        try:
            qty = int(adjustment.get("qty"))
        except (TypeError, ValueError):
            qty = current_qty
        if decision == "skip":
            return decision, 0
        qty = qty if qty > 0 else forecast_qty
        return decision, self._round_batch_qty(qty, definition)

    # ------------------------------------------------------------------
    # Memory and context
    # ------------------------------------------------------------------

    def _remember_run_summary(self, rows: List[Dict[str, Any]], optimize: bool, trigger_reason: str) -> None:
        if not rows:
            return
        total = int(sum(float(row.get("qty") or 0.0) for row in rows))
        self._remember(
            "global",
            "last_run",
            {
                "title": "Latest forecast run",
                "summary": f"{len(rows)} items forecast, {total} total plates.",
                "optimized": bool(optimize),
            },
            {"trigger": trigger_reason, "forecast_ids": [row.get("id") for row in rows]},
            1.0,
            "deterministic",
            valid_for=SECONDS_PER_DAY,
        )

    def _remember(
        self,
        scope_type: str,
        scope_ref: str,
        insight: Dict[str, Any],
        evidence: Dict[str, Any],
        confidence: float,
        source: str,
        valid_for: float,
    ) -> None:
        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            row = DemandForecasterMemory(
                scope_type=scope_type,
                scope_ref=str(scope_ref),
                insight=insight,
                evidence=evidence,
                confidence=max(0.0, min(1.0, float(confidence))),
                created_at=now,
                last_seen_at=now,
                valid_until=now + valid_for,
                source=source,
            )
            session.add(row)
            session.commit()
        finally:
            session.close()

    @staticmethod
    def _memory_context(session: Any) -> List[Dict[str, Any]]:
        rows = (
            session.query(DemandForecasterMemory)
            .order_by(DemandForecasterMemory.last_seen_at.desc(), DemandForecasterMemory.id.desc())
            .limit(20)
            .all()
        )
        return [
            {
                "scope_type": row.scope_type,
                "scope_ref": row.scope_ref,
                "insight": row.insight,
                "confidence": row.confidence,
                "source": row.source,
            }
            for row in rows
        ]

    def _suggestion_context(self) -> Dict[str, Any]:
        session = self.db_session_factory()
        try:
            forecasts = (
                session.query(Forecast)
                .order_by(Forecast.generated_at.desc())
                .limit(20)
                .all()
            )
            batches = (
                session.query(Batch)
                .order_by(Batch.decided_at.desc())
                .limit(20)
                .all()
            )
            return {
                "sim_time": float(self.bus.sim_time),
                "forecasts": [self._forecast_to_dict(row) for row in forecasts],
                "batches": [self._batch_to_dict(row) for row in batches],
                "memory": self._memory_context(session),
            }
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Serialization and small helpers
    # ------------------------------------------------------------------

    def _weather_context(self, session: Any) -> Optional[Dict[str, Any]]:
        row = session.query(WeatherLog).order_by(WeatherLog.sim_time.desc(), WeatherLog.id.desc()).first()
        return self._weather_to_dict(row) if row is not None else None

    @staticmethod
    def _prepared_context(entry: Dict[str, Any]) -> Dict[str, Any]:
        item = entry["item"]
        return {
            "menu_item_id": item.id,
            "name": item.name,
            "category": item.category,
            "station_id": item.station_id,
            "weather_tags": item.weather_tags or [],
            "description": item.description,
            "baseline": entry["baseline"],
            "multipliers": entry["multipliers"],
            "explanations": entry.get("explanations") or {},
            "hard_override": entry.get("hard_override"),
            "deterministic_recommendation": entry.get("deterministic_recommendation") or {},
            "material_context": DemandForecaster._has_material_change_evidence(entry.get("multipliers") or {}),
        }

    @staticmethod
    def _signal_context(signal: Signal) -> Dict[str, Any]:
        return {
            "type": signal.type,
            "source": signal.source,
            "payload": signal.payload,
            "created_at": signal.created_at,
            "expires_at": signal.expires_at,
        }

    @staticmethod
    def _material_forecast_context(
        prepared: Iterable[Dict[str, Any]],
        live: Iterable[Signal],
    ) -> Dict[str, Any]:
        active_signals = []
        for signal in live:
            if signal.type in {
                SignalType.DEMAND_EVENT.value,
                SignalType.PRODUCTION_CONSTRAINT.value,
                SignalType.STAFF_AVAILABILITY.value,
                SignalType.INGREDIENT_SHORTAGE_REPORTED.value,
                SignalType.EXPIRY_USE_PRIORITY.value,
                SignalType.CUSTOMER_FEEDBACK_NOTE.value,
                SignalType.COMPETITOR_NOTE.value,
                SignalType.STAFF_COVERAGE.value,
                SignalType.MENU_TOGGLE.value,
                SignalType.STOCKOUT_RISK.value,
                SignalType.COMPETITOR_UPDATE.value,
                SignalType.COMPETITOR_INTEL.value,
                SignalType.COMPETITOR_MARKET_SIGNAL.value,
                SignalType.REVIEW_INSIGHT.value,
                SignalType.WEATHER_UPDATE.value,
            }:
                active_signals.append(
                    {
                        "type": signal.type,
                        "source": signal.source,
                        "payload": signal.payload,
                        "expires_at": signal.expires_at,
                    }
                )
        return {
            "has_active_signals": bool(active_signals),
            "active_signals": active_signals[:20],
            "items_with_material_multipliers": [
                int(entry["item"].id)
                for entry in prepared
                if DemandForecaster._has_material_change_evidence(entry.get("multipliers") or {})
            ],
        }

    @staticmethod
    def _round_batch_qty(forecast_qty: float, definition: BatchDefinition) -> int:
        step = float(definition.batch_size_step or 1.0)
        minimum = float(definition.batch_size_min or 0.0)
        maximum = float(definition.batch_size_max or forecast_qty)
        rounded = round(float(forecast_qty) / step) * step if step > 0 else nearest_int(float(forecast_qty))
        return int(max(0, min(maximum, max(minimum, rounded))))

    @staticmethod
    def _forecast_to_dict(row: Forecast) -> Dict[str, Any]:
        data = {col.key: getattr(row, col.key) for col in row.__table__.columns}
        data["forecast_qty"] = int(round(float(data.get("forecast_qty") or 0)))
        return data

    @staticmethod
    def _batch_to_dict(row: Batch) -> Dict[str, Any]:
        data = {col.key: getattr(row, col.key) for col in row.__table__.columns}
        data["planned_qty"] = int(round(float(data.get("planned_qty") or 0)))
        return data

    @staticmethod
    def _batch_definition_to_dict(row: BatchDefinition) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _item_to_dict(row: MenuItem) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _weather_to_dict(row: WeatherLog) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _write_trace_ledger(
        session: Any,
        forecast: Forecast,
        run_id: str,
        item: MenuItem,
        daypart: str,
        window: Dict[str, float],
        trace: Dict[str, Any],
        now: float,
    ) -> None:
        session.add(
            ForecastTrace(
                forecast_id=forecast.id,
                run_id=run_id,
                menu_item_id=item.id,
                daypart=daypart,
                window=window,
                trace=trace,
                summary=str(trace.get("summary") or ""),
                created_at=now,
            )
        )
        for entry in trace.get("adjustments") or []:
            if not isinstance(entry, dict):
                continue
            session.add(
                ForecastAdjustment(
                    forecast_id=forecast.id,
                    run_id=run_id,
                    menu_item_id=item.id,
                    stage=str(entry.get("stage") or ""),
                    source=str(entry.get("source") or ""),
                    modifier_key=str(entry.get("key") or ""),
                    operation=str(entry.get("operation") or ""),
                    value={"value": entry.get("value")},
                    reason=str(entry.get("reason") or ""),
                    evidence={
                        "trace_version": trace.get("version", 1),
                        "signals": entry.get("evidence") or [],
                    },
                    created_at=now,
                )
            )

    def _forecast_trace(
        self,
        *,
        run_id: str,
        item: MenuItem,
        daypart: str,
        window: Dict[str, float],
        baseline: float,
        multipliers: Dict[str, float],
        explanations: Dict[str, str],
        raw_qty: float,
        latent_qty: float,
        forecast_qty: int,
        confidence: float,
        hard_override: Optional[int],
        optimized: bool,
        trigger_reason: str,
        deterministic_recommendation: Optional[Dict[str, Any]] = None,
        finalizer_decision: Optional[Dict[str, Any]] = None,
        live_signals: Optional[Iterable[Signal]] = None,
    ) -> Dict[str, Any]:
        adjustments = [
            {
                "source": self._modifier_source(key),
                "stage": self._modifier_stage(key),
                "key": key,
                "operation": self._modifier_operation(key, value),
                "value": round(float(value), 3),
                "reason": explanations.get(key, key),
                "evidence": self._modifier_evidence(key, item, live_signals or []),
            }
            for key, value in multipliers.items()
        ]
        constraints = [
            entry for entry in adjustments
            if entry["operation"].startswith("hard_zero") or entry["stage"] == "feasibility"
        ]
        zero_reason = self._zero_reason(hard_override, multipliers)
        return {
            "run_id": run_id,
            "version": 1,
            "scope": {
                "menu_item_id": int(item.id),
                "item_name": item.name,
                "daypart": daypart,
                "window": window,
            },
            "baseline": {"qty": round(float(baseline), 2), "source": "historical_or_settings"},
            "deterministic_recommendation": deterministic_recommendation or {},
            "llm_final_decision": finalizer_decision or {},
            "adjustments": adjustments,
            "constraints": constraints,
            "final": {
                "constrained_raw_qty": round(float(raw_qty), 3),
                "latent_demand_qty": round(float(latent_qty), 3),
                "servable_demand_qty": int(forecast_qty),
                "production_recommendation_qty": int(forecast_qty),
                "confidence": round(float(confidence), 3),
                "hard_override": hard_override,
                "zero_reason": zero_reason,
            },
            "summary": self._top_trace_reason(adjustments, zero_reason),
            "optimized": bool(optimized),
            "trigger": trigger_reason,
        }

    @staticmethod
    def _modifier_source(key: str) -> str:
        if key == "authority_override":
            return "authority_resolver"
        if key.startswith("llm"):
            return "llm"
        if key in {"availability", "production_constraint", "staff_coverage"}:
            return "operational_constraint"
        return "deterministic"

    @staticmethod
    def _modifier_stage(key: str) -> str:
        if key == "authority_override":
            return "authority"
        if key in {"availability", "production_constraint", "staff_coverage"}:
            return "feasibility"
        if key.startswith("llm"):
            return "llm_proposal"
        return "demand_modifier"

    def _modifier_evidence(
        self,
        key: str,
        item: MenuItem,
        live_signals: Iterable[Signal],
    ) -> List[Dict[str, Any]]:
        if key != "competitor_market":
            return []
        evidence: List[Dict[str, Any]] = []
        for sig in live_signals:
            if sig.type != SignalType.COMPETITOR_MARKET_SIGNAL.value:
                continue
            payload = sig.payload or {}
            if self._competitor_item_affinity(item, payload) <= 0:
                continue
            evidence.append(
                {
                    "signal_id": sig.signal_id,
                    "signal_kind": payload.get("signal_kind"),
                    "competitor_id": payload.get("competitor_id"),
                    "direction": payload.get("direction"),
                    "impact_score": payload.get("impact_score"),
                    "confidence": payload.get("confidence"),
                    "evidence": payload.get("evidence") or [],
                }
            )
        return evidence[:5]

    @staticmethod
    def _modifier_operation(key: str, value: float) -> str:
        if float(value) <= 0 and key in {
            "availability",
            "production_constraint",
            "staff_coverage",
            "llm_override",
            "authority_override",
        }:
            return "hard_zero_production"
        if key == "llm_target":
            return "set_target"
        return "multiply"

    @staticmethod
    def _zero_reason(hard_override: Optional[int], multipliers: Dict[str, float]) -> Optional[str]:
        if hard_override != 0:
            return None
        if float(multipliers.get("availability", 1.0)) <= 0:
            return "availability_blocked"
        if float(multipliers.get("production_constraint", 1.0)) <= 0:
            return "production_constraint"
        if float(multipliers.get("staff_coverage", 1.0)) <= 0:
            return "staff_unavailable"
        if float(multipliers.get("llm_override", 1.0)) <= 0:
            return "llm_override"
        if float(multipliers.get("authority_override", 1.0)) <= 0:
            return "forecast_override"
        return "hard_override"

    @staticmethod
    def _top_trace_reason(adjustments: List[Dict[str, Any]], zero_reason: Optional[str]) -> str:
        authority = next(
            (
                entry for entry in adjustments
                if entry.get("key") == "authority_override"
            ),
            None,
        )
        if authority:
            return str(authority.get("reason") or "Forecast override is active.")
        if zero_reason:
            constraint = next(
                (
                    entry for entry in adjustments
                    if entry.get("operation") == "hard_zero_production"
                ),
                None,
            )
            if constraint:
                return str(constraint.get("reason") or zero_reason)
            return zero_reason
        ranked = sorted(
            adjustments,
            key=lambda entry: abs(float(entry.get("value") or 1.0) - 1.0),
            reverse=True,
        )
        for entry in ranked:
            if abs(float(entry.get("value") or 1.0) - 1.0) >= 0.03:
                return str(entry.get("reason") or entry.get("key") or "Forecast adjusted.")
        return "Forecast generated with no major active demand driver."

    def _latent_demand_qty(
        self,
        baseline: float,
        multipliers: Dict[str, float],
        hard_override: Optional[int],
    ) -> float:
        if hard_override is not None and self._is_target_override(multipliers):
            return float(max(0, hard_override))
        qty = float(baseline)
        for key, value in multipliers.items():
            if not self._counts_toward_latent_demand(key, float(value)):
                continue
            qty *= float(value)
        return max(0.0, qty)

    @staticmethod
    def _is_target_override(multipliers: Dict[str, float]) -> bool:
        return "llm_target" in multipliers or (
            "authority_override" in multipliers and float(multipliers.get("authority_override", 0.0)) > 0
        )

    @staticmethod
    def _counts_toward_latent_demand(key: str, value: float) -> bool:
        if key in {"availability", "production_constraint", "staff_coverage"}:
            return False
        if key in {"llm_override", "authority_override"} and value <= 0:
            return False
        return True


def current_daypart(now: float) -> str:
    tod = now % SECONDS_PER_DAY
    for name, (start, end, _weight) in DAYPART_SECONDS.items():
        if start <= tod < end:
            return name
    return "late" if tod >= DAY_CLOSE_OFFSET else "breakfast"


def current_window(now: float) -> Tuple[str, Dict[str, float]]:
    day = math.floor(now / SECONDS_PER_DAY)
    daypart = current_daypart(now)
    start, end, _weight = DAYPART_SECONDS[daypart]
    window_start = max(now, day * SECONDS_PER_DAY + start)
    window_end = day * SECONDS_PER_DAY + end
    if window_end <= window_start:
        window_end = day * SECONDS_PER_DAY + DAY_CLOSE_OFFSET
    return daypart, {"start": float(window_start), "end": float(window_end)}


def _window_fraction(daypart: str, window: Dict[str, float]) -> float:
    start, end, _weight = DAYPART_SECONDS[daypart]
    daypart_len = max(float(end - start), 1.0)
    window_len = max(float(window.get("end", 0.0)) - float(window.get("start", 0.0)), 0.0)
    return min(1.0, max(0.0, window_len / daypart_len))


def forecast_run_id(now: float, daypart: str, window: Dict[str, float], trigger_reason: str) -> str:
    clean_trigger = re.sub(r"[^a-zA-Z0-9_:-]+", "_", trigger_reason or "manual")[:40]
    return f"fr:{int(now)}:{daypart}:{int(float(window['start']))}:{clean_trigger}"


def forecast_window_matches(candidate: Dict[str, Any], window: Dict[str, float]) -> bool:
    try:
        return (
            math.isclose(float(candidate.get("start")), float(window.get("start")), abs_tol=0.001)
            and math.isclose(float(candidate.get("end")), float(window.get("end")), abs_tol=0.001)
        )
    except (TypeError, ValueError):
        return False



def nearest_int(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def confidence_from(multipliers: Dict[str, float]) -> float:
    values = [float(v) for v in multipliers.values()]
    spread = (max(values) - min(values)) if values else 0.0
    return round(1.0 / (1.0 + spread), 3)


def windows_overlap(a: Dict[str, float], b: Dict[str, float]) -> bool:
    return float(a.get("start", 0.0)) < float(b.get("end", 0.0)) and float(b.get("start", 0.0)) < float(a.get("end", 0.0))
