"""Integration tests for the POS simulator + data formatter (§10 / §16).

Gate (this session):
- POS sim + formatter + bus over a seeded DB produce ≥1 Order and ≥1 OrderLine.
- ``formatter.item_velocity`` returns a positive float after some orders.
- A voided line produces a ``WASTE_EVENT`` with ``waste_type='cancelled_order'``.
- ``bus.notify_order_line`` is called for each non-voided line.
"""

import pytest

from core.clock import SimClock, get_or_create_sim_state
from core.formatter import DataFormatter
from core.models import (
    Ingredient,
    MenuItem,
    Order,
    OrderLine,
    Recipe,
    RecipeLine,
    SimSettings,
    Station,
)
from core.pos_simulator import POSSimulator
from core.signals import SignalType


def _seed(session_factory):
    """Tiny inline seed: 2 menu items, 1 recipe each, and POS settings."""
    session = session_factory()
    try:
        station = Station(name="line")
        session.add(station)
        session.flush()

        ing1 = Ingredient(
            name="tomato", category="produce", base_unit="g",
            perishable=1, shelf_life_days=5.0,
        )
        ing2 = Ingredient(
            name="bun", category="bakery", base_unit="each",
            perishable=1, shelf_life_days=3.0,
        )
        session.add_all([ing1, ing2])
        session.flush()

        item1 = MenuItem(
            name="Margherita", category="pizza", station_id=station.id,
            dine_in_price=12.0, online_price=14.0, prep_time_min=10.0,
            is_batchable=1, active=1,
        )
        item2 = MenuItem(
            name="Burger", category="main", station_id=station.id,
            dine_in_price=9.0, online_price=11.0, prep_time_min=8.0,
            is_batchable=0, active=1,
        )
        session.add_all([item1, item2])
        session.flush()

        recipe1 = Recipe(menu_item_id=item1.id)
        recipe2 = Recipe(menu_item_id=item2.id)
        session.add_all([recipe1, recipe2])
        session.flush()

        session.add_all([
            RecipeLine(recipe_id=recipe1.id, ingredient_id=ing1.id,
                       qty=100.0, unit="g", optional=0),
            RecipeLine(recipe_id=recipe2.id, ingredient_id=ing2.id,
                       qty=1.0, unit="each", optional=0),
        ])

        session.add(SimSettings(
            id=1,
            base_orders_per_day=300,
            velocity=1.0,
            dish_mix_weights={str(item1.id): 1.0, str(item2.id): 1.0},
            daypart_curve=None,
            channel_mix={"dine_in": 0.7, "delivery": 0.2, "takeout": 0.1},
            anomaly_injections=None,
        ))
        session.commit()
        return item1.id, item2.id
    finally:
        session.close()


@pytest.fixture
def wired(bus, session_factory):
    """A seeded DB wired with clock + formatter + POS simulator."""
    item_ids = _seed(session_factory)
    clock = SimClock(session_factory, bus)
    formatter = DataFormatter(bus, session_factory)
    sim = POSSimulator(bus, session_factory, clock, formatter=formatter)
    return sim, formatter, bus, session_factory, item_ids


def test_pos_sim_creates_orders_and_lines(wired):
    """10 ticks at 09:15 into a sim day create ≥1 Order and ≥1 OrderLine."""
    sim, formatter, bus, session_factory, _item_ids = wired

    sim_time = 28800 + 900  # 09:15
    bus.sim_time = sim_time
    for _ in range(10):
        sim.tick(sim_time)

    session = session_factory()
    try:
        orders = session.query(Order).count()
        lines = session.query(OrderLine).count()
    finally:
        session.close()

    assert orders >= 1
    assert lines >= 1


def test_item_velocity_positive_after_orders(wired):
    """After processing sold lines, item_velocity is a positive float."""
    sim, formatter, bus, session_factory, item_ids = wired
    item1_id, _item2_id = item_ids

    sim_time = 28800 + 900
    bus.sim_time = sim_time

    order = Order(sim_time=sim_time, service_mode="dine_in", channel="dine_in",
                  guest_count=1, status="closed", total=12.0)
    line = OrderLine(menu_item_id=item1_id, qty=2.0, unit_price=12.0,
                     line_total=24.0, status="sold", sim_time=sim_time)
    formatter.on_order(order, [line])

    velocity = formatter.item_velocity(item1_id)
    assert isinstance(velocity, float)
    assert velocity > 0.0


def test_voided_line_emits_cancelled_order_waste(wired):
    """A voided line produces a WASTE_EVENT(cancelled_order) on the bus and is
    NOT forwarded via notify_order_line; non-voided lines are forwarded."""
    sim, formatter, bus, session_factory, item_ids = wired
    item1_id, item2_id = item_ids

    forwarded = []
    bus.register_order_line_handler(lambda line: forwarded.append(line.menu_item_id))

    bus.sim_time = 28800 + 900
    order = Order(sim_time=bus.sim_time, service_mode="dine_in",
                  channel="dine_in", guest_count=1, status="closed", total=12.0)
    sold = OrderLine(menu_item_id=item1_id, qty=1.0, unit_price=12.0,
                     line_total=12.0, status="sold", sim_time=bus.sim_time)
    voided = OrderLine(menu_item_id=item2_id, qty=1.0, unit_price=9.0,
                       line_total=9.0, status="voided", sim_time=bus.sim_time)
    formatter.on_order(order, [sold, voided])

    waste_signals = bus.live(type=SignalType.WASTE_EVENT)
    assert any(
        s.payload.get("waste_type") == "cancelled_order"
        and s.payload.get("menu_item_id") == item2_id
        for s in waste_signals
    )
    # Cancelled-order routing: inventory + human only (§16).
    cancelled = next(
        s for s in waste_signals if s.payload.get("waste_type") == "cancelled_order"
    )
    assert set(cancelled.groups) == {"inventory", "human"}

    # notify_order_line fired for the sold line only.
    assert forwarded == [item1_id]


def test_daypart_weight_zero_outside_hours(wired):
    """Outside operating hours the daypart weight is 0 (no arrivals)."""
    sim, _formatter, _bus, _sf, _ids = wired
    # 03:00 into the day — closed.
    assert sim.daypart_weight(3 * 3600) == 0.0
    # 12:00 — lunch daypart, positive weight.
    assert sim.daypart_weight(12 * 3600) > 0.0
