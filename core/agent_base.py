"""BaseAgent — the abstract base every track agent subclasses.

An agent talks to the rest of the system *only* through the signal bus (§2):
it ``subscribe``s to groups (the orchestrator routes in-group signals to its
``on_signal``), ``emit``s new signals (defaulting ``source`` to its name), and
``log_event``s narrative rows into ``event_log`` stamped with the current
``sim_time`` (read from the bus, which the clock advances each tick).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bus import PayloadArg, SignalBus, SignalTypeArg
from .models import EventLog, Signal


class BaseAgent(ABC):
    """Abstract agent: subscribe to groups, react to signals, emit, log."""

    def __init__(
        self,
        bus: SignalBus,
        db_session_factory: Any,
        name: str,
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.name = name
        self._subscribed_groups: List[str] = []
        # Optional WS broadcast sink ``fn(event, payload)`` wired by the app
        # shell (mirrors the core component pattern in approvals.py/calls.py);
        # a no-op (None) in tests / headless runs.
        self.ws_broadcast = ws_broadcast

    # -- subscription -------------------------------------------------------

    def subscribe(self, groups: List[str]) -> None:
        """Record the groups this agent listens to (orchestrator routes by it)."""
        self._subscribed_groups = list(groups)

    @property
    def subscribed_groups(self) -> List[str]:
        return list(self._subscribed_groups)

    def capabilities(self) -> Dict[str, Any]:
        return {"module": self.name, "subscribed_groups": self.subscribed_groups}

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

    # -- WS broadcast ---------------------------------------------------------

    def broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        """Push one ``{event, payload}`` WS message, if a sink is wired."""
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)

    # -- narrative log ------------------------------------------------------

    def log_event(
        self,
        category: str,
        summary: str,
        detail: Optional[Any] = None,
    ) -> EventLog:
        """Write one ``event_log`` row at the current sim-time (§19.4) and
        broadcast it as ``event_logged`` (the Activity Log's narrative feed)."""
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
        finally:
            session.close()

        self.broadcast(
            "event_logged",
            {
                "event": {
                    "id": row.id,
                    "sim_time": row.sim_time,
                    "category": row.category,
                    "actor": row.actor,
                    "summary": row.summary,
                    "detail": row.detail,
                }
            },
        )
        return row

    # -- deferred side effects ---------------------------------------------

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        """Publish a WebSocket event when the agent has a broadcast sink."""
        self.broadcast(event, payload)

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
            elif kind == "forecast_override":
                persist_override = getattr(self, "_persist_override")
                persist_override(*payload)

    # -- clock --------------------------------------------------------------

    @property
    def sim_time(self) -> float:
        """Current sim-time, delegated to the bus."""
        return self.bus.sim_time
