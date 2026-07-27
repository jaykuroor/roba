"""Kitchen ticket lifecycle (docs/fable/progress.md Phase 1).

Orders are no longer born closed: in ``lifecycle`` mode they enter the pass as
``queued`` and are drained ``queued → cooking → served`` at a rate set by how
many cooks are actually on shift. ``instant`` mode restores the old behaviour.

Gate:
- lifecycle mode stamps new orders ``queued`` with no ``served_at``;
- the backlog grows while the cooks are out and drains once they are back;
- ``instant`` mode reproduces the born-served behaviour *and* flushes a backlog
  left over from lifecycle mode;
- ``served_at`` is stamped exactly once.
"""

import random

import pytest

from core import config
from core.clock import SimClock
from core.models import (
    Attendance,
    MenuItem,
    Order,
    SimSettings,
    Staff,
    StaffStation,
    Station,
)
from core.ops_snapshot import build_ops_snapshot
from core.pos_simulator import POSSimulator

# 09:15 on sim-day 0 — inside the operating window, so orders actually arrive.
MORNING = 28800 + 900


def _seed(session_factory, *, ticket_mode="lifecycle", cook_status="present"):
    """One station, one dish, one cook — with today's attendance already set."""
    session = session_factory()
    try:
        station = Station(name="line")
        session.add(station)
        session.flush()

        item = MenuItem(
            name="Margherita", category="pizza", station_id=station.id,
            dine_in_price=12.0, online_price=14.0, prep_time_min=10.0,
            is_batchable=0, active=1,
        )
        cook = Staff(name="Luca", role="cook", skill_level=3, hourly_cost=15.0, active=1)
        session.add_all([item, cook])
        session.flush()

        session.add(StaffStation(staff_id=cook.id, station_id=station.id))
        session.add(Attendance(
            staff_id=cook.id, date_sim_day=0, status=cook_status,
            daypart=None, sim_time=MORNING,
        ))
        session.add(SimSettings(
            id=1,
            base_orders_per_day=300,
            velocity=1.0,
            dish_mix_weights={str(item.id): 1.0},
            daypart_curve=None,
            channel_mix={"dine_in": 1.0},
            anomaly_injections=None,
            kitchen_ticket_mode=ticket_mode,
        ))
        session.commit()
        return cook.id
    finally:
        session.close()


def _sim(bus, session_factory):
    return POSSimulator(bus, session_factory, SimClock(session_factory, bus),
                        rng=random.Random(4242))


def _add_queued(session_factory, count, *, first_sim_time=MORNING):
    """Put ``count`` tickets on the pass, oldest first."""
    session = session_factory()
    try:
        for i in range(count):
            session.add(Order(
                sim_time=first_sim_time + i, service_mode="dine_in", guest_count=1,
                status="closed", channel="dine_in", total=12.0,
                kitchen_status="queued",
            ))
        session.commit()
    finally:
        session.close()


def _ticket_states(session_factory):
    session = session_factory()
    try:
        counts = {"queued": 0, "cooking": 0, "served": 0}
        for order in session.query(Order).all():
            counts[order.kitchen_status] = counts.get(order.kitchen_status, 0) + 1
        return counts
    finally:
        session.close()


# -- generation ------------------------------------------------------------


def test_lifecycle_orders_enter_the_pass_queued(bus, session_factory):
    _seed(session_factory)
    sim = _sim(bus, session_factory)
    bus.sim_time = MORNING

    # A single tick generates but never drains (no elapsed window yet).
    sim.tick(MORNING)

    session = session_factory()
    try:
        orders = session.query(Order).all()
        assert orders, "expected the POS to generate at least one order"
        assert all(o.kitchen_status == "queued" for o in orders)
        assert all(o.served_at is None for o in orders)
    finally:
        session.close()


def test_instant_mode_reproduces_born_served_behaviour(bus, session_factory):
    _seed(session_factory, ticket_mode="instant")
    sim = _sim(bus, session_factory)
    bus.sim_time = MORNING

    sim.tick(MORNING)

    session = session_factory()
    try:
        orders = session.query(Order).all()
        assert orders
        assert all(o.kitchen_status == "served" for o in orders)
        assert all(o.served_at == o.sim_time for o in orders)
    finally:
        session.close()


def test_instant_mode_flushes_a_backlog_left_by_lifecycle_mode(bus, session_factory):
    """Flipping the Controls toggle to `instant` must empty the pass, not freeze it."""
    _seed(session_factory, ticket_mode="instant")
    _add_queued(session_factory, 7)
    sim = _sim(bus, session_factory)

    sim.tick(MORNING)
    sim.tick(MORNING + 60)

    counts = _ticket_states(session_factory)
    assert counts["queued"] == 0 and counts["cooking"] == 0


# -- the drain -------------------------------------------------------------


def test_backlog_holds_while_the_cook_is_out(bus, session_factory):
    _seed(session_factory, cook_status="sick")
    _add_queued(session_factory, 5)
    sim = _sim(bus, session_factory)

    # A full sim-hour of drain capacity — worth nothing with nobody on the pass.
    sim._drain_tickets(MORNING + 3600, 3600.0, "lifecycle")

    assert _ticket_states(session_factory)["queued"] == 5


def test_backlog_drains_once_the_cook_is_present(bus, session_factory):
    _seed(session_factory)
    _add_queued(session_factory, 5)
    sim = _sim(bus, session_factory)

    # One cook × KITCHEN_TICKETS_PER_COOK_PER_HOUR ≥ 5 tickets in one sim-hour,
    # but a ticket must pass through `cooking`, so it takes a second slice.
    sim._drain_tickets(MORNING + 3600, 3600.0, "lifecycle")
    sim._drain_tickets(MORNING + 7200, 3600.0, "lifecycle")

    counts = _ticket_states(session_factory)
    assert counts["queued"] == 0
    assert counts["served"] == 5


def test_drain_is_oldest_first(bus, session_factory):
    _seed(session_factory)
    _add_queued(session_factory, 4)
    sim = _sim(bus, session_factory)

    # Exactly one ticket of capacity: 3600/KITCHEN_TICKETS_PER_COOK_PER_HOUR sim-s.
    one_ticket_s = 3600.0 / config.KITCHEN_TICKETS_PER_COOK_PER_HOUR
    sim._drain_tickets(MORNING + one_ticket_s, one_ticket_s, "lifecycle")

    session = session_factory()
    try:
        cooking = session.query(Order).filter(Order.kitchen_status == "cooking").all()
        assert len(cooking) == 1
        assert cooking[0].sim_time == MORNING  # the oldest ticket, not the newest
    finally:
        session.close()


def test_fractional_capacity_accumulates_across_ticks(bus, session_factory):
    """At 1× a tick buys well under one ticket — the remainder must carry over."""
    _seed(session_factory)
    _add_queued(session_factory, 2)
    sim = _sim(bus, session_factory)

    one_ticket_s = 3600.0 / config.KITCHEN_TICKETS_PER_COOK_PER_HOUR
    slice_s = one_ticket_s / 4
    for i in range(4):
        sim._drain_tickets(MORNING + slice_s * (i + 1), slice_s, "lifecycle")

    # Four quarter-slices = one whole ticket of capacity.
    assert _ticket_states(session_factory)["cooking"] == 1


def test_idle_pass_does_not_bank_capacity(bus, session_factory):
    """A quiet night must not buy tickets that instantly clear the morning rush."""
    _seed(session_factory)
    sim = _sim(bus, session_factory)

    sim._drain_tickets(MORNING, 8 * 3600.0, "lifecycle")  # closed, nothing queued
    _add_queued(session_factory, 3)
    one_ticket_s = 3600.0 / config.KITCHEN_TICKETS_PER_COOK_PER_HOUR
    sim._drain_tickets(MORNING + one_ticket_s, one_ticket_s, "lifecycle")

    counts = _ticket_states(session_factory)
    assert counts["cooking"] == 1 and counts["queued"] == 2


def test_served_at_is_stamped_exactly_once(bus, session_factory):
    _seed(session_factory)
    _add_queued(session_factory, 1)
    sim = _sim(bus, session_factory)

    sim._drain_tickets(MORNING + 3600, 3600.0, "lifecycle")   # queued → cooking
    sim._drain_tickets(MORNING + 7200, 3600.0, "lifecycle")   # cooking → served
    first = _served_at(session_factory)
    assert first == pytest.approx(MORNING + 7200)

    sim._drain_tickets(MORNING + 10800, 3600.0, "lifecycle")  # nothing left to do
    assert _served_at(session_factory) == pytest.approx(first)


def _served_at(session_factory):
    session = session_factory()
    try:
        return session.query(Order).one().served_at
    finally:
        session.close()


# -- the snapshot the manager card reads -----------------------------------


def test_snapshot_reports_backlog_and_ticket_time(bus, session_factory):
    _seed(session_factory)
    _add_queued(session_factory, 3)
    sim = _sim(bus, session_factory)
    bus.sim_time = MORNING + 7200

    sim._drain_tickets(MORNING + 3600, 3600.0, "lifecycle")
    sim._drain_tickets(MORNING + 7200, 3600.0, "lifecycle")

    snapshot = build_ops_snapshot(session_factory, None, bus=bus)
    assert snapshot["queued_count"] == 0
    assert snapshot["cooking_count"] == 0
    # Served at MORNING+7200 having arrived around MORNING → ~120 sim-minutes.
    assert snapshot["avg_ticket_minutes"] == pytest.approx(120.0, abs=0.5)


def test_snapshot_ticket_time_is_none_without_recent_service(bus, session_factory):
    _seed(session_factory)
    _add_queued(session_factory, 2)
    bus.sim_time = MORNING

    snapshot = build_ops_snapshot(session_factory, None, bus=bus)
    assert snapshot["queued_count"] == 2
    assert snapshot["avg_ticket_minutes"] is None
