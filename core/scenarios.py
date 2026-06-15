"""Scenario engine (§18.8 / §18.9).

A *scenario* is an ordered list of ``scenario_events`` ``{at_sim_time,
event_type, payload}`` that perturb the simulated world at fixed sim-times so a
presenter can run a repeatable, fully-scripted demo. The engine:

- :meth:`activate` / :meth:`deactivate` — flip ``scenarios.is_active``.
- :meth:`tick` — fire every active scenario's events whose ``at_sim_time`` has
  arrived and that have not yet ``fired``; apply the event's deterministic
  effect (§18.9) and mark it ``fired=1`` (idempotent — never re-fires).
- :func:`ScenarioEngine.seed_default_scenario` — ship the flagship
  **"Friday Rush"** scenario on first run (when no scenarios exist).

Event types (§18.9): ``inject_signal``, ``change_setting``, ``inject_review``,
``set_competitor``, ``call_in_sick``, ``supplier_change``, ``weather_set``,
``velocity_mult``.

The engine owns only the perturbation writes the doc assigns it (settings,
attendance exceptions, supplier-catalog dynamic fields, reviews, competitor
fields, weather overrides) plus bus emits for ``inject_signal``. Downstream
agents react to the resulting signals / table state through their normal paths.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from .clock import DAY_OPEN_OFFSET, SECONDS_PER_DAY
from .events import log_event
from .models import (
    Attendance,
    Competitor,
    Ingredient,
    InventoryLot,
    Review,
    Scenario,
    ScenarioEvent,
    SimSettings,
    Staff,
    StaffStation,
    Station,
    SupplierCatalog,
)
from .signals import SignalType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Flagship "Friday Rush" scenario (§18.9).
#
# ``at_sim_time`` values are day-0 sim-seconds from the sim-epoch (08:00 =
# 28800):
#   11:30 = 41400, 12:15 = 44700, 13:00 = 46800,
#   15:00 = 54000, 18:00 = 64800, 21:30 = 77400.
# This single run exercises every agent and the full cascade.
# ---------------------------------------------------------------------------

FRIDAY_RUSH_NAME = "Friday Rush"
FRIDAY_RUSH_DESCRIPTION = (
    "Flagship demo: a busy Friday that exercises every agent and the full "
    "signal cascade — lunch surge, a sick grill cook, a delayed tomato "
    "delivery, afternoon rain, a dinner surge, and surplus mozzarella nearing "
    "expiry."
)

FRIDAY_RUSH_EVENTS: List[Tuple[float, str, Dict[str, Any]]] = [
    # 11:30 — lunch velocity surge ×1.6.
    (41400.0, "velocity_mult", {"mult": 1.6, "label": "Lunch rush"}),
    # 12:15 — the grill cook calls in sick (grill station left uncovered).
    (44700.0, "call_in_sick", {
        "station": "Grill",
        "status": "sick",
        "reason": "Grill cook called in sick",
    }),
    # 13:00 — tomato delivery delayed → supplier marks tomato out of stock.
    (46800.0, "supplier_change", {
        "ingredient_name": "Tomato",
        "availability": "out",
        "reason": "Tomato delivery delayed",
    }),
    # 15:00 — rain sets in (drives the weather channel shift, §18.5).
    (54000.0, "weather_set", {
        "temp_c": 14.0,
        "condition": "rain",
        "precip_mm": 6.0,
        "wind_kph": 22.0,
    }),
    # 18:00 — dinner velocity surge ×1.4.
    (64800.0, "velocity_mult", {"mult": 1.4, "label": "Dinner rush"}),
    # 21:30 — surplus mozzarella nearing expiry → EXPIRY_RISK (→ promo path).
    (77400.0, "inject_signal", {
        "signal_type": "EXPIRY_RISK",
        "ingredient_name": "Mozzarella",
        "resolve_lot": True,
        "payload": {"projected_usage_before_expiry": 500.0},
    }),
]


class ScenarioEngine:
    """Activate scenarios and fire their scripted events on the sim clock."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        clock: Any,
        pos_simulator: Any,
        weather: Any,
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.clock = clock
        self.pos_simulator = pos_simulator
        self.weather = weather

    # -- activation (§18.9) -------------------------------------------------

    def activate(self, scenario_id: int) -> Optional[Scenario]:
        """Set ``scenarios.is_active = True``; returns the row (or ``None``)."""
        return self._set_active(scenario_id, 1)

    def deactivate(self, scenario_id: int) -> Optional[Scenario]:
        """Set ``scenarios.is_active = False``; returns the row (or ``None``)."""
        return self._set_active(scenario_id, 0)

    def _set_active(self, scenario_id: int, value: int) -> Optional[Scenario]:
        session = self.db_session_factory()
        try:
            scenario = session.get(Scenario, scenario_id)
            if scenario is None:
                return None
            scenario.is_active = value
            session.commit()
            session.refresh(scenario)
            session.expunge(scenario)
            return scenario
        finally:
            session.close()

    # -- per-tick firing (§18.9) -------------------------------------------

    def tick(self, sim_time: float) -> List[int]:
        """Fire all due, unfired events of every active scenario.

        Returns the list of fired ``scenario_events.id``. Each event's effect
        runs in its own session(s); a failing effect is logged but still marks
        the event ``fired`` so the engine never loops on a bad event.
        """
        session = self.db_session_factory()
        try:
            active_ids = [
                s.id
                for s in session.query(Scenario).filter(Scenario.is_active == 1).all()
            ]
            if not active_ids:
                return []
            due = (
                session.query(ScenarioEvent)
                .filter(
                    ScenarioEvent.scenario_id.in_(active_ids),
                    ScenarioEvent.fired == 0,
                    ScenarioEvent.at_sim_time <= sim_time,
                )
                .order_by(ScenarioEvent.at_sim_time.asc())
                .all()
            )
            detached = [
                (ev.id, ev.event_type, dict(ev.payload or {}))
                for ev in due
            ]
        finally:
            session.close()

        fired: List[int] = []
        for event_id, event_type, payload in detached:
            try:
                self._dispatch(event_type, payload, sim_time)
            except Exception:  # noqa: BLE001 — a bad event must not wedge the run.
                logger.exception(
                    "Scenario event %s (%s) failed to apply", event_id, event_type
                )
            self._mark_fired(event_id)
            fired.append(event_id)
        return fired

    def _mark_fired(self, event_id: int) -> None:
        session = self.db_session_factory()
        try:
            ev = session.get(ScenarioEvent, event_id)
            if ev is not None:
                ev.fired = 1
                session.commit()
        finally:
            session.close()

    def _dispatch(self, event_type: str, payload: Dict[str, Any], sim_time: float) -> None:
        handler = {
            "inject_signal": self._inject_signal,
            "change_setting": self._change_setting,
            "inject_review": self._inject_review,
            "set_competitor": self._set_competitor,
            "call_in_sick": self._call_in_sick,
            "supplier_change": self._supplier_change,
            "weather_set": self._weather_set,
            "velocity_mult": self._velocity_mult,
        }.get(event_type)
        if handler is None:
            logger.warning("Unknown scenario event_type %r; skipping", event_type)
            return
        handler(payload, sim_time)

    # -- event handlers (§18.9) --------------------------------------------

    def _inject_signal(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Emit a bus signal. Supports resolving ``ingredient_name`` → an
        ``ingredient_id`` (and, with ``resolve_lot``, the soonest-expiring
        active lot's ``lot_id`` / ``qty`` / ``expiry``) before emitting."""
        signal_type = payload.get("signal_type") or payload.get("type")
        if not signal_type:
            logger.warning("inject_signal missing signal_type; skipping")
            return
        sig_payload: Dict[str, Any] = dict(payload.get("payload") or {})

        ingredient_name = payload.get("ingredient_name")
        if ingredient_name:
            session = self.db_session_factory()
            try:
                ingredient = (
                    session.query(Ingredient)
                    .filter(Ingredient.name.ilike(str(ingredient_name)))
                    .first()
                )
                if ingredient is not None:
                    sig_payload.setdefault("ingredient_id", ingredient.id)
                    if payload.get("resolve_lot"):
                        lot = (
                            session.query(InventoryLot)
                            .filter(
                                InventoryLot.ingredient_id == ingredient.id,
                                InventoryLot.status == "active",
                            )
                            .order_by(InventoryLot.expiry_date.asc())
                            .first()
                        )
                        if lot is not None:
                            sig_payload.setdefault("lot_id", lot.id)
                            sig_payload.setdefault("qty", lot.qty_on_hand)
                            sig_payload.setdefault("expiry", lot.expiry_date)
            finally:
                session.close()

        self.bus.emit(
            signal_type,
            sig_payload,
            source="scenario",
            groups=payload.get("groups"),
            priority=payload.get("priority"),
            ttl=payload.get("ttl"),
            dedup_key=payload.get("dedup_key"),
        )
        self._log(sim_time, "scenario", f"Injected signal {signal_type}", payload)

    def _change_setting(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Update one or more ``sim_settings`` fields (``{field, value}`` or a
        ``{settings: {...}}`` mapping)."""
        updates: Dict[str, Any] = {}
        if "field" in payload:
            updates[payload["field"]] = payload.get("value")
        if isinstance(payload.get("settings"), dict):
            updates.update(payload["settings"])
        if not updates:
            return

        session = self.db_session_factory()
        try:
            settings = self._get_or_create_settings(session)
            for field, value in updates.items():
                if hasattr(settings, field) and field != "id":
                    setattr(settings, field, value)
            session.commit()
        finally:
            session.close()
        self._log(sim_time, "scenario", "Changed sim setting", updates)

    def _inject_review(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Insert a ``reviews`` row (the Review agent processes it normally)."""
        session = self.db_session_factory()
        try:
            review = Review(
                source=payload.get("source", "scenario"),
                rating=payload.get("rating"),
                text=payload.get("text", ""),
                dish_mentions=payload.get("dish_mentions", []),
                sentiment=payload.get("sentiment"),
                sim_time=sim_time,
                processed=0,
            )
            session.add(review)
            session.commit()
        finally:
            session.close()
        self._log(sim_time, "scenario", "Injected review", payload)

    def _set_competitor(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Update a ``competitors`` row (resolved by ``competitor_id`` or
        ``name``)."""
        session = self.db_session_factory()
        try:
            competitor = self._resolve_competitor(session, payload)
            if competitor is None:
                logger.warning("set_competitor: competitor not found (%s)", payload)
                return
            for field in ("is_open", "rating", "price_tier", "distance_km", "platform"):
                if field in payload:
                    setattr(competitor, field, payload[field])
            if "cuisine" in payload:
                competitor.cuisine = payload["cuisine"]
            competitor.updated_at = sim_time
            session.commit()
        finally:
            session.close()
        self._log(sim_time, "scenario", "Updated competitor", payload)

    def _call_in_sick(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Write an ``attendance`` exception (status ``sick`` by default). The
        affected staff member is resolved by ``staff_id``, ``staff_name``, or
        the first staff covering a named ``station`` (§11 attendance is the
        queryable source of truth the Staff agent reads for coverage)."""
        status = payload.get("status", "sick")
        day = int(sim_time // SECONDS_PER_DAY)

        session = self.db_session_factory()
        try:
            staff_id = self._resolve_staff_id(session, payload)
            row = Attendance(
                staff_id=staff_id,
                date_sim_day=day,
                status=status,
                daypart=payload.get("daypart"),
                reason=payload.get("reason", status),
                sim_time=sim_time,
            )
            session.add(row)
            session.commit()
        finally:
            session.close()
        self._log(sim_time, "scenario", f"Staff marked {status}", payload)

    def _supplier_change(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Update the dynamic fields of every ``supplier_catalog`` row for an
        ingredient (availability / current_price) — e.g. mark it ``out``."""
        session = self.db_session_factory()
        try:
            ingredient_id = payload.get("ingredient_id")
            if ingredient_id is None and payload.get("ingredient_name"):
                ingredient = (
                    session.query(Ingredient)
                    .filter(Ingredient.name.ilike(str(payload["ingredient_name"])))
                    .first()
                )
                ingredient_id = ingredient.id if ingredient is not None else None
            if ingredient_id is None:
                logger.warning("supplier_change: ingredient not found (%s)", payload)
                return

            query = session.query(SupplierCatalog).filter(
                SupplierCatalog.ingredient_id == ingredient_id
            )
            if payload.get("supplier_id") is not None:
                query = query.filter(SupplierCatalog.supplier_id == payload["supplier_id"])
            rows = query.all()
            for row in rows:
                if "availability" in payload:
                    row.availability = payload["availability"]
                if "current_price" in payload:
                    row.current_price = payload["current_price"]
                row.updated_at = sim_time
            session.commit()
        finally:
            session.close()
        self._log(sim_time, "scenario", "Changed supplier catalog", payload)

    def _weather_set(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Drive a demo weather override (§9.1 / §18.5)."""
        self.weather.override(
            temp_c=float(payload.get("temp_c", 20.0)),
            condition=str(payload.get("condition", "clear")),
            precip_mm=float(payload.get("precip_mm", 0.0)),
            wind_kph=float(payload.get("wind_kph", 0.0)),
        )
        self._log(sim_time, "scenario", "Set weather", payload)

    def _velocity_mult(self, payload: Dict[str, Any], sim_time: float) -> None:
        """Multiply ``sim_settings.velocity`` (takes effect next tick, §10)."""
        mult = float(payload.get("mult", 1.0))
        session = self.db_session_factory()
        try:
            settings = self._get_or_create_settings(session)
            current = settings.velocity if settings.velocity is not None else 1.0
            settings.velocity = current * mult
            session.commit()
            new_velocity = settings.velocity
        finally:
            session.close()
        self._log(
            sim_time, "scenario",
            f"Velocity ×{mult} → {new_velocity:.3f}", payload,
        )

    # -- resolution helpers -------------------------------------------------

    @staticmethod
    def _resolve_competitor(session: Any, payload: Dict[str, Any]) -> Optional[Competitor]:
        if payload.get("competitor_id") is not None:
            return session.get(Competitor, payload["competitor_id"])
        if payload.get("name"):
            return (
                session.query(Competitor)
                .filter(Competitor.name.ilike(str(payload["name"])))
                .first()
            )
        return None

    @staticmethod
    def _resolve_staff_id(session: Any, payload: Dict[str, Any]) -> Optional[int]:
        if payload.get("staff_id") is not None:
            return int(payload["staff_id"])
        if payload.get("staff_name"):
            staff = (
                session.query(Staff)
                .filter(Staff.name.ilike(str(payload["staff_name"])))
                .first()
            )
            if staff is not None:
                return staff.id
        if payload.get("station"):
            station = (
                session.query(Station)
                .filter(Station.name.ilike(str(payload["station"])))
                .first()
            )
            if station is not None:
                coverage = (
                    session.query(StaffStation)
                    .filter(StaffStation.station_id == station.id)
                    .first()
                )
                if coverage is not None:
                    return coverage.staff_id
        return None

    @staticmethod
    def _get_or_create_settings(session: Any) -> SimSettings:
        settings = session.get(SimSettings, 1)
        if settings is None:
            settings = SimSettings(id=1, velocity=1.0)
            session.add(settings)
            session.flush()
        return settings

    def _log(self, sim_time: float, actor: str, summary: str, detail: Dict[str, Any]) -> None:
        session = self.db_session_factory()
        try:
            log_event(session, sim_time, "scenario", actor, summary, detail)
        except Exception:  # noqa: BLE001 — narrative logging must never fail a tick.
            logger.exception("Failed to write scenario event_log row")
        finally:
            session.close()

    # -- default scenario seeding (§18.9) ----------------------------------

    def seed_default_scenario(self) -> Optional[int]:
        """Seed the flagship **Friday Rush** scenario if no scenarios exist.

        Seeded *inactive* so it never fires until the presenter activates it via
        ``POST /api/scenarios/{id}/activate``. Returns the new scenario id (or
        ``None`` when scenarios already exist)."""
        session = self.db_session_factory()
        try:
            if session.query(Scenario).count() > 0:
                return None
            scenario = Scenario(
                name=FRIDAY_RUSH_NAME,
                description=FRIDAY_RUSH_DESCRIPTION,
                is_active=0,
            )
            session.add(scenario)
            session.flush()
            for at_sim_time, event_type, event_payload in FRIDAY_RUSH_EVENTS:
                session.add(
                    ScenarioEvent(
                        scenario_id=scenario.id,
                        at_sim_time=at_sim_time,
                        event_type=event_type,
                        payload=event_payload,
                        fired=0,
                    )
                )
            session.commit()
            return scenario.id
        finally:
            session.close()
