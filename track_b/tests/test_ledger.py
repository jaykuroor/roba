"""Tests for the Inventory Ledger agent (02 §B9 / §18.4)."""

from types import SimpleNamespace

import pytest

from core.models import (
    Ingredient,
    InventoryLedger,
    InventoryLevel,
    InventoryLot,
    MenuItem,
    PurchaseOrder,
    PurchaseOrderLine,
    Recipe,
    RecipeLine,
    Station,
    Supplier,
)
from core.signals import SignalType
from track_b.agents.ledger import InventoryLedger as Ledger


def _seed_recipe(session_factory, recipe_qty=100.0, par=500.0, reorder=200.0, safety=100.0):
    """One ingredient, one menu item with a recipe using it, and its level."""
    session = session_factory()
    try:
        station = Station(name="line")
        session.add(station)
        session.flush()

        ing = Ingredient(
            name="tomato", category="produce", base_unit="g",
            perishable=1, shelf_life_days=5.0,
        )
        session.add(ing)
        session.flush()

        item = MenuItem(
            name="Margherita", category="pizza", station_id=station.id,
            dine_in_price=12.0, online_price=14.0, prep_time_min=10.0,
            is_batchable=1, active=1,
        )
        session.add(item)
        session.flush()

        recipe = Recipe(menu_item_id=item.id)
        session.add(recipe)
        session.flush()
        session.add(RecipeLine(recipe_id=recipe.id, ingredient_id=ing.id, qty=recipe_qty, unit="g", optional=0))

        session.add(InventoryLevel(
            ingredient_id=ing.id, par_level=par, reorder_point=reorder,
            safety_stock=safety, yield_factor=1.0, on_hand_cached=0.0,
        ))
        session.commit()
        return ing.id, item.id
    finally:
        session.close()


def _add_lot(session_factory, ingredient_id, qty, expiry_date, purchase_price=1.0):
    """Add a lot and keep ``inventory_levels.on_hand_cached`` in lockstep, as a
    real receipt would (mirrors :meth:`InventoryLedger.receive`)."""
    session = session_factory()
    try:
        lot = InventoryLot(
            ingredient_id=ingredient_id, qty_on_hand=qty, unit="g",
            purchase_price=purchase_price, purchase_date=0.0, received_date=0.0,
            expiry_date=expiry_date, supplier_id=None, storage_location="main",
            status="active",
        )
        session.add(lot)
        session.flush()
        level = (
            session.query(InventoryLevel)
            .filter(InventoryLevel.ingredient_id == ingredient_id)
            .first()
        )
        balance_after = float(level.on_hand_cached or 0.0) + qty
        level.on_hand_cached = balance_after
        session.add(InventoryLedger(
            ingredient_id=ingredient_id, lot_id=lot.id, delta_qty=qty,
            reason="receipt", ref_id=lot.id, sim_time=0.0, balance_after=balance_after,
        ))
        session.commit()
        session.refresh(lot)
        return lot.id
    finally:
        session.close()


def _on_hand(session_factory, ingredient_id):
    session = session_factory()
    try:
        level = (
            session.query(InventoryLevel)
            .filter(InventoryLevel.ingredient_id == ingredient_id)
            .first()
        )
        return float(level.on_hand_cached or 0.0)
    finally:
        session.close()


def _ledger_sum(session_factory, ingredient_id):
    session = session_factory()
    try:
        rows = (
            session.query(InventoryLedger.delta_qty)
            .filter(InventoryLedger.ingredient_id == ingredient_id)
            .all()
        )
        return float(sum((r[0] or 0.0) for r in rows))
    finally:
        session.close()


@pytest.fixture
def ledger(bus, session_factory):
    return Ledger(bus, session_factory)


def test_fifo_depletion_across_two_lots(ledger, bus, session_factory):
    """Depleting more than the oldest lot holds spills FIFO into the next lot;
    ledger sum always equals on_hand_cached."""
    ing_id, item_id = _seed_recipe(session_factory, recipe_qty=100.0)
    _add_lot(session_factory, ing_id, qty=150.0, expiry_date=1000.0)   # oldest
    _add_lot(session_factory, ing_id, qty=300.0, expiry_date=5000.0)   # newer
    bus.sim_time = 10.0

    # Sell 2 units of the dish -> needs 200g, more than the oldest lot's 150g.
    line = SimpleNamespace(status="sold", menu_item_id=item_id, qty=2.0, id=1, sim_time=10.0)
    ledger.handle_order_line(line)

    on_hand = _on_hand(session_factory, ing_id)
    assert on_hand == pytest.approx(450.0 - 200.0)
    assert _ledger_sum(session_factory, ing_id) == pytest.approx(on_hand)

    session = session_factory()
    try:
        lots = session.query(InventoryLot).order_by(InventoryLot.expiry_date.asc()).all()
        assert lots[0].qty_on_hand == pytest.approx(0.0)
        assert lots[0].status == "depleted"
        assert lots[1].qty_on_hand == pytest.approx(250.0)
    finally:
        session.close()


def test_voided_line_not_depleted(ledger, session_factory):
    ing_id, item_id = _seed_recipe(session_factory)
    _add_lot(session_factory, ing_id, qty=100.0, expiry_date=1000.0)
    line = SimpleNamespace(status="voided", menu_item_id=item_id, qty=1.0, id=1, sim_time=10.0)
    ledger.handle_order_line(line)
    assert _on_hand(session_factory, ing_id) == pytest.approx(100.0)


def test_batch_decision_cook_depletes(ledger, bus, session_factory):
    ing_id, item_id = _seed_recipe(session_factory, recipe_qty=50.0)
    _add_lot(session_factory, ing_id, qty=200.0, expiry_date=1000.0)
    bus.sim_time = 20.0

    signal = SimpleNamespace(
        type=SignalType.BATCH_DECISION.value,
        payload={"menu_item_id": item_id, "qty": 2.0, "decision": "cook", "batch_definition_id": 7},
    )
    ledger.on_signal(signal)
    assert _on_hand(session_factory, ing_id) == pytest.approx(200.0 - 100.0)


def test_low_stock_then_stockout_signals(ledger, bus, session_factory):
    """Depleting below safety_stock emits LOW_STOCK; to <=0 emits STOCKOUT_RISK."""
    ing_id, item_id = _seed_recipe(session_factory, recipe_qty=100.0, safety=50.0)
    _add_lot(session_factory, ing_id, qty=120.0, expiry_date=1000.0)
    bus.sim_time = 10.0

    line = SimpleNamespace(status="sold", menu_item_id=item_id, qty=1.0, id=1, sim_time=10.0)
    ledger.handle_order_line(line)  # on_hand -> 20, below safety_stock(50)

    low_stock = bus.live(type=SignalType.LOW_STOCK)
    assert any(s.payload["ingredient_id"] == ing_id for s in low_stock)

    line2 = SimpleNamespace(status="sold", menu_item_id=item_id, qty=1.0, id=2, sim_time=11.0)
    ledger.handle_order_line(line2)  # on_hand -> negative -> stockout

    stockout = bus.live(type=SignalType.STOCKOUT_RISK)
    assert any(s.payload["ingredient_id"] == ing_id for s in stockout)
    assert item_id in stockout[0].payload["affected_items"]


def test_receive_creates_lot_and_ledger(ledger, bus, session_factory):
    ing_id, _item_id = _seed_recipe(session_factory)
    session = session_factory()
    try:
        supplier = Supplier(name="GreenFarm", lead_time_days=2.0, reliability_score=0.9, min_order_value=0.0, contact="")
        session.add(supplier)
        session.flush()
        po = PurchaseOrder(supplier_id=supplier.id, status="placed", created_at=0.0,
                            expected_delivery=100.0, total_cost=20.0, created_by="optimizer", approval_id=None)
        session.add(po)
        session.flush()
        session.add(PurchaseOrderLine(po_id=po.id, ingredient_id=ing_id, qty=20.0, unit="g", unit_price=1.0, line_total=20.0))
        session.commit()
        po_id = po.id
    finally:
        session.close()

    bus.sim_time = 100.0
    ledger.receive(po_id)

    assert _on_hand(session_factory, ing_id) == pytest.approx(20.0)
    session = session_factory()
    try:
        lots = session.query(InventoryLot).filter(InventoryLot.ingredient_id == ing_id).all()
        assert len(lots) == 1
        assert lots[0].qty_on_hand == pytest.approx(20.0)
    finally:
        session.close()


def test_scan_expiry_raises_risk_and_then_waste(ledger, bus, session_factory):
    ing_id, _item_id = _seed_recipe(session_factory)
    lot_id = _add_lot(session_factory, ing_id, qty=50.0, expiry_date=1000.0, purchase_price=2.0)

    bus.sim_time = 1000.0 - 1000.0  # well within the 2-day expiry window, not yet expired
    ledger.scan_expiry()
    risk = bus.live(type=SignalType.EXPIRY_RISK)
    assert any(s.payload["lot_id"] == lot_id for s in risk)

    bus.sim_time = 1000.0 + 1.0  # now expired
    ledger.scan_expiry()

    waste = bus.live(type=SignalType.WASTE_EVENT)
    assert any(s.payload.get("lot_id") == lot_id and s.payload.get("waste_type") == "expiry" for s in waste)
    assert _on_hand(session_factory, ing_id) == pytest.approx(0.0)

    session = session_factory()
    try:
        lot = session.get(InventoryLot, lot_id)
        assert lot.status == "expired"
    finally:
        session.close()
