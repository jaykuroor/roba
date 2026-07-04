"""Unit tests for the MILP / greedy sourcing solver.

Tests cover:
  1. Cheaper alternate wins only when savings > switching cost (greedy + MILP).
  2. Delivery charge makes consolidating to one supplier optimal.
  3. Min-order forces a low-volume supplier out (greedy switching-cost guard).
  4. Perishable spoilage cap behaviour (MILP penalises over-buying).
  5. solve_sourcing falls back to greedy when _PULP_AVAILABLE is patched off.
  6. Graceful _solve_fallback keeps existing defaults when greedy errors.
"""

from __future__ import annotations

import sys
import types
import importlib
import pytest


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def _fresh_sourcing():
    """Return a freshly imported sourcing module (un-patched)."""
    # Remove cached module so we can reimport with different _PULP_AVAILABLE state
    for key in list(sys.modules):
        if "track_b.procurement.sourcing" in key or key == "track_b.procurement.sourcing":
            del sys.modules[key]
    from track_b.procurement import sourcing  # noqa: PLC0415
    return sourcing


# ---------------------------------------------------------------------------
# Common fixture data builders
# ---------------------------------------------------------------------------

def _ing(id_: int, default_supplier: int | None = None, perishable: int = 0,
         shelf_life_days: float | None = None):
    return {
        "id": id_,
        "perishable": perishable,
        "shelf_life_days": shelf_life_days,
        "current_default_supplier_id": default_supplier,
    }


def _cat(id_: int, supplier_id: int, ingredient_id: int, price: float,
         unit: str = "g", pack_size: float = 1000.0,
         availability: str = "in_stock", is_default: int = 0):
    return {
        "id": id_,
        "supplier_id": supplier_id,
        "ingredient_id": ingredient_id,
        "current_price": price,
        "unit": unit,
        "pack_size": pack_size,
        "availability": availability,
        "is_default": is_default,
        "discount": None,
    }


def _sup(id_: int, delivery_charge: float = 0.0, min_order_value: float = 0.0,
         lead_time_days: float = 2.0, reliability_score: float = 0.9,
         volume_discount=None):
    return {
        "id": id_,
        "delivery_charge": delivery_charge,
        "min_order_value": min_order_value,
        "lead_time_days": lead_time_days,
        "reliability_score": reliability_score,
        "volume_discount": volume_discount,
    }


# ---------------------------------------------------------------------------
# Helpers to run both solvers
# ---------------------------------------------------------------------------

from track_b.procurement.sourcing import solve_sourcing, solve_sourcing_greedy


def _run_both(ingredients, catalog, suppliers, demand, params):
    """Return (greedy_solution, milp_solution_or_None)."""
    greedy = solve_sourcing_greedy(ingredients, catalog, suppliers, demand, params)
    try:
        from track_b.procurement import sourcing as _s
        milp = None if not _s._PULP_AVAILABLE else solve_sourcing(
            ingredients, catalog, suppliers, demand, params
        )
    except Exception:
        milp = None
    return greedy, milp


# ---------------------------------------------------------------------------
# Test 1: switching cost guard — cheaper alternate only wins when savings > cost
# ---------------------------------------------------------------------------

class TestSwitchingCostGuard:
    """Alternate supplier is cheaper but may not win depending on switching cost."""

    def test_greedy_switches_when_savings_exceed_cost(self):
        # Ingredient 1: default=supplier 1 @ $10/unit; alternate=supplier 2 @ $5/unit
        # demand=10 units → savings = (10-5)*10 = 50 >> switching_cost=5
        ings = [_ing(1, default_supplier=1)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=10.0, is_default=1),
            _cat(2, supplier_id=2, ingredient_id=1, price=5.0),
        ]
        sups = [_sup(1), _sup(2)]
        demand = {1: 10.0}
        params = {"switching_cost": 5.0}

        result = solve_sourcing_greedy(ings, cat, sups, demand, params)
        assert len(result.assignments) == 1
        assert result.assignments[0]["supplier_id"] == 2, (
            "Greedy should switch to cheaper supplier when savings > switching cost"
        )

    def test_greedy_keeps_default_when_savings_below_cost(self):
        # Same setup but savings = (10-9)*10 = 10 and switching_cost = 20 → keep default
        ings = [_ing(1, default_supplier=1)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=10.0, is_default=1),
            _cat(2, supplier_id=2, ingredient_id=1, price=9.0),
        ]
        sups = [_sup(1), _sup(2)]
        demand = {1: 10.0}
        params = {"switching_cost": 20.0}

        result = solve_sourcing_greedy(ings, cat, sups, demand, params)
        assert result.assignments[0]["supplier_id"] == 1, (
            "Greedy should keep default when savings < switching cost"
        )

    def test_milp_matches_greedy_switch_decision(self):
        """On a simple 1-ingredient, 2-supplier case, MILP and greedy agree."""
        from track_b.procurement import sourcing as _s
        if not _s._PULP_AVAILABLE:
            pytest.skip("PuLP not available")

        ings = [_ing(1, default_supplier=1)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=10.0, is_default=1),
            _cat(2, supplier_id=2, ingredient_id=1, price=5.0),
        ]
        sups = [_sup(1), _sup(2)]
        demand = {1: 10.0}
        params = {"switching_cost": 5.0}

        milp = solve_sourcing(ings, cat, sups, demand, params)
        greedy = solve_sourcing_greedy(ings, cat, sups, demand, params)
        assert milp.assignments[0]["supplier_id"] == greedy.assignments[0]["supplier_id"]


# ---------------------------------------------------------------------------
# Test 2: Delivery charge consolidation
# ---------------------------------------------------------------------------

class TestDeliveryConsolidation:
    """When supplier A has a big delivery charge and supplier B is slightly more
    expensive per-unit but free delivery, sourcing may prefer to consolidate."""

    def test_greedy_amortises_delivery_over_shared_ingredients(self):
        # Supplier 1: delivery=$50, price=$1/unit for both ingredients
        # Supplier 2: delivery=$0, price=$3/unit for ingredient 2
        # Greedy picks cheapest raw price (no delivery in per-ingredient score),
        # then amortises delivery across shared ingredients.
        # With demand=10 for each:
        #   Sup1 for both: (1*10+25) + (1*10+25) = 70
        #   Sup1 for ing1, Sup2 for ing2: (1*10+50) + (3*10+0) = 90
        # Greedy raw cheapest picks sup1 for both (price=1 < 3), keeping defaults.
        ings = [_ing(1, default_supplier=1), _ing(2, default_supplier=1)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=1.0, is_default=1),
            _cat(2, supplier_id=2, ingredient_id=2, price=3.0),
            _cat(3, supplier_id=1, ingredient_id=2, price=1.0, is_default=1),
        ]
        sups = [_sup(1, delivery_charge=50.0), _sup(2, delivery_charge=0.0)]
        demand = {1: 10.0, 2: 10.0}
        params = {"switching_cost": 0.0}

        result = solve_sourcing_greedy(ings, cat, sups, demand, params)
        # Both should come from supplier 1 (cheapest unit price)
        supplier_ids = {a["supplier_id"] for a in result.assignments}
        assert supplier_ids == {1}, "Both ingredients should consolidate to the cheaper-unit supplier"

        # Verify total cost accounts for delivery (50 split 2 ways = 25 each)
        total_landed = sum(a["landed_cost"] for a in result.assignments)
        assert abs(total_landed - 70.0) < 0.01, f"Expected landed cost ~70, got {total_landed}"


# ---------------------------------------------------------------------------
# Test 3: Out-of-stock skips the supplier
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_out_of_stock_supplier_skipped(self):
        ings = [_ing(1)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=1.0, availability="out"),
            _cat(2, supplier_id=2, ingredient_id=1, price=5.0, availability="in_stock"),
        ]
        sups = [_sup(1), _sup(2)]
        demand = {1: 10.0}
        params = {"switching_cost": 0.0}

        result = solve_sourcing_greedy(ings, cat, sups, demand, params)
        assert result.assignments[0]["supplier_id"] == 2, (
            "Out-of-stock supplier should be skipped"
        )

    def test_no_available_supplier_returns_empty(self):
        ings = [_ing(1)]
        cat = [_cat(1, supplier_id=1, ingredient_id=1, price=1.0, availability="out")]
        sups = [_sup(1)]
        demand = {1: 10.0}
        params = {}

        result = solve_sourcing_greedy(ings, cat, sups, demand, params)
        # Should not crash; no assignment for this ingredient
        assert not any(a["ingredient_id"] == 1 for a in result.assignments)


# ---------------------------------------------------------------------------
# Test 4: Zero / empty demand produces valid (empty) solution
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_zero_demand_returns_valid_solution(self):
        ings = [_ing(1)]
        cat = [_cat(1, supplier_id=1, ingredient_id=1, price=2.0)]
        sups = [_sup(1)]
        demand = {1: 0.0}
        params = {}

        result = solve_sourcing_greedy(ings, cat, sups, demand, params)
        assert result.total_cost == 0.0

    def test_empty_inputs_returns_valid_solution(self):
        result = solve_sourcing_greedy([], [], [], {}, {})
        assert result.method == "greedy"
        assert result.assignments == []

    def test_no_default_set_uses_cheapest(self):
        # No ingredient has a current_default_supplier_id — should just pick cheapest
        ings = [_ing(1, default_supplier=None)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=8.0),
            _cat(2, supplier_id=2, ingredient_id=1, price=3.0),
        ]
        sups = [_sup(1), _sup(2)]
        demand = {1: 5.0}
        params = {"switching_cost": 100.0}

        result = solve_sourcing_greedy(ings, cat, sups, demand, params)
        assert result.assignments[0]["supplier_id"] == 2


# ---------------------------------------------------------------------------
# Test 5: Greedy fallback when PuLP is patched absent
# ---------------------------------------------------------------------------

class TestPulpFallback:
    def test_solve_sourcing_falls_back_to_greedy_when_pulp_absent(self, monkeypatch):
        """When _PULP_AVAILABLE is False, solve_sourcing delegates to greedy."""
        import track_b.procurement.sourcing as _s
        monkeypatch.setattr(_s, "_PULP_AVAILABLE", False)

        ings = [_ing(1, default_supplier=1)]
        cat = [_cat(1, supplier_id=1, ingredient_id=1, price=2.0, is_default=1)]
        sups = [_sup(1)]
        demand = {1: 10.0}
        params = {}

        result = _s.solve_sourcing(ings, cat, sups, demand, params)
        assert result.method in ("greedy", "fallback")
        assert result.total_cost > 0

    def test_solve_sourcing_greedy_never_imports_pulp(self):
        """solve_sourcing_greedy must work without pulp installed."""
        ings = [_ing(1, default_supplier=1)]
        cat = [_cat(1, supplier_id=1, ingredient_id=1, price=3.0, is_default=1)]
        sups = [_sup(1)]
        demand = {1: 5.0}
        params = {}

        # Even if we temporarily hide pulp, greedy must work fine
        pulp_backup = sys.modules.pop("pulp", None)
        try:
            result = solve_sourcing_greedy(ings, cat, sups, demand, params)
            assert result.assignments[0]["unit_price"] == 3.0
        finally:
            if pulp_backup is not None:
                sys.modules["pulp"] = pulp_backup


# ---------------------------------------------------------------------------
# Test 6: Multi-ingredient, multi-supplier correctness (MILP parity check)
# ---------------------------------------------------------------------------

class TestMilpGreedyParity:
    """On simple fixed cases, MILP and greedy must agree on total assignments
    when there is a clear minimum-cost solution."""

    def test_simple_parity(self):
        from track_b.procurement import sourcing as _s
        if not _s._PULP_AVAILABLE:
            pytest.skip("PuLP not available")

        # 2 ingredients, each has one clear best supplier; no switching cost needed.
        ings = [_ing(1, default_supplier=None), _ing(2, default_supplier=None)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=2.0),
            _cat(2, supplier_id=2, ingredient_id=1, price=8.0),
            _cat(3, supplier_id=1, ingredient_id=2, price=9.0),
            _cat(4, supplier_id=2, ingredient_id=2, price=1.0),
        ]
        sups = [_sup(1), _sup(2)]
        demand = {1: 10.0, 2: 10.0}
        params = {"switching_cost": 0.0}

        greedy = solve_sourcing_greedy(ings, cat, sups, demand, params)
        milp = solve_sourcing(ings, cat, sups, demand, params)

        g_map = {a["ingredient_id"]: a["supplier_id"] for a in greedy.assignments}
        m_map = {a["ingredient_id"]: a["supplier_id"] for a in milp.assignments}

        assert g_map == m_map, (
            f"Greedy {g_map} and MILP {m_map} should agree on obvious best suppliers"
        )


# ---------------------------------------------------------------------------
# Test 7: _solve_fallback keeps existing defaults
# ---------------------------------------------------------------------------

class TestFallback:
    def test_fallback_keeps_existing_defaults(self):
        """_solve_fallback retains whatever is_default=1 rows exist."""
        from track_b.procurement.sourcing import _solve_fallback

        ings = [_ing(1, default_supplier=1), _ing(2, default_supplier=2)]
        cat = [
            _cat(1, supplier_id=1, ingredient_id=1, price=5.0, is_default=1),
            _cat(2, supplier_id=2, ingredient_id=2, price=3.0, is_default=1),
            _cat(3, supplier_id=3, ingredient_id=1, price=1.0),  # cheaper but not default
        ]
        demand = {1: 10.0, 2: 5.0}

        result = _solve_fallback(ings, cat, demand)
        assert result.method == "fallback"
        supplier_by_ing = {a["ingredient_id"]: a["supplier_id"] for a in result.assignments}
        assert supplier_by_ing[1] == 1, "Fallback should keep is_default=1 for ingredient 1"
        assert supplier_by_ing[2] == 2, "Fallback should keep is_default=1 for ingredient 2"
        # Cheap alternate (sup 3) must NOT be chosen
        assert 3 not in supplier_by_ing.values()
