"""ORDER_BACKLOG + EQUIPMENT_FAILURE detectors (Phase 3).

Both complete an incident category that had no detector. They work in opposite
directions: the backlog signal is *refreshed* by a constant dedup_key while the
queue stays deep, and the equipment signal's *TTL is the outage window*, so its
expiry is what re-opens the station.

Gate:
- no signal below BACKLOG_WARN; warning at it; critical at BACKLOG_CRIT;
- a sustained backlog stays one live row (refreshed, never duplicated);
- an equipment event blocks the station's dishes and re-enables at `until_sim`;
- an equipment outage does not clobber an unrelated block on the same dish;
- the orchestrator no longer burns an inactive scenario's events (§5).
"""

import pytest

from core import config
from core.availability import RC_EQUIPMENT_DOWN, RC_STATION_UNSTAFFED, recompute_availability
from core.clock import SimClock
from core.models import (
    MenuItem,
    MenuToggle,
    Order,
    Scenario,
    ScenarioEvent,
    SimSettings,
    Staff,
    StaffStation,
    Station,
)
from core.orchestrator import Orchestrator
from core.pos_simulator import POSSimulator
from core.scenarios import ScenarioEngine
from core.signals import SignalType

MORNING = 28800 + 900  # 09:15


# -- ORDER_BACKLOG ---------------------------------------------------------


def _pos_seed(session_factory):
    session = session_factory()
    try:
        station = Station(name="line")
        session.add(station)
        session.flush()
        item = MenuItem(name="Margherita", category="pizza", station_id=station.id,
                        dine_in_price=12.0, online_price=14.0, active=1)
        cook = Staff(name="Luca", role="cook", skill_level=3, active=1)
        session.add_all([item, cook])
        session.flush()
        session.add(StaffStation(staff_id=cook.id, station_id=station.id))
        session.add(SimSettings(id=1, base_orders_per_day=300, velocity=1.0,
                                dish_mix_weights={str(item.id): 1.0}, daypart_curve=None,
                                channel_mix={"dine_in": 1.0}, anomaly_injections=None,
                                kitchen_ticket_mode="lifecycle"))
        session.commit()
    finally:
        session.close()


def _queue(session_factory, count):
    session = session_factory()
    try:
        for i in range(count):
            session.add(Order(sim_time=MORNING + i, service_mode="dine_in", guest_count=1,
                              status="closed", channel="dine_in", total=12.0,
                              kitchen_status="queued"))
        session.commit()
    finally:
        session.close()


def _backlog_signals(bus):
    return bus.live(type=SignalType.ORDER_BACKLOG)


@pytest.mark.parametrize("queued,expected", [
    (config.BACKLOG_WARN - 1, None),
    (config.BACKLOG_WARN, "warning"),
    (config.BACKLOG_CRIT, "critical"),
])
def test_backlog_signal_thresholds(bus, session_factory, queued, expected):
    _pos_seed(session_factory)
    _queue(session_factory, queued)
    sim = POSSimulator(bus, session_factory, SimClock(session_factory, bus))
    bus.sim_time = MORNING

    sim._emit_backlog(MORNING)

    live = _backlog_signals(bus)
    if expected is None:
        assert live == []
    else:
        assert len(live) == 1
        assert live[0].payload["level"] == expected
        assert live[0].payload["queued_count"] == queued


def test_sustained_backlog_stays_one_live_signal(bus, session_factory):
    """A deep queue must not emit a signal per tick — the dedup_key refreshes
    the single live row in place."""
    _pos_seed(session_factory)
    _queue(session_factory, config.BACKLOG_CRIT)
    sim = POSSimulator(bus, session_factory, SimClock(session_factory, bus))
    bus.sim_time = MORNING

    for _ in range(5):
        sim._emit_backlog(MORNING)
    _queue(session_factory, 3)            # queue deepens → payload changes
    sim._emit_backlog(MORNING)

    live = _backlog_signals(bus)
    assert len(live) == 1
    assert live[0].payload["queued_count"] == config.BACKLOG_CRIT + 3


def test_backlog_signal_reports_who_is_on_the_pass(bus, session_factory):
    _pos_seed(session_factory)
    _queue(session_factory, config.BACKLOG_CRIT)
    sim = POSSimulator(bus, session_factory, SimClock(session_factory, bus))
    bus.sim_time = MORNING

    sim._emit_backlog(MORNING)

    assert _backlog_signals(bus)[0].payload["cooks_present"] == 1


def test_instant_mode_never_raises_a_backlog(bus, session_factory):
    """Instant mode has no pass to back up; ticking must stay silent."""
    _pos_seed(session_factory)
    session = session_factory()
    try:
        session.get(SimSettings, 1).kitchen_ticket_mode = "instant"
        session.commit()
    finally:
        session.close()
    _queue(session_factory, config.BACKLOG_CRIT)
    sim = POSSimulator(bus, session_factory, SimClock(session_factory, bus))
    bus.sim_time = MORNING

    sim.tick(MORNING)
    sim.tick(MORNING + 60)

    assert _backlog_signals(bus) == []


# -- EQUIPMENT_FAILURE -----------------------------------------------------


def _kitchen_seed(session_factory):
    """Two stations, one dish each, both fully staffed."""
    session = session_factory()
    try:
        grill = Station(name="Grill")
        cold = Station(name="Cold")
        session.add_all([grill, cold])
        session.flush()
        burger = MenuItem(name="Burger", category="main", station_id=grill.id,
                          dine_in_price=9.0, active=1)
        salad = MenuItem(name="Salad", category="starter", station_id=cold.id,
                         dine_in_price=6.0, active=1)
        cook = Staff(name="Jake", role="cook", skill_level=3, active=1)
        session.add_all([burger, salad, cook])
        session.flush()
        session.add_all([
            StaffStation(staff_id=cook.id, station_id=grill.id),
            StaffStation(staff_id=cook.id, station_id=cold.id),
        ])
        session.commit()
        return burger.id, salad.id, grill.id
    finally:
        session.close()


def _active(session_factory, item_id):
    session = session_factory()
    try:
        return bool(session.get(MenuItem, item_id).active)
    finally:
        session.close()


def _blocks(session_factory, item_id, reason_code):
    session = session_factory()
    try:
        return session.query(MenuToggle).filter(
            MenuToggle.menu_item_id == item_id,
            MenuToggle.reason_code == reason_code,
            MenuToggle.action == "disable",
            MenuToggle.active == 1,
        ).count()
    finally:
        session.close()


def test_equipment_event_blocks_the_station_and_reopens_at_until_sim(bus, session_factory):
    burger_id, salad_id, _grill_id = _kitchen_seed(session_factory)
    engine = ScenarioEngine(bus, session_factory, SimClock(session_factory, bus),
                            pos_simulator=None, weather=None)
    bus.sim_time = MORNING

    engine._equipment_failure(
        {"station": "Grill", "label": "flat-top grill", "duration_sim_s": 3600.0}, MORNING
    )

    # The grill's dish is off the menu; the cold station is untouched.
    assert _active(session_factory, burger_id) is False
    assert _blocks(session_factory, burger_id, RC_EQUIPMENT_DOWN) == 1
    assert _active(session_factory, salad_id) is True

    signal = bus.live(type=SignalType.EQUIPMENT_FAILURE)[0]
    assert signal.payload["station"] == "Grill"
    assert signal.payload["label"] == "flat-top grill"
    assert signal.payload["until_sim"] == MORNING + 3600.0
    # The TTL *is* the window — that expiry is what re-opens the station.
    assert signal.expires_at == MORNING + 3600.0

    # Past the window the signal sweeps and the dish comes back.
    after = MORNING + 3601.0
    bus.sim_time = after
    bus.sweep(after)
    recompute_availability(session_factory, bus, None, agent_name="test")

    assert _blocks(session_factory, burger_id, RC_EQUIPMENT_DOWN) == 0
    assert _active(session_factory, burger_id) is True


def test_equipment_outage_does_not_clear_an_unrelated_block(bus, session_factory):
    """Re-enable happens only when ALL blocks clear — an unstaffed station must
    survive the equipment window ending."""
    burger_id, _salad_id, grill_id = _kitchen_seed(session_factory)
    engine = ScenarioEngine(bus, session_factory, SimClock(session_factory, bus),
                            pos_simulator=None, weather=None)
    bus.sim_time = MORNING
    engine._equipment_failure({"station": "Grill", "duration_sim_s": 600.0}, MORNING)

    # The only cook for the grill calls in sick, so the station is also unstaffed.
    session = session_factory()
    try:
        session.query(StaffStation).filter(StaffStation.station_id == grill_id).delete()
        session.add(StaffStation(staff_id=session.query(Staff).one().id, station_id=grill_id))
        session.query(Staff).one().active = 0
        session.commit()
    finally:
        session.close()

    after = MORNING + 601.0
    bus.sim_time = after
    bus.sweep(after)
    recompute_availability(session_factory, bus, None, agent_name="test")

    assert _blocks(session_factory, burger_id, RC_EQUIPMENT_DOWN) == 0
    assert _blocks(session_factory, burger_id, RC_STATION_UNSTAFFED) == 1
    assert _active(session_factory, burger_id) is False


def test_unknown_station_is_ignored_not_raised(bus, session_factory):
    _kitchen_seed(session_factory)
    engine = ScenarioEngine(bus, session_factory, SimClock(session_factory, bus),
                            pos_simulator=None, weather=None)
    bus.sim_time = MORNING

    engine._equipment_failure({"station": "Sushi Bar", "duration_sim_s": 600.0}, MORNING)

    assert bus.live(type=SignalType.EQUIPMENT_FAILURE) == []


# -- the §5 scenario-event bug --------------------------------------------


def test_orchestrator_no_longer_burns_an_inactive_scenarios_events(bus, session_factory):
    """The seeded-but-inactive Friday Rush used to have its events consumed by
    the orchestrator as sim time passed them, so activating it later fired
    nothing at all."""
    session = session_factory()
    try:
        scenario = Scenario(name="Friday Rush", description="", is_active=0)
        session.add(scenario)
        session.commit()
        session.add(ScenarioEvent(scenario_id=scenario.id, at_sim_time=MORNING,
                                  event_type="velocity_mult", payload={"mult": 1.6},
                                  fired=0))
        session.add(SimSettings(id=1, base_orders_per_day=300, velocity=1.0,
                                dish_mix_weights={}, daypart_curve=None,
                                channel_mix=None, anomaly_injections=None))
        session.commit()
        scenario_id = scenario.id
    finally:
        session.close()

    clock = SimClock(session_factory, bus)
    orch = Orchestrator(clock, bus, session_factory)
    engine = ScenarioEngine(bus, session_factory, clock, pos_simulator=None, weather=None)
    bus.sim_time = MORNING + 60

    orch.tick()  # sim time is well past the event

    session = session_factory()
    try:
        assert session.query(ScenarioEvent).one().fired == 0, "orchestrator burned it"
    finally:
        session.close()

    # Activating afterwards must still fire it.
    engine.activate(scenario_id)
    assert engine.tick(MORNING + 120) != []
    session = session_factory()
    try:
        assert session.query(ScenarioEvent).one().fired == 1
        assert session.get(SimSettings, 1).anomaly_injections, "effect never applied"
    finally:
        session.close()
