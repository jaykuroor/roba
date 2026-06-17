"""Demand Forecaster agent.

This module owns the Track A forecasting stack:

1. deterministic baseline and multiplier calculation,
2. operational constraint resolution from live signals and vague user facts,
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
    Attendance,
    Batch,
    BatchDefinition,
    DemandForecasterMemory,
    Forecast,
    MenuItem,
    OrderLine,
    Signal,
    SimSettings,
    Staff,
    StaffDishSkill,
    StaffStation,
    Station,
    WeatherLog,
)
from core.pos_simulator import active_injections
from core.signals import SignalType


def _hhmm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 3600 + int(m) * 60


DAYPART_SECONDS = {
    name: (_hhmm(start), _hhmm(end), weight)
    for name, (start, end, weight) in config.DAYPARTS.items()
}


STATION_ALIASES: Dict[str, List[str]] = {
    "chinese": ["chinese", "wok", "asian", "noodle", "dim sum", "stir fry"],
    "pizza": ["pizza", "oven", "grill"],
    "pasta": ["pasta", "noodle"],
    "cold": ["cold", "salad", "dessert", "beverage", "ice cream"],
    "bar": ["bar", "drink", "beverage"],
    "fry": ["fry", "fries", "fryer"],
    "grill": ["grill", "burger", "steak", "tandoor"],
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
        self.subscribe(["forecasting"])

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            lambda: self.run_forecast("interval", optimize=self.llm_auto_mode),
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
            SignalType.REVIEW_INSIGHT.value,
            SignalType.WEATHER_UPDATE.value,
            SignalType.USER_FACT.value,
            SignalType.MENU_TOGGLE.value,
            SignalType.STOCKOUT_RISK.value,
        }:
            self.run_forecast(
                f"signal:{signal.type}",
                optimize=self.llm_auto_mode,
            )

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

    # ------------------------------------------------------------------
    # Public forecast and batch API
    # ------------------------------------------------------------------

    def run_forecast(
        self,
        trigger_reason: str = "manual",
        optimize: bool = False,
    ) -> List[Forecast]:
        """Forecast every active menu item and emit integer demand signals."""
        now = float(self.bus.sim_time)
        daypart, window = current_window(now)
        live = self.bus.live()
        rows: List[Forecast] = []
        row_summaries: List[Dict[str, Any]] = []
        after_commit: List[Tuple[str, Any]] = []

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
            llm_plan = self._llm_plan(session, prepared, live, daypart, window) if optimize else {}
            if llm_plan:
                self._queue_llm_plan_memory(llm_plan, after_commit)

            for prepared_item in prepared:
                item = prepared_item["item"]
                baseline = float(prepared_item["baseline"])
                multipliers = dict(prepared_item["multipliers"])
                explanations = dict(prepared_item["explanations"])
                hard_override = prepared_item.get("hard_override")
                llm_adjustment = self._adjustment_for(llm_plan, item.id)

                if llm_adjustment:
                    hard_override = self._apply_llm_adjustment(
                        item.id,
                        multipliers,
                        explanations,
                        hard_override,
                        llm_adjustment,
                        after_commit,
                    )

                raw_qty = baseline
                for value in multipliers.values():
                    raw_qty *= float(value)
                qty = int(hard_override) if hard_override is not None else nearest_int(raw_qty)
                qty = max(0, qty)

                confidence = confidence_from(multipliers)
                if llm_adjustment:
                    confidence = min(confidence, float(llm_adjustment.get("confidence") or 0.85))

                forecast = Forecast(
                    menu_item_id=item.id,
                    window=window,
                    daypart=daypart,
                    forecast_qty=float(qty),
                    baseline_qty=round(baseline, 2),
                    multipliers=multipliers,
                    confidence=round(confidence, 3),
                    generated_at=now,
                    trigger_reason="llm_manual" if optimize and trigger_reason == "manual" else trigger_reason,
                )
                session.add(forecast)
                session.flush()
                session.refresh(forecast)
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
                }
                log_detail = {
                    "forecast_id": forecast.id,
                    "menu_item_id": item.id,
                    "item_name": item.name,
                    "baseline": round(baseline, 2),
                    "raw_qty": round(raw_qty, 3),
                    "forecast_qty": qty,
                    "multipliers": multipliers,
                    "explanations": explanations,
                    "hard_override": hard_override,
                    "optimized": bool(optimize),
                    "trigger": trigger_reason,
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
        self._remember_run_summary(row_summaries, optimize, trigger_reason)
        self.decide_batches(trigger_reason, optimize=optimize)
        return rows

    def optimize_forecast(self, trigger_reason: str = "manual") -> List[Forecast]:
        return self.run_forecast(trigger_reason, optimize=True)

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
                    .first()
                )
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
        explanations = self._base_explanations(multipliers)
        hard_override = self._apply_hard_constraints(session, item, multipliers, explanations, live)
        return {
            "item": item,
            "baseline": baseline,
            "multipliers": multipliers,
            "explanations": explanations,
            "hard_override": hard_override,
        }

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
            "competitor": round(self._competitor_multiplier(item, live), 3),
            "review": round(self._review_multiplier(item, live), 3),
            "staff_coverage": round(self._staff_multiplier(item, live), 3),
            "weather": round(self._weather_multiplier(session, item), 3),
            "recent_velocity": round(self._velocity_multiplier(item.id, baseline, daypart), 3),
        }

    @staticmethod
    def _base_explanations(multipliers: Dict[str, float]) -> Dict[str, str]:
        labels = {
            "settings_demand": "Simulation demand settings versus historical baseline.",
            "event": "Active events from user facts.",
            "competitor": "Competitor intelligence and offer changes.",
            "review": "Recent review sentiment for this item.",
            "staff_coverage": "Station and qualified-staff capacity.",
            "weather": "Weather and temperature response.",
            "recent_velocity": "Rolling POS velocity versus expectation.",
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
        window_fraction = _window_fraction(daypart, window)

        weights = self._settings_item_weights(session, settings, now)
        total_weight = sum(weights.values())
        item_weight = weights.get(int(item.id), 0.0)
        share = item_weight / total_weight if total_weight > 0 else 0.0

        for inj in active_injections(getattr(settings, "anomaly_injections", None), now):
            mult = inj.get("velocity_mult")
            if mult is not None:
                velocity *= float(mult)

        return max(0.0, base * velocity * share * daypart_weight * window_fraction)

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
            if sig.type != SignalType.USER_FACT.value:
                continue
            payload = sig.payload or {}
            if payload.get("intent") != "add_event":
                continue
            fact_window = payload.get("effective_window")
            if fact_window and not windows_overlap(window, fact_window):
                continue
            try:
                mult *= float(payload.get("value") or config.EVENT_MULT)
            except (TypeError, ValueError):
                mult *= config.EVENT_MULT
        return mult

    def _competitor_multiplier(self, item: MenuItem, live: Iterable[Signal]) -> float:
        value = 1.0
        name = (item.name or "").lower()
        for sig in live:
            payload = sig.payload or {}
            if sig.type == SignalType.COMPETITOR_INTEL.value:
                dishes = [str(d).lower() for d in payload.get("popular_dishes") or []]
                if any(name in dish or dish in name for dish in dishes):
                    value *= 1.05
            elif sig.type == SignalType.COMPETITOR_UPDATE.value and payload.get("offers_changed"):
                summary = str(payload.get("summary") or "").lower()
                if item.category and str(item.category).lower() in summary:
                    value *= 0.97
        return value

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
            elif sig.type == SignalType.STAFF_COVERAGE.value:
                affected = payload.get("affected_items") or []
                if payload.get("covered") is False and (item.id in affected or item.station_id == payload.get("station_id")):
                    hard_override = 0
                    multipliers["staff_coverage"] = 0.0
                    explanations["staff_coverage"] = "Station has no remaining qualified staff."

        vague = self._vague_capacity_constraint(session, item, live)
        if vague:
            if vague["remaining"] <= 0:
                hard_override = 0
                multipliers["staff_coverage"] = 0.0
            else:
                multipliers["staff_coverage"] = round(
                    min(multipliers.get("staff_coverage", 1.0), vague["multiplier"]),
                    3,
                )
            explanations["staff_coverage"] = vague["reason"]
        return hard_override

    def _vague_capacity_constraint(
        self,
        session: Any,
        item: MenuItem,
        live: Iterable[Signal],
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        for sig in live:
            if sig.type != SignalType.USER_FACT.value:
                continue
            payload = sig.payload or {}
            raw = str(payload.get("raw_text") or payload.get("value") or "").lower()
            if not self._is_absence_fact(payload, raw):
                continue
            if not self._fact_targets_item(session, item, payload, raw):
                continue
            all_staff = self._all_staff_absent(payload, raw)
            qualified = self._qualified_staff_ids(session, item.id, item.station_id)
            available = [
                sid for sid in qualified
                if self._staff_available(session, sid, int(self.bus.sim_time // SECONDS_PER_DAY), current_daypart(self.bus.sim_time))
            ]
            absent_count = len(qualified) if all_staff else 1
            remaining = max(0, len(available) - absent_count)
            multiplier = 0.0 if len(available) <= 0 else max(0.25, remaining / max(len(available), 1))
            target = payload.get("entity_ref") or self._target_phrase(raw)
            reason = (
                f"Operational note '{target}' leaves no qualified staff."
                if remaining <= 0
                else f"Operational note '{target}' reduces station capacity to {remaining}/{len(available)} qualified staff."
            )
            candidate = {
                "remaining": remaining,
                "qualified": len(qualified),
                "available": len(available),
                "multiplier": multiplier,
                "reason": reason,
            }
            if best is None or candidate["remaining"] < best["remaining"]:
                best = candidate
        return best

    @staticmethod
    def _is_absence_fact(payload: Dict[str, Any], raw: str) -> bool:
        if payload.get("intent") == "set_operational_constraint":
            return True
        return any(w in raw for w in ("absent", "unavailable", "missing", "off sick", "sick")) and any(
            w in raw for w in ("station", "worker", "cook", "chef", "staff", "making")
        )

    @staticmethod
    def _all_staff_absent(payload: Dict[str, Any], raw: str) -> bool:
        value = payload.get("value")
        if isinstance(value, dict) and value.get("all_qualified_staff"):
            return True
        return any(
            phrase in raw
            for phrase in ("all ", "every ", "no one", "nobody", "none of", "all the possible", "everyone")
        )

    def _fact_targets_item(self, session: Any, item: MenuItem, payload: Dict[str, Any], raw: str) -> bool:
        phrase = " ".join(
            str(part or "")
            for part in (
                payload.get("entity_ref"),
                payload.get("attribute"),
                payload.get("raw_text"),
                raw,
            )
        ).lower()
        tokens = self._target_tokens(phrase)
        if not tokens:
            return False
        station = session.get(Station, item.station_id)
        haystacks = [
            item.name or "",
            item.category or "",
            item.description or "",
            station.name if station is not None else "",
        ]
        joined = " ".join(haystacks).lower()
        for token in tokens:
            if token in joined:
                return True
            aliases = STATION_ALIASES.get(token, [])
            if aliases and any(alias in joined for alias in aliases):
                return True
        return False

    @staticmethod
    def _target_tokens(phrase: str) -> List[str]:
        words = re.findall(r"[a-z0-9]+", phrase.lower())
        ignored = {
            "the", "all", "possible", "staff", "worker", "workers", "cook",
            "chef", "station", "making", "make", "are", "is", "was", "were",
            "absent", "unavailable", "missing", "sick", "off", "for",
        }
        return [word for word in words if word not in ignored and len(word) > 2]

    @staticmethod
    def _target_phrase(raw: str) -> str:
        for pattern in (
            r"(?:the\s+)?([a-z0-9 '&-]+?)\s+station",
            r"(?:making|make|prep|prepping)\s+([a-z0-9 '&-]+?)(?:\s+are|\s+is|\s+absent|$)",
        ):
            m = re.search(pattern, raw, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return raw[:80]

    @staticmethod
    def _qualified_staff_ids(session: Any, menu_item_id: int, station_id: int) -> List[int]:
        station_staff = {
            row[0]
            for row in session.query(StaffStation.staff_id)
            .filter(StaffStation.station_id == station_id)
            .all()
        }
        dish_staff = {
            row[0]
            for row in session.query(StaffDishSkill.staff_id)
            .filter(StaffDishSkill.menu_item_id == menu_item_id)
            .all()
        }
        return sorted(station_staff.union(dish_staff))

    @staticmethod
    def _staff_available(session: Any, staff_id: int, day: int, daypart: str) -> bool:
        staff = session.get(Staff, staff_id)
        if staff is None or not staff.active:
            return False
        rows = (
            session.query(Attendance)
            .filter(Attendance.staff_id == staff_id, Attendance.date_sim_day == day)
            .order_by(Attendance.sim_time.desc(), Attendance.id.desc())
            .all()
        )
        status = "present"
        for row in rows:
            if row.daypart not in (None, daypart):
                continue
            status = row.status or "present"
            break
        return status not in {"leave", "sick"}

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
        context = {
            "sim_time": float(self.bus.sim_time),
            "daypart": daypart,
            "window": window,
            "weather": self._weather_context(session),
            "temperature_thresholds_c": {
                "cold_lte": config.COLD_TEMP_C,
                "hot_gte": config.HOT_TEMP_C,
            },
            "multiplier_limits": {
                "min": config.LLM_MULTIPLIER_CLAMP[0],
                "max": config.LLM_MULTIPLIER_CLAMP[1],
            },
            "items": [self._prepared_context(entry) for entry in prepared],
            "live_signals": [self._signal_context(sig) for sig in live],
            "memory": self._memory_context(session),
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You optimize restaurant demand forecasts. Return compact JSON only. "
                    "You may adjust existing multipliers between 0.0 and 2.0, set "
                    "hard_override_qty only to 0 when an item cannot operationally be made, "
                    "or set target_forecast_qty when a direct integer forecast is safer than "
                    "a multiplier. Provide concise user-facing reasons. Do not reveal hidden reasoning."
                ),
            },
            {"role": "user", "content": str(context)},
        ]
        schema = {
            "type": "object",
            "properties": {
                "item_adjustments": {"type": "array"},
                "global_notes": {"type": "array"},
                "memory_updates": {"type": "array"},
                "confidence": {"type": "number"},
            },
            "required": ["item_adjustments", "global_notes", "memory_updates"],
        }
        result = self.llm.complete(
            messages,
            json_schema=schema,
            max_tokens=900,
            use_site="forecaster_optimization",
        )
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
        result = self.llm.complete(
            messages,
            json_schema=schema,
            max_tokens=600,
            use_site="forecaster_optimization",
        )
        if not isinstance(result, dict) or result.get("note") == CANNED_NOTE:
            return {}
        return result

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

    def _apply_llm_adjustment(
        self,
        item_id: int,
        multipliers: Dict[str, float],
        explanations: Dict[str, str],
        hard_override: Optional[int],
        adjustment: Dict[str, Any],
        after_commit: List[Tuple[str, Any]],
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
        else:
            target_qty = self._target_qty_from_adjustment(adjustment)
            if target_qty is not None:
                hard_override = max(0, target_qty)
                multipliers["llm_target"] = 1.0
                explanations["llm_target"] = reason
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
            "hard_override": entry.get("hard_override"),
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
    def _round_batch_qty(forecast_qty: float, definition: BatchDefinition) -> int:
        step = float(definition.batch_size_step or 1.0)
        minimum = float(definition.batch_size_min or 0.0)
        maximum = float(definition.batch_size_max or forecast_qty)
        rounded = round(float(forecast_qty) / step) * step if step > 0 else nearest_int(float(forecast_qty))
        return int(max(0, min(maximum, max(minimum, rounded))))

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)

    def _run_after_commit(self, actions: List[Tuple[str, Any]]) -> None:
        for kind, payload in actions:
            if kind == "emit":
                signal_type, signal_payload, kwargs = payload
                self.emit(signal_type, signal_payload, **kwargs)
            elif kind == "log":
                category, summary, detail = payload
                self.log_event(category, summary, detail)
            elif kind == "broadcast":
                event, ws_payload = payload
                self._broadcast(event, ws_payload)
            elif kind == "remember":
                self._remember(*payload)

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


def nearest_int(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def confidence_from(multipliers: Dict[str, float]) -> float:
    values = [float(v) for v in multipliers.values()]
    spread = (max(values) - min(values)) if values else 0.0
    return round(1.0 / (1.0 + spread), 3)


def windows_overlap(a: Dict[str, float], b: Dict[str, float]) -> bool:
    return float(a.get("start", 0.0)) < float(b.get("end", 0.0)) and float(b.get("start", 0.0)) < float(a.get("end", 0.0))
