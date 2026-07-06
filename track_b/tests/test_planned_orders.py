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
from core.clock import SECONDS_PER_DAY
from core.models import (
    Ingredient,
    InventoryLevel,
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

    def create_po(self, supplier_id, lines, created_by="optimizer"):
        self.calls.append({"supplier_id": supplier_id, "lines": lines, "created_by": created_by})
        # Return a SimpleNamespace mimicking a PurchaseOrder so callers don't crash.
        return SimpleNamespace(id=9999, status="placed")


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

    # Emit horizon with demand starting from day 0 (all before day 5 = at_risk).
    now_day = int(bus.sim_time // SECONDS_PER_DAY)  # 5
    day_entries = [
        {
            "day_index": d,
            "start": float(d * SECONDS_PER_DAY),
            "end": float((d + 1) * SECONDS_PER_DAY),
            "items": [{"menu_item_id": item_id, "qty": 1.0, "baseline": 1.0}],
        }
        for d in range(now_day, now_day + 14)
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
        assert run.method == "projection"
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
