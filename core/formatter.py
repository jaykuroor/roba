"""Data formatter & wastage relay (§16).

The formatter turns raw POS output into the system's derived signals using
mostly deterministic rules:

- **Velocity enrichment:** maintains a rolling per-item sales rate from
  ``order_lines`` over the last ``VELOCITY_WINDOW_SIM_S`` (a ring buffer keyed
  by ``menu_item_id``), exposed to the Forecaster and broadcast as part of the
  ``order_created`` WS payload.
- **Order-line fan-out:** each non-voided line is handed to the in-process
  order-line callback (``bus.notify_order_line``) which drives Track B's
  depletion; each voided line becomes a ``cancelled_order`` waste event.
- **Wastage relay (§16 routing):** every ``waste_events`` row emits one
  ``WASTE_EVENT`` signal whose groups fan out per waste type.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from .models import OrderLine, WasteEvent
from .signals import SignalType

# §16 routing — which signal groups each waste type fans out to.
WASTE_ROUTING: Dict[str, List[str]] = {
    "overproduction": ["inventory", "forecasting", "procurement", "human"],
    "spoilage": ["inventory", "procurement", "human"],
    "expiry": ["inventory", "procurement", "human"],
    "cancelled_order": ["inventory", "human"],
    "prep_error": ["inventory", "human"],
}


class DataFormatter:
    """Velocity enrichment + order-line fan-out + wastage relay (§16)."""

    def __init__(self, bus: Any, db_session_factory: Callable[[], Any]):
        self.bus = bus
        self.db_session_factory = db_session_factory
        # Ring buffers of (sim_time, qty) per menu_item_id over the velocity
        # window. In-memory only (derived, cheap to rebuild).
        self._velocity: Dict[int, List[Tuple[float, float]]] = {}
        # Optional WS broadcast sink ``fn(event: str, payload: dict)``, wired by
        # the API layer; a no-op (None) in tests / headless runs.
        self.ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None

    # -- WS wiring ----------------------------------------------------------

    def set_ws_broadcast(self, fn: Callable[[str, Dict[str, Any]], Any]) -> None:
        """Wire the sink the formatter pushes ``order_created`` events to."""
        self.ws_broadcast = fn

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)

    # -- velocity ring buffer (§16) ----------------------------------------

    def _record_sale(self, menu_item_id: int, sim_time: float, qty: float) -> None:
        buffer = self._velocity.setdefault(menu_item_id, [])
        buffer.append((sim_time, qty))
        self._prune(buffer, sim_time)

    @staticmethod
    def _prune(buffer: List[Tuple[float, float]], now: float) -> None:
        cutoff = now - config.VELOCITY_WINDOW_SIM_S
        # Drop entries older than the rolling window.
        while buffer and buffer[0][0] < cutoff:
            buffer.pop(0)

    def item_velocity(self, menu_item_id: int) -> float:
        """Current items-per-sim-second rate for ``menu_item_id`` over the
        rolling window (``0.0`` if no data yet)."""
        buffer = self._velocity.get(menu_item_id)
        if not buffer:
            return 0.0
        # Prune relative to the most recent known time (sim clock or last sale).
        now = max(self.bus.sim_time, buffer[-1][0])
        self._prune(buffer, now)
        if not buffer:
            return 0.0
        total = sum(qty for _t, qty in buffer)
        return total / config.VELOCITY_WINDOW_SIM_S

    # -- order handling (§16) ----------------------------------------------

    def on_order(self, order: Any, lines: List[Any]) -> None:
        """Process one created order's lines (§16).

        For each non-voided line: update the rolling velocity and fire the
        order-line callback (``bus.notify_order_line``). Each voided line emits
        a ``cancelled_order`` waste event. Finally broadcast ``order_created``
        with the per-item velocity dict.
        """
        for line in lines:
            if getattr(line, "status", "sold") == "voided":
                self.emit_waste(
                    line,
                    waste_type="cancelled_order",
                    menu_item_id=line.menu_item_id,
                    qty=float(line.qty or 0.0),
                    unit="each",
                    reason="order line voided",
                )
                continue

            sim_time = line.sim_time if line.sim_time is not None else order.sim_time
            self._record_sale(line.menu_item_id, float(sim_time), float(line.qty or 0.0))
            self.bus.notify_order_line(line)

        velocity = {
            str(line.menu_item_id): self.item_velocity(line.menu_item_id)
            for line in lines
            if getattr(line, "status", "sold") != "voided"
        }
        self._broadcast(
            "order_created",
            {
                "order": self._order_to_dict(order),
                "lines": [self._line_to_dict(line) for line in lines],
                "velocity": velocity,
            },
        )

    # -- wastage relay (§16) -----------------------------------------------

    def emit_waste(
        self,
        source_obj: Any,
        waste_type: str,
        ingredient_id: Optional[int] = None,
        menu_item_id: Optional[int] = None,
        lot_id: Optional[int] = None,
        qty: float = 0.0,
        unit: str = "",
        cost: float = 0.0,
        reason: str = "",
    ) -> Any:
        """Write a ``waste_events`` row, then emit one ``WASTE_EVENT`` signal
        routed per §16 (groups depend on ``waste_type``).

        ``source_obj`` is the originating row (e.g. an :class:`OrderLine`); its
        ``sim_time`` stamps the waste event when available.
        """
        sim_time = getattr(source_obj, "sim_time", None)
        if sim_time is None:
            sim_time = self.bus.sim_time

        session = self.db_session_factory()
        try:
            row = WasteEvent(
                waste_type=waste_type,
                ingredient_id=ingredient_id,
                menu_item_id=menu_item_id,
                lot_id=lot_id,
                qty=qty,
                unit=unit,
                cost=cost,
                reason=reason,
                sim_time=sim_time,
                source="formatter",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
        finally:
            session.close()

        payload = {
            "waste_type": waste_type,
            "ingredient_id": ingredient_id,
            "menu_item_id": menu_item_id,
            "lot_id": lot_id,
            "qty": qty,
            "unit": unit,
            "cost": cost,
            "reason": reason,
        }
        groups = WASTE_ROUTING.get(waste_type)
        return self.bus.emit(
            SignalType.WASTE_EVENT,
            payload,
            source="formatter",
            groups=groups,
        )

    # -- serialization helpers ---------------------------------------------

    @staticmethod
    def _order_to_dict(order: Any) -> Dict[str, Any]:
        return {
            "id": order.id,
            "sim_time": order.sim_time,
            "service_mode": order.service_mode,
            "channel": order.channel,
            "guest_count": order.guest_count,
            "status": order.status,
            "total": order.total,
        }

    @staticmethod
    def _line_to_dict(line: Any) -> Dict[str, Any]:
        return {
            "id": line.id,
            "order_id": line.order_id,
            "menu_item_id": line.menu_item_id,
            "qty": line.qty,
            "unit_price": line.unit_price,
            "line_total": line.line_total,
            "status": line.status,
            "sim_time": line.sim_time,
        }
