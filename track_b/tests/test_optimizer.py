"""Tests for the Inventory Optimizer agent (02 §B9 / §18.8)."""

from types import SimpleNamespace

import pytest

from core import config
from core.models import (
    Ingredient,
    InventoryLevel,
    MenuItem,
    OrderLine,
    Promotion,
    Recipe,
    RecipeLine,
    Station,
    Supplier,
    SupplierCatalog,
)
from core.signals import SignalType
from track_b.agents.optimizer import InventoryOptimizer


class _FakeProcurement:
    def __init__(self):
        self.calls = []

    def create_po(self, supplier_id, lines, created_by="optimizer"):
        self.calls.append({"supplier_id": supplier_id, "lines": lines, "created_by": created_by})


class _FakeApprovals:
    def __init__(self):
        self.created = []

    def create(self, type, title, summary, payload=None, urgency="normal", ref_id=None):
        self.created.append({"type": type, "title": title, "ref_id": ref_id, "payload": payload})
        return SimpleNamespace(id=999)


def _seed_two_suppliers(session_factory, ingredient_id):
    session = session_factory()
    try:
        cheap = Supplier(name="Cheap Co", lead_time_days=3.0, reliability_score=0.8, min_order_value=0.0, contact="")
        pricey = Supplier(name="Fast Co", lead_time_days=1.0, reliability_score=0.9, min_order_value=0.0, contact="")
        session.add_all([cheap, pricey])
        session.flush()
        session.add_all([
            SupplierCatalog(supplier_id=cheap.id, ingredient_id=ingredient_id, current_price=1.0,
                             unit="g", pack_size=50.0, availability="in_stock", updated_at=0.0),
            SupplierCatalog(supplier_id=pricey.id, ingredient_id=ingredient_id, current_price=2.0,
                             unit="g", pack_size=50.0, availability="in_stock", updated_at=0.0),
        ])
        session.commit()
        return cheap.id, pricey.id
    finally:
        session.close()


def _seed_ingredient_and_level(session_factory, on_hand=50.0, reorder_point=100.0, par=500.0):
    session = session_factory()
    try:
        ing = Ingredient(name="tomato", category="produce", base_unit="g", perishable=1, shelf_life_days=5.0)
        session.add(ing)
        session.flush()
        session.add(InventoryLevel(
            ingredient_id=ing.id, par_level=par, reorder_point=reorder_point,
            safety_stock=50.0, yield_factor=1.0, on_hand_cached=on_hand,
        ))
        session.commit()
        return ing.id
    finally:
        session.close()


def _seed_dish(session_factory, ingredient_id, recipe_qty, price=10.0, name="Dish"):
    session = session_factory()
    try:
        station = session.query(Station).first()
        if station is None:
            station = Station(name="line")
            session.add(station)
            session.flush()
        item = MenuItem(name=name, category="main", station_id=station.id, dine_in_price=price,
                         online_price=price * 1.1, prep_time_min=5.0, is_batchable=0, active=1)
        session.add(item)
        session.flush()
        recipe = Recipe(menu_item_id=item.id)
        session.add(recipe)
        session.flush()
        session.add(RecipeLine(recipe_id=recipe.id, ingredient_id=ingredient_id, qty=recipe_qty, unit="g", optional=0))
        session.commit()
        return item.id
    finally:
        session.close()


@pytest.fixture
def optimizer_with_fakes(bus, session_factory):
    procurement = _FakeProcurement()
    approvals = _FakeApprovals()
    opt = InventoryOptimizer(bus, session_factory, procurement=procurement, approvals=approvals)
    return opt, procurement, approvals


def test_reorder_chooses_supplier_and_rounds_to_pack_size(optimizer_with_fakes, session_factory):
    opt, procurement, _approvals = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory, on_hand=50.0, reorder_point=100.0, par=240.0)
    _cheap_id, pricey_id = _seed_two_suppliers(session_factory, ing_id)

    opt._maybe_reorder(ing_id)

    assert len(procurement.calls) == 1
    call = procurement.calls[0]
    # score = availability_weight - price_norm - lead_norm (§18.8): the
    # pricier-but-much-faster supplier (lead 1d vs 3d) scores higher here.
    assert call["supplier_id"] == pricey_id
    # needed = 240 - 50 = 190, rounded up to pack_size 50 -> 200
    assert call["lines"][0]["qty"] == pytest.approx(200.0)


def test_reorder_skipped_when_above_reorder_point(optimizer_with_fakes, session_factory):
    opt, procurement, _ = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory, on_hand=500.0, reorder_point=100.0)
    _seed_two_suppliers(session_factory, ing_id)
    opt._maybe_reorder(ing_id)
    assert procurement.calls == []


def test_reorder_skips_when_all_suppliers_out(optimizer_with_fakes, session_factory):
    opt, procurement, _ = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory, on_hand=10.0, reorder_point=100.0)
    session = session_factory()
    try:
        sup = Supplier(name="OnlyOne", lead_time_days=2.0, reliability_score=0.9, min_order_value=0.0, contact="")
        session.add(sup)
        session.flush()
        session.add(SupplierCatalog(supplier_id=sup.id, ingredient_id=ing_id, current_price=1.0,
                                     unit="g", pack_size=10.0, availability="out", updated_at=0.0))
        session.commit()
    finally:
        session.close()
    opt._maybe_reorder(ing_id)
    assert procurement.calls == []


def test_toggle_disables_lowest_margin_velocity_dish(optimizer_with_fakes, session_factory, bus):
    opt, _procurement, _approvals = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory, on_hand=10.0, reorder_point=100.0)
    _seed_two_suppliers(session_factory, ing_id)  # gives a resupply_eta to compare against
    cheap_item = _seed_dish(session_factory, ing_id, recipe_qty=50.0, price=5.0, name="LowValue")
    rich_item = _seed_dish(session_factory, ing_id, recipe_qty=50.0, price=50.0, name="HighValue")

    # Make the rich item have nonzero sales (higher velocity*margin) so the
    # cheap item is selected for disabling.
    session = session_factory()
    try:
        session.add(OrderLine(order_id=None, menu_item_id=rich_item, qty=1.0, unit_price=50.0,
                               line_total=50.0, status="sold", sim_time=0.0))
        session.commit()
    finally:
        session.close()

    bus.sim_time = 10.0
    # projected_runout very soon, well inside the (long) resupply eta -> toggle fires.
    opt._maybe_toggle(ing_id, projected_runout=bus.sim_time + 10.0)

    session = session_factory()
    try:
        cheap = session.get(MenuItem, cheap_item)
        rich = session.get(MenuItem, rich_item)
    finally:
        session.close()
    assert cheap.active == 0
    assert rich.active == 1

    toggles = bus.live(type=SignalType.MENU_TOGGLE)
    assert any(s.payload["menu_item_id"] == cheap_item and s.payload["action"] == "disable" for s in toggles)


def test_toggle_skipped_when_single_dish_uses_ingredient(optimizer_with_fakes, session_factory, bus):
    opt, _procurement, _approvals = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory, on_hand=10.0, reorder_point=100.0)
    _seed_two_suppliers(session_factory, ing_id)
    only_item = _seed_dish(session_factory, ing_id, recipe_qty=50.0, price=5.0, name="OnlyDish")

    bus.sim_time = 10.0
    opt._maybe_toggle(ing_id, projected_runout=bus.sim_time + 10.0)

    session = session_factory()
    try:
        item = session.get(MenuItem, only_item)
    finally:
        session.close()
    assert item.active == 1


def test_expiry_risk_proposes_promo_and_approval(optimizer_with_fakes, session_factory, bus):
    opt, _procurement, approvals = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=20.0, price=8.0, name="Special")

    bus.sim_time = 5.0
    signal = SimpleNamespace(
        type=SignalType.EXPIRY_RISK.value,
        payload={"ingredient_id": ing_id, "lot_id": 42, "qty": 30.0, "expiry": 1000.0,
                  "projected_usage_before_expiry": 5.0},
    )
    opt.on_signal(signal)

    assert len(approvals.created) == 1
    assert approvals.created[0]["type"] == "promo"

    session = session_factory()
    try:
        promos = session.query(Promotion).all()
    finally:
        session.close()
    assert len(promos) == 1
    assert promos[0].discount_pct == pytest.approx(config.PROMO_DISCOUNT_PCT)
    assert item_id in promos[0].menu_items

    proposals = bus.live(type=SignalType.PROMO_PROPOSAL)
    assert len(proposals) == 1


def test_activate_promo_sets_active(optimizer_with_fakes, session_factory):
    opt, _procurement, _approvals = optimizer_with_fakes
    session = session_factory()
    try:
        promo = Promotion(type="discount", menu_items=[1], trigger="expiry", discount_pct=20.0,
                           channel="both", status="proposed", sim_time=0.0)
        session.add(promo)
        session.commit()
        session.refresh(promo)
        promo_id = promo.id
    finally:
        session.close()

    opt.activate_promo(promo_id)

    session = session_factory()
    try:
        promo = session.get(Promotion, promo_id)
        assert promo.status == "active"
    finally:
        session.close()
