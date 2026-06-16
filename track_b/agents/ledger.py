"""Inventory Ledger agent — the source of truth for stock (02 §B4.1).

Maintains stock as an append-only ledger and raises stock / expiry / waste
signals. It is the **only** writer of ``inventory_ledger / inventory_lots /
inventory_levels`` (§19.4) and the single component that depletes inventory.

**Scaffold status:** this is a registered stub. It already subscribes to the
``inventory`` group, takes the per-line depletion callback (``handle_order_line``,
§10), and owns the ``EXPIRY_SCAN_SIM_S`` scan trigger — but the depletion math
(§18.4), threshold signals (§18.8), receipts, reconciliation, and waste emission
are filled in by a later milestone. The reactor/trigger entry points below are
intentionally no-ops so the wiring is exercised end-to-end first.
"""

from __future__ import annotations

from typing import Any

from core.agent_base import BaseAgent
from core.models import Signal

# Signal groups this agent listens to (02 §B4.1).
GROUPS = ["inventory"]


class InventoryLedger(BaseAgent):
    """Deterministic stock ledger; depletion, thresholds, receipts, waste."""

    def __init__(self, bus: Any, db_session_factory: Any, name: str = "ledger"):
        super().__init__(bus, db_session_factory, name)
        self.subscribe(GROUPS)

    # -- signal handling ----------------------------------------------------

    def on_signal(self, signal: Signal) -> None:
        """React to ``BATCH_DECISION(cook)`` / ``USER_FACT`` / PO deliveries.

        Stub: depletion + threshold logic (§18.4 / §18.8) lands in a later
        milestone."""
        # TODO(track_b): batch depletion, receipts, reconciliation (§18.4).
        return None

    # -- order-line callback (§10) -----------------------------------------

    def handle_order_line(self, line: Any) -> None:
        """Deplete ingredients for one sold ``order_line`` (FIFO, §18.4).

        Registered with ``bus.register_order_line_handler`` so the core POS sim
        drives depletion without putting order lines on the signal bus."""
        # TODO(track_b): FIFO depletion + ledger append + threshold checks.
        return None

    # -- triggers -----------------------------------------------------------

    def scan_expiry(self) -> None:
        """Expiry scan every ``EXPIRY_SCAN_SIM_S`` → ``EXPIRY_RISK`` / waste."""
        # TODO(track_b): expiry scan + WASTE_EVENT on expired lots (§18.8).
        return None

    # -- receipts (called by Procurement) ----------------------------------

    def receive(self, po: Any) -> None:
        """Create a receipt lot + ledger entry on PO delivery (§B4.1)."""
        # TODO(track_b): create inventory_lot + inventory_ledger(receipt).
        return None
