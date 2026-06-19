"""BaseAgent — the abstract base every track agent subclasses.

An agent talks to the rest of the system *only* through the signal bus (§2):
it ``subscribe``s to groups (the orchestrator routes in-group signals to its
``on_signal``), ``emit``s new signals (defaulting ``source`` to its name), and
``log_event``s narrative rows into ``event_log`` stamped with the current
``sim_time`` (read from the bus, which the clock advances each tick).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from .bus import PayloadArg, SignalBus, SignalTypeArg
from .models import EventLog, Signal


class BaseAgent(ABC):
    """Abstract agent: subscribe to groups, react to signals, emit, log."""

    def __init__(self, bus: SignalBus, db_session_factory: Any, name: str):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.name = name
        self._subscribed_groups: List[str] = []

    # -- subscription -------------------------------------------------------

    def subscribe(self, groups: List[str]) -> None:
        """Record the groups this agent listens to (orchestrator routes by it)."""
        self._subscribed_groups = list(groups)

    @property
    def subscribed_groups(self) -> List[str]:
        return list(self._subscribed_groups)

    # -- signal handling ----------------------------------------------------

    @abstractmethod
    def on_signal(self, signal: Signal) -> None:
        """React to a signal routed to one of this agent's groups."""
        raise NotImplementedError

    def emit(
        self,
        type: SignalTypeArg,
        payload: PayloadArg,
        source: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[Signal]:
        """Thin wrapper around :meth:`SignalBus.emit`; defaults source to name."""
        return self.bus.emit(type, payload, source=source or self.name, **kwargs)

    # -- narrative log ------------------------------------------------------

    def log_event(
        self,
        category: str,
        summary: str,
        detail: Optional[Any] = None,
    ) -> EventLog:
        """Write one ``event_log`` row at the current sim-time (§19.4)."""
        session = self.db_session_factory()
        try:
            row = EventLog(
                sim_time=self.sim_time,
                category=category,
                actor=self.name,
                summary=summary,
                detail=detail,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row
        finally:
            session.close()

    # -- deferred side effects ---------------------------------------------

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        """Publish a WebSocket event when the agent has a broadcast sink."""
        sink = getattr(self, "ws_broadcast", None)
        if sink is not None:
            sink(event, payload)

    def _run_after_commit(self, actions: List[Tuple[str, Any]]) -> None:
        """Run signal/log/broadcast work after an agent commits DB changes."""
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
                remember = getattr(self, "_remember")
                remember(*payload)

    # -- clock --------------------------------------------------------------

    @property
    def sim_time(self) -> float:
        """Current sim-time, delegated to the bus."""
        return self.bus.sim_time
