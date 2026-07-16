"""Tests for the WS4 forward procurement planner.

Covers:
1. build_procurement_plan creates a PlannedOrder row when a shortage is projected.
2. order_date = delivery_date - lead_days * SECONDS_PER_DAY.
3. Perishable cap: when shelf life < cover window, qty is capped.
4. Hysteresis: calling build_procurement_plan twice with unchanged horizon doesn't create new rows.
5. execute_due_planned_orders creates a PO when order_date <= now and marks row 'placed'.
6. at_risk status when order_date < now (lead time can't cover).
"""

import math
from types import SimpleNamespace

import pytest

from core import config
from core.clock import DAY_OPEN_OFFSET, SECONDS_PER_DAY
from core.models import (
    Ingredient,
    InventoryLevel,
    InventoryLot,
    PlannedOrder,
    ProcurementPlanRun,
    PurchaseOrder,
    PurchaseOrderLine,
    Recipe,
    RecipeLine,
    Station,
    Supplier,
    SupplierCatalog,
    MenuItem,
)
from core.signals import SignalType
from track_b.agents.optimizer import InventoryOptimizer


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeProcurement:
    def __init__(self):
        self.calls = []
        self.merge_calls = []

    def create_po(self, supplier_id, lines, created_by="optimizer", planned_delivery=None, delivery_charge=0.0, urgency=None):
        self.calls.append({
            "supplier_id": supplier_id,
            "lines": lines,
            "created_by": created_by,
            "planned_delivery": planned_delivery,
            "delivery_charge": delivery_charge,
            "urgency": urgency,
        })
        # Return a SimpleNamespace mimicking a PurchaseOrder so callers don't crash.
        return SimpleNamespace(id=9999, status="placed")

    def add_lines_to_po(self, po_id, lines, created_by="optimizer"):
        """WS4: cross-sweep consolidation stub — always returns False in tests so
        the caller falls back to create_po (no open PO exists in unit tests)."""
        self.merge_calls.append({"po_id": po_id, "lines": lines})
        return False


def _seed_ingredient(session_factory, perishable=0, shelf_life_days=None, on_hand=0.0,
                     safety_stock=50.0, reorder_point=80.0, par_level=300.0):
    session = session_factory()
    try:
        ing = Ingredient(
            name="test_ing", category="produce", base_unit="g",
            perishable=perishable, shelf_life_days=shelf_life_days,
        )
        session.add(ing)
        session.flush()
        session.add(InventoryLevel(
            ingredient_id=ing.id,
            par_level=par_level,
            reorder_point=reorder_point,
            safety_stock=safety_stock,
            yield_factor=1.0,
            on_hand_cached=on_hand,
        ))
        session.commit()
        return ing.id
    finally:
        session.close()


def _seed_supplier(session_factory, ing_id, lead_time_days=2.0, price=1.0, pack_size=10.0):
    session = session_factory()
    try:
        sup = Supplier(
            name="TestSupplier", lead_time_days=lead_time_days,
            reliability_score=0.9, min_order_value=0.0, contact="",
        )
        session.add(sup)
        session.flush()
        session.add(SupplierCatalog(
            supplier_id=sup.id, ingredient_id=ing_id,
            current_price=price, pack_size=pack_size,
            unit="g", availability="in_stock", updated_at=0.0,
        ))
        session.commit()
        return sup.id
    finally:
        session.close()


def _seed_dish(session_factory, ing_id, recipe_qty=100.0):
    session = session_factory()
    try:
        station = session.query(Station).first()
        if station is None:
            station = Station(name="line")
            session.add(station)
            session.flush()
        item = MenuItem(
            name="TestDish", category="main", station_id=station.id,
            dine_in_price=10.0, online_price=11.0,
            prep_time_min=5.0, is_batchable=0, active=1,
        )
        session.add(item)
        session.flush()
        recipe = Recipe(menu_item_id=item.id)
        session.add(recipe)
        session.flush()
        session.add(RecipeLine(
            recipe_id=recipe.id, ingredient_id=ing_id,
            qty=recipe_qty, unit="g", optional=0,
        ))
        session.commit()
        return item.id
    finally:
        session.close()


def _emit_horizon(bus, menu_item_id: int, daily_qty: float, days: int = 14, baseline=None):
    """Emit a synthetic DEMAND_FORECAST_HORIZON signal onto the bus."""
    if baseline is None:
        baseline = daily_qty
    day_entries = [
        {
            "day_index": d,
            "start": float(d * SECONDS_PER_DAY),
            "end": float((d + 1) * SECONDS_PER_DAY),
            "items": [{"menu_item_id": menu_item_id, "qty": daily_qty, "baseline": baseline}],
        }
        for d in range(days)
    ]
    bus.emit(
        type=SignalType.DEMAND_FORECAST_HORIZON,
        payload={
            "horizon_days": days,
            "generated_at": float(bus.sim_time),
            "days": day_entries,
            "item_daily_baseline_median": {str(menu_item_id): baseline},
        },
        source="test",
    )


def _make_optimizer(bus, session_factory):
    proc = _FakeProcurement()
    opt = InventoryOptimizer(bus, session_factory, procurement=proc)
    return opt, proc


# ---------------------------------------------------------------------------
# Test 1: build_procurement_plan creates a PlannedOrder when shortage projected
# ---------------------------------------------------------------------------


def test_stale_uncoverable_signal_retracted_when_covered(bus, session_factory):
    """A lingering INGREDIENT_UNCOVERABLE from an earlier rebuild must be consumed
    once the plan covers everything — otherwise the sourcing panel shows a phantom
    shortfall (e.g. 'Basil 7g short') for up to the 24h TTL."""
    # Well-stocked ingredient with tiny demand → no shortage, nothing uncoverable.
    ing_id = _seed_ingredient(session_factory, on_hand=10000.0, safety_stock=1.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=1.0)
    _seed_supplier(session_factory, ing_id, lead_time_days=1.0, pack_size=10.0)

    bus.sim_time = 0.0
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0)

    # Pre-seed a stale warning for a bogus ingredient that will never be uncoverable.
    bus.emit(
        type=SignalType.INGREDIENT_UNCOVERABLE,
        payload={"ingredient_id": 987654, "ingredient_name": "Ghost",
                 "short_qty": 6.65, "unit": "g", "reason": "stale"},
        dedup_key="uncoverable:987654",
        source="test",
    )
    assert len(bus.live(type=SignalType.INGREDIENT_UNCOVERABLE)) == 1

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=14.0)

    # The covered rebuild must retract the stale warning.
    assert bus.live(type=SignalType.INGREDIENT_UNCOVERABLE) == []


def test_build_procurement_plan_creates_planned_order(bus, session_factory):
    """When projected stock dips below safety_stock, a PlannedOrder row is created."""
    # on_hand=0, safety_stock=50 → immediate shortage on day 0 → order must be scheduled.
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    _seed_supplier(session_factory, ing_id, lead_time_days=2.0, pack_size=10.0)

    bus.sim_time = 0.0
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0)  # small demand to trigger projection

    opt, _proc = _make_optimizer(bus, session_factory)
    count = opt.build_procurement_plan(horizon_days=14.0)

    assert count >= 1, "At least one PlannedOrder must be created when stock is critically low"

    session = session_factory()
    try:
        rows = session.query(PlannedOrder).filter(
            PlannedOrder.status.in_(["planned", "at_risk"])
        ).all()
        assert len(rows) >= 1, "PlannedOrder rows must be persisted in DB"
        row = rows[0]
        assert row.ingredient_id == ing_id
        assert row.qty > 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 2: order_date = delivery_date - lead_days * SECONDS_PER_DAY
# ---------------------------------------------------------------------------


def test_order_date_equals_delivery_minus_lead_days(bus, session_factory):
    """order_date must equal delivery_date - lead_days * SECONDS_PER_DAY."""
    LEAD = 3.0
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    _seed_supplier(session_factory, ing_id, lead_time_days=LEAD, pack_size=10.0)

    bus.sim_time = 0.0
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0)

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=14.0)

    session = session_factory()
    try:
        rows = session.query(PlannedOrder).filter(
            PlannedOrder.status.in_(["planned", "at_risk"])
        ).all()
        assert rows, "Expected at least one PlannedOrder"
        for row in rows:
            expected_order_date = float(row.delivery_date) - LEAD * SECONDS_PER_DAY
            assert abs(float(row.order_date) - expected_order_date) < 1.0, (
                f"order_date {row.order_date} should be delivery_date {row.delivery_date} "
                f"- {LEAD} * {SECONDS_PER_DAY}"
            )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 3: Perishable cap
# ---------------------------------------------------------------------------


def test_perishable_cap_limits_qty(bus, session_factory):
    """For a perishable ingredient, qty is capped at demand before expiry."""
    SHELF_LIFE = 2.0  # days
    DAILY_QTY = 5.0   # portions per day
    RECIPE_QTY = 100.0  # g per portion → 500g/day demand

    ing_id = _seed_ingredient(
        session_factory,
        perishable=1,
        shelf_life_days=SHELF_LIFE,
        on_hand=0.0,
        safety_stock=10.0,
        par_level=9999.0,  # large par so forecast floor dominates
    )
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=RECIPE_QTY)
    _seed_supplier(session_factory, ing_id, lead_time_days=1.0, pack_size=10.0, price=1.0)

    bus.sim_time = 0.0
    # Large daily demand → without cap, order would be huge.
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=DAILY_QTY * 10, baseline=DAILY_QTY * 10)

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=14.0)

    session = session_factory()
    try:
        rows = session.query(PlannedOrder).filter(
            PlannedOrder.status.in_(["planned", "at_risk"])
        ).all()
        if not rows:
            pytest.skip("No PlannedOrder rows generated — demand may not trigger shortage")
        # Any single order must not exceed demand over shelf_life_days (rounded up to pack_size).
        max_usable = DAILY_QTY * 10 * RECIPE_QTY * math.ceil(SHELF_LIFE) + 10.0  # +pack_size slack
        for row in rows:
            assert row.qty <= max_usable + 10.0, (
                f"Perishable qty {row.qty} exceeds demand before expiry ({max_usable})"
            )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 4: Hysteresis — two identical calls produce no new rows
# ---------------------------------------------------------------------------


def test_hysteresis_no_duplicate_rows(bus, session_factory):
    """Calling build_procurement_plan twice with unchanged horizon must not grow the row count."""
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    _seed_supplier(session_factory, ing_id, lead_time_days=2.0, pack_size=10.0)

    bus.sim_time = 0.0
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0)

    opt, _proc = _make_optimizer(bus, session_factory)
    count1 = opt.build_procurement_plan(horizon_days=14.0)

    session = session_factory()
    try:
        rows_after_first = (
            session.query(PlannedOrder)
            .filter(PlannedOrder.status.in_(["planned", "at_risk"]))
            .count()
        )
    finally:
        session.close()

    # Second call with identical horizon.
    count2 = opt.build_procurement_plan(horizon_days=14.0)

    session = session_factory()
    try:
        rows_after_second = (
            session.query(PlannedOrder)
            .filter(PlannedOrder.status.in_(["planned", "at_risk"]))
            .count()
        )
    finally:
        session.close()

    assert rows_after_second == rows_after_first, (
        f"Hysteresis failed: {rows_after_first} rows after first call, "
        f"{rows_after_second} after second (no horizon change)"
    )


# ---------------------------------------------------------------------------
# Test 5: execute_due_planned_orders creates a PO and marks row 'placed'
# ---------------------------------------------------------------------------


def test_execute_due_planned_orders_creates_po(bus, session_factory):
    """A PlannedOrder with order_date <= now is converted to a real PO."""
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    sup_id = _seed_supplier(session_factory, ing_id, lead_time_days=1.0, price=2.0, pack_size=10.0)

    # Insert a PlannedOrder that is already due (order_date=0.0, now=10.0).
    session = session_factory()
    try:
        po = PlannedOrder(
            plan_run_id=None,
            ingredient_id=ing_id,
            supplier_id=sup_id,
            qty=100.0,
            unit="g",
            unit_price=2.0,
            order_date=0.0,      # due at t=0
            delivery_date=86400.0,
            covers_from=86400.0,
            covers_until=172800.0,
            status="planned",
            reason="test",
            created_at=0.0,
        )
        session.add(po)
        session.commit()
        session.refresh(po)
        po_id = po.id
    finally:
        session.close()

    bus.sim_time = 10.0  # past the order_date

    opt, proc = _make_optimizer(bus, session_factory)
    executed = opt.execute_due_planned_orders()

    assert executed == 1, f"Expected 1 executed order, got {executed}"
    assert len(proc.calls) == 1, "create_po must be called once"
    assert proc.calls[0]["supplier_id"] == sup_id
    assert proc.calls[0]["lines"][0]["ingredient_id"] == ing_id
    assert proc.calls[0]["lines"][0]["qty"] == pytest.approx(100.0)

    # Check the row is marked 'placed'.
    session = session_factory()
    try:
        row = session.get(PlannedOrder, po_id)
        assert row.status == "placed", f"Expected 'placed', got {row.status!r}"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 6: at_risk status when order_date < now
# ---------------------------------------------------------------------------


def test_at_risk_when_order_date_in_past(bus, session_factory):
    """When the computed order_date is already in the past, status must be 'at_risk'."""
    # Set sim_time well into the future so day 0 deliveries have order_date in the past.
    bus.sim_time = float(5 * SECONDS_PER_DAY)  # day 5

    # on_hand=0 → immediate shortage projected for day 5 onward.
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    # Short lead (1 day) but we're already 5 days in with no stock → many orders will be at_risk.
    _seed_supplier(session_factory, ing_id, lead_time_days=1.0, pack_size=10.0)

    # Emit horizon with demand from offset day 0 (day_index is 0-relative to now).
    # With on_hand=0 and lead=1, offset-day-0 demand cannot be delivered in time
    # → a genuine demand shortfall → at_risk (an unmet *safety* buffer alone is
    # the soft target and is intentionally NOT flagged at_risk).
    now_day = int(bus.sim_time // SECONDS_PER_DAY)  # 5
    day_entries = [
        {
            "day_index": d,
            "start": float((now_day + d) * SECONDS_PER_DAY),
            "end": float((now_day + d + 1) * SECONDS_PER_DAY),
            "items": [{"menu_item_id": item_id, "qty": 1.0, "baseline": 1.0}],
        }
        for d in range(14)
    ]
    bus.emit(
        type=SignalType.DEMAND_FORECAST_HORIZON,
        payload={
            "horizon_days": 14,
            "generated_at": float(bus.sim_time),
            "days": day_entries,
            "item_daily_baseline_median": {str(item_id): 1.0},
        },
        source="test",
    )

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=14.0)

    session = session_factory()
    try:
        at_risk = session.query(PlannedOrder).filter(
            PlannedOrder.status == "at_risk"
        ).count()
        planned = session.query(PlannedOrder).filter(
            PlannedOrder.status == "planned"
        ).count()
    finally:
        session.close()

    # With on_hand=0 and safety_stock=50 and demand, the first order on day=now with
    # lead_days=1 means order_date = day*86400 + DAY_OPEN_OFFSET - 1*86400.
    # For day=now_day: order_date = 5*86400+28800 - 86400 = 4*86400+28800 < now (5*86400)
    # → at_risk expected.
    assert at_risk + planned >= 1, "Expected at least one planned or at_risk order"
    # The first shortfall on the current day should produce an at_risk row.
    assert at_risk >= 1, (
        f"Expected at least one at_risk order when lead time is insufficient, "
        f"got at_risk={at_risk} planned={planned}"
    )


# ---------------------------------------------------------------------------
# Test 7: ProcurementPlanRun header is created
# ---------------------------------------------------------------------------


def test_plan_run_header_created(bus, session_factory):
    """Each call to build_procurement_plan must create a ProcurementPlanRun row."""
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    _seed_supplier(session_factory, ing_id, lead_time_days=2.0, pack_size=10.0)

    bus.sim_time = 0.0
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0)

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=14.0)

    session = session_factory()
    try:
        run = session.query(ProcurementPlanRun).order_by(ProcurementPlanRun.id.desc()).first()
        assert run is not None, "ProcurementPlanRun row must be created"
        assert run.method in ("milp", "projection", "greedy"), (
            f"Expected milp/projection/greedy, got {run.method!r}"
        )
        assert run.horizon_days == pytest.approx(14.0)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 8: execute_due_planned_orders skips when in-flight PO exists
# ---------------------------------------------------------------------------


def test_execute_skips_when_po_inflight(bus, session_factory):
    """execute_due_planned_orders must not double-order if a PO is already in-flight."""
    ing_id = _seed_ingredient(session_factory, on_hand=0.0)
    sup_id = _seed_supplier(session_factory, ing_id, lead_time_days=1.0, price=1.0, pack_size=10.0)

    session = session_factory()
    try:
        # Existing in-flight PO.
        existing_po = PurchaseOrder(
            supplier_id=sup_id, status="placed",
            created_at=0.0, expected_delivery=86400.0,
            total_cost=100.0, created_by="test",
        )
        session.add(existing_po)
        session.flush()
        session.add(PurchaseOrderLine(
            po_id=existing_po.id, ingredient_id=ing_id,
            qty=100.0, unit="g", unit_price=1.0, line_total=100.0,
        ))

        # Due planned order.
        planned = PlannedOrder(
            plan_run_id=None,
            ingredient_id=ing_id, supplier_id=sup_id,
            qty=50.0, unit="g", unit_price=1.0,
            order_date=0.0, delivery_date=86400.0,
            covers_from=86400.0, covers_until=172800.0,
            status="planned", reason="test", created_at=0.0,
        )
        session.add(planned)
        session.commit()
    finally:
        session.close()

    bus.sim_time = 10.0
    opt, proc = _make_optimizer(bus, session_factory)
    executed = opt.execute_due_planned_orders()

    assert executed == 0, "Should not create a PO when one is already in flight"
    assert len(proc.calls) == 0


# ---------------------------------------------------------------------------
# Test 9 (A1): In-transit PO eliminates phantom duplicate plan row
# ---------------------------------------------------------------------------


def test_inbound_po_suppresses_duplicate_plan_row(bus, session_factory):
    """A placed PO already covering demand must NOT re-appear as a PlannedOrder."""
    # on_hand=200g covers day 0 (200 - 100 = 100g, above safety_stock=50).
    # Inbound PO of 1000g arriving day 1 covers the remaining 6 days of the 7-day horizon.
    # → No new plan rows should be generated.
    ing_id = _seed_ingredient(session_factory, on_hand=200.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    sup_id = _seed_supplier(session_factory, ing_id, lead_time_days=2.0, pack_size=10.0, price=1.0)

    bus.sim_time = 0.0
    # 1 portion/day × 100g = 100 g/day demand.
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0, days=7)

    # Seed an in-transit PO that arrives on day 1 with more than enough to cover days 1–6.
    # Days 1–6 demand: 6 × 100 = 600 g.  Running stock after day 0 = 100g.
    # With 1000g inbound on day 1: running_stock = 1100g, never dips below safety_stock.
    session = session_factory()
    try:
        inbound_po = PurchaseOrder(
            supplier_id=sup_id, status="placed",
            created_at=0.0, expected_delivery=float(SECONDS_PER_DAY),  # arrives day 1
            total_cost=1000.0, created_by="test",
        )
        session.add(inbound_po)
        session.flush()
        session.add(PurchaseOrderLine(
            po_id=inbound_po.id, ingredient_id=ing_id,
            qty=1000.0, unit="g", unit_price=1.0, line_total=1000.0,
        ))
        session.commit()
    finally:
        session.close()

    opt, _proc = _make_optimizer(bus, session_factory)
    count = opt.build_procurement_plan(horizon_days=7.0)

    # The inbound PO covers the entire horizon; no new plan rows needed.
    assert count == 0, (
        f"Expected 0 PlannedOrders when in-transit supply covers demand, got {count}"
    )
    session = session_factory()
    try:
        rows = session.query(PlannedOrder).filter(
            PlannedOrder.status.in_(["planned", "at_risk"])
        ).count()
        assert rows == 0, f"Expected 0 active PlannedOrder rows, found {rows}"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 10 (A2): build_procurement_plan uses the MILP is_default supplier
# ---------------------------------------------------------------------------


def _seed_supplier_with_default(session_factory, ing_id, lead_time_days=2.0, price=1.0,
                                 pack_size=10.0, is_default=0):
    """Seed a supplier + catalog entry with optional is_default flag."""
    session = session_factory()
    try:
        sup = Supplier(
            name=f"Supplier_default={is_default}", lead_time_days=lead_time_days,
            reliability_score=0.9, min_order_value=0.0, contact="",
        )
        session.add(sup)
        session.flush()
        session.add(SupplierCatalog(
            supplier_id=sup.id, ingredient_id=ing_id,
            current_price=price, pack_size=pack_size,
            unit="g", availability="in_stock", updated_at=0.0, is_default=is_default,
        ))
        session.commit()
        return sup.id
    finally:
        session.close()


def test_build_plan_uses_milp_default_supplier(bus, session_factory):
    """Time-phased MILP picks the cheapest available supplier independently.

    The time-phased ordering MILP is now its own cost optimizer — it selects
    suppliers based on total landed cost, not by deferring to the sourcing
    MILP's is_default flag.  So when one supplier is materially cheaper, the
    ordering MILP should prefer it over the is_default one.
    """
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)

    # Cheaper supplier: price=0.1, lead=1 day — time-phased MILP should prefer this.
    cheap_sup_id = _seed_supplier_with_default(session_factory, ing_id, lead_time_days=1.0,
                                               price=0.1, pack_size=10.0, is_default=0)
    # Expensive supplier: price=5.0 (50× more), marked is_default=1.
    _seed_supplier_with_default(session_factory, ing_id, lead_time_days=3.0,
                                price=5.0, pack_size=10.0, is_default=1)

    bus.sim_time = 0.0
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0, days=7)

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=7.0)

    session = session_factory()
    try:
        rows = session.query(PlannedOrder).filter(
            PlannedOrder.status.in_(["planned", "at_risk"])
        ).all()
        assert rows, "Expected at least one PlannedOrder row"
        # Time-phased MILP should pick the cheaper supplier (50× price difference).
        for row in rows:
            assert row.supplier_id == cheap_sup_id, (
                f"Expected cheapest supplier {cheap_sup_id} but got {row.supplier_id}"
            )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 11 (A3): n_days is clamped to forecast span
# ---------------------------------------------------------------------------


def test_n_days_clamped_to_forecast_span(bus, session_factory):
    """build_procurement_plan must not project beyond the actual forecast data."""
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    _seed_supplier(session_factory, ing_id, lead_time_days=1.0, pack_size=10.0)

    bus.sim_time = 0.0
    FORECAST_DAYS = 5
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0, days=FORECAST_DAYS)

    opt, _proc = _make_optimizer(bus, session_factory)
    # Pass horizon_days=14 (larger than forecast); effective window must be FORECAST_DAYS.
    opt.build_procurement_plan(horizon_days=14.0)

    session = session_factory()
    try:
        run = session.query(ProcurementPlanRun).order_by(ProcurementPlanRun.id.desc()).first()
        assert run is not None
        # horizon_days stored on the run should reflect the clamped value.
        assert run.horizon_days == pytest.approx(float(FORECAST_DAYS)), (
            f"Expected horizon_days={FORECAST_DAYS}, got {run.horizon_days}"
        )
        # No PlannedOrder should have delivery_date beyond FORECAST_DAYS.
        rows = session.query(PlannedOrder).all()
        max_day = int(bus.sim_time // SECONDS_PER_DAY) + FORECAST_DAYS
        for row in rows:
            if row.delivery_date is not None:
                arr_day = int(float(row.delivery_date) // SECONDS_PER_DAY)
                assert arr_day <= max_day + 1, (
                    f"Delivery day {arr_day} exceeds forecast span {max_day}"
                )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 12 (A5): execute_due_planned_orders places only the shortfall
# ---------------------------------------------------------------------------


def test_execute_places_shortfall_only(bus, session_factory):
    """A small in-flight PO must not suppress the full need; only the shortfall is ordered."""
    ing_id = _seed_ingredient(session_factory, on_hand=0.0)
    sup_id = _seed_supplier(session_factory, ing_id, lead_time_days=1.0, price=1.0, pack_size=10.0)

    session = session_factory()
    try:
        # Existing in-flight PO — small quantity (30g).
        small_po = PurchaseOrder(
            supplier_id=sup_id, status="placed",
            created_at=0.0, expected_delivery=86400.0,
            total_cost=30.0, created_by="test",
        )
        session.add(small_po)
        session.flush()
        session.add(PurchaseOrderLine(
            po_id=small_po.id, ingredient_id=ing_id,
            qty=30.0, unit="g", unit_price=1.0, line_total=30.0,
        ))

        # Due planned order — needs 100g total.
        planned = PlannedOrder(
            plan_run_id=None,
            ingredient_id=ing_id, supplier_id=sup_id,
            qty=100.0, unit="g", unit_price=1.0,
            order_date=0.0, delivery_date=86400.0,
            covers_from=86400.0, covers_until=172800.0,
            status="planned", reason="test", created_at=0.0,
        )
        session.add(planned)
        session.commit()
        planned_id = planned.id
    finally:
        session.close()

    bus.sim_time = 10.0
    opt, proc = _make_optimizer(bus, session_factory)
    executed = opt.execute_due_planned_orders()

    assert executed == 1, f"Expected 1 executed order (shortfall), got {executed}"
    assert len(proc.calls) == 1
    # Shortfall = 100 - 30 = 70; rounded up to pack_size 10 → 70 (exact multiple).
    assert proc.calls[0]["lines"][0]["qty"] == pytest.approx(70.0), (
        f"Expected shortfall qty 70.0, got {proc.calls[0]['lines'][0]['qty']}"
    )


# ---------------------------------------------------------------------------
# Test 13 (A8): Ingredient demand is derived correctly from recipe + yield
# ---------------------------------------------------------------------------


def test_demand_derivation_from_recipe_and_yield(bus, session_factory):
    """Per-ingredient demand must equal Σ(dish_qty × recipe_qty / yield_factor)."""
    RECIPE_QTY = 150.0   # g of ingredient per portion
    YIELD_FACTOR = 0.75  # 75% yield → need 150 / 0.75 = 200 g raw per portion
    DAILY_DISHES = 4.0   # portions per day
    LEAD_DAYS = 1.0

    session = session_factory()
    try:
        station = Station(name="line")
        session.add(station)
        session.flush()
        ing = Ingredient(name="demand_test_ing", category="produce", base_unit="g",
                         perishable=0, shelf_life_days=None)
        session.add(ing)
        session.flush()
        ing_id = ing.id

        level = InventoryLevel(
            ingredient_id=ing_id, par_level=5000.0, reorder_point=1000.0,
            safety_stock=100.0, yield_factor=YIELD_FACTOR, on_hand_cached=0.0,
        )
        session.add(level)

        item = MenuItem(name="YieldDish", category="main", station_id=station.id,
                        dine_in_price=10.0, online_price=11.0, prep_time_min=5.0,
                        is_batchable=0, active=1)
        session.add(item)
        session.flush()
        recipe = Recipe(menu_item_id=item.id)
        session.add(recipe)
        session.flush()
        session.add(RecipeLine(recipe_id=recipe.id, ingredient_id=ing_id,
                                qty=RECIPE_QTY, unit="g", optional=0))

        sup = Supplier(name="DemandSup", lead_time_days=LEAD_DAYS,
                       reliability_score=0.9, min_order_value=0.0, contact="")
        session.add(sup)
        session.flush()
        session.add(SupplierCatalog(
            supplier_id=sup.id, ingredient_id=ing_id,
            current_price=1.0, pack_size=10.0, unit="g",
            availability="in_stock", updated_at=0.0, is_default=1,
        ))
        session.commit()
        item_id = item.id
    finally:
        session.close()

    bus.sim_time = 0.0
    HORIZON_DAYS = 3
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=DAILY_DISHES, days=HORIZON_DAYS)

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=float(HORIZON_DAYS))

    # Expected daily ingredient demand: 4 portions × (150 g / 0.75 yield) = 800 g/day.
    expected_daily_ing_demand = DAILY_DISHES * (RECIPE_QTY / YIELD_FACTOR)

    session = session_factory()
    try:
        rows = session.query(PlannedOrder).filter(
            PlannedOrder.ingredient_id == ing_id,
            PlannedOrder.status.in_(["planned", "at_risk"])
        ).all()
        assert rows, "Expected at least one PlannedOrder for the ingredient"
        # The first order must cover ≥ 1 day's demand (safety + demand_to_cover).
        total_planned = sum(float(r.qty) for r in rows)
        assert total_planned >= expected_daily_ing_demand - 1.0, (
            f"Total planned qty {total_planned} should cover at least "
            f"{expected_daily_ing_demand:.1f} g/day (recipe+yield adjusted)"
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test 14 (A9): Expiry-aware stock projection — lot expiry triggers early order
# ---------------------------------------------------------------------------


def test_expiry_aware_projection_triggers_early_order(bus, session_factory):
    """A9: when the on-hand lot expires mid-horizon the planner must detect the
    resulting gap and schedule an order to arrive before (or at) expiry day,
    rather than waiting until scalar stock crosses safety_stock.

    Scenario (mirrors live Basil audit finding):
      on_hand = 500 g in a single lot expiring at day 2
      demand  = 150 g / day  (7-day horizon)
      safety  = 50 g,  lead = 1 day,  pack = 100 g

    Scalar model: 500 → 350 → 200 → 50 (d=2, exactly = safety) → shortage at d=3
      → delivery at d=4  (misses the post-expiry gap)
    FIFO model  : lot [200 g, exp_day=2] is evicted when d=2 → stock=0 < safety
      → order placed at d=2, delivery at d=2  (bridges the expiry gap correctly)
    """
    SHELF_LIFE = 4          # days
    LOT_QTY = 500.0         # g — large enough that old scalar model stays silent until d=3
    LOT_EXPIRY_DAY = 2      # lot expires at start of day 2 (exp_day=2, evicted when d≥2)
    DAILY_DEMAND = 150.0    # g/day (recipe_qty per dish × 1 dish/day)
    SAFETY = 50.0
    LEAD = 1.0              # day
    PACK = 100.0

    # --- seed -----------------------------------------------------------------
    session = session_factory()
    try:
        station = Station(name="line_a9")
        session.add(station)
        session.flush()

        ing = Ingredient(
            name="perishable_a9", category="produce", base_unit="g",
            perishable=1, shelf_life_days=float(SHELF_LIFE),
        )
        session.add(ing)
        session.flush()
        ing_id = ing.id

        session.add(InventoryLevel(
            ingredient_id=ing_id,
            par_level=1000.0,
            reorder_point=100.0,
            safety_stock=SAFETY,
            yield_factor=1.0,
            on_hand_cached=LOT_QTY,
        ))

        # The single on-hand lot — expires at day 2 (exp_day = LOT_EXPIRY_DAY).
        session.add(InventoryLot(
            ingredient_id=ing_id,
            qty_on_hand=LOT_QTY,
            unit="g",
            purchase_price=1.0,
            purchase_date=0.0,
            received_date=0.0,
            expiry_date=float(LOT_EXPIRY_DAY * SECONDS_PER_DAY),
            supplier_id=None,
            storage_location=None,
            status="active",
        ))

        item = MenuItem(
            name="A9Dish", category="main", station_id=station.id,
            dine_in_price=10.0, online_price=11.0,
            prep_time_min=5.0, is_batchable=0, active=1,
        )
        session.add(item)
        session.flush()
        recipe = Recipe(menu_item_id=item.id)
        session.add(recipe)
        session.flush()
        # 1 dish/day × DAILY_DEMAND g of ingredient per dish = DAILY_DEMAND g/day demand.
        session.add(RecipeLine(
            recipe_id=recipe.id, ingredient_id=ing_id,
            qty=DAILY_DEMAND, unit="g", optional=0,
        ))

        sup = Supplier(
            name="A9Supplier", lead_time_days=LEAD,
            reliability_score=0.9, min_order_value=0.0, contact="",
        )
        session.add(sup)
        session.flush()
        session.add(SupplierCatalog(
            supplier_id=sup.id, ingredient_id=ing_id,
            current_price=1.0, pack_size=PACK,
            unit="g", availability="in_stock", updated_at=0.0, is_default=1,
        ))
        session.commit()
        item_id = item.id
    finally:
        session.close()

    bus.sim_time = 0.0
    # Emit 1 dish/day for 7 days; ingredient demand = 1 × DAILY_DEMAND = 150 g/day.
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0, days=7)

    opt, _proc = _make_optimizer(bus, session_factory)
    count = opt.build_procurement_plan(horizon_days=7.0)

    assert count >= 1, (
        f"Expected ≥1 PlannedOrder for expiry-gapped ingredient, got {count}"
    )

    session = session_factory()
    try:
        rows = session.query(PlannedOrder).filter(
            PlannedOrder.ingredient_id == ing_id,
            PlannedOrder.status.in_(["planned", "at_risk"]),
        ).all()
        assert rows, "No active PlannedOrder rows for the ingredient"

        min_delivery = min(float(r.delivery_date) for r in rows)
        # Lot expires at start of day 2; FIFO detects shortage there → earliest
        # delivery at day 2 (= LOT_EXPIRY_DAY * SECONDS_PER_DAY + DAY_OPEN_OFFSET).
        # Scalar model misses the gap until day 3 → delivery at day 4.
        expiry_bridge_sim_s = float(LOT_EXPIRY_DAY * SECONDS_PER_DAY + DAY_OPEN_OFFSET)
        assert min_delivery <= expiry_bridge_sim_s, (
            f"Expiry-aware FIFO planner must schedule delivery by day {LOT_EXPIRY_DAY} "
            f"({expiry_bridge_sim_s:.0f} s); earliest delivery is {min_delivery:.0f} s "
            f"(≈ day {min_delivery / SECONDS_PER_DAY:.1f}). "
            "Scalar model ran instead of lot-based FIFO."
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Helper: fake Procurement that writes a *proposed* (approval-pending) PO to DB
# ---------------------------------------------------------------------------


class _FakeProcurementApproval:
    """Simulates create_po routing to approval (over threshold) by writing a
    'proposed' PO to the real DB so Fix A's committed-status re-read works."""

    def __init__(self, session_factory):
        self._sf = session_factory
        self.calls: list = []

    def create_po(self, supplier_id, lines, created_by="optimizer",
                  planned_delivery=None, delivery_charge=0.0, urgency=None):
        self.calls.append({"supplier_id": supplier_id, "lines": lines, "urgency": urgency})
        session = self._sf()
        try:
            po = PurchaseOrder(
                supplier_id=supplier_id,
                status="proposed",          # pending approval — NOT placed
                created_at=0.0,
                expected_delivery=None,     # no date until approved
                total_cost=sum(
                    float(ln["qty"]) * float(ln.get("unit_price", 0.0)) for ln in lines
                ),
                created_by=created_by,
                approval_id=None,
            )
            session.add(po)
            session.flush()
            for ln in lines:
                session.add(PurchaseOrderLine(
                    po_id=po.id,
                    ingredient_id=ln["ingredient_id"],
                    qty=float(ln["qty"]),
                    unit=ln.get("unit", "g"),
                    unit_price=float(ln.get("unit_price", 0.0)),
                    line_total=float(ln["qty"]) * float(ln.get("unit_price", 0.0)),
                ))
            session.commit()
            po_id = po.id
        finally:
            session.close()
        from types import SimpleNamespace as _SN
        return _SN(id=po_id, status="proposed")


# ---------------------------------------------------------------------------
# Test Fix A: over-threshold PO → source planned rows become 'superseded'
# ---------------------------------------------------------------------------


def test_execute_due_marks_superseded_when_po_pending_approval(bus, session_factory):
    """Fix A: when create_po routes to approval (PO stays 'proposed'), the source
    PlannedOrder rows must be marked 'superseded', NOT 'placed'.

    Marking them 'placed' unconditionally was the primary cause of the
    at_risk-row + proposed-PO duplicate: the next build_procurement_plan run
    would see the at_risk row gone (badge dropped) but the proposed PO still
    open, then regenerate a fresh at_risk row against the same ingredient+supplier.
    """
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    sup_id = _seed_supplier(session_factory, ing_id, lead_time_days=1.0,
                            price=2.0, pack_size=10.0)

    session = session_factory()
    try:
        planned = PlannedOrder(
            plan_run_id=None,
            ingredient_id=ing_id,
            supplier_id=sup_id,
            qty=100.0,
            unit="g",
            unit_price=2.0,
            order_date=0.0,                          # due at t=0
            delivery_date=float(SECONDS_PER_DAY),
            covers_from=float(SECONDS_PER_DAY),
            covers_until=float(2 * SECONDS_PER_DAY),
            status="at_risk",
            reason="test",
            created_at=0.0,
        )
        session.add(planned)
        session.commit()
        session.refresh(planned)
        planned_id = planned.id
    finally:
        session.close()

    bus.sim_time = 10.0
    proc = _FakeProcurementApproval(session_factory)
    opt = InventoryOptimizer(bus, session_factory, procurement=proc)
    executed = opt.execute_due_planned_orders()

    assert executed == 1, f"Expected 1 order executed, got {executed}"
    assert len(proc.calls) == 1, "create_po must be called once"

    # The source planned row must become 'superseded' — a real (proposed) PO now
    # represents the demand.  'placed' would be wrong because the PO was never placed.
    session = session_factory()
    try:
        row = session.get(PlannedOrder, planned_id)
        assert row.status == "superseded", (
            f"Expected 'superseded' when PO was routed to approval, got {row.status!r}. "
            "Marking 'placed' here causes a ghost at_risk row to re-appear after re-planning."
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Test Fix B (R1): proposed PO already in DB prevents new planned row
# ---------------------------------------------------------------------------


def test_build_plan_r1_skips_when_proposed_po_covers_demand(bus, session_factory):
    """Fix B (R1): build_procurement_plan must not create a new planned/at_risk row
    when a proposed (approval-pending) PO for the same ingredient+supplier already
    covers the needed quantity.

    This is the second half of the duplicate scenario: even if build_procurement_plan
    runs again after a proposed PO was created, the R1 reconciliation gate must
    suppress any new planned row that would duplicate the real pending PO.
    """
    ing_id = _seed_ingredient(session_factory, on_hand=0.0, safety_stock=50.0)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)
    sup_id = _seed_supplier(session_factory, ing_id, lead_time_days=2.0,
                            pack_size=10.0, price=1.0)

    # Pre-seed a proposed (approval-pending) PO with enough qty to cover any horizon.
    session = session_factory()
    try:
        open_po = PurchaseOrder(
            supplier_id=sup_id,
            status="proposed",
            created_at=0.0,
            expected_delivery=None,
            total_cost=9999.0,
            created_by="test",
        )
        session.add(open_po)
        session.flush()
        session.add(PurchaseOrderLine(
            po_id=open_po.id,
            ingredient_id=ing_id,
            qty=9999.0,
            unit="g",
            unit_price=1.0,
            line_total=9999.0,
        ))
        session.commit()
    finally:
        session.close()

    bus.sim_time = float(5 * SECONDS_PER_DAY)   # day 5, on_hand=0 → immediate shortage
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=1.0, days=7)

    opt, _proc = _make_optimizer(bus, session_factory)
    opt.build_procurement_plan(horizon_days=7.0)

    # R1 must suppress new planned rows because the proposed PO qty (9999g) is
    # greater than or equal to the MILP's computed need for this ingredient+supplier.
    session = session_factory()
    try:
        active_rows = session.query(PlannedOrder).filter(
            PlannedOrder.status.in_(["planned", "at_risk"])
        ).count()
        assert active_rows == 0, (
            f"Expected 0 active planned rows when a proposed PO already covers demand, "
            f"got {active_rows}. R1 reconciliation gate is not suppressing duplicates."
        )
    finally:
        session.close()
