"""Track B agent wiring (02 §B1–§B5).

``register`` is the single entry point the core app shell calls at startup
(``core/api.py`` bootstrap) once per process. It constructs the three Track B
agents, subscribes them to their groups, registers them with the orchestrator
(so in-group signals are routed to ``on_signal``), wires the per-line depletion
callback (§10), and registers each agent's §17 interval triggers.

When ``DEMO_MODE=track_b`` it also constructs and registers the
:class:`~track_b.mocks.mock_forecaster.MockForecaster`, the placeholder that
drives the whole track (§B5). In ``combined`` the mock is omitted — real Track A
``DEMAND_FORECAST`` / ``BATCH_DECISION`` signals arrive instead, with no other
code change (§B8).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core import config

from .ledger import InventoryLedger
from .market_spectator import MarketSpectator
from .optimizer import InventoryOptimizer

logger = logging.getLogger(__name__)


def register(
    *,
    bus: Any,
    orchestrator: Any,
    db_session_factory: Any,
    demo_mode: str = "combined",
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Wire Track B into the running core (agents, triggers, mock).

    Extra keyword args (``llm``, ``calls``, ``approvals`` …) are accepted and
    ignored for now so the core call-site can pass the full context without this
    scaffold needing to know which pieces a later milestone consumes.

    Returns a dict of the constructed components (handy for tests)."""
    ledger = InventoryLedger(bus, db_session_factory)
    optimizer = InventoryOptimizer(bus, db_session_factory)
    market = MarketSpectator(bus, db_session_factory)

    for agent in (ledger, optimizer, market):
        orchestrator.register_agent(agent)

    # Per-line depletion is driven by the core POS sim via this callback, not by
    # the signal bus (§10) — the Ledger is the registered consumer.
    bus.register_order_line_handler(ledger.handle_order_line)

    # §17 interval triggers each agent owns.
    orchestrator.register(
        "interval",
        ledger.scan_expiry,
        interval_sim_s=config.EXPIRY_SCAN_SIM_S,
        name="ledger_expiry_scan",
    )
    orchestrator.register(
        "interval",
        optimizer.reorder_check,
        interval_sim_s=config.FORECAST_INTERVAL_SIM_S,
        name="optimizer_reorder_check",
    )
    orchestrator.register(
        "interval",
        market.review_prices,
        interval_sim_s=config.WEATHER_FETCH_SIM_S,
        name="market_price_review",
    )

    components: Dict[str, Any] = {
        "ledger": ledger,
        "optimizer": optimizer,
        "market_spectator": market,
        "mock_forecaster": None,
    }

    # The placeholder that drives the whole track when running standalone (§B5).
    if demo_mode == "track_b":
        from ..mocks.mock_forecaster import MockForecaster

        mock = MockForecaster(bus, db_session_factory)
        mock.register(orchestrator)
        components["mock_forecaster"] = mock
        logger.info("Track B: MockForecaster registered (DEMO_MODE=track_b)")

    logger.info("Track B agents registered (demo_mode=%s)", demo_mode)
    return components
