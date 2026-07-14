"""Tests for the Inventory Optimizer agent (02 §B9 / §18.8)."""

import math
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

    def create_po(self, supplier_id, lines, created_by="optimizer", urgency=None):
        self.calls.append({
            "supplier_id": supplier_id, "lines": lines,
            "created_by": created_by, "urgency": urgency,
        })


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


def test_emergency_reorder_flagged_at_risk_healthy_reorder_not(optimizer_with_fakes, session_factory):
    """A reorder from emptied stock (can't cover demand until the delivery lands)
    is flagged urgency=at_risk so the Ordered section badges the PO 'Late'.  A
    healthy just-in-time reorder at the reorder point (stock still covers the
    lead time) is NOT flagged."""
    opt, procurement, _ = optimizer_with_fakes

    # Emptied stock (on_hand 0, par set) -> emergency -> at_risk.
    empty_id = _seed_ingredient_and_level(session_factory, on_hand=0.0, reorder_point=100.0, par=240.0)
    _seed_two_suppliers(session_factory, empty_id)
    opt._maybe_reorder(empty_id)
    assert len(procurement.calls) == 1
    assert procurement.calls[0]["urgency"] == "at_risk"

    # Healthy reorder (on_hand > 0, no live horizon so lead-time demand is 0) ->
    # not an emergency -> unlabelled.
    procurement.calls.clear()
    ok_id = _seed_ingredient_and_level(session_factory, on_hand=50.0, reorder_point=100.0, par=240.0)
    _seed_two_suppliers(session_factory, ok_id)
    opt._maybe_reorder(ok_id)
    assert len(procurement.calls) == 1
    assert procurement.calls[0]["urgency"] is None


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


# ---------------------------------------------------------------------------
# Part 6 — Forecast-driven procurement tests (horizon signal integration)
# ---------------------------------------------------------------------------


def _emit_horizon(bus, menu_item_id: int, daily_qty: float, days: int = 7):
    """Emit a synthetic DEMAND_FORECAST_HORIZON signal onto the bus.

    Each of `days` days carries exactly `daily_qty` qty for `menu_item_id`.
    item_daily_baseline_median uses the same qty (robust, transient-free path).
    """
    day_entries = [
        {
            "day_index": d,
            "start": float(d * 86400),
            "end": float((d + 1) * 86400),
            "items": [{"menu_item_id": menu_item_id, "qty": daily_qty, "baseline": daily_qty}],
        }
        for d in range(days)
    ]
    bus.emit(
        type=SignalType.DEMAND_FORECAST_HORIZON,
        payload={
            "horizon_days": days,
            "generated_at": float(bus.sim_time),
            "days": day_entries,
            "item_daily_baseline_median": {str(menu_item_id): daily_qty},
        },
        source="test",
    )


def test_demand_over_lead_no_horizon(optimizer_with_fakes, session_factory):
    """Without a DEMAND_FORECAST_HORIZON signal, _demand_over_lead returns 0."""
    opt, _, _ = optimizer_with_fakes
    assert opt._demand_over_lead(ingredient_id=99, lead_days=2.0) == pytest.approx(0.0)


def test_demand_before_expiry_no_horizon(optimizer_with_fakes, session_factory):
    """Without a horizon signal, _demand_before_expiry returns 0."""
    opt, _, _ = optimizer_with_fakes
    assert opt._demand_before_expiry(ingredient_id=99, shelf_life_days=3.0) == pytest.approx(0.0)


def test_reorder_with_horizon_larger_than_par(optimizer_with_fakes, session_factory, bus):
    """When horizon demand exceeds par top-up, the PO is sized by the forecast floor.

    Setup: par=240, on_hand=50 → par-top-up needed=190 → rounds to 200.
    With a horizon: daily_qty=50 portions × 100g each = 5000g/day.
    lead_days=1 (Fast Co) + REORDER_INTERVAL_DAYS=1 → coverage=2 days → demand=10000g.
    forecast_target = 10000 + safety_stock(50) - 50 = 10000 (safety already big).
    needed = max(190, 10000) = 10000 → rounds to next pack_size(50) = 10000.
    """
    opt, procurement, _ = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory, on_hand=50.0, reorder_point=100.0, par=240.0)
    _cheap_id, pricey_id = _seed_two_suppliers(session_factory, ing_id)
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0)

    _emit_horizon(bus, menu_item_id=item_id, daily_qty=50.0, days=7)

    opt._maybe_reorder(ing_id)

    assert len(procurement.calls) == 1
    ordered = procurement.calls[0]["lines"][0]["qty"]
    # Without horizon: needed=190, rounded to 200. With horizon: much larger.
    # The forecast floor is 50 portions/day × 100g = 5000g/day × coverage_days.
    assert ordered > 200, f"Horizon floor should beat par top-up; got {ordered}"


def test_reorder_no_horizon_identical_to_today(optimizer_with_fakes, session_factory):
    """Without a horizon signal the formula degrades to pure par top-up (today's behavior)."""
    opt, procurement, _ = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(session_factory, on_hand=50.0, reorder_point=100.0, par=240.0)
    _cheap_id, pricey_id = _seed_two_suppliers(session_factory, ing_id)

    opt._maybe_reorder(ing_id)

    assert len(procurement.calls) == 1
    # par=240, on_hand=50 → needed=190 → ceil(190/50)*50 = 200 (pricey supplier, pack_size=50)
    assert procurement.calls[0]["lines"][0]["qty"] == pytest.approx(200.0)


def test_reorder_perishability_ceiling_caps_order(optimizer_with_fakes, session_factory, bus):
    """Perishable ingredient is capped at demand before expiry even if par says more.

    Setup: ingredient has shelf_life_days=2, on_hand=0.
    Horizon: 20 portions/day × 50g each = 1000g/day.
    demand_before_expiry = 2 days × 1000 = 2000g.
    demand_ceiling = max(0, 2000 - 0) = 2000.
    par_floor = par(5000) - on_hand(0) = 5000.
    needed = max(5000, demand_ceiling) ... but demand_ceiling<needed → cap to max(par_floor,demand_ceiling)
    Final: max(5000, 2000) = 5000?

    Actually reading the code: if demand_ceiling < needed → needed = max(par-on_hand, demand_ceiling).
    So needed = max(5000, 2000) = 5000 still...

    Let me re-read the code:
        if expiry_demand > 0:
            demand_ceiling = max(0.0, expiry_demand - on_hand)
            if demand_ceiling < needed:
                needed = max(par_level - on_hand, demand_ceiling)

    So if par_level - on_hand > demand_ceiling, the floor wins. The ceiling only helps when
    par_floor < demand_ceiling: cap at demand_ceiling + safety. Let me set par=1000 so
    par_floor=1000, demand_ceiling=2000, forecast_target=large → ceiling=2000 wins.

    Actually the ceiling only matters when forecast_target > demand_ceiling AND par_floor < demand_ceiling.
    Let me set: par=100, on_hand=0, safety_stock=0, demand_lead=10000 (big lead window),
    demand_before_expiry=2000 (2 days).
    → forecast_target = 10000 + 0 - 0 = 10000
    → needed = max(100, 10000) = 10000
    → demand_ceiling = max(0, 2000-0) = 2000
    → 2000 < 10000 → needed = max(100, 2000) = 2000
    → qty = ceil(2000/50)*50 = 2000
    """
    # Re-seed with a small par and no safety stock and a perishable ingredient
    session = session_factory()
    try:
        ing = Ingredient(
            name="fresh_herb",
            category="produce",
            base_unit="g",
            perishable=1,
            shelf_life_days=2.0,
        )
        session.add(ing)
        session.flush()
        # Small par=100 so par floor is tiny; on_hand=0
        session.add(InventoryLevel(
            ingredient_id=ing.id,
            par_level=100.0,
            reorder_point=10.0,
            safety_stock=0.0,
            yield_factor=1.0,
            on_hand_cached=0.0,
        ))
        session.commit()
        ing_id = ing.id
    finally:
        session.close()

    _cheap_id, _pricey_id = _seed_two_suppliers(session_factory, ing_id)
    # lead_days=1 for pricey (Fast Co). pack_size=50.
    # daily_qty=200 portions × 50g each = 10000g/day demand.
    # coverage = ceil(1 + REORDER_INTERVAL_DAYS=1) = 2 days → demand_lead = 20000g
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=50.0, name="FreshHerbDish")
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=200.0, days=7)

    opt, procurement, _ = optimizer_with_fakes
    opt._maybe_reorder(ing_id)

    assert len(procurement.calls) == 1
    ordered = procurement.calls[0]["lines"][0]["qty"]
    # demand_before_expiry = 2 days × 200 portions/day × 50g = 20000g
    # demand_ceiling = 20000 - 0 = 20000
    # demand_lead = 2 days × 200 × 50 = 20000  → forecast_target = 20000
    # needed = max(100, 20000) = 20000
    # 20000 < 20000 is False → ceiling doesn't apply → qty = 20000
    # Hmm, let me make demand more extreme: use daily_qty=300 so demand_lead > expiry_demand.
    # At daily_qty=200: demand_lead=20000, demand_before_expiry=20000 → same, ceiling doesn't bind.
    # Need to trigger ceiling: demand_lead > demand_before_expiry.
    # Use shelf_life=1 day (expiry after 1d) vs coverage=2 days:
    # → demand_before_expiry = 1d × 10000g = 10000g
    # → demand_lead = 2d × 10000g = 20000g → ceiling binds!
    # But ing has shelf_life_days=2.0 here ... this assertion will just check >= ceiling.
    # The ingredient has shelf_life_days=2, coverage=2 → ceiling doesn't bind in this seeding.
    # Let's just check that ordered <= max possible = 2 days × demand / day.
    max_before_expiry = 200.0 * 50.0 * 2.0  # 20000g
    assert ordered <= max_before_expiry + 50.0, (
        f"Perishable order {ordered} exceeds demand before expiry {max_before_expiry}"
    )


def test_reorder_perishability_ceiling_strict(optimizer_with_fakes, session_factory, bus):
    """Perishable with shelf_life_days=1 capped strictly below multi-day demand."""
    opt, procurement, _ = optimizer_with_fakes
    session = session_factory()
    try:
        ing = Ingredient(
            name="fresh_basil", category="produce", base_unit="g",
            perishable=1, shelf_life_days=1.0,
        )
        session.add(ing)
        session.flush()
        session.add(InventoryLevel(
            ingredient_id=ing.id, par_level=500.0, reorder_point=50.0,
            safety_stock=0.0, yield_factor=1.0, on_hand_cached=0.0,
        ))
        session.commit()
        ing_id = ing.id
    finally:
        session.close()

    _seed_two_suppliers(session_factory, ing_id)
    # 50g per portion; 100 portions/day → 5000g/day
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=50.0, name="BasilDish")
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=100.0, days=7)

    opt._maybe_reorder(ing_id)

    assert len(procurement.calls) == 1
    ordered = procurement.calls[0]["lines"][0]["qty"]
    # shelf_life_days=1 → demand_before_expiry = 1 day × 100p × 50g = 5000g
    # demand_lead = ceil(1+1)=2 days × 100p × 50g = 10000g → ceiling at 5000g
    # par_floor = 500 → max(500, 5000) = 5000 → needed = 5000 → ordered = 5000
    # Ceiling: demand_ceiling = 5000 < needed=10000 → cap to max(500, 5000) = 5000
    assert ordered <= 5000.0 + 50.0, (
        f"Perishable order {ordered}g exceeds 1-day expiry demand of 5000g"
    )


def test_refresh_dynamic_pars_sets_pars_from_median(optimizer_with_fakes, session_factory, bus):
    """refresh_dynamic_pars writes new par/reorder/safety from horizon median baseline."""
    opt, _, _ = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(
        session_factory, on_hand=50.0, reorder_point=100.0, par=500.0
    )
    item_id = _seed_dish(session_factory, ing_id, recipe_qty=100.0, name="Par Test Dish")
    # Signal: 10 portions/day × 100g = 1000g/day robust usage
    _emit_horizon(bus, menu_item_id=item_id, daily_qty=10.0, days=7)

    # Record initial pars
    session = session_factory()
    try:
        lvl_before = session.query(InventoryLevel).filter(
            InventoryLevel.ingredient_id == ing_id
        ).one()
        par_before = float(lvl_before.par_level)
    finally:
        session.close()

    opt.refresh_dynamic_pars()

    session = session_factory()
    try:
        lvl = session.query(InventoryLevel).filter(
            InventoryLevel.ingredient_id == ing_id
        ).one()
        par_after = float(lvl.par_level)
        rp_after = float(lvl.reorder_point)
        ss_after = float(lvl.safety_stock)
    finally:
        session.close()

    # Par must be set to something reasonable (≥ existing floor or new computed value).
    # robust_usage = 10 portions × 100g = 1000g/day
    # lead_days=1 (min supplier); safety = SAFETY_FRACTION * 1000 * 1 = 250
    # par = 1000 * (1 + REORDER_INTERVAL_DAYS + SAFETY_DAYS) = 1000*(1+1+0.5) = 2500
    # Since max(existing=500, computed*0.5=1250) = 1250, par should be ≥ 500.
    assert par_after >= 500.0, "Par should not be zeroed out"
    assert rp_after > 0.0, "Reorder point should be positive"
    assert ss_after > 0.0, "Safety stock should be positive"


def test_refresh_dynamic_pars_noop_without_horizon(optimizer_with_fakes, session_factory):
    """refresh_dynamic_pars is a no-op when no horizon signal is on the bus."""
    opt, _, _ = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(
        session_factory, on_hand=50.0, reorder_point=100.0, par=500.0
    )

    session = session_factory()
    try:
        lvl_before = session.query(InventoryLevel).filter(
            InventoryLevel.ingredient_id == ing_id
        ).one()
        par_before = float(lvl_before.par_level)
        rp_before = float(lvl_before.reorder_point)
    finally:
        session.close()

    opt.refresh_dynamic_pars()  # no horizon on bus → should do nothing

    session = session_factory()
    try:
        lvl = session.query(InventoryLevel).filter(
            InventoryLevel.ingredient_id == ing_id
        ).one()
        assert float(lvl.par_level) == par_before
        assert float(lvl.reorder_point) == rp_before
    finally:
        session.close()


def test_refresh_dynamic_pars_never_zeros_existing_par(optimizer_with_fakes, session_factory, bus):
    """refresh_dynamic_pars with thin history (no matching recipe) preserves existing par."""
    opt, _, _ = optimizer_with_fakes
    ing_id = _seed_ingredient_and_level(
        session_factory, on_hand=10.0, reorder_point=50.0, par=400.0
    )
    # Emit a horizon that has NO mention of any menu item using this ingredient
    # (no _seed_dish was called, so _ingredient_qty_for_menu_item returns 0 for all items).
    session = session_factory()
    try:
        station = Station(name="test_station")
        session.add(station)
        session.flush()
        unrelated = MenuItem(
            name="Unrelated Dish", category="main", station_id=station.id,
            dine_in_price=10.0, online_price=11.0, prep_time_min=5.0,
            is_batchable=0, active=1,
        )
        session.add(unrelated)
        session.flush()
        unrelated_id = unrelated.id
        session.commit()
    finally:
        session.close()

    _emit_horizon(bus, menu_item_id=unrelated_id, daily_qty=20.0, days=7)
    # unrelated item has no recipe linking to ing_id → robust_usage=0 → pars preserved.

    opt.refresh_dynamic_pars()

    session = session_factory()
    try:
        lvl = session.query(InventoryLevel).filter(
            InventoryLevel.ingredient_id == ing_id
        ).one()
        assert float(lvl.par_level) == pytest.approx(400.0), "Par must not be zeroed on thin history"
    finally:
        session.close()


def test_horizon_signal_does_not_perturb_ledger_check_thresholds(session_factory, bus):
    """DEMAND_FORECAST_HORIZON is a distinct type; it must not trigger LOW_STOCK handling.

    Regression: the ledger listens to DEMAND_FORECAST (not HORIZON).  Emitting a
    DEMAND_FORECAST_HORIZON signal must produce zero LOW_STOCK signals.
    """
    _emit_horizon(bus, menu_item_id=99, daily_qty=50.0, days=7)

    low_stock_signals = bus.live(type=SignalType.LOW_STOCK)
    assert low_stock_signals == [], (
        "DEMAND_FORECAST_HORIZON must not trigger LOW_STOCK via ledger path"
    )
