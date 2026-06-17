"""Track A demand forecaster.

The agent reads only core/reference tables and live signals, writes Track A
forecast/batch rows, and communicates outward through the signal bus.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core import config
from core.agent_base import BaseAgent
from core.clock import DAY_CLOSE_OFFSET, DAY_OPEN_OFFSET, SECONDS_PER_DAY
from core.llm import CANNED_NOTE
from core.models import (
    Batch,
    BatchDefinition,
    Forecast,
    MenuItem,
    OrderLine,
    Recipe,
    RecipeLine,
    Signal,
    SimSettings,
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


class DemandForecaster(BaseAgent):
    """Rolling item forecasts, batch cook/skip decisions, and explanations."""

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
        self.subscribe(["forecasting"])

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            lambda: self.run_forecast("interval"),
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
            self.run_forecast(f"signal:{signal.type}")

    def run_forecast(self, trigger_reason: str = "manual") -> List[Forecast]:
        """Forecast every active and not-signal-disabled menu item."""
        now = float(self.bus.sim_time)
        daypart, window = current_window(now)
        rows: List[Forecast] = []
        live = self.bus.live()
        after_commit: List[Tuple[str, Any]] = []

        session = self.db_session_factory()
        try:
            items = (
                session.query(MenuItem)
                .filter(MenuItem.active == 1)
                .order_by(MenuItem.id.asc())
                .all()
            )
            for item in items:
                if self._is_disabled_by_signal(item.id, live):
                    after_commit.append(
                        (
                            "log",
                            (
                                "forecast",
                                f"Skipped {item.name}: menu disabled by inventory signal",
                                {"menu_item_id": item.id, "trigger": trigger_reason},
                            ),
                        )
                    )
                    continue

                baseline = self.baseline_qty(session, item.id, daypart, now)
                multipliers = self._multipliers(session, item, baseline, daypart, window, live)
                qty = baseline
                for value in multipliers.values():
                    qty *= float(value)
                qty = max(0.0, round(qty, 2))
                confidence = confidence_from(multipliers)

                forecast = Forecast(
                    menu_item_id=item.id,
                    window=window,
                    daypart=daypart,
                    forecast_qty=qty,
                    baseline_qty=round(baseline, 2),
                    multipliers=multipliers,
                    confidence=confidence,
                    generated_at=now,
                    trigger_reason=trigger_reason,
                )
                session.add(forecast)
                session.flush()
                session.refresh(forecast)
                forecast_id = forecast.id
                item_id = item.id
                item_name = item.name
                item_payload = self._item_to_dict(item)
                rows.append(forecast)

                forecast_payload = {
                        "menu_item_id": item_id,
                        "window": window,
                        "daypart": daypart,
                        "qty": qty,
                        "baseline": round(baseline, 2),
                        "multipliers": multipliers,
                        "confidence": confidence,
                    }
                log_detail = {
                        "menu_item_id": item_id,
                        "baseline": round(baseline, 2),
                        "multipliers": multipliers,
                        "confidence": confidence,
                        "trigger": trigger_reason,
                    }
                ws_payload = {
                    "forecast": {
                            "id": forecast_id,
                            "menu_item_id": item_id,
                            "window": window,
                            "daypart": daypart,
                            "forecast_qty": qty,
                            "baseline_qty": round(baseline, 2),
                            "multipliers": multipliers,
                            "confidence": confidence,
                            "generated_at": now,
                            "trigger_reason": trigger_reason,
                        },
                    "item": item_payload,
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
                                    "dedup_key": f"forecast:{item_id}:{int(window['start'])}",
                                },
                            ),
                        ),
                        (
                            "log",
                            (
                                "forecast",
                                f"Forecast {item_name}: {qty:g} for {daypart}",
                                log_detail,
                            ),
                        ),
                        ("broadcast", ("forecast_updated", ws_payload)),
                    ]
                )

            session.commit()
        finally:
            session.close()

        self._run_after_commit(after_commit)
        self.decide_batches(trigger_reason)
        return rows

    def baseline_qty(self, session: Any, item_id: int, daypart: str, now: float) -> float:
        """Baseline fallback chain: same daypart+dow, daypart, then seed mix."""
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
        projected = self._settings_projected_qty(session, item, daypart, now)
        return max(1.0, projected)

    def decide_batches(self, trigger_reason: str = "manual") -> List[Batch]:
        now = float(self.bus.sim_time)
        daypart, window = current_window(now)
        live = self.bus.live()
        rows: List[Batch] = []
        after_commit: List[Tuple[str, Any]] = []

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
                    .order_by(Forecast.generated_at.desc())
                    .first()
                )
                f_qty = float(forecast.forecast_qty if forecast is not None else 0.0)
                reasons: List[str] = []
                available = not self._is_blocked_for_batch(item.id, definition.station_id, live, reasons)
                should_cook = f_qty >= float(definition.batch_size_min) and available
                planned = self._round_batch_qty(f_qty, definition) if should_cook else 0.0
                decision = "cook" if should_cook else "skip"
                if f_qty < float(definition.batch_size_min):
                    reasons.append(f"forecast {f_qty:g} below min {definition.batch_size_min:g}")
                if not reasons and should_cook:
                    reasons.extend([f"{daypart} forecast {f_qty:g}", "station staffed", "ingredients OK"])

                row = Batch(
                    batch_definition_id=definition.id,
                    menu_item_id=item.id,
                    decided_at=now,
                    serve_window=window,
                    decision=decision,
                    planned_qty=planned,
                    actual_made_qty=0.0,
                    sold_qty=0.0,
                    wasted_qty=0.0,
                    status="decided",
                    by="agent",
                )
                session.add(row)
                session.flush()
                session.refresh(row)
                batch_id = row.id
                rows.append(row)

                signal_payload = {
                        "batch_definition_id": definition.id,
                        "menu_item_id": item.id,
                        "serve_window": window,
                        "decision": decision,
                        "qty": planned,
                        "by": "agent",
                    }
                reason_text = ", ".join(reasons)
                summary = f"{decision} {planned:g} {item.name}: {reason_text}"
                log_detail = {
                        "menu_item_id": item.id,
                        "batch_definition_id": definition.id,
                        "forecast_qty": f_qty,
                        "decision": decision,
                        "planned_qty": planned,
                        "reasons": reasons,
                        "trigger": trigger_reason,
                    }
                ws_payload = {
                    "batch": {
                            "id": batch_id,
                            "batch_definition_id": definition.id,
                            "menu_item_id": item.id,
                            "decided_at": now,
                            "serve_window": window,
                            "decision": decision,
                            "planned_qty": planned,
                            "status": "decided",
                            "by": "agent",
                        },
                    "reason": reason_text,
                }
                after_commit.extend(
                    [
                        (
                            "emit",
                            (
                                SignalType.BATCH_DECISION,
                                signal_payload,
                                {
                                    "dedup_key": f"batch:{definition.id}:{int(window['start'])}:{decision}",
                                },
                            ),
                        ),
                        ("log", ("batch", summary, log_detail)),
                        ("broadcast", ("batch_decided", ws_payload)),
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
            }
        finally:
            session.close()

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

    def _multipliers(
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

    def _settings_multiplier(
        self,
        session: Any,
        item: MenuItem,
        baseline: float,
        daypart: str,
    ) -> float:
        """Make live sim_settings visible even when history supplies baseline.

        Historical order lines remain the explainable baseline, while the
        editable simulation controls act as a demand-plan multiplier. This keeps
        the forecast dashboard responsive to the POS settings drawer instead of
        hiding those edits behind already-seeded history.
        """
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
    def _settings_item_weights(
        session: Any,
        settings: Optional[SimSettings],
        now: float,
    ) -> Dict[int, float]:
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
                return config.STAFF_CAP_FACTOR
        return 1.0

    def _weather_multiplier(self, session: Any, item: MenuItem) -> float:
        weather = session.query(WeatherLog).order_by(WeatherLog.sim_time.desc()).first()
        if weather is None:
            return 1.0
        tags = {str(t).lower() for t in (item.weather_tags or [])}
        condition = str(weather.condition or "").lower()
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

    @staticmethod
    def _is_disabled_by_signal(item_id: int, live: Iterable[Signal]) -> bool:
        disabled = False
        for sig in live:
            if sig.type != SignalType.MENU_TOGGLE.value:
                continue
            payload = sig.payload or {}
            if payload.get("menu_item_id") == item_id:
                disabled = payload.get("action") == "disable"
        return disabled

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

    @staticmethod
    def _round_batch_qty(forecast_qty: float, definition: BatchDefinition) -> float:
        step = float(definition.batch_size_step or 1.0)
        minimum = float(definition.batch_size_min or 0.0)
        maximum = float(definition.batch_size_max or forecast_qty)
        rounded = round(forecast_qty / step) * step if step > 0 else round(forecast_qty)
        return min(maximum, max(minimum, rounded))

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

    @staticmethod
    def _forecast_to_dict(row: Forecast) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _batch_to_dict(row: Batch) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _item_to_dict(row: MenuItem) -> Dict[str, Any]:
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


def confidence_from(multipliers: Dict[str, float]) -> float:
    values = [float(v) for v in multipliers.values()]
    spread = (max(values) - min(values)) if values else 0.0
    return round(1.0 / (1.0 + spread), 3)


def windows_overlap(a: Dict[str, float], b: Dict[str, float]) -> bool:
    return float(a.get("start", 0.0)) < float(b.get("end", 0.0)) and float(b.get("start", 0.0)) < float(a.get("end", 0.0))
