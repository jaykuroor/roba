"""Tests for WS2/WS3: project_fefo_coverage + WS4: PO consolidation sentinel.

Core invariant verified:
  1. FEFO validator correctly identifies mascarpone 1220g shortage.
  2. After adding 3000g total, shortage resolves.
  3. Day-by-day demand phasing: stock arriving after demand stays uncovered.
  4. Perishable lot expiry respected.
  5. coverage_depends_on_planned and late_delivery flags are correct.
"""
import pytest
from track_b.procurement.plan_optimizer import project_fefo_coverage

_INF = 10 ** 9


def _fefo(**kwargs):
    """Call project_fefo_coverage with sensible defaults for params not under test."""
    return project_fefo_coverage(
        n_days=kwargs.get("n_days", 8),
        demand_by_day=kwargs.get("demand_by_day", {}),
        on_hand=kwargs.get("on_hand", {}),
        lots_by_ing=kwargs.get("lots_by_ing", {}),
        open_po_arrivals_by_day=kwargs.get("open_po_arrivals_by_day", {}),
        plan_arrivals_by_day=kwargs.get("plan_arrivals_by_day", {}),
        safety_stock=kwargs.get("safety_stock", {}),
        ingredient_shelf_life=kwargs.get("ingredient_shelf_life", {}),
    )


# ---------------------------------------------------------------------------
# 1. Mascarpone reviewer scenario
# ---------------------------------------------------------------------------

def test_mascarpone_shortage_detected():
    """With 2400g on-hand + 1500g inbound and 5120g demand → 1220g short."""
    result = _fefo(
        n_days=8,
        demand_by_day={12: {d: 640.0 for d in range(8)}},   # 64 Tiramisu × 80g / 8 days
        on_hand={12: 2400.0},
        lots_by_ing={12: [[2400.0, _INF]]},
        open_po_arrivals_by_day={12: {2: 1500.0}},           # one Bella pack arrives day 2
        plan_arrivals_by_day={},
        safety_stock={12: 600.0},
    )
    assert not result["coverage_ok"], "Should be uncovered"
    assert abs(result["total_short"] - 1220.0) < 1.0
    assert 12 in result["short_by_ing"]


def test_mascarpone_covered_with_3000g():
    """With 2400g on-hand + 3000g (two packs) ordered → fully covered."""
    result = _fefo(
        n_days=8,
        demand_by_day={12: {d: 640.0 for d in range(8)}},
        on_hand={12: 2400.0},
        lots_by_ing={12: [[2400.0, _INF]]},
        open_po_arrivals_by_day={12: {2: 1500.0}},
        plan_arrivals_by_day={12: {2: 1500.0}},              # second pack as planned order
        safety_stock={12: 600.0},
    )
    assert result["coverage_ok"], "Should be fully covered"
    assert result["total_short"] == 0.0
    assert 12 not in result["short_by_ing"]


def test_subunit_gap_absorbed_by_coverage_tolerance():
    """A sub-unit shortfall (integer-pack MILP vs continuous FEFO rounding) must
    NOT flip coverage_ok or emit a short when a pack-scale tolerance is passed.

    Reproduces the live 0.3g "Basil" phantom that flipped coverage_ok=False and
    fired a false INGREDIENT_UNCOVERABLE on an otherwise-covered plan.
    """
    common = dict(
        n_days=2,
        demand_by_day={4: {0: 100.3}},        # 0.3 more than on-hand
        on_hand={4: 100.0},
        lots_by_ing={4: [[100.0, _INF]]},
        open_po_arrivals_by_day={},
        plan_arrivals_by_day={},
        safety_stock={},
    )
    tight = project_fefo_coverage(**common)                 # default tight epsilon
    assert not tight["coverage_ok"]
    assert abs(tight["total_short"] - 0.3) < 1e-6

    toler = project_fefo_coverage(**common, coverage_tolerance=1.0)
    assert toler["coverage_ok"], "sub-unit rounding gap must be tolerated"
    assert toler["total_short"] == 0.0
    assert 4 not in toler["short_by_ing"]

    # A real (pack-scale) shortage still surfaces despite the tolerance.
    big = dict(common)
    big["demand_by_day"] = {4: {0: 250.0}}
    real = project_fefo_coverage(**big, coverage_tolerance=1.0)
    assert not real["coverage_ok"]
    assert abs(real["total_short"] - 150.0) < 1e-6


def test_mascarpone_covered_depends_on_planned():
    """coverage_depends_on_planned must be True when the second pack is still a planned order."""
    result = _fefo(
        n_days=8,
        demand_by_day={12: {d: 640.0 for d in range(8)}},
        on_hand={12: 2400.0},
        lots_by_ing={12: [[2400.0, _INF]]},
        open_po_arrivals_by_day={12: {2: 1500.0}},
        plan_arrivals_by_day={12: {2: 1500.0}},
        safety_stock={12: 600.0},
    )
    assert result["coverage_depends_on_planned"]


# ---------------------------------------------------------------------------
# 2. Timing: arrival after demand day is too late
# ---------------------------------------------------------------------------

def test_late_arrival_causes_shortage():
    """100g demanded on day 0, 200g arriving on day 1 — day 0 is uncovered."""
    result = _fefo(
        n_days=3,
        demand_by_day={1: {0: 100.0}},
        on_hand={1: 0.0},
        lots_by_ing={},
        open_po_arrivals_by_day={1: {1: 200.0}},             # arrives AFTER day 0 demand
        plan_arrivals_by_day={},
        safety_stock={1: 0.0},
    )
    assert not result["coverage_ok"]
    assert result["total_short"] >= 100.0 - 1.0


def test_arrival_on_same_day_covers():
    """100g demanded on day 1, 200g arriving on day 1 — covered (arrival before demand)."""
    result = _fefo(
        n_days=3,
        demand_by_day={1: {1: 100.0}},
        on_hand={1: 0.0},
        lots_by_ing={},
        open_po_arrivals_by_day={1: {1: 200.0}},
        plan_arrivals_by_day={},
        safety_stock={1: 0.0},
    )
    assert result["coverage_ok"]
    assert result["total_short"] == 0.0


# ---------------------------------------------------------------------------
# 3. Perishable lot expiry
# ---------------------------------------------------------------------------

def test_expired_lot_not_counted():
    """A lot expiring on day 2 cannot satisfy demand on day 3."""
    result = _fefo(
        n_days=5,
        demand_by_day={7: {3: 100.0}},                       # demand on day 3
        on_hand={7: 200.0},
        lots_by_ing={7: [[200.0, 2.0]]},                     # lot expires at START of day 2
        open_po_arrivals_by_day={},
        plan_arrivals_by_day={},
        safety_stock={7: 0.0},
        ingredient_shelf_life={7: 3.0},
    )
    assert not result["coverage_ok"], "Expired lot should not cover day-3 demand"


def test_fresh_lot_with_adequate_shelf_life():
    """A lot expiring on day 5 can satisfy demand on days 0-4."""
    result = _fefo(
        n_days=5,
        demand_by_day={7: {3: 100.0}},
        on_hand={7: 200.0},
        lots_by_ing={7: [[200.0, 5.0]]},                     # lot expires at START of day 5
        open_po_arrivals_by_day={},
        plan_arrivals_by_day={},
        safety_stock={7: 0.0},
        ingredient_shelf_life={7: 3.0},
    )
    assert result["coverage_ok"]


# ---------------------------------------------------------------------------
# 4. Late-delivery flag
# ---------------------------------------------------------------------------

def test_late_delivery_flag_false_when_tight():
    """When demand is met on-time but a 1-day slip causes shortage."""
    # On time: 100g arriving day 0, 100g demand on day 0 → covered
    # +1 day slip: arrival on day 1, but demand is day 0 → short
    result = _fefo(
        n_days=3,
        demand_by_day={1: {0: 100.0}},
        on_hand={1: 0.0},
        lots_by_ing={},
        open_po_arrivals_by_day={1: {0: 100.0}},
        plan_arrivals_by_day={},
        safety_stock={1: 0.0},
    )
    assert result["coverage_ok"]
    assert not result["late_delivery_coverage_ok"], "1-day slip should cause shortage"


def test_late_delivery_flag_true_when_buffer():
    """When stock on-hand covers even a 1-day slip."""
    result = _fefo(
        n_days=3,
        demand_by_day={1: {1: 50.0}},
        on_hand={1: 100.0},
        lots_by_ing={1: [[100.0, _INF]]},
        open_po_arrivals_by_day={},
        plan_arrivals_by_day={},
        safety_stock={1: 0.0},
    )
    assert result["coverage_ok"]
    assert result["late_delivery_coverage_ok"]


# ---------------------------------------------------------------------------
# 5. Multi-ingredient partial coverage
# ---------------------------------------------------------------------------

def test_multiple_ingredients_one_short():
    """Ingredient 1 is covered, ingredient 2 is short — total_short captures only 2."""
    result = _fefo(
        n_days=4,
        demand_by_day={
            1: {0: 100.0},   # covered
            2: {0: 500.0},   # NOT covered
        },
        on_hand={1: 200.0, 2: 100.0},
        lots_by_ing={1: [[200.0, _INF]], 2: [[100.0, _INF]]},
        open_po_arrivals_by_day={},
        plan_arrivals_by_day={},
        safety_stock={1: 0.0, 2: 0.0},
    )
    assert not result["coverage_ok"]
    assert abs(result["total_short"] - 400.0) < 1.0
    assert 1 not in result["short_by_ing"]
    assert 2 in result["short_by_ing"]
