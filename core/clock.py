"""SimClock — the simulation clock and its state machine (§6).

The clock owns the *control* side of the simulation: it reads and writes the
``sim_state`` singleton row (``id=1``) and *only* that row. All sim logic uses
sim-seconds (a float since sim-epoch = 00:00 of day 0); never wall-clock.

State machine (§6.2): ``STOPPED → RUNNING ⇄ PAUSED`` plus the transient
``CALL_FROZEN`` (§6.3). The status strings stored in the DB are lower-case to
match the ``sim_state.status`` enum documented in §19.4
(``stopped | running | paused | call_frozen``).

Per-tick *advancement* of ``sim_time`` is the orchestrator's job (§17); the
clock provides the controls (play/pause/stop/restart/step/jump/speed/freeze)
and the day-boundary helpers the orchestrator and agents read.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from . import config
from .bus import SignalBus
from .models import ScenarioEvent, SimState

# -- status constants (§6.2 / §19.4) ---------------------------------------
STOPPED = "stopped"
RUNNING = "running"
PAUSED = "paused"
CALL_FROZEN = "call_frozen"

# -- day geometry (§6.1) ---------------------------------------------------
SECONDS_PER_DAY = 86400
DAY_OPEN_OFFSET = 28800   # 08:00 in seconds-into-day
DAY_CLOSE_OFFSET = 82800  # 23:00 in seconds-into-day

# Used by ``stop()`` to expire *every* live signal regardless of TTL (§6.2).
SWEEP_ALL_NOW = 999999999


def get_or_create_sim_state(session: Any) -> SimState:
    """Return the ``sim_state`` singleton (``id=1``), creating it with binding
    defaults from §22 if it does not yet exist.

    The clock and the orchestrator are the only writers of this row; both go
    through this helper so the singleton's defaults live in exactly one place.
    """
    state = session.get(SimState, 1)
    if state is None:
        state = SimState(
            id=1,
            sim_time=float(DAY_OPEN_OFFSET),       # start of day 0 = 08:00
            day_number=0,
            day_of_week=0,
            speed=1.0,
            status=STOPPED,
            operating_window=list(config.OPERATING_WINDOW),
            skip_closed_hours=1 if config.SKIP_CLOSED_HOURS else 0,
            call_mode=config.CALL_MODE,
            active_seed_id=None,
        )
        session.add(state)
        session.commit()
        session.refresh(state)
    return state


class SimClock:
    """The simulation clock + §6.2 state machine over the ``sim_state`` row."""

    # Re-exported so callers can reference clock states without importing the
    # module-level constants directly.
    STOPPED = STOPPED
    RUNNING = RUNNING
    PAUSED = PAUSED
    CALL_FROZEN = CALL_FROZEN

    def __init__(self, db_session_factory: Callable[[], Any], bus: SignalBus):
        self.db_session_factory = db_session_factory
        self.bus = bus
        # Wired by ``Orchestrator.__init__`` so ``step()`` can ask for the next
        # scheduled trigger due time. Optional: the clock works without it.
        self.orchestrator: Optional[Any] = None

    # -- orchestrator wiring ------------------------------------------------

    def attach_orchestrator(self, orchestrator: Any) -> None:
        """Give the clock a back-reference to the orchestrator (for ``step``)."""
        self.orchestrator = orchestrator

    # -- day geometry helpers (§6.1) ---------------------------------------

    def start_of_day_sim_s(self, day_number: int) -> float:
        """Sim-seconds at 08:00 (open) of ``day_number``."""
        return float(day_number) * SECONDS_PER_DAY + DAY_OPEN_OFFSET

    def end_of_day_sim_s(self, day_number: int) -> float:
        """Sim-seconds at 23:00 (close) of ``day_number``."""
        return float(day_number) * SECONDS_PER_DAY + DAY_CLOSE_OFFSET

    # -- state read --------------------------------------------------------

    def current_state(self) -> Dict[str, Any]:
        """Read the ``sim_state`` row and return the §6.2 control snapshot."""
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            return {
                "sim_time": state.sim_time,
                "day_number": state.day_number,
                "day_of_week": state.day_of_week,
                "speed": state.speed,
                "status": state.status,
                "call_mode": state.call_mode,
            }
        finally:
            session.close()

    @property
    def sim_time(self) -> float:
        """Current sim-time (seconds), read from the ``sim_state`` row."""
        session = self.db_session_factory()
        try:
            return get_or_create_sim_state(session).sim_time
        finally:
            session.close()

    @property
    def day_number(self) -> int:
        """Current day number, read from the ``sim_state`` row."""
        session = self.db_session_factory()
        try:
            return get_or_create_sim_state(session).day_number
        finally:
            session.close()

    # -- internal: write sim_time + derived day fields ---------------------

    def _set_sim_time(self, state: SimState, new_time: float) -> None:
        """Set ``sim_time`` on ``state`` and recompute ``day_number`` /
        ``day_of_week`` from it (§6.1 display helpers)."""
        state.sim_time = float(new_time)
        state.day_number = int(new_time // SECONDS_PER_DAY)
        state.day_of_week = state.day_number % 7

    # -- controls (§6.2) ---------------------------------------------------

    def play(self) -> Dict[str, Any]:
        """``→ RUNNING``: resume advancing from the current ``sim_time``."""
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            state.status = RUNNING
            session.commit()
            self.bus.sim_time = state.sim_time
            return self.current_state()
        finally:
            session.close()

    def pause(self) -> Dict[str, Any]:
        """``→ PAUSED``: freeze; keep everything."""
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            state.status = PAUSED
            session.commit()
            return self.current_state()
        finally:
            session.close()

    def stop(self) -> Dict[str, Any]:
        """``→ STOPPED``: reset ``sim_time`` to the start of the current day,
        clear live signals, keep reference data (§6.2)."""
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            current_day = int(state.sim_time // SECONDS_PER_DAY)
            self._set_sim_time(state, self.start_of_day_sim_s(current_day))
            state.status = STOPPED
            session.commit()
            new_time = state.sim_time
        finally:
            session.close()

        # Clear *all* live signals (transient world state) — KEEP seed data,
        # ledger history, logs (those live in other tables). §6.2.
        self.bus.sweep(now=SWEEP_ALL_NOW)
        self.bus.sim_time = new_time
        return self.current_state()

    def restart(self, seeding_fn: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
        """Full reset (§6.2): optionally re-seed, then reset to the start of
        day 0 in the ``STOPPED`` state."""
        if seeding_fn is not None:
            seeding_fn()

        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            self._set_sim_time(state, self.start_of_day_sim_s(0))
            state.status = STOPPED
            session.commit()
            new_time = state.sim_time
        finally:
            session.close()

        self.bus.sim_time = new_time
        return self.current_state()

    def set_speed(self, speed: float) -> Dict[str, Any]:
        """Change the speed multiplier live; must be one of ``config.SPEEDS``."""
        if speed not in config.SPEEDS:
            raise ValueError(
                f"Illegal speed {speed!r}; must be one of {config.SPEEDS}"
            )
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            state.speed = float(speed)
            session.commit()
            return self.current_state()
        finally:
            session.close()

    # -- call freeze/restore (§6.3) ----------------------------------------

    def freeze_for_call(self) -> Tuple[str, float]:
        """Enter ``CALL_FROZEN`` (sim time stops). Returns ``(prior_status,
        prior_speed)`` so the caller can restore them with
        :meth:`unfreeze_from_call`."""
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            prior_status, prior_speed = state.status, state.speed
            state.status = CALL_FROZEN
            session.commit()
            return prior_status, prior_speed
        finally:
            session.close()

    def unfreeze_from_call(self, prior_status: str, prior_speed: float) -> Dict[str, Any]:
        """Restore the state/speed captured before a call's ``CALL_FROZEN``."""
        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            state.status = prior_status
            state.speed = prior_speed
            session.commit()
            self.bus.sim_time = state.sim_time
            return self.current_state()
        finally:
            session.close()

    # -- step / jump (§6.2) ------------------------------------------------

    def step(self) -> Dict[str, Any]:
        """Advance to the next orchestrator trigger's due time, then ``PAUSE``.

        If no orchestrator is attached or no future trigger is scheduled, this
        only transitions to ``PAUSED`` without moving time.
        """
        now = self.sim_time
        target: Optional[float] = None
        if self.orchestrator is not None:
            target = self.orchestrator.next_due_at(now)

        session = self.db_session_factory()
        try:
            state = get_or_create_sim_state(session)
            if target is not None:
                self._set_sim_time(state, target)
            state.status = PAUSED
            session.commit()
            new_time = state.sim_time
        finally:
            session.close()

        self.bus.sim_time = new_time
        return self.current_state()

    def jump_to_next_event(self) -> Dict[str, Any]:
        """Fast-forward to the earliest unfired ``scenario_events.at_sim_time``
        in the future, then ``PAUSE`` (§6.2). No-op advance if none remain."""
        now = self.sim_time
        session = self.db_session_factory()
        try:
            target = (
                session.query(ScenarioEvent.at_sim_time)
                .filter(
                    ScenarioEvent.fired == 0,
                    ScenarioEvent.at_sim_time > now,
                )
                .order_by(ScenarioEvent.at_sim_time.asc())
                .first()
            )
            state = get_or_create_sim_state(session)
            if target is not None:
                self._set_sim_time(state, float(target[0]))
            state.status = PAUSED
            session.commit()
            new_time = state.sim_time
        finally:
            session.close()

        self.bus.sim_time = new_time
        return self.current_state()
