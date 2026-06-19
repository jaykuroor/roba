import ast
from pathlib import Path

from core.signals import SignalType
from track_a import TRACK_A_SIGNAL_TYPES
from track_a.agents.competitor import CompetitorAgent
from track_a.agents.forecaster import DemandForecaster
from track_a.agents.review import ReviewAgent
from track_a.agents.staff import StaffAgent


def test_agents_subscribe_only_to_allowed_groups(bus, session_factory):
    agents = [
        DemandForecaster(bus, session_factory),
        CompetitorAgent(bus, session_factory),
        ReviewAgent(bus, session_factory),
        StaffAgent(bus, session_factory),
    ]
    allowed = {"forecasting", "sensing"}
    for agent in agents:
        assert set(agent.subscribed_groups).issubset(allowed)


def test_track_a_emitted_payloads_validate(bus):
    bus.emit(
        SignalType.DEMAND_FORECAST,
        {"menu_item_id": 1, "window": {"start": 1.0, "end": 2.0}, "daypart": "lunch", "qty": 3.0, "baseline": 2.0, "multipliers": {"event": 1.0}, "confidence": 1.0},
        source="test",
    )
    bus.emit(
        SignalType.BATCH_DECISION,
        {"batch_definition_id": 1, "menu_item_id": 1, "serve_window": {"start": 1.0, "end": 2.0}, "decision": "cook", "qty": 4.0, "by": "agent"},
        source="test",
    )
    bus.emit(
        SignalType.COMPETITOR_INTEL,
        {"competitor_id": 1, "popular_dishes": ["Pizza"], "price_points": {}, "method": "call", "call_id": 1},
        source="test",
    )
    bus.emit(
        SignalType.REVIEW_INSIGHT,
        {"review_id": 1, "severity": "low", "summary": "ok", "suggested_action": "none", "dish_mentions": []},
        source="test",
    )
    bus.emit(
        SignalType.STAFF_COVERAGE,
        {"station_id": 1, "covered": True, "affected_items": [], "shortfall": 0.0},
        source="test",
        ttl=100.0,
    )


def test_no_track_b_imports():
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "track_b" for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "track_b"


def test_registered_signal_types_are_core_enums():
    assert all(isinstance(signal_type, SignalType) for signal_type in TRACK_A_SIGNAL_TYPES)
