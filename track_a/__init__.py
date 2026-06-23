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
    SignalType.USER_FACT,
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

    for signal_type in TRACK_A_SIGNAL_TYPES:
        bus.subscribe(signal_type, orchestrator.on_signal)

    mock_inventory = None
    if os.getenv("DEMO_MODE", "combined") == "track_a":
        mock_inventory = MockInventory(bus, db_session_factory, ws_broadcast)
        mock_inventory.register(orchestrator)

    agents["mock_inventory"] = mock_inventory
    return agents
