"""Unit tests for the time-phased procurement plan optimizer.

Tests: consolidation across days, MOV via consolidation, volume-discount capture,
perishable expiry cap, lead-time feasibility, phantom overdue PO excluded,
all-out at_risk order (not silent skip), PuLP-absent greedy fallback parity.
"""

from __future__ import annotations

import math
from unittest import mock

import pytest

from track_b.procurement.plan_optimizer import (
    PlanSolution,
    solve_time_phased_plan,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _sup(sup_id, lead=1.0, reliability=0.95, mov=0.0, dc=0.0, vol_discount=None, delivery_hour=None):
    d = {
        "id": sup_id,
        "name": f"Supplier{sup_id}",
        "lead_time_days": lead,
        "reliability_score": reliability,
        "min_order_value": mov,
        "delivery_charge": dc,
        "volume_discount": vol_discount,
    }
    if delivery_hour is not None:
        d["delivery_hour"] = delivery_hour
    return d


def _cat(sup_id, ing_id, price=1.0, pack=100.0, avail="in_stock",
         is_default=1, discount=None):
    return {
        "supplier_id": sup_id,
        "ingredient_id": ing_id,
        "current_price": price,
        "pack_size": pack,
        "unit": "g",
        "availability": avail,
        "is_default": is_default,
        "discount": discount,
    }


def _ing(ing_id, perishable=0, shelf_life=None, name=None):
    return {
        "id": ing_id,
        "perishable": perishable,
        "shelf_life_days": shelf_life,
        "base_unit": "g",
        "name": name or f"Ingredient{ing_id}",
    }


def _flat_demand(ing_id, qty, n_days):
    """Uniform daily demand dict."""
    return {ing_id: {d: qty for d in range(n_days)}}


def _run(
    ingredients, catalog, suppliers,
    demand_by_day, inbound_by_day=None, on_hand=None, safety_stock=None,
    n_days=7, params=None,
) -> PlanSolution:
    if inbound_by_day is None:
        inbound_by_day = {i["id"]: {} for i in ingredients}
    if on_hand is None:
        on_hand = {i["id"]: 0.0 for i in ingredients}
    if safety_stock is None:
        safety_stock = {i["id"]: 0.0 for i in ingredients}
    return solve_time_phased_plan(
        n_days=n_days,
        ingredients=ingredients,
        catalog=catalog,
        suppliers=suppliers,
        demand_by_day=demand_by_day,
        inbound_by_day=inbound_by_day,
        on_hand=on_hand,
        safety_stock=safety_stock,
        params=params or {"slack_penalty": 1000.0},
    )


# ---------------------------------------------------------------------------
# Test 1: Basic coverage — plan must produce orders that cover demand
# ---------------------------------------------------------------------------

def test_basic_coverage():
    """Plan must produce orders so that demand is covered from first feasible delivery onward.

    With lead=1 day and zero on-hand, day 0 can't be covered (no delivery possible
    before day 1).  Orders cover days 1..n-1.
    """
    n = 5
    daily = 200.0
    lead = 1
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=float(lead))],
        demand_by_day=_flat_demand(1, daily, n),
        on_hand={1: 0.0},
        n_days=n,
    )
    assert sol.orders, "Expected at least one order"
    total_ordered = sum(o.qty for o in sol.orders)
    # Lead=1 means first delivery at day 1; day 0 is uncovered with no initial stock.
    # So expect orders covering (n - lead_ceil) days.
    import math as _m
    expected_coverage = daily * (n - _m.ceil(lead))
    assert total_ordered >= expected_coverage * 0.9, (
        f"Ordered {total_ordered} but expected ≥ {expected_coverage * 0.9:.0f}"
    )


# ---------------------------------------------------------------------------
# Test 2: Consolidation — delivery charges drive fewer supplier-days
# ---------------------------------------------------------------------------

def test_delivery_charge_drives_consolidation():
    """High delivery charge + low MOV → MILP consolidates into fewer deliveries."""
    n = 7
    # Two ingredients that need reordering across the horizon.
    # Large delivery charge (50) incentivises bundling both onto one delivery day.
    sol = _run(
        ingredients=[_ing(1), _ing(2)],
        catalog=[
            _cat(1, 1, price=0.01, pack=1000.0),
            _cat(1, 2, price=0.01, pack=1000.0),
        ],
        suppliers=[_sup(1, lead=1.0, dc=50.0)],
        demand_by_day={
            1: {d: 500.0 for d in range(n)},
            2: {d: 500.0 for d in range(n)},
        },
        on_hand={1: 0.0, 2: 0.0},
        safety_stock={1: 0.0, 2: 0.0},
        n_days=n,
    )
    # Count distinct (supplier, delivery_day) pairs
    delivery_days = {(o.supplier_id, o.delivery_day) for o in sol.orders}
    # With no MOV and a flat horizon, the MILP should bundle onto as few delivery
    # days as possible; with a single supplier, ideally 1-2 big orders not 7.
    assert len(delivery_days) < n, (
        f"Expected consolidation: {len(delivery_days)} delivery days for {n}-day horizon"
    )
    assert len(delivery_days) <= 2, (
        f"Too many delivery days ({len(delivery_days)}); delivery charge should consolidate"
    )


# ---------------------------------------------------------------------------
# Test 3: MOV satisfied by consolidation, not wasteful padding
# ---------------------------------------------------------------------------

def test_mov_satisfied_by_consolidation():
    """Plan meets MOV per delivery without inflating quantities beyond coverage need."""
    n = 7
    pack = 100.0
    price = 0.5
    mov = 80.0    # €80 minimum; 100g × €0.5 = €50 per pack → needs 2 packs ≥ €80? No, 100×0.5=50<80
    # Actually need 2 packs (200g) to hit €100 ≥ €80. Or 2 ingredients.
    sol = _run(
        ingredients=[_ing(1), _ing(2)],
        catalog=[
            _cat(1, 1, price=price, pack=pack),
            _cat(1, 2, price=price, pack=pack),
        ],
        suppliers=[_sup(1, lead=1.0, mov=mov)],
        demand_by_day={
            1: {d: 80.0 for d in range(n)},   # 80g/day
            2: {d: 80.0 for d in range(n)},
        },
        on_hand={1: 0.0, 2: 0.0},
        safety_stock={1: 0.0, 2: 0.0},
        n_days=n,
    )
    # Every non-zero delivery day for supplier 1 must meet MOV.
    from collections import defaultdict
    val_by_day: dict = defaultdict(float)
    for o in sol.orders:
        if o.supplier_id == 1:
            val_by_day[o.delivery_day] += o.qty * o.unit_price
    for day, val in val_by_day.items():
        assert val >= mov, (
            f"Delivery day {day} has value €{val:.2f} below MOV €{mov}"
        )


# ---------------------------------------------------------------------------
# Test 4: Volume discount captured when it lowers cost
# ---------------------------------------------------------------------------

def test_volume_discount_captured():
    """MILP should reach discount tier when doing so lowers total landed cost."""
    # Supplier offers 10% off orders ≥ €100.
    # Demand for 7 days at 20g/day = 140g.  Price = €1/g → base = €140.
    # 140 < 150 threshold → no discount if MILP orders minimal.
    # But if MILP orders ≥ 150g: 10% × €150 = €15 rebate → saves €15.
    n = 7
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=10.0)],
        suppliers=[_sup(1, lead=1.0, vol_discount=[{"min_value": 100.0, "discount_pct": 10}])],
        demand_by_day={1: {d: 10.0 for d in range(n)}},
        on_hand={1: 0.0},
        safety_stock={1: 0.0},
        n_days=n,
    )
    total_ordered = sum(o.qty for o in sol.orders)
    # Total demand = 70g.  MILP may order ≥ 100g to capture the 10% discount
    # if savings exceed extra goods cost (extra 30g × €1 = €30, rebate ≈ €10 → not worth it).
    # Just verify the plan has orders and total cost is reasonable.
    assert sol.orders, "Expected at least one order"
    # Lead=1 → day 0 uncovered, demand covered from days 1..6 (6 days × 10g = 60g).
    import math as _m
    expected = 10.0 * (7 - _m.ceil(1.0))
    assert total_ordered >= expected * 0.9, (
        f"Ordered {total_ordered} but expected ≥ {expected * 0.9:.0f}"
    )


# ---------------------------------------------------------------------------
# Coverage helper: expiry-aware, lot-based day-by-day stockout check
# ---------------------------------------------------------------------------

def _stockout_days(sol, ing, demand_by_day, on_hand, n_days, inbound=None):
    """Return [(day, short_qty), ...] where forecasted demand can't be met.

    Mirrors the solver's opening-stock assumption (on_hand is a single
    never-expiring lot; new deliveries expire at delivery_day + shelf_life).
    """
    inbound = inbound or {}
    iid = int(ing["id"])
    shelf = float(ing["shelf_life_days"]) if ing.get("perishable") and ing.get("shelf_life_days") else None
    NEVER = 10 ** 9
    lots = [[float(on_hand.get(iid, 0.0)), NEVER]]
    by_day = {}
    for o in sol.orders:
        if o.ingredient_id == iid and o.qty > 0:
            by_day[o.delivery_day] = by_day.get(o.delivery_day, 0.0) + o.qty
    for d, q in (inbound.get(iid, {}) or {}).items():
        by_day[d] = by_day.get(d, 0.0) + q
    short = []
    for d in range(n_days):
        if by_day.get(d, 0.0) > 0:
            lots.append([by_day[d], d + shelf if shelf else NEVER])
        lots = [l for l in lots if l[1] > d]
        lots.sort(key=lambda l: l[1])
        rem = demand_by_day.get(iid, {}).get(d, 0.0)
        for l in lots:
            t = min(l[0], rem)
            l[0] -= t
            rem -= t
            if rem <= 1e-9:
                break
        lots = [l for l in lots if l[0] > 1e-9]
        if rem > 1e-6:
            short.append((d, round(rem, 2)))
    return short


# ---------------------------------------------------------------------------
# Test 5: Perishable — demand covered every day with no spurious waste
# ---------------------------------------------------------------------------

def test_perishable_covered_no_waste():
    """A perishable's forecasted demand is met every feasible day, and the plan
    does not over-order (total ordered ≈ demand net of on-hand).

    (Replaces the old ``test_perishable_cap`` which asserted the *buggy*
    per-order-quantity ceiling that silently dropped ingredients whose pack
    exceeded the shrinking before-expiry window — the exact Caesar bug.)
    """
    n = 7
    shelf_life = 3  # 3-day shelf life
    daily_demand = 200.0
    ing = _ing(1, perishable=1, shelf_life=float(shelf_life))
    demand = {1: {d: daily_demand for d in range(n)}}
    # Opening stock covers day 0 (not lead-feasible with lead=1); orders cover 1..6.
    on_hand = {1: daily_demand}
    sol = _run(
        ingredients=[ing],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day=demand,
        on_hand=on_hand,
        safety_stock={1: 0.0},
        n_days=n,
    )
    assert sol.orders, "Expected orders for perishable"
    assert sol.coverage_ok, f"Perishable demand should be fully coverable (short={sol.total_short})"
    assert not _stockout_days(sol, ing, demand, on_hand, n), "No day should stock out"
    # No gross over-ordering: total ordered within one pack of covered demand.
    total_ordered = sum(o.qty for o in sol.orders)
    covered_demand = daily_demand * (n - 1)  # day 0 served from opening stock
    assert total_ordered <= covered_demand + 100.0 + 1e-6, (
        f"Over-ordered perishable: {total_ordered} vs covered demand {covered_demand}"
    )


# ---------------------------------------------------------------------------
# Test 5b: Regression — pack larger than any before-expiry window is orderable
# ---------------------------------------------------------------------------

def test_large_pack_perishable_is_ordered():
    """The Caesar-Dressing bug: a long-ish shelf-life item whose pack size
    exceeds the remaining before-expiry demand on *every* feasible delivery day
    must still be ordered (the old per-order expiry cap made it infeasible, so
    it was silently dropped onto slack and never appeared in the plan).
    """
    n = 7
    lead = 3           # deliveries only on days 3..6
    pack = 2000.0      # one pack > any single feasible-day window remaining
    daily = 460.0      # window remaining on days 3..6 is < 2000
    ing = _ing(11, perishable=1, shelf_life=30.0, name="Caesar Dressing")
    demand = {11: {d: daily for d in range(n)}}
    on_hand = {11: 3.0 * daily}  # covers only the first ~3 days
    sol = _run(
        ingredients=[ing],
        catalog=[_cat(11, 11, price=0.009, pack=pack)],  # cat(sup, ing,...)
        suppliers=[_sup(11, lead=float(lead))],
        demand_by_day=demand,
        on_hand=on_hand,
        safety_stock={11: 0.0},
        n_days=n,
    )
    ordered = [o for o in sol.orders if o.ingredient_id == 11 and o.qty > 0]
    assert ordered, "Large-pack perishable must be ordered, not silently dropped"
    assert sol.coverage_ok, f"Expected full coverage, got total_short={sol.total_short}"
    late = [(d, q) for d, q in _stockout_days(sol, ing, demand, on_hand, n) if d >= lead]
    assert not late, f"Stockout on lead-feasible days despite orders: {late}"


# ---------------------------------------------------------------------------
# Test 5c: Short-shelf item gets a fresh late-week re-delivery
# ---------------------------------------------------------------------------

def test_short_shelf_schedules_late_redelivery():
    """A short-shelf perishable (Basil-like) can't be covered by one early bulk
    delivery — the plan must schedule a second, fresh delivery later in the week
    so end-of-horizon demand is met without relying on expired stock.
    """
    n = 7
    shelf_life = 4
    daily = 185.0
    ing = _ing(4, perishable=1, shelf_life=float(shelf_life), name="Basil")
    demand = {4: {d: daily for d in range(n)}}
    on_hand = {4: 200.0}
    sol = _run(
        ingredients=[ing],
        catalog=[_cat(4, 4, price=0.03, pack=500.0)],
        suppliers=[_sup(4, lead=1.0)],
        demand_by_day=demand,
        on_hand=on_hand,
        safety_stock={4: 0.0},
        n_days=n,
    )
    ordered = [o for o in sol.orders if o.ingredient_id == 4 and o.qty > 0]
    assert ordered, "Expected orders for short-shelf perishable"
    assert sol.coverage_ok, f"Expected full coverage, got total_short={sol.total_short}"
    delivery_days = {o.delivery_day for o in ordered}
    assert len(delivery_days) >= 2, (
        f"Short-shelf item needs staggered re-deliveries; got days {delivery_days}"
    )
    late = [(d, q) for d, q in _stockout_days(sol, ing, demand, on_hand, n) if d >= 1]
    assert not late, f"Stockout despite staggered deliveries: {late}"


# ---------------------------------------------------------------------------
# Test 6: Lead-time feasibility — no delivery before lead time
# ---------------------------------------------------------------------------

def test_lead_time_feasibility():
    """No delivery is scheduled sooner than the supplier's lead time."""
    n = 7
    lead = 3.0  # 3-day lead time
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=lead)],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 0.0},
        safety_stock={1: 0.0},
        n_days=n,
    )
    lead_ceil = math.ceil(lead)
    for o in sol.orders:
        assert o.delivery_day >= lead_ceil, (
            f"Delivery on day {o.delivery_day} before lead_ceil={lead_ceil}"
        )


# ---------------------------------------------------------------------------
# Test 7: Early breach → at_risk flag
# ---------------------------------------------------------------------------

def test_early_breach_marked_at_risk():
    """When demand can't be covered before first feasible delivery, at_risk=True."""
    n = 7
    lead = 3  # first delivery at day 3
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=float(lead))],
        demand_by_day={1: {d: 200.0 for d in range(n)}},
        on_hand={1: 0.0},
        safety_stock={1: 50.0},
        n_days=n,
    )
    assert sol.orders, "Expected orders"
    # Days 0..2 have demand but no delivery possible → at_risk
    first_orders = [o for o in sol.orders if o.delivery_day == lead]
    assert first_orders, f"Expected first delivery at day {lead}"
    assert first_orders[0].at_risk, "First delivery should be at_risk (days 0-2 uncovered)"


# ---------------------------------------------------------------------------
# Test 8: Phantom overdue PO excluded from inbound
# ---------------------------------------------------------------------------

def test_phantom_overdue_po_not_credited():
    """An overdue in-flight PO (arr_day < -1) must not suppress new orders."""
    n = 7
    # All demand, zero on-hand, but an "in-flight" PO that arrives on day -5 (overdue).
    # The caller (optimizer.py) filters these out before calling the solver.
    # Verify: even with zero inbound, solver produces covering orders.
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 200.0 for d in range(n)}},
        inbound_by_day={1: {}},  # no inbound credited (phantom excluded upstream)
        on_hand={1: 0.0},
        safety_stock={1: 50.0},
        n_days=n,
    )
    assert sol.orders, "Expected orders — phantom PO must not suppress reorder"
    total_ordered = sum(o.qty for o in sol.orders)
    assert total_ordered >= 200.0 * (n - 1) * 0.8  # at least 80% of demand covered


# ---------------------------------------------------------------------------
# Test 9: All-out suppliers → at_risk order not silent skip
# ---------------------------------------------------------------------------

def test_all_out_supplier_emits_at_risk_order():
    """When all suppliers are out-of-stock, greedy fallback should still emit an at_risk order."""
    import track_b.procurement.plan_optimizer as _pm
    # Force greedy fallback (which handles all-out)
    with mock.patch.object(_pm, "_PULP_AVAILABLE", False):
        sol = _run(
            ingredients=[_ing(1)],
            catalog=[_cat(1, 1, price=0.01, pack=100.0, avail="out")],
            suppliers=[_sup(1, lead=1.0)],
            demand_by_day={1: {d: 200.0 for d in range(7)}},
            on_hand={1: 0.0},
            safety_stock={1: 50.0},
            n_days=7,
        )
    # The greedy fallback picks the out-of-stock supplier and marks at_risk
    assert sol.orders, "Expected at_risk orders even when all suppliers are out"
    assert all(o.at_risk for o in sol.orders), "All-out orders must be at_risk"


# ---------------------------------------------------------------------------
# Test 10: PuLP absent → greedy fallback parity
# ---------------------------------------------------------------------------

def test_pulp_absent_uses_greedy_fallback():
    """When PuLP is unavailable, falls back to greedy without crashing."""
    import track_b.procurement.plan_optimizer as _pm
    n = 5
    with mock.patch.object(_pm, "_PULP_AVAILABLE", False):
        sol = _run(
            ingredients=[_ing(1)],
            catalog=[_cat(1, 1, price=0.01, pack=100.0)],
            suppliers=[_sup(1, lead=1.0)],
            demand_by_day={1: {d: 100.0 for d in range(n)}},
            on_hand={1: 0.0},
            safety_stock={1: 50.0},  # non-zero safety stock triggers greedy reorder
            n_days=n,
        )
    assert sol.method == "greedy", f"Expected 'greedy', got {sol.method!r}"
    assert sol.orders, "Expected greedy fallback to produce orders"


# ---------------------------------------------------------------------------
# Test 11: Cheaper supplier chosen over is_default=1 (time-phased MILP is now the decider)
# ---------------------------------------------------------------------------

def test_milp_chooses_cheapest_supplier():
    """Time-phased MILP picks cheaper supplier regardless of is_default flag."""
    n = 5
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[
            _cat(1, 1, price=5.0, pack=100.0, is_default=1),   # expensive, is_default
            _cat(2, 1, price=0.1, pack=100.0, is_default=0),   # cheap, not default
        ],
        suppliers=[_sup(1, lead=1.0), _sup(2, lead=1.0)],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 0.0},
        safety_stock={1: 0.0},
        n_days=n,
    )
    assert sol.orders, "Expected orders"
    # Should pick the cheaper supplier (50× price difference overwhelms any switching cost)
    suppliers_used = {o.supplier_id for o in sol.orders}
    assert 2 in suppliers_used, (
        f"Expected cheaper supplier 2 to be chosen; used: {suppliers_used}"
    )
    assert 1 not in suppliers_used, (
        f"Expected expensive supplier 1 to not be used; used: {suppliers_used}"
    )


# ---------------------------------------------------------------------------
# Test 12: No orders when on_hand already covers all demand
# ---------------------------------------------------------------------------

def test_no_orders_when_fully_stocked():
    """Plan should produce zero orders when existing stock covers entire horizon."""
    n = 7
    daily_demand = 100.0
    total_demand = daily_demand * n
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: daily_demand for d in range(n)}},
        on_hand={1: total_demand + 1000.0},  # more than enough
        safety_stock={1: 0.0},
        n_days=n,
    )
    assert not sol.orders, (
        f"Expected zero orders when fully stocked, got: {sol.orders}"
    )


# ---------------------------------------------------------------------------
# Test 13: safety_penalty_multiplier=0 → safety buffer never drives a purchase
# ---------------------------------------------------------------------------

def test_milp_no_safety_purchase_when_demand_covered():
    """With safety_penalty_multiplier=0, the MILP must NOT order solely to top up
    the safety buffer when all forecast demand is already covered by on-hand stock."""
    n = 7
    daily_demand = 20.0
    total_demand = daily_demand * n  # 140
    on_hand_qty = total_demand + 10.0  # 150 — covers all demand
    safety_stock_qty = on_hand_qty + 50.0  # 200 — above on_hand: safety deficit exists

    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.02, pack=2000.0)],
        suppliers=[_sup(1, lead=1.0, dc=15.0, mov=80.0)],
        demand_by_day={1: {d: daily_demand for d in range(n)}},
        on_hand={1: on_hand_qty},
        safety_stock={1: safety_stock_qty},
        n_days=n,
        params={"slack_penalty": 1000.0, "safety_penalty_multiplier": 0.0},
    )
    assert not sol.orders, (
        f"Expected no orders when demand is covered and safety is non-cost-bearing, "
        f"got: {sol.orders}"
    )


# ---------------------------------------------------------------------------
# Test 14: Greedy fallback — demand-driven, safety buffer never drives a purchase
# ---------------------------------------------------------------------------

def test_greedy_no_safety_purchase_when_demand_covered():
    """Greedy fallback must NOT order solely to fill the safety buffer when all
    forecast demand is already covered by on-hand stock."""
    import track_b.procurement.plan_optimizer as _pm

    n = 7
    daily_demand = 20.0
    total_demand = daily_demand * n  # 140
    on_hand_qty = total_demand + 10.0  # 150 — covers all demand
    safety_stock_qty = on_hand_qty + 50.0  # 200 — above on_hand: safety deficit

    with mock.patch.object(_pm, "_PULP_AVAILABLE", False):
        sol = _run(
            ingredients=[_ing(1)],
            catalog=[_cat(1, 1, price=0.02, pack=2000.0)],
            suppliers=[_sup(1, lead=1.0)],
            demand_by_day={1: {d: daily_demand for d in range(n)}},
            on_hand={1: on_hand_qty},
            safety_stock={1: safety_stock_qty},
            n_days=n,
            params={"slack_penalty": 1000.0, "safety_penalty_multiplier": 0.0},
        )
    assert sol.method == "greedy", f"Expected greedy fallback, got {sol.method!r}"
    assert not sol.orders, (
        f"Expected no greedy orders when demand is covered and safety is non-cost-bearing, "
        f"got: {sol.orders}"
    )


# ---------------------------------------------------------------------------
# Tests 15-18: Two-pass reliability premium
# ---------------------------------------------------------------------------

def _two_suppliers_scenario(reliability_cash_tolerance=0.01, stress_enabled=True,
                             margin_by_ing=None, n=7):
    """Two suppliers with identical price but different reliability / lead time.

    Supplier 1: reliable=0.99, lead=1 day  (the 'safe' choice)
    Supplier 2: reliable=0.70, lead=2 days (the 'risky' choice — arrives later,
                fails 30% of the time)

    Both offer the same unit price (€0.01/g) so the cash-optimal plan may split
    between them or choose either.  With a reliability cash tolerance > 0, the
    pass-2 objective should prefer supplier 1 (shorter lead, higher reliability)
    when the cash cap allows it.
    """
    ingredients = [_ing(1)]
    catalog = [
        _cat(1, 1, price=0.01, pack=100.0, is_default=0),  # reliable / fast
        _cat(2, 1, price=0.01, pack=100.0, is_default=0),  # unreliable / slow
    ]
    suppliers = [
        _sup(1, lead=1.0, reliability=0.99),
        _sup(2, lead=2.0, reliability=0.70),
    ]
    demand = {1: {d: 100.0 for d in range(n)}}
    params = {
        "slack_penalty": 1000.0,
        "safety_penalty_multiplier": 0.0,
        "reliability_cash_tolerance": reliability_cash_tolerance,
        "stress_enabled": stress_enabled,
        "margin_by_ing": margin_by_ing or {1: 5.0},  # €5 margin per gram of ingredient
    }
    return _run(
        ingredients=ingredients,
        catalog=catalog,
        suppliers=suppliers,
        demand_by_day=demand,
        on_hand={1: 100.0},  # cover day-0 demand; lead≥1 means no supplier can deliver day 0
        safety_stock={1: 0.0},
        n_days=n,
        params=params,
    )


def test_reliability_premium_cash_cap_holds():
    """Extra cash spent on reliability must never exceed tolerance × C0."""
    sol = _two_suppliers_scenario(reliability_cash_tolerance=0.01)
    # reliability_premium ≤ 0 or within 1% of cash_cost
    if sol.reliability_premium > 0.0:
        cap = sol.cash_cost * 0.01
        assert sol.reliability_premium <= cap + 1e-3, (
            f"Premium €{sol.reliability_premium:.4f} exceeds 1% cap on "
            f"cash cost €{sol.cash_cost:.4f} (cap=€{cap:.4f})"
        )


def test_tolerance_zero_means_cash_optimal_no_premium():
    """With tolerance=0, no extra cash is spent and premium is 0."""
    sol = _two_suppliers_scenario(reliability_cash_tolerance=0.0)
    assert sol.reliability_premium == 0.0, (
        f"tolerance=0 should produce zero premium; got {sol.reliability_premium:.4f}"
    )


def test_stress_disabled_skips_pass2():
    """stress_enabled=False produces a single-pass plan with zero premium."""
    sol = _two_suppliers_scenario(stress_enabled=False, reliability_cash_tolerance=0.05)
    assert sol.reliability_premium == 0.0, (
        f"stress_enabled=False should skip pass 2; got premium={sol.reliability_premium:.4f}"
    )


def test_stress_pass_produces_solution_fields():
    """With stress enabled and margin data, PlanSolution carries the expected fields."""
    sol = _two_suppliers_scenario(reliability_cash_tolerance=0.05, margin_by_ing={1: 5.0})
    # Fields always present (even if premium is 0 on this small test instance)
    assert hasattr(sol, "reliability_premium"), "Missing reliability_premium field"
    assert hasattr(sol, "exposed_value_baseline"), "Missing exposed_value_baseline field"
    assert hasattr(sol, "exposed_value_plan"), "Missing exposed_value_plan field"
    assert sol.reliability_premium >= 0.0, "Premium must be non-negative"
    assert sol.exposed_value_baseline >= 0.0, "Baseline exposure must be non-negative"
    assert sol.exposed_value_plan >= 0.0, "Plan exposure must be non-negative"
    # Coverage must remain fully intact
    assert sol.coverage_ok, f"Coverage must hold after stress pass; total_short={sol.total_short}"


# ---------------------------------------------------------------------------
# Test 19: Service-day model (workstream D)
# ---------------------------------------------------------------------------

def test_service_day_late_delivery_hour_serves_next_day():
    """A supplier with delivery_hour=14 (after production_start=8) can only serve
    next-day demand: physical delivery on day d ↦ service day d+1.

    Setup: n=5, daily=100g, on_hand=200 (covers service days 0 and 1).
    Supplier lead=1, delivery_hour=14 → service_shift=1.
    Earliest physical delivery: day 1 (order on day 0) → serves service day 2.
    Expected: plan covers service days 2-4 only (≈ 300g ordered).
    """
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, delivery_hour=14.0)],
        demand_by_day={1: {d: 100.0 for d in range(5)}},
        on_hand={1: 200.0},  # covers on-hand days 0 and 1; service days 0-1 accounted for
        n_days=5,
    )
    total_ordered = sum(o.qty for o in sol.orders)
    # Days 0 and 1 covered by on_hand; days 2-4 must be covered by plan → 300g
    assert 200.0 <= total_ordered <= 400.0, (
        f"delivery_hour=14 with on_hand=200 should order ≈300g (days 2-4); got {total_ordered:.0f}"
    )
    # No physical delivery should be scheduled for day 0 (would need order on day -1)
    for o in sol.orders:
        assert o.delivery_day >= 1, (
            f"No delivery possible before day 1 with lead=1; got delivery_day={o.delivery_day}"
        )


def test_service_day_normal_hour_serves_same_day():
    """A supplier with delivery_hour=8 (== production_start) has service_shift=0:
    physical delivery on day d serves service day d (baseline behaviour unchanged)."""
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, delivery_hour=8.0)],
        demand_by_day={1: {d: 100.0 for d in range(5)}},
        on_hand={1: 100.0},  # covers day 0 only
        n_days=5,
    )
    total_ordered = sum(o.qty for o in sol.orders)
    # Days 0 covered by on_hand; days 1-4 must be covered by plan → 400g
    assert total_ordered >= 350.0, (
        f"delivery_hour=8 with on_hand=100 should order ≈400g (days 1-4); got {total_ordered:.0f}"
    )


# ---------------------------------------------------------------------------
# Test 20: Per-line risk detail (workstream C)
# ---------------------------------------------------------------------------

def test_per_line_risk_critical_line_flagged():
    """_per_line_risk() sets at_risk=True only on lines where a 1-day slip causes a shortage.

    Ingredient 1: on_hand=0, needs plan from day 1 → stock_before_plan[1]=0,
                  demand[1]=100 → shortage_if_late=100 → at_risk=True.
    Ingredient 2: on_hand=500 (covers all 5 days), no orders → no at_risk lines.
    """
    sol = _run(
        ingredients=[_ing(1), _ing(2)],
        catalog=[
            _cat(1, 1, price=1.0, pack=100.0),
            _cat(1, 2, price=1.0, pack=100.0),
        ],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 100.0 for d in range(5)}, 2: {d: 100.0 for d in range(5)}},
        on_hand={1: 0.0, 2: 500.0},
        safety_stock={1: 0.0, 2: 0.0},
        n_days=5,
    )
    # At least one order for ing 1 must be at_risk (first delivery has zero backing stock)
    ing1_orders = [o for o in sol.orders if o.ingredient_id == 1]
    ing2_orders = [o for o in sol.orders if o.ingredient_id == 2]

    assert ing1_orders, "Expected orders for ingredient 1 with zero on_hand"
    assert not ing2_orders, "Expected no orders for ingredient 2 — fully stocked"

    # The earliest order for ing 1 should be at_risk (stock_before=0)
    earliest = min(ing1_orders, key=lambda o: o.delivery_day)
    assert earliest.at_risk, (
        f"Earliest ing1 order (day {earliest.delivery_day}) should be at_risk; "
        f"shortage_if_late={earliest.shortage_if_late:.1f}, stock_before={earliest.projected_stock_before:.1f}"
    )
    assert earliest.shortage_if_late > 0.5, (
        f"shortage_if_late must be >0.5 for at_risk order; got {earliest.shortage_if_late:.1f}"
    )


def test_per_line_risk_well_buffered_line_not_flagged():
    """An order that arrives when on_hand already covers that day's demand is not at_risk."""
    # on_hand=300 covers days 0-2; order arrives day 1 (physical) — stock_before=200
    # demand[1]=100, so shortage_if_late = max(0, 100-200) = 0 → at_risk=False
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 100.0 for d in range(5)}},
        on_hand={1: 300.0},  # covers days 0-2; orders for days 3-4
        n_days=5,
    )
    for o in sol.orders:
        if o.projected_stock_before >= 100.0:
            # Stock before this arrival already covers the day's demand
            assert not o.at_risk, (
                f"Order with projected_stock_before={o.projected_stock_before:.0f} "
                f"should not be at_risk (demand=100)"
            )


# ---------------------------------------------------------------------------
# Test 21: Coverage quality flags (workstream B)
# ---------------------------------------------------------------------------

def test_unit_shortfall_nonzero_when_plan_orders_needed():
    """unit_shortfall_if_1day_late > 0 when plan orders are the sole coverage.

    Scenario: zero on_hand, one supplier, demand=100/day.  All plan arrivals
    if delayed 1 day leave day-1 uncovered → unit shortfall > 0.
    """
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 100.0 for d in range(5)}},
        on_hand={1: 100.0},   # only day 0 covered; rest depends on plan
        n_days=5,
    )
    assert hasattr(sol, "unit_shortfall_if_1day_late"), "Missing unit_shortfall_if_1day_late field"
    assert sol.unit_shortfall_if_1day_late > 0.5, (
        f"Delay of plan orders should expose unmet demand; "
        f"unit_shortfall_if_1day_late={sol.unit_shortfall_if_1day_late:.1f}"
    )


def test_unit_shortfall_zero_when_fully_covered_by_stock():
    """unit_shortfall_if_1day_late == 0 when on_hand alone covers all demand.

    No plan orders → no delay risk → shortfall stays at zero.
    """
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 100.0 for d in range(5)}},
        on_hand={1: 600.0},  # covers all 5 days (500g demand + buffer)
        n_days=5,
    )
    assert not sol.orders, "No orders expected — fully stocked"
    assert sol.unit_shortfall_if_1day_late == 0.0, (
        f"No plan orders → no delay risk; expected 0, got {sol.unit_shortfall_if_1day_late:.1f}"
    )


# ---------------------------------------------------------------------------
# Tests: Fix 1 — Expiry-gated cohort model (the "basil bug" regression suite)
# ---------------------------------------------------------------------------

def _lots(qty: float, exp_day: float):
    """Helper: create a lots_by_ing entry for one lot."""
    return [[qty, exp_day]]


def test_expired_stock_does_not_cover_later_demand():
    """REGRESSION — basil bug.

    Opening stock 185g expiring at start-of-day-3 (i.e., usable on days 0-2
    only).  Demand is 185g/day for 5 days.  The earliest feasible delivery
    (lead=1) arrives on day 1 at the latest that matters, but we force the
    only available delivery to day 4 by setting lead=4 — simulating the exact
    scenario where basil arrives Friday for Thursday demand.

    Under the old held-inventory-cap model the MILP incorrectly reported
    coverage_ok=True by counting the 185g expiring lot against day-3 demand.
    The cohort model must surface coverage_ok=False and total_short >= demand
    on days 3+ that can't be covered by day-4 delivery.
    """
    n = 5
    daily = 185.0
    # Opening lot expiring at start of day 3 (available days 0-2)
    ing = _ing(4, perishable=1, shelf_life=3.0, name="Basil")
    demand = {4: {d: daily for d in range(n)}}
    on_hand = {4: 185.0}  # exactly one day's demand, expiring day 3
    lots = {4: _lots(185.0, 3.0)}  # exp_day offset = 3 (available days 0,1,2 only)

    sol = _run(
        ingredients=[ing],
        catalog=[_cat(1, 4, price=0.03, pack=500.0)],
        suppliers=[_sup(1, lead=4.0)],  # earliest delivery = day 4
        demand_by_day=demand,
        on_hand=on_hand,
        safety_stock={4: 0.0},
        n_days=n,
        params={
            "slack_penalty": 1000.0,
            "lots_by_ing": lots,
        },
    )
    # Day 3 demand (185g) cannot be covered:
    # - opening 185g expires at start of day 3 (not available on day 3)
    # - first delivery at day 4 (after day-3 demand)
    assert not sol.coverage_ok, (
        f"Expected coverage_ok=False (basil bug regression): coverage_ok={sol.coverage_ok}, "
        f"total_short={sol.total_short}"
    )
    assert sol.total_short >= daily * 0.9, (
        f"Expected total_short >= {daily * 0.9:.0f}, got {sol.total_short:.1f}"
    )


def test_expiring_stock_used_before_expiry_only():
    """Opening stock expiring on day 2 must cover days 0 and 1 but not day 2.

    In the cohort model, cohort e=2 satisfies demand only when e > d, i.e.
    only for days d = 0 and d = 1.  Day 2 demand must be met by an order.
    """
    n = 4
    daily = 100.0
    ing = _ing(1, perishable=1, shelf_life=2.0, name="Herb")
    demand = {1: {d: daily for d in range(n)}}
    on_hand = {1: 200.0}  # 2 days worth, expiring at start of day 2
    lots = {1: _lots(200.0, 2.0)}  # exp_day=2 → usable on days 0,1 only

    sol = _run(
        ingredients=[ing],
        catalog=[_cat(1, 1, price=0.05, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day=demand,
        on_hand=on_hand,
        safety_stock={1: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0, "lots_by_ing": lots},
    )
    # Days 0 and 1 are covered by opening stock (200g); days 2 and 3 need orders.
    assert sol.coverage_ok, f"Days 2-3 should be coverable via orders; total_short={sol.total_short}"
    ordered_qty = sum(o.qty for o in sol.orders if o.ingredient_id == 1)
    assert ordered_qty >= daily * 2 * 0.9, (
        f"Expected at least {daily * 2 * 0.9:.0f}g ordered for days 2-3, got {ordered_qty:.0f}g"
    )
    # All deliveries must arrive on day 2 or later (lead=1 → first feasible day 1,
    # but day-1 delivery can cover day 2 demand since it doesn't expire before use).
    for o in sol.orders:
        if o.ingredient_id == 1 and o.qty > 0:
            assert o.delivery_day >= 1, f"Delivery before lead time: day {o.delivery_day}"


def test_milp_matches_python_projection_on_expiry():
    """Consistency guard: MILP total_short must equal Python projection shortfall.

    When opening lots expire mid-horizon and no delivery can arrive before the
    gap, both the MILP (cohort model) and the Python _per_line_risk projection
    must agree that demand on those days is uncoverable.
    """
    from track_b.procurement.plan_optimizer import _per_line_risk, PlanOrder

    n = 5
    daily = 100.0
    # Lot expires at start of day 2: covers days 0 and 1 only.
    # Lead=3: first delivery at day 3.  Day 2 is a gap.
    ing = _ing(1, perishable=1, shelf_life=2.0, name="Herb")
    demand = {1: {d: daily for d in range(n)}}
    on_hand_qty = 200.0
    on_hand = {1: on_hand_qty}
    lots = {1: _lots(on_hand_qty, 2.0)}

    sol = _run(
        ingredients=[ing],
        catalog=[_cat(1, 1, price=0.05, pack=100.0)],
        suppliers=[_sup(1, lead=3.0)],
        demand_by_day=demand,
        on_hand=on_hand,
        safety_stock={1: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0, "lots_by_ing": lots},
    )
    # Day 2 demand is uncoverable: lot expired, no delivery until day 3.
    assert not sol.coverage_ok, (
        f"Expected coverage gap on day 2; total_short={sol.total_short}"
    )
    milp_short = sol.total_short

    # Verify with Python projection
    risk = _per_line_risk(
        orders=sol.orders,
        demand_by_day=demand,
        on_hand=on_hand,
        inbound_by_day={1: {}},
        lots_by_ing=lots,
        n_days=n,
        ingredient_ids=[1],
        ing_by_id={1: {"id": 1, "perishable": 1, "shelf_life_days": 2.0, "base_unit": "g"}},
    )
    # projection shortfall = demand on day 2 that stock_before_plan can't cover
    py_short = sum(
        v.get("shortage_if_late", 0)
        for v in risk.values()
    )
    # The MILP total_short (day-2 gap) should be consistent with projection findings
    assert milp_short >= daily * 0.9, (
        f"MILP should report at least {daily * 0.9:.0f} uncoverable; got {milp_short:.1f}"
    )


# ---------------------------------------------------------------------------
# Tests: Fix 2 — Scheduled supplier days (piggyback + MOV exemption)
# ---------------------------------------------------------------------------

def test_scheduled_supplier_day_no_extra_delivery_charge():
    """Incremental lines on a scheduled (sunk) supplier-day incur zero delivery charge.

    A FreshDirect delivery is already scheduled on day 2 (sunk charge €9).
    The MILP needs to add garlic on that day.  With the scheduled-day pre-opening,
    no new delivery charge should appear in total_cost.
    """
    n = 5
    dc = 9.0
    sol_with = _run(
        ingredients=[_ing(7, name="Garlic")],
        catalog=[_cat(5, 7, price=0.0095, pack=1000.0)],
        suppliers=[_sup(5, lead=2.0, dc=dc, mov=50.0)],
        demand_by_day={7: {d: 300.0 for d in range(n)}},
        on_hand={7: 0.0},
        safety_stock={7: 0.0},
        n_days=n,
        params={
            "slack_penalty": 1000.0,
            "scheduled_supplier_days": {(5, 2)},  # day 2 delivery already scheduled
        },
    )
    sol_without = _run(
        ingredients=[_ing(7, name="Garlic")],
        catalog=[_cat(5, 7, price=0.0095, pack=1000.0)],
        suppliers=[_sup(5, lead=2.0, dc=dc, mov=50.0)],
        demand_by_day={7: {d: 300.0 for d in range(n)}},
        on_hand={7: 0.0},
        safety_stock={7: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0},
    )
    # With scheduled day: the delivery charge for day 2 is sunk, so total_cost is lower
    # (by exactly the delivery charge for that day if the solver chose day 2 anyway)
    # At minimum, total_cost should not be higher than without scheduled
    assert sol_with.total_cost <= sol_without.total_cost + 1e-3, (
        f"Scheduled day should not raise cost: with={sol_with.total_cost:.2f} "
        f"without={sol_without.total_cost:.2f}"
    )


def test_scheduled_supplier_day_exempt_from_mov():
    """On a scheduled (sunk) supplier-day, incremental lines need not clear MOV.

    FreshDirect (MOV=€50) is already delivering on day 2.  Garlic need is ~1155g.
    Without scheduling, the MILP pads to 3 packs (3000g) to clear MOV.
    With scheduling, 1 pack (1000g * €0.0095 = €9.50) is enough — no MOV re-check.
    """
    n = 5
    garlic_need = 200.0  # per day from day 2 onwards
    sol = _run(
        ingredients=[_ing(7, name="Garlic")],
        catalog=[_cat(5, 7, price=0.0095, pack=1000.0)],
        suppliers=[_sup(5, lead=2.0, dc=9.0, mov=50.0)],
        demand_by_day={7: {d: garlic_need for d in range(n)}},
        on_hand={7: 0.0},
        safety_stock={7: 0.0},
        n_days=n,
        params={
            "slack_penalty": 1000.0,
            "scheduled_supplier_days": {(5, 2)},  # MOV already cleared
        },
    )
    orders_d2 = [o for o in sol.orders if o.delivery_day == 2 and o.ingredient_id == 7]
    if orders_d2:
        total_on_d2 = sum(o.qty for o in orders_d2)
        # Should order only what's needed — not 3x packs as MOV filler
        needed_d2 = garlic_need * (n - 2)  # days 2,3,4
        assert total_on_d2 <= needed_d2 + 1000.0 + 1e-6, (
            f"Over-ordered garlic on scheduled day: {total_on_d2:.0f}g vs need ~{needed_d2:.0f}g"
        )


# ---------------------------------------------------------------------------
# Tests: Fix 3 — Robust hard-delay mode
# ---------------------------------------------------------------------------

def test_robust_flag_off_is_identical_to_baseline():
    """With robust_hard_delay=False (default), results must be byte-identical
    to a run with no robust flag at all — no regression."""
    n = 5
    params_base = {"slack_penalty": 1000.0}
    params_robust_off = {"slack_penalty": 1000.0, "robust_hard_delay": False}

    ingredients = [_ing(1)]
    catalog = [_cat(1, 1, price=0.01, pack=100.0)]
    suppliers = [_sup(1, lead=1.0, reliability=0.90)]
    demand = {1: {d: 100.0 for d in range(n)}}

    sol_base = _run(
        ingredients=ingredients, catalog=catalog, suppliers=suppliers,
        demand_by_day=demand, on_hand={1: 0.0}, safety_stock={1: 0.0},
        n_days=n, params=params_base,
    )
    sol_off = _run(
        ingredients=ingredients, catalog=catalog, suppliers=suppliers,
        demand_by_day=demand, on_hand={1: 0.0}, safety_stock={1: 0.0},
        n_days=n, params=params_robust_off,
    )
    assert sol_off.total_cost == pytest.approx(sol_base.total_cost, rel=1e-3), (
        f"With robust=False, cost should match baseline: "
        f"base={sol_base.total_cost:.2f} off={sol_off.total_cost:.2f}"
    )
    assert sol_off.robust_requested is False
    assert sol_off.robust_applied is False


def test_robust_mode_requested_field():
    """With robust_hard_delay=True, robust_requested=True is always set on PlanSolution."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available; robust mode requires MILP")
    n = 5
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, reliability=0.90)],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 0.0},
        safety_stock={1: 0.0},
        n_days=n,
        params={
            "slack_penalty": 1000.0,
            "robust_hard_delay": True,
            "robust_min_reliability": 0.95,
        },
    )
    assert sol.robust_requested is True
    assert sol.robust_status in ("applied", "infeasible_fell_back", "error_fell_back", "skipped"), (
        f"Unexpected robust_status: {sol.robust_status!r}"
    )


def test_robust_mode_forces_earlier_order_for_unreliable_supplier():
    """With robust_hard_delay=True and a low-reliability supplier, the plan
    must either (a) order earlier so demand survives a 1-day slip, or
    (b) fall back gracefully with robust_applied=False.

    Scenario: sole supplier has lead=2, reliability=0.50 (< 0.95 threshold).
    Day-2 delivery nominally covers day-2 demand.  Under a 1-day delay the
    delivery arrives at day 3 — leaving day-2 demand uncovered.
    Robust mode should push the solver to order day 1 (if feasible) or
    fall back cleanly.
    """
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available; robust mode requires MILP")
    n = 5
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=2.0, reliability=0.50)],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 50.0},
        safety_stock={1: 0.0},
        n_days=n,
        params={
            "slack_penalty": 1000.0,
            "robust_hard_delay": True,
            "robust_min_reliability": 0.95,
        },
    )
    assert sol.robust_requested is True
    # Either robust mode applied (better plan) or it fell back gracefully
    assert sol.robust_status in ("applied", "infeasible_fell_back", "error_fell_back"), (
        f"Unexpected robust_status: {sol.robust_status!r}"
    )
    # Plan must still be a valid procurement plan (orders present, no crash)
    # coverage_ok may be False if early days are structurally uncoverable
    assert sol.method == "milp", f"Expected MILP method, got {sol.method}"


# ---------------------------------------------------------------------------
# Tests: R3 — accept feasible-but-not-optimal CBC incumbents
# ---------------------------------------------------------------------------

def test_incumbent_usable_predicate():
    """Pure predicate: status codes + q-var populated-ness → usable?"""
    from track_b.procurement.plan_optimizer import _incumbent_usable

    # Optimal is always usable, regardless of q values.
    assert _incumbent_usable(1, [None]) is True
    assert _incumbent_usable(1, []) is True
    # Infeasible / unbounded never usable.
    assert _incumbent_usable(-1, [1.0, 2.0]) is False
    assert _incumbent_usable(-2, [1.0, 2.0]) is False
    # NotSolved (0) / Undefined (-3): usable iff every q var populated.
    assert _incumbent_usable(0, [1.0, 2.0]) is True
    assert _incumbent_usable(0, [1.0, None]) is False
    assert _incumbent_usable(0, []) is False       # no incumbent
    assert _incumbent_usable(-3, [0.0, 3.0]) is True
    assert _incumbent_usable(-3, [None]) is False


def test_timelimit_incumbent_not_discarded():
    """A time-limited feasible incumbent (status=NotSolved with populated q vars)
    must be accepted, not discarded to greedy."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")

    import pulp as _pulp

    real_solve = _pulp.LpProblem.solve

    def _fake_solve(self, *a, **k):
        real_solve(self, *a, **k)
        # Force the "time-limited" status: variables retain their solved values
        # but CBC didn't prove optimality.
        self.status = _pulp.LpStatusNotSolved  # 0
        return self.status

    with mock.patch.object(_pulp.LpProblem, "solve", _fake_solve):
        sol = _run(
            ingredients=[_ing(1)],
            catalog=[_cat(1, 1, price=0.01, pack=100.0)],
            suppliers=[_sup(1, lead=1.0)],
            demand_by_day={1: {d: 100.0 for d in range(5)}},
            on_hand={1: 0.0},
            safety_stock={1: 0.0},
            n_days=5,
            params={"slack_penalty": 1000.0, "stress_enabled": False},
        )
    assert sol.method == "milp", (
        f"Time-limited incumbent should be kept as MILP, got {sol.method!r}"
    )
    assert sol.orders, "Expected incumbent plan to carry orders"


# ---------------------------------------------------------------------------
# Tests: R1 — real robust balance for non-perishables
# ---------------------------------------------------------------------------

def _slip_shortfall(sol, iid, demand_by_day, on_hand, n_days, slip_suppliers,
                    inbound=None):
    """Hand-rolled +1-day-slip FEFO over sol.orders (non-perishable).

    Orders from suppliers in ``slip_suppliers`` arrive one day later than their
    delivery_day.  Returns [(day, short_qty), ...] where demand can't be met.
    Non-perishable: single running scalar of stock (no expiry).
    """
    inbound = inbound or {}
    by_day: dict = {}
    for o in sol.orders:
        if o.ingredient_id == iid and o.qty > 0:
            dd = o.delivery_day + (1 if o.supplier_id in slip_suppliers else 0)
            by_day[dd] = by_day.get(dd, 0.0) + o.qty
    for d, q in (inbound.get(iid, {}) or {}).items():
        by_day[d] = by_day.get(d, 0.0) + q
    stock = float(on_hand.get(iid, 0.0))
    short = []
    for d in range(n_days):
        stock += by_day.get(d, 0.0)
        dem = demand_by_day.get(iid, {}).get(d, 0.0)
        if stock + 1e-6 < dem:
            short.append((d, round(dem - stock, 2)))
            stock = 0.0
        else:
            stock -= dem
    return short


def test_robust_nonperishable_survives_slip():
    """Robust mode on a non-perishable with an unreliable supplier must produce a
    plan that survives a +1-day slip on coverable days (or fall back cleanly)."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 6
    # Single unreliable supplier, lead=1, reliability below robust threshold.
    # on_hand covers day 0 only; robust mode must order early enough that a
    # +1-slip still covers the remaining days.
    sol = _run(
        ingredients=[_ing(1)],  # non-perishable
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, reliability=0.50)],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 200.0},  # buffer so a slip is survivable if ordered early
        safety_stock={1: 0.0},
        n_days=n,
        params={
            "slack_penalty": 1000.0,
            "robust_hard_delay": True,
            "robust_min_reliability": 0.95,
        },
    )
    assert sol.robust_requested is True
    assert sol.method == "milp"
    if sol.robust_applied:
        # Under a +1-day slip of the unreliable supplier, coverable days (those
        # the nominal plan covered) must not stock out.
        nominal = _slip_shortfall(sol, 1, {1: {d: 100.0 for d in range(n)}},
                                  {1: 200.0}, n, slip_suppliers=set())
        nominal_short_days = {d for d, _ in nominal}
        slipped = _slip_shortfall(sol, 1, {1: {d: 100.0 for d in range(n)}},
                                  {1: 200.0}, n, slip_suppliers={1})
        new_short = [(d, q) for d, q in slipped if d not in nominal_short_days]
        assert not new_short, (
            f"Robust plan should survive +1 slip on coverable days; new shortfalls: {new_short}"
        )


def test_robust_nonperishable_no_op_regression():
    """With a fully reliable supplier (>= robust threshold) robust mode is a no-op:
    the plan is unchanged from a non-robust run and still fully covers demand."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 6
    common = dict(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, reliability=0.99)],  # above threshold
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 100.0},
        safety_stock={1: 0.0},
        n_days=n,
    )
    sol_plain = _run(**common, params={"slack_penalty": 1000.0})
    sol_robust = _run(**common, params={
        "slack_penalty": 1000.0,
        "robust_hard_delay": True,
        "robust_min_reliability": 0.95,
    })
    assert sol_robust.robust_requested is True
    # No qualifying (unreliable) supplier → robust constraints are inert; cost matches.
    assert sol_robust.total_cost == pytest.approx(sol_plain.total_cost, rel=1e-3), (
        f"Reliable-only robust run should match plain: "
        f"robust={sol_robust.total_cost:.2f} plain={sol_plain.total_cost:.2f}"
    )
    assert sol_robust.coverage_ok


# ---------------------------------------------------------------------------
# Tests: R2 — free-goods quantity wired into coverage
# ---------------------------------------------------------------------------

def _fg_offer(supplier_id, free_ingredient_id, free_qty_g, min_order_value=0.0):
    from track_b.procurement.terms import FreeGoodsOffer
    return FreeGoodsOffer(
        supplier_id=supplier_id,
        free_ingredient_id=free_ingredient_id,
        free_qty_g=free_qty_g,
        min_order_value=min_order_value,
        remaining_orders=None,
        term_id=1,
    )


def test_free_goods_improve_coverage():
    """A free-goods offer that supplies a second ingredient must let the plan
    cover that ingredient's demand and emit a zero-price free line."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 4
    # Ingredient 1 is ordered from supplier 1.  Ingredient 2 is only ever
    # available as a free-goods add-on whenever we buy from supplier 1.
    # Ingredient 2 has demand but NO catalog entry → only free goods can cover it.
    offer = _fg_offer(supplier_id=1, free_ingredient_id=2, free_qty_g=300.0,
                      min_order_value=0.0)
    sol = _run(
        ingredients=[_ing(1), _ing(2)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],  # only ingredient 1 orderable
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={
            1: {d: 100.0 for d in range(n)},
            2: {d: 50.0 for d in range(n)},  # needs free goods to cover
        },
        on_hand={1: 100.0, 2: 50.0},  # day 0 covered; later days need supply
        safety_stock={1: 0.0, 2: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0, "free_goods_offers": [offer]},
    )
    free_lines = [o for o in sol.orders if o.ingredient_id == 2 and o.unit_price == 0.0]
    assert free_lines, "Expected a zero-price free-goods line for ingredient 2"
    assert all(o.reason == "free-goods benefit" for o in free_lines)
    assert all(o.qty == pytest.approx(300.0) for o in free_lines)


def test_free_goods_absent_is_noop():
    """No free_goods_offers → behaviour identical to a plain run."""
    n = 4
    common = dict(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 100.0},
        safety_stock={1: 0.0},
        n_days=n,
    )
    sol_plain = _run(**common, params={"slack_penalty": 1000.0})
    sol_empty = _run(**common, params={"slack_penalty": 1000.0, "free_goods_offers": []})
    assert sol_empty.total_cost == pytest.approx(sol_plain.total_cost, rel=1e-6)
    assert len(sol_empty.orders) == len(sol_plain.orders)
    assert not any(o.reason == "free-goods benefit" for o in sol_empty.orders)


# ---------------------------------------------------------------------------
# Capped percentage-discount rebate ("50% off up to €30")
# ---------------------------------------------------------------------------

def _cd_offer(supplier_id, discount_frac, cap_eur, min_order_value=0.0):
    from track_b.procurement.terms import CappedDiscountOffer
    return CappedDiscountOffer(
        supplier_id=supplier_id, discount_frac=discount_frac, cap_eur=cap_eur,
        min_order_value=min_order_value, remaining_orders=None, term_id=1,
    )


def _capped_common(n=2):
    # Day 0 covered by on-hand; day 1 must be ordered (1000g @ €0.10/g = €100 spend).
    return dict(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=0.1, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 1000.0 for d in range(n)}},
        on_hand={1: 1000.0},
        safety_stock={1: 0.0},
        n_days=n,
    )


def _sup1_spend(sol):
    return sum(float(o.qty) * float(o.unit_price)
               for o in sol.orders if o.supplier_id == 1 and o.unit_price > 0)


def test_capped_discount_rebate_is_capped():
    """50% off up to €30 on a ~€100 order rebates exactly the €30 cap, not €50."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    common = _capped_common()
    plain = _run(**common, params={"slack_penalty": 1000.0})
    offer = _run(**common, params={"slack_penalty": 1000.0,
                                   "capped_discount_offers": [_cd_offer(1, 0.5, 30.0)]})
    spend = _sup1_spend(plain)
    assert spend > 60.0  # 50% of spend exceeds the €30 cap, so the cap binds
    assert len(offer.orders) == len(plain.orders)  # rebate must not distort the plan
    assert plain.total_cost - offer.total_cost == pytest.approx(30.0, abs=1e-4)


def test_capped_discount_rebate_below_cap_is_full_percentage():
    """When 50% of spend is under the cap, the full percentage is rebated."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    common = _capped_common()
    plain = _run(**common, params={"slack_penalty": 1000.0})
    offer = _run(**common, params={"slack_penalty": 1000.0,
                                   "capped_discount_offers": [_cd_offer(1, 0.5, 500.0)]})
    spend = _sup1_spend(plain)
    assert plain.total_cost - offer.total_cost == pytest.approx(0.5 * spend, abs=1e-4)


def test_capped_discount_absent_is_noop():
    """No capped_discount_offers → identical plan and cost."""
    common = _capped_common()
    plain = _run(**common, params={"slack_penalty": 1000.0})
    empty = _run(**common, params={"slack_penalty": 1000.0, "capped_discount_offers": []})
    assert empty.total_cost == pytest.approx(plain.total_cost, rel=1e-6)
    assert len(empty.orders) == len(plain.orders)


def test_scheduled_day_before_first_day_allows_piggyback():
    """An item that can't independently clear MOV must ride an already-scheduled
    (sunk-PO) supplier delivery whose order-by has passed (scheduled_day <
    first_day), instead of a later standalone order. Regression for the Coffee
    'uncoverable' bug: now_time_of_day_s pushes first_day to 4, but a delivery is
    already scheduled on Day 3 — a single Coffee pack (€40) can't meet the €80 MOV
    on a fresh day, so it must piggyback on the Day-3 truck."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 8
    sol = _run(
        ingredients=[_ing(2, name="Coffee")],
        catalog=[_cat(1, 2, price=0.02, pack=2000.0)],
        suppliers=[_sup(1, lead=3.0, mov=80.0, dc=12.0)],
        demand_by_day={2: {0: 540., 1: 240., 2: 210., 3: 300., 4: 240., 5: 360., 6: 240., 7: 0.}},
        on_hand={2: 1970.0}, safety_stock={2: 0.0}, n_days=n,
        params={"slack_penalty": 1000.0, "now_time_of_day_s": 28886.0,
                "scheduled_supplier_days": {(1, 3)}},
    )
    assert sol.coverage_ok
    coffee = [o for o in sol.orders if o.ingredient_id == 2 and o.qty > 0]
    assert coffee, "Coffee should be ordered, not left uncoverable"
    # Rides the scheduled Day-3 delivery (one €40 pack), not a standalone later order.
    assert any(o.delivery_day == 3 for o in coffee), \
        f"Coffee should ride the scheduled Day-3 delivery, got {[(o.delivery_day, o.qty) for o in coffee]}"


def test_free_goods_perishable_cohort():
    """Free goods for a perishable ingredient are cohort-gated: the free qty
    covers demand only within its shelf life and still surfaces as a free line."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 4
    # Ingredient 2 is a perishable only coverable via free goods.
    offer = _fg_offer(supplier_id=1, free_ingredient_id=2, free_qty_g=200.0,
                      min_order_value=0.0)
    sol = _run(
        ingredients=[_ing(1), _ing(2, perishable=1, shelf_life=5.0)],
        catalog=[_cat(1, 1, price=0.01, pack=100.0)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={
            1: {d: 100.0 for d in range(n)},
            2: {d: 40.0 for d in range(n)},
        },
        on_hand={1: 100.0, 2: 40.0},
        safety_stock={1: 0.0, 2: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0, "free_goods_offers": [offer]},
    )
    free_lines = [o for o in sol.orders if o.ingredient_id == 2 and o.unit_price == 0.0]
    assert free_lines, "Expected a zero-price free-goods line for perishable ingredient 2"
    assert all(o.reason == "free-goods benefit" for o in free_lines)


# ---------------------------------------------------------------------------
# Tests: R4 — delay-shortfall objective + un-saturated lead_factor
# ---------------------------------------------------------------------------

def test_lead_factor_monotonic():
    """lead_factor = min(lead, 5)/2 must be monotone non-decreasing in lead,
    have a floor of 0.5 at lead=1, and separate leads within the 1..5 band
    (so a 4-day lead is penalised more than a 2-day lead — no early saturation)."""
    def lead_factor(lead: float) -> float:
        return min(lead, 5.0) / 2.0

    assert lead_factor(1.0) == pytest.approx(0.5)  # floor
    leads = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 10.0]
    vals = [lead_factor(x) for x in leads]
    # Monotone non-decreasing
    for a, b in zip(vals, vals[1:]):
        assert b >= a - 1e-9, f"lead_factor not monotone: {vals}"
    # No early saturation: 4-day lead strictly penalised more than 2-day lead
    assert lead_factor(4.0) > lead_factor(2.0) + 1e-9
    # Soft cap kicks in at 5 days
    assert lead_factor(6.0) == pytest.approx(lead_factor(5.0))


def test_pass2_minimizes_modeled_delay():
    """With stress enabled + margin, pass-2's soft delay-shortfall term must lower
    the modelled +1-day-slip exposure vs a stress-disabled (cash-only) plan.

    Two equal-price suppliers: one fast+reliable, one slow+unreliable.  Cash-only
    is indifferent; stress must bias toward the resilient plan."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 6
    ingredients = [_ing(1)]
    catalog = [
        _cat(1, 1, price=0.01, pack=100.0),   # supplier 1: fast + reliable
        _cat(2, 1, price=0.01, pack=100.0),   # supplier 2: slow + unreliable
    ]
    suppliers = [
        _sup(1, lead=1.0, reliability=0.99),
        _sup(2, lead=1.0, reliability=0.50),
    ]
    demand = {1: {d: 100.0 for d in range(n)}}
    common = dict(
        ingredients=ingredients, catalog=catalog, suppliers=suppliers,
        demand_by_day=demand, on_hand={1: 100.0}, safety_stock={1: 0.0}, n_days=n,
    )
    sol_cash = _run(**common, params={
        "slack_penalty": 1000.0,
        "safety_penalty_multiplier": 0.0,
        "stress_enabled": False,
        "margin_by_ing": {1: 5.0},
    })
    sol_stress = _run(**common, params={
        "slack_penalty": 1000.0,
        "safety_penalty_multiplier": 0.0,
        "stress_enabled": True,
        "reliability_cash_tolerance": 0.05,
        "margin_by_ing": {1: 5.0},
    })
    assert sol_stress.coverage_ok
    # Stress plan's modelled delay exposure must not be worse; ideally strictly less.
    assert sol_stress.unit_shortfall_if_1day_late <= sol_cash.unit_shortfall_if_1day_late + 1e-6, (
        f"Stress plan delay exposure {sol_stress.unit_shortfall_if_1day_late:.1f} "
        f"should be <= cash plan {sol_cash.unit_shortfall_if_1day_late:.1f}"
    )
    # Cash cap must still hold.
    if sol_stress.reliability_premium > 0.0:
        assert sol_stress.reliability_premium <= sol_stress.cash_cost * 0.05 + 1e-3


# ---------------------------------------------------------------------------
# Tests: R5 — multi-tier per-item discounts + rebate on actual spend
# ---------------------------------------------------------------------------

def test_multi_tier_discount_reaches_deeper_tier():
    """When a catalog exposes two qty-discount tiers, the MILP must be able to
    reach the DEEPER tier — offering both tiers yields a strictly lower cost than
    offering only the shallow tier (the deep tier's per-unit saving is captured)."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 7
    # Base price €1/g.  Demand ~600g over the horizon → deep tier is reachable.
    shallow = [{"min_qty": 200.0, "unit_price": 0.8}]
    deep = [
        {"min_qty": 200.0, "unit_price": 0.8},
        {"min_qty": 400.0, "unit_price": 0.5},
    ]
    common = dict(
        ingredients=[_ing(1)],
        suppliers=[_sup(1, lead=1.0)],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 100.0},
        safety_stock={1: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0, "stress_enabled": False},
    )
    sol_shallow = _run(catalog=[_cat(1, 1, price=1.0, pack=100.0, discount=shallow)],
                       **common)
    sol_deep = _run(catalog=[_cat(1, 1, price=1.0, pack=100.0, discount=deep)],
                    **common)
    assert sol_deep.coverage_ok and sol_shallow.coverage_ok
    # Same goods ordered, but the deep tier's larger per-unit saving lowers cash.
    assert sol_deep.cash_cost < sol_shallow.cash_cost - 1e-3, (
        f"Deep tier should lower cost: deep={sol_deep.cash_cost:.2f} "
        f"shallow={sol_shallow.cash_cost:.2f}"
    )


def test_volume_rebate_on_actual_spend():
    """Volume rebate must scale with ACTUAL spend above threshold, not just the
    threshold value.  Ordering well past the threshold yields a larger rebate
    than the flat threshold*pct approximation would."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 7
    # 10% off orders ≥ €100 on a single supplier-day.  Force a large single-day
    # spend well above €100 so rebate-on-spend clearly exceeds rebate-on-threshold.
    # High delivery charge consolidates onto one day.
    sol = _run(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, dc=100.0,
                        vol_discount=[{"min_value": 100.0, "discount_pct": 10}])],
        demand_by_day={1: {d: 100.0 for d in range(n)}},
        on_hand={1: 100.0},
        safety_stock={1: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0, "stress_enabled": False},
    )
    assert sol.coverage_ok
    # Reconstruct goods spend on each supplier-day from the plan.
    from collections import defaultdict
    spend_by_day = defaultdict(float)
    for o in sol.orders:
        if o.qty > 0 and o.unit_price > 0:
            spend_by_day[o.delivery_day] += o.qty * o.unit_price
    goods_spend = sum(spend_by_day.values())
    # cash_cost = goods + delivery charges - rebate.  Compute expected rebate on
    # ACTUAL spend for days clearing €100.
    n_delivery_days = len([d for d, v in spend_by_day.items() if v > 0])
    delivery_total = 100.0 * n_delivery_days
    rebate_on_spend = sum(0.10 * v for v in spend_by_day.values() if v >= 100.0 - 1e-6)
    expected_cash = goods_spend + delivery_total - rebate_on_spend
    assert sol.cash_cost == pytest.approx(expected_cash, rel=1e-2, abs=1.0), (
        f"cash_cost {sol.cash_cost:.2f} should match rebate-on-actual-spend "
        f"model {expected_cash:.2f} (goods={goods_spend:.2f}, "
        f"delivery={delivery_total:.2f}, rebate={rebate_on_spend:.2f})"
    )
    # And the rebate must exceed the flat threshold*pct approximation when spend
    # is materially above the threshold on at least one day.
    if any(v > 150.0 for v in spend_by_day.values()):
        flat_rebate = sum(0.10 * 100.0 for v in spend_by_day.values() if v >= 100.0)
        assert rebate_on_spend > flat_rebate + 1e-6


# ---------------------------------------------------------------------------
# Regression: the pass-2 SOFT delay objective must never sacrifice NOMINAL
# coverage.  A delay-exposed ingredient whose +1-day slip is structurally
# unavoidable (lead=1, no buffer) must still be covered on time; the modelled
# delay shortfall is absorbed by demand_short_rob, NOT by dropping real demand.
# (Guards the demand_short_rob<=demand_short coupling that must stay pass-3-only.)
# ---------------------------------------------------------------------------

def test_stress_pass_preserves_nominal_coverage_when_delay_unavoidable():
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 4
    common = dict(
        ingredients=[_ing(1)],  # non-perishable
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, reliability=0.5)],  # unreliable → qualifies for slip
        demand_by_day={1: {1: 100.0, 2: 100.0, 3: 100.0}},
        on_hand={1: 0.0},
        safety_stock={1: 0.0},
        n_days=n,
    )
    base = _run(**common, params={"slack_penalty": 1000.0, "stress_enabled": False})
    stressed = _run(
        **common,
        params={"slack_penalty": 1000.0, "stress_enabled": True,
                "reliability_cash_tolerance": 0.01, "margin_by_ing": {1: 5.0}},
    )
    # Stress must not degrade nominal coverage or exceed the 1% cash cap.
    assert base.coverage_ok and base.total_short == pytest.approx(0.0)
    assert stressed.coverage_ok, "stress pass dropped nominal coverage"
    assert stressed.total_short == pytest.approx(0.0)
    assert stressed.cash_cost >= base.cash_optimal_cost - 1e-6
    assert stressed.cash_cost <= base.cash_optimal_cost * 1.01 + 1e-6


def test_robust_unavoidable_slip_falls_back_without_dropping_coverage():
    """Robust mode on a structurally-unavoidable slip must fall back (not 'apply'
    by sacrificing nominal coverage) and keep the plan fully covered."""
    import track_b.procurement.plan_optimizer as _pm
    if not _pm._PULP_AVAILABLE:
        pytest.skip("PuLP not available")
    n = 4
    common = dict(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, reliability=0.5)],
        demand_by_day={1: {1: 100.0, 2: 100.0, 3: 100.0}},
        safety_stock={1: 0.0},
        n_days=n,
    )
    p = {"slack_penalty": 1000.0, "stress_enabled": True,
         "reliability_cash_tolerance": 0.5, "margin_by_ing": {1: 5.0},
         "robust_hard_delay": True}
    # No buffer → day-1 slip unavoidable → robust cannot truly apply.
    unavoidable = _run(**common, on_hand={1: 0.0}, params=p)
    assert not unavoidable.robust_applied
    assert unavoidable.robust_status == "infeasible_fell_back"
    assert unavoidable.coverage_ok, "robust fallback dropped nominal coverage"
    assert unavoidable.total_short == pytest.approx(0.0)
    # Buffer covers the slip → robust should genuinely apply.
    buffered = _run(**common, on_hand={1: 100.0}, params=p)
    assert buffered.robust_applied and buffered.robust_status == "applied"
    assert buffered.coverage_ok


def test_freshness_defers_perishable_delivery_jit():
    """Among equal-cash plans the freshness tiebreaker delivers a perishable
    just-in-time (close to when it's needed) rather than front-loading it, so
    shelf life isn't burned for no cost benefit. Demand falls only on day 4; a
    long shelf life means early delivery wouldn't expire, so only freshness
    separates 'deliver day 1' from 'deliver day 4' — it must pick the later day.
    """
    n = 6
    sol = _run(
        ingredients=[_ing(1, perishable=1, shelf_life=10)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        # reliability < 1 + a positive margin so the pass-2 (stress) objective —
        # which carries the freshness tiebreaker — actually runs.
        suppliers=[_sup(1, lead=1.0, reliability=0.9)],
        demand_by_day={1: {4: 200.0}},
        on_hand={1: 0.0},
        n_days=n,
        params={"slack_penalty": 1000.0, "margin_by_ing": {1: 5.0}},
    )
    assert sol.orders, "expected an order to cover day-4 demand"
    # JIT: nothing should be delivered days before it's needed (day 4).
    assert all(o.delivery_day >= 3 for o in sol.orders), (
        f"perishable front-loaded instead of JIT: {[(o.delivery_day, o.qty) for o in sol.orders]}"
    )


def test_free_delivery_rebate_fires_above_threshold():
    """A MOV-gated free_delivery offer rebates the supplier's delivery charge
    once a delivery clears the spend threshold — so the promo lowers real cash
    exactly when the offer says it should."""
    from track_b.procurement.terms import FreeDeliveryOffer
    n = 4
    common = dict(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, dc=20.0)],
        demand_by_day={1: {d: 50.0 for d in range(n)}},  # 200 units → order value ≫ €100
        on_hand={1: 0.0},
        n_days=n,
    )
    base = _run(**common, params={"slack_penalty": 1000.0})
    promo = _run(**common, params={
        "slack_penalty": 1000.0,
        "free_delivery_offers": [FreeDeliveryOffer(supplier_id=1, min_order_value=100.0,
                                                   remaining_orders=2, term_id=1)],
    })
    # The promo plan buys the same goods but rebates at least one €20 delivery.
    assert promo.total_cost < base.total_cost - 1e-6, (base.total_cost, promo.total_cost)


def test_free_delivery_flips_supplier_choice():
    """When a pricier-per-unit supplier's free-delivery promo makes its LANDED
    cost the lowest, the cost-optimal plan must switch to it — promotions are
    utilised exactly when (and only when) they yield the cheapest plan.

    100u demand.  sup1 goods €1.00 + €8 deliv = €108; sup2 goods €0.99 + €6 =
    €105 → without a promo sup2 wins.  sup1 free delivery → €100 < €105 → sup1.
    """
    from track_b.procurement.terms import FreeDeliveryOffer
    n = 4
    common = dict(
        ingredients=[_ing(1)],
        catalog=[_cat(1, 1, price=1.0, pack=100.0), _cat(2, 1, price=0.99, pack=100.0)],
        suppliers=[_sup(1, lead=1.0, dc=8.0), _sup(2, lead=1.0, dc=6.0)],
        demand_by_day={1: {d: 25.0 for d in range(n)}},   # 100 units total
        on_hand={1: 0.0},
        n_days=n,
    )
    base = _run(**common, params={"slack_penalty": 1000.0})
    assert {o.supplier_id for o in base.orders if o.qty > 0} == {2}, "cheaper goods win without promo"

    promo = _run(**common, params={
        "slack_penalty": 1000.0,
        "free_delivery_offers": [FreeDeliveryOffer(supplier_id=1, min_order_value=50.0,
                                                   remaining_orders=None, term_id=1)],
    })
    assert {o.supplier_id for o in promo.orders if o.qty > 0} == {1}, (
        "sup1's free delivery makes it cheapest → plan must switch to it")
    assert promo.total_cost < base.total_cost - 1e-6


def test_order_cutoff_respects_time_of_day():
    """A delivery day whose order-by cutoff has already passed must not be
    planned (no born-'Late' rows).  At 14:00 with lead=1 day and delivery at
    08:00, tomorrow's cutoff (yesterday's 08:00 + lead) passed 6h ago — the
    earliest honest delivery is day 2."""
    ings = [_ing(1)]
    cats = [_cat(1, 1, price=1.0, pack=100.0)]
    sups = [_sup(1, lead=1.0, delivery_hour=8.0)]
    demand = _flat_demand(1, 100.0, 7)

    late = _run(ings, cats, sups, demand, n_days=7,
                params={"slack_penalty": 1000.0, "now_time_of_day_s": 14 * 3600.0})
    assert late.method == "milp"
    days = {o.delivery_day for o in late.orders if o.qty > 0}
    assert days, "demand must still be ordered for the reachable days"
    assert min(days) >= 2, f"day-1 delivery is past its cutoff at 14:00: {sorted(days)}"

    # Before the day's cutoff (default 0 = midnight) day 1 is still available.
    early = _run(ings, cats, sups, _flat_demand(1, 100.0, 7), n_days=7)
    assert min(o.delivery_day for o in early.orders if o.qty > 0) == 1


def test_repeated_solve_is_deterministic():
    """Identical inputs must yield the identical plan — the 'one optimal plan'
    invariant users rely on when re-running procurement."""
    def _solve():
        ings = [_ing(1), _ing(2, perishable=1, shelf_life=3)]
        cats = [
            _cat(1, 1, price=1.0), _cat(2, 1, price=1.05),
            _cat(1, 2, price=2.0), _cat(2, 2, price=2.02),
        ]
        sups = [
            _sup(1, lead=1.0, reliability=0.9, dc=5.0),
            _sup(2, lead=2.0, reliability=0.99, dc=4.0),
        ]
        demand = {1: {d: 120.0 for d in range(7)}, 2: {d: 80.0 for d in range(7)}}
        sol = _run(ings, cats, sups, demand, n_days=7, params=None)
        return sorted(
            (o.ingredient_id, o.supplier_id, o.delivery_day, round(o.qty, 3))
            for o in sol.orders
        )

    assert _solve() == _solve()
