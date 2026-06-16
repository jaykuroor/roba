"""Contract tests for Track B (02 §B9):

- every signal type B emits validates against the §15 payload schema;
- B's agents subscribe only to the §14.4 group taxonomy (and the ones the
  brief assigns them);
- B never imports Track A modules.
"""

import ast
import pathlib

import pytest

from core.signals import SIGNAL_PAYLOADS, SignalType
from track_b.agents.ledger import GROUPS as LEDGER_GROUPS
from track_b.agents.market_spectator import GROUPS as MARKET_GROUPS
from track_b.agents.optimizer import GROUPS as OPTIMIZER_GROUPS

VALID_GROUPS = {"forecasting", "inventory", "procurement", "kitchen", "sensing", "human", "frontend"}

# Every signal type Track B emits, per 02_TRACK_B.md, with a minimal payload
# that must validate against its §15 pydantic model.
TRACK_B_EMITS = {
    SignalType.LOW_STOCK: {
        "ingredient_id": 1, "on_hand": 5.0, "threshold": 10.0,
        "projected_runout": 100.0, "unit": "g",
    },
    SignalType.STOCKOUT_RISK: {
        "ingredient_id": 1, "on_hand": 0.0, "projected_runout": 100.0, "affected_items": [1, 2],
    },
    SignalType.EXPIRY_RISK: {
        "ingredient_id": 1, "lot_id": 1, "qty": 5.0, "expiry": 1000.0,
        "projected_usage_before_expiry": 2.0,
    },
    SignalType.WASTE_EVENT: {
        "waste_type": "expiry", "ingredient_id": 1, "menu_item_id": None, "lot_id": 1,
        "qty": 5.0, "unit": "g", "cost": 1.0, "reason": "expired",
    },
    SignalType.MENU_TOGGLE: {"menu_item_id": 1, "action": "disable", "reason": "low stock"},
    SignalType.REORDER_PLACED: {
        "po_id": 1, "supplier_id": 1, "lines": [{"ingredient_id": 1, "qty": 10.0}],
        "total": 10.0, "eta": 1000.0,
    },
    SignalType.SUPPLIER_PRICE_UPDATE: {
        "supplier_id": 1, "ingredient_id": 1, "old_price": 2.0, "new_price": 1.5,
        "availability": "in_stock", "via": "call",
    },
    SignalType.PROMO_PROPOSAL: {
        "promo_id": 1, "type": "discount", "menu_items": [1], "discount_pct": 20.0,
        "channel": "both", "trigger": "expiry",
    },
}


@pytest.mark.parametrize("sig_type,payload", list(TRACK_B_EMITS.items()))
def test_emitted_signal_validates_against_registry(bus, sig_type, payload):
    """Every signal Track B emits must validate against its §15 payload model."""
    signal = bus.emit(sig_type, payload, source="track_b_test")
    assert signal is not None
    model = SIGNAL_PAYLOADS[sig_type]
    model.model_validate(signal.payload)  # raises on schema mismatch


def test_all_signal_types_covered():
    """Guard against silently forgetting to add a new B-emitted signal here."""
    missing = {
        SignalType.LOW_STOCK, SignalType.STOCKOUT_RISK, SignalType.EXPIRY_RISK,
        SignalType.WASTE_EVENT, SignalType.MENU_TOGGLE, SignalType.REORDER_PLACED,
        SignalType.SUPPLIER_PRICE_UPDATE, SignalType.PROMO_PROPOSAL,
    } - set(TRACK_B_EMITS)
    assert not missing


def test_agents_subscribe_only_to_valid_groups():
    for groups in (LEDGER_GROUPS, OPTIMIZER_GROUPS, MARKET_GROUPS):
        assert set(groups).issubset(VALID_GROUPS)

    assert set(LEDGER_GROUPS) == {"inventory"}
    assert set(OPTIMIZER_GROUPS) == {"inventory", "procurement"}
    assert set(MARKET_GROUPS) == {"procurement", "inventory"}


def test_track_b_never_imports_track_a():
    """Static check: no track_b module imports anything from track_a."""
    track_b_root = pathlib.Path(__file__).resolve().parents[1]
    py_files = [p for p in track_b_root.rglob("*.py") if "tests" not in p.parts]

    offenders = []
    for path in py_files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("track_a"):
                        offenders.append((path, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("track_a"):
                    offenders.append((path, node.module))
    assert not offenders, f"track_b imports track_a: {offenders}"
