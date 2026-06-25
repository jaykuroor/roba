"""Track A bootstrap."""

from __future__ import annotations

import os
from typing import Any, Dict

from core.signals import SignalType

from .agents.competitor import CompetitorAgent
from .agents.forecaster import DemandForecaster
from .agents.review import ReviewAgent
from .agents.staff import StaffAgent
from .mocks.mock_inventory import MockInventory


TRACK_A_SIGNAL_TYPES = [
    SignalType.WASTE_EVENT,
    SignalType.STAFF_COVERAGE,
    SignalType.COMPETITOR_UPDATE,
    SignalType.COMPETITOR_INTEL,
    SignalType.COMPETITOR_MARKET_SIGNAL,
    SignalType.REVIEW_INSIGHT,
    SignalType.WEATHER_UPDATE,
    SignalType.DEMAND_EVENT,
    SignalType.PRODUCTION_CONSTRAINT,
    SignalType.STAFF_AVAILABILITY,
    SignalType.INGREDIENT_SHORTAGE_REPORTED,
    SignalType.EXPIRY_USE_PRIORITY,
    SignalType.CUSTOMER_FEEDBACK_NOTE,
    SignalType.COMPETITOR_NOTE,
    SignalType.MENU_TOGGLE,
    SignalType.STOCKOUT_RISK,
    SignalType.CALL_OUTCOME,
]


def bootstrap_track_a(
    bus: Any,
    db_session_factory: Any,
    orchestrator: Any,
    formatter: Any = None,
    calls: Any = None,
    llm: Any = None,
    ws_broadcast: Any = None,
) -> Dict[str, Any]:
    """Wire Track A agents into core without crossing track boundaries."""
    forecaster = DemandForecaster(
        bus, db_session_factory, formatter, ws_broadcast, llm=llm
    )
    competitor = CompetitorAgent(bus, db_session_factory, calls, ws_broadcast)
    review = ReviewAgent(bus, db_session_factory, llm, ws_broadcast)
    staff = StaffAgent(bus, db_session_factory, ws_broadcast)
    agents = {
        "forecaster": forecaster,
        "competitor": competitor,
        "review": review,
        "staff": staff,
    }
    for agent in agents.values():
        orchestrator.register_agent(agent)
        agent.register(orchestrator)

    mock_inventory = None
    if os.getenv("DEMO_MODE", "combined") == "track_a":
        mock_inventory = MockInventory(bus, db_session_factory, ws_broadcast)
        mock_inventory.register(orchestrator)

    agents["mock_inventory"] = mock_inventory
    return agents
