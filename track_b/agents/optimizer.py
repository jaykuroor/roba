"""Inventory Optimizer agent — the decisions (02 §B4.2).

Consumes demand to size reorders, toggle menu items, and turn near-expiry lots
into promos (§18.8). Writes ``purchase_orders`` (via Procurement),
``menu_toggles`` (+ ``menu_items.active``) and ``promotions``.

**Scaffold status:** registered stub. Subscribes to ``inventory, procurement``
and owns the reorder-check interval, but the reorder / toggle / promo logic
(§18.8) is filled in by a later milestone. Entry points are no-ops for now.
"""

from __future__ import annotations

from typing import Any

from core.agent_base import BaseAgent
from core.models import Signal

# Signal groups this agent listens to (02 §B4.2).
GROUPS = ["inventory", "procurement"]


class InventoryOptimizer(BaseAgent):
    """Reorder, menu-toggle, and expiry→promo decisions driven by demand."""

    def __init__(self, bus: Any, db_session_factory: Any, name: str = "optimizer"):
        super().__init__(bus, db_session_factory, name)
        self.subscribe(GROUPS)

    # -- signal handling ----------------------------------------------------

    def on_signal(self, signal: Signal) -> None:
        """React to ``DEMAND_FORECAST`` / ``LOW_STOCK`` / ``STOCKOUT_RISK`` /
        ``EXPIRY_RISK`` / ``BATCH_DECISION`` / ``WASTE_EVENT`` (§B4.2)."""
        # TODO(track_b): reorder / toggle / expiry→promo dispatch (§18.8).
        return None

    # -- triggers -----------------------------------------------------------

    def reorder_check(self) -> None:
        """Periodic reorder sweep: ``on_hand ≤ reorder_point`` → PO (§18.8)."""
        # TODO(track_b): build PO, choose supplier, route to Procurement.
        return None

    # -- approval callbacks (called by the approval handlers) --------------

    def activate_promo(self, promo_id: int) -> None:
        """Mark an approved promotion ``active`` (§B4.5)."""
        # TODO(track_b): set promotions.status = active.
        return None
