"""Module capability registry for signal-based routing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List

from .signals import SignalType


@dataclass(frozen=True)
class ModuleCapability:
    module: str
    consumes: List[str]
    produces: List[str]
    examples: List[str]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


CAPABILITIES: List[ModuleCapability] = [
    ModuleCapability(
        module="track_a.forecaster",
        consumes=[
            SignalType.DEMAND_EVENT.value,
            SignalType.PRODUCTION_CONSTRAINT.value,
            SignalType.MENU_TOGGLE.value,
            SignalType.STOCKOUT_RISK.value,
            SignalType.STAFF_COVERAGE.value,
            SignalType.INGREDIENT_SHORTAGE_REPORTED.value,
            SignalType.EXPIRY_USE_PRIORITY.value,
            SignalType.COMPETITOR_NOTE.value,
            SignalType.CUSTOMER_FEEDBACK_NOTE.value,
        ],
        produces=[SignalType.DEMAND_FORECAST.value, SignalType.BATCH_DECISION.value],
        examples=["rush expected", "pizza oven broken", "VIP event tonight"],
    ),
    ModuleCapability(
        module="track_a.staff",
        consumes=[SignalType.STAFF_AVAILABILITY.value],
        produces=[SignalType.STAFF_COVERAGE.value],
        examples=["Marco is sick", "all pasta staff are absent"],
    ),
    ModuleCapability(
        module="track_a.review",
        consumes=[SignalType.CUSTOMER_FEEDBACK_NOTE.value],
        produces=[SignalType.REVIEW_INSIGHT.value],
        examples=["customers say tiramisu is too sweet"],
    ),
    ModuleCapability(
        module="track_a.competitor",
        consumes=[SignalType.COMPETITOR_NOTE.value, SignalType.CALL_OUTCOME.value],
        produces=[
            SignalType.COMPETITOR_INTEL.value,
            SignalType.COMPETITOR_MARKET_SIGNAL.value,
        ],
        examples=["Mario's started a pizza discount"],
    ),
    ModuleCapability(
        module="track_b.ledger",
        consumes=[
            SignalType.INVENTORY_RECEIPT_REPORTED.value,
            SignalType.INVENTORY_COUNT_REPORTED.value,
            SignalType.INGREDIENT_SHORTAGE_REPORTED.value,
            SignalType.BATCH_DECISION.value,
        ],
        produces=[
            SignalType.LOW_STOCK.value,
            SignalType.STOCKOUT_RISK.value,
            SignalType.EXPIRY_RISK.value,
            SignalType.WASTE_EVENT.value,
        ],
        examples=["big delivery of tomatoes", "almost out of basil"],
    ),
    ModuleCapability(
        module="track_b.optimizer",
        consumes=[
            SignalType.LOW_STOCK.value,
            SignalType.STOCKOUT_RISK.value,
            SignalType.EXPIRY_RISK.value,
            SignalType.EXPIRY_USE_PRIORITY.value,
            SignalType.MENU_TOGGLE_REQUEST.value,
        ],
        produces=[
            SignalType.MENU_TOGGLE.value,
            SignalType.REORDER_PLACED.value,
            SignalType.PROMO_PROPOSAL.value,
        ],
        examples=["mozzarella expires tomorrow, use it up", "disable tiramisu"],
    ),
]


def capability_prompt_context() -> List[Dict[str, object]]:
    return [cap.to_dict() for cap in CAPABILITIES]
