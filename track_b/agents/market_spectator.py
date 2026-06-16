"""Market Spectator agent — supplier costs & negotiation (02 §B4.3).

Tracks supplier prices, negotiates via approval-gated voice calls (core call
subsystem §8), and reacts to spoilage. Writes ``supplier_catalog`` dynamic
fields, ``supplier_price_history`` and ``negotiations`` on agreed outcomes.

**Scaffold status:** registered stub. Subscribes to ``procurement, inventory``
and owns the price-review interval, but monitoring / negotiation / spoilage
logic (§B4.3) is filled in by a later milestone. Entry points are no-ops.
"""

from __future__ import annotations

from typing import Any

from core.agent_base import BaseAgent
from core.models import Signal

# Signal groups this agent listens to (02 §B4.3).
GROUPS = ["procurement", "inventory"]


class MarketSpectator(BaseAgent):
    """Supplier price monitoring + negotiation calls + spoilage reaction."""

    def __init__(self, bus: Any, db_session_factory: Any, name: str = "market_spectator"):
        super().__init__(bus, db_session_factory, name)
        self.subscribe(GROUPS)

    # -- signal handling ----------------------------------------------------

    def on_signal(self, signal: Signal) -> None:
        """React to ``CALL_OUTCOME`` (negotiation result) and
        ``WASTE_EVENT(spoilage)`` (§B4.3)."""
        # TODO(track_b): apply agreed price; spoilage → order less/fresher.
        return None

    # -- triggers -----------------------------------------------------------

    def review_prices(self) -> None:
        """Periodic price review against ``supplier_price_history`` (§B4.3)."""
        # TODO(track_b): flag above-median prices; consider negotiating.
        return None
