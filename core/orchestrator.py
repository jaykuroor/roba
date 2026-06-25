"""Orchestrator — the per-tick engine and trigger registry (§17).

Every tick (§6.1) the orchestrator:
  1. advances ``sim_time`` by ``Δsim = 60 × speed × 0.25`` (handling the
     closed-hours auto-jump 23:00 → next-day 08:00 when ``skip_closed_hours``),
  2. writes the new ``sim_time`` (and derived day fields) back to ``sim_state``,
  3. fires due **interval** triggers,
  4. fires due **deadline** triggers,
  5. sweeps expired signals via ``bus.sweep(now)``,
  6. fires ``scenario_events`` whose ``at_sim_time ≤ now`` and ``fired = 0``,
  7. publishes ``now`` onto the bus (``bus.sim_time``), and
  8. returns the list of WS events to broadcast (at minimum ``sim_tick``).

It also holds the in-process registry of the five §17 trigger kinds
(``interval | deadline | signal_driven | threshold | manual``) and routes
signals to subscribed agents (§14.4). The async ``run_loop`` drives ``tick``
on the real-time cadence while the clock is ``RUNNING``.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import inspect
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from . import config
from .agent_base import BaseAgent
from .bus import SignalBus
from .clock import (
    DAY_CLOSE_OFFSET,
    DAY_OPEN_OFFSET,
    RUNNING,
    SECONDS_PER_DAY,
    SimClock,
    get_or_create_sim_state,
)
from .models import ScenarioEvent, Signal

logger = logging.getLogger(__name__)

# The five trigger kinds from §17.
TRIGGER_TYPES = ("interval", "deadline", "signal_driven", "threshold", "manual")


def _time_of_day(sim_time: float) -> str:
    """Format the ``HH:MM:SS`` clock-time within the current sim day (§6.1)."""
    secs = int(sim_time % SECONDS_PER_DAY)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _signal_to_dict(sig: Signal) -> Dict[str, Any]:
    """Serialize a :class:`Signal` row into a WS-friendly dict (§14.1)."""
    return {
        "signal_id": sig.signal_id,
        "type": sig.type,
        "source": sig.source,
        "groups": sig.groups,
        "priority": sig.priority,
        "payload": sig.payload,
        "created_at": sig.created_at,
        "expires_at": sig.expires_at,
        "dedup_key": sig.dedup_key,
        "status": sig.status,
        "correlation_id": sig.correlation_id,
    }


class Trigger:
    """A registered trigger (§17). ``interval`` triggers track a rolling
    ``next_due``; ``deadline`` triggers fire once at ``due_at``; the
    ``signal_driven`` / ``threshold`` / ``manual`` kinds are fired by their
    respective wake paths, not by the time scheduler."""

    def __init__(
        self,
        trigger_type: str,
        fn: Callable[..., Any],
        interval_sim_s: Optional[float] = None,
        due_at: Optional[float] = None,
        signal_types: Optional[List[str]] = None,
        name: Optional[str] = None,
    ):
        self.trigger_type = trigger_type
        self.fn = fn
        self.interval_sim_s = interval_sim_s
        self.due_at = due_at
        self.signal_types = list(signal_types) if signal_types else []
        self.name = name or getattr(fn, "__name__", trigger_type)
        # Rolling next-due time for interval triggers; deadline triggers use
        # ``due_at`` directly and flip ``fired`` once they run.
        self.next_due: Optional[float] = None
        self.fired = False

    def __repr__(self) -> str:
        return (f"<Trigger {self.name!r} type={self.trigger_type!r} "
                f"next_due={self.next_due} due_at={self.due_at}>")


class Orchestrator:
    """Trigger registry + per-tick dispatch + signal routing (§17)."""

    def __init__(
        self,
        clock: SimClock,
        bus: SignalBus,
        db_session_factory: Callable[[], Any],
    ):
        self.clock = clock
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.triggers: List[Trigger] = []
        self.agents: List[BaseAgent] = []
        self._loop_running = False
        self.coordinator: Optional[Any] = None
        # Let the clock query us for the next scheduled due time (``step``).
        clock.attach_orchestrator(self)

    # -- registration (§17) -------------------------------------------------

    def register(
        self,
        trigger_type: str,
        fn: Callable[..., Any],
        interval_sim_s: Optional[float] = None,
        due_at: Optional[float] = None,
        signal_types: Optional[List[str]] = None,
        name: Optional[str] = None,
    ) -> Trigger:
        """Register one of the five §17 trigger kinds; stored in-process."""
        if trigger_type not in TRIGGER_TYPES:
            raise ValueError(
                f"Unknown trigger_type {trigger_type!r}; "
                f"must be one of {TRIGGER_TYPES}"
            )
        trigger = Trigger(
            trigger_type=trigger_type,
            fn=fn,
            interval_sim_s=interval_sim_s,
            due_at=due_at,
            signal_types=signal_types,
            name=name,
        )
        if trigger_type == "interval":
            # First fire is one interval after the current sim_time, unless an
            # explicit starting ``due_at`` was supplied.
            if due_at is not None:
                trigger.next_due = float(due_at)
            elif interval_sim_s is not None:
                trigger.next_due = self.bus.sim_time + float(interval_sim_s)
        self.triggers.append(trigger)
        return trigger

    def reset_schedules(self) -> None:
        """Re-anchor interval triggers to the current ``sim_time``.

        Called by the clock after a rewind (stop/restart resets ``sim_time`` to
        the start of the day). Without this, each interval trigger keeps its
        stale ``next_due`` from the previous run — which now lies in the future
        — so nothing fires (and no POS orders are generated) until sim_time
        climbs back to it. Re-anchoring restarts every interval cadence on the
        new timeline."""
        now = self.bus.sim_time
        for t in self.triggers:
            if t.trigger_type == "interval" and t.interval_sim_s is not None:
                t.next_due = now + float(t.interval_sim_s)

    def next_due_at(self, now: float) -> Optional[float]:
        """Earliest scheduled trigger due time strictly after ``now`` (used by
        :meth:`SimClock.step`); ``None`` if nothing is scheduled ahead."""
        candidates: List[float] = []
        for t in self.triggers:
            if t.trigger_type == "interval" and t.next_due is not None and t.next_due > now:
                candidates.append(t.next_due)
            elif (
                t.trigger_type == "deadline"
                and not t.fired
                and t.due_at is not None
                and t.due_at > now
            ):
                candidates.append(t.due_at)
        return min(candidates) if candidates else None

    # -- agents (§14.4) -----------------------------------------------------

    def register_agent(self, agent: BaseAgent) -> None:
        """Store an agent; signals are routed to it by group intersection."""
        self.agents.append(agent)

    def on_signal(self, signal: Signal) -> None:
        """Fan a signal out to every agent whose groups intersect the
        signal's ``groups`` (§14.4)."""
        signal_groups = set(signal.groups or [])
        routed = False
        for agent in self.agents:
            if signal_groups.intersection(set(agent.subscribed_groups)):
                routed = True
                started = time.perf_counter()
                try:
                    agent.on_signal(signal)
                except Exception as exc:  # noqa: BLE001 - isolate agent failures.
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    self.bus.record_delivery(
                        signal,
                        consumer=agent.name,
                        delivery_kind="agent",
                        status="failed",
                        duration_ms=duration_ms,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    logger.exception("Agent %s failed handling %s", agent.name, signal.type)
                else:
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    self.bus.record_delivery(
                        signal,
                        consumer=agent.name,
                        delivery_kind="agent",
                        status="ack",
                        duration_ms=duration_ms,
                    )
        if not routed:
            self.bus.record_delivery(
                signal,
                consumer="orchestrator",
                delivery_kind="dead_letter",
                status="unrouted",
                error="No registered agent subscribed to the signal groups.",
            )

    # -- the tick (§6.1 / §17) ---------------------------------------------

    def tick(self) -> List[Dict[str, Any]]:
        """Advance one tick and return the WS events to broadcast."""
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            speed = state.speed if state.speed is not None else 1.0
            skip_closed = bool(state.skip_closed_hours)
            sim_time = state.sim_time if state.sim_time is not None else 0.0

            # (1) advance by Δsim = 60 × speed × 0.25 (§6.1 tick math),
            #     with the closed-hours auto-jump 23:00 → next day 08:00.
            delta = 60.0 * float(speed) * 0.25
            candidate = sim_time + delta
            day = int(sim_time // SECONDS_PER_DAY)
            day_close = day * SECONDS_PER_DAY + DAY_CLOSE_OFFSET
            if skip_closed and candidate >= day_close:
                now = float((day + 1) * SECONDS_PER_DAY + DAY_OPEN_OFFSET)
                jumped = True
            else:
                now = float(candidate)
                jumped = False

            # (2) write the new sim_time + derived day fields back.
            state.sim_time = now
            state.day_number = int(now // SECONDS_PER_DAY)
            state.day_of_week = state.day_number % 7
            session.commit()

            time_of_day = _time_of_day(now)
            status = state.status
            tick_speed = speed
        finally:
            session.close()

        # (7) publish the new sim_time onto the bus *before* firing triggers,
        #     so any handler / emitted signal is stamped with ``now``.
        self.bus.sim_time = now

        # When this tick auto-jumped over closed hours, the restaurant was shut
        # for the whole skipped window ``[window_start, now)`` (23:00 → next-day
        # 08:00). Nothing operational runs while closed: triggers / scenario
        # events whose due time lands inside that window are NOT fired on this
        # tick — interval triggers roll their schedule forward across it, and
        # one-shot deadlines / scenario events stay pending so they fire on the
        # next operating tick. ``window_start`` is unused when no jump occurred.
        window_start = float(day_close) if jumped else None

        # (3) fire due interval triggers.
        self._fire_interval_triggers(now, jumped, window_start)
        # (4) fire due deadline triggers.
        self._fire_deadline_triggers(now, jumped, window_start)
        # (5) sweep expired signals.
        self.bus.sweep(now)
        # (6) fire due scenario events.
        self._fire_scenario_events(now, jumped, window_start)

        # (8) assemble WS events: sim_tick first, then any signals emitted by
        #     the handlers above (queued on the bus), drained here.
        events: List[Dict[str, Any]] = [
            {
                "event": "sim_tick",
                "payload": {
                    "sim_time": now,
                    "day_number": int(now // SECONDS_PER_DAY),
                    "time_of_day": time_of_day,
                    "speed": tick_speed,
                    "status": status,
                },
            }
        ]
        for sig in self.bus.pending_broadcasts():
            events.append(
                {"event": "signal_emitted", "payload": {"signal": _signal_to_dict(sig)}}
            )
        self.bus.clear_broadcasts()
        return events

    # -- tick internals -----------------------------------------------------

    def _in_closed_window(
        self, due: float, jumped: bool, window_start: Optional[float], now: float
    ) -> bool:
        """True if ``due`` falls inside the closed window skipped by this tick's
        auto-jump ``[window_start, now)`` — i.e. the restaurant was shut then."""
        return jumped and window_start is not None and window_start <= due < now

    def _fire_interval_triggers(
        self, now: float, jumped: bool, window_start: Optional[float]
    ) -> None:
        for t in self.triggers:
            if t.trigger_type != "interval" or t.interval_sim_s is None:
                continue
            if t.next_due is None:
                continue
            # Catch up across large jumps without drifting the cadence; slots
            # inside the skipped closed window are rolled past, not fired.
            while t.next_due is not None and t.next_due <= now:
                if not self._in_closed_window(t.next_due, jumped, window_start, now):
                    self._safe_call(t)
                t.next_due += float(t.interval_sim_s)

    def _fire_deadline_triggers(
        self, now: float, jumped: bool, window_start: Optional[float]
    ) -> None:
        for t in self.triggers:
            if t.trigger_type != "deadline" or t.fired or t.due_at is None:
                continue
            if t.due_at > now:
                continue
            # A deadline that falls during closed hours stays pending (it fires
            # on the next operating tick), so nothing runs while shut.
            if self._in_closed_window(t.due_at, jumped, window_start, now):
                continue
            self._safe_call(t)
            t.fired = True

    def _fire_scenario_events(
        self, now: float, jumped: bool, window_start: Optional[float]
    ) -> None:
        session = self.db_session_factory()
        try:
            due = (
                session.query(ScenarioEvent)
                .filter(ScenarioEvent.fired == 0, ScenarioEvent.at_sim_time <= now)
                .order_by(ScenarioEvent.at_sim_time.asc())
                .all()
            )
            for ev in due:
                # Scenario events inside the skipped closed window stay pending
                # so they take effect on the next operating tick.
                if self._in_closed_window(ev.at_sim_time, jumped, window_start, now):
                    continue
                ev.fired = 1
            session.commit()
        finally:
            session.close()

    def _safe_call(self, trigger: Trigger) -> None:
        """Invoke a trigger's callback, isolating failures so one bad trigger
        cannot break the tick."""
        try:
            trigger.fn()
        except Exception:  # noqa: BLE001 — a trigger must never kill the loop.
            logger.exception("Trigger %r raised during tick", trigger.name)

    # -- async run loop (§17) ----------------------------------------------

    async def run_loop(self, broadcast_fn: Callable[[List[Dict[str, Any]]], Any]) -> None:
        """Drive ``tick`` every ``TICK_REAL_MS`` real-ms while the clock is
        ``RUNNING``; broadcast each tick's WS payloads via ``broadcast_fn``.

        Pauses (advances nothing, broadcasts nothing) while the clock is
        ``PAUSED`` / ``STOPPED`` / ``CALL_FROZEN``. Runs until
        :meth:`stop_loop` is called or the task is cancelled.
        """
        interval_s = config.TICK_REAL_MS / 1000.0
        self._loop_running = True
        try:
            while self._loop_running:
                guard = self.coordinator or nullcontext()
                with guard:
                    if self.clock.current_state()["status"] != RUNNING:
                        events = None
                    else:
                        events = self.tick()
                if events is not None:
                    result = broadcast_fn(events)
                    if inspect.isawaitable(result):
                        await result
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            self._loop_running = False
            raise

    def stop_loop(self) -> None:
        """Signal :meth:`run_loop` to exit after its current iteration."""
        self._loop_running = False
