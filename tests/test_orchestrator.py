"""Orchestrator tick tests (§6.1 / §17) — closed-hours auto-jump behavior."""

import pytest

from core.clock import SimClock, get_or_create_sim_state
from core.models import Scenario, ScenarioEvent
from core.orchestrator import Orchestrator


@pytest.fixture
def system(bus, session_factory):
    """A wired clock + orchestrator over the in-memory DB."""
    clock = SimClock(session_factory, bus)
    orch = Orchestrator(clock, bus, session_factory)
    return clock, orch, bus, session_factory


def _set_clock(session_factory, sim_time, speed=1.0, status="running"):
    session = session_factory()
    try:
        state = get_or_create_sim_state(session)
        state.sim_time = sim_time
        state.day_number = int(sim_time // 86400)
        state.day_of_week = state.day_number % 7
        state.speed = speed
        state.status = status
        session.commit()
    finally:
        session.close()


def _add_scenario_event(session_factory, at_sim_time):
    session = session_factory()
    try:
        scenario = Scenario(name="t", description="", is_active=1)
        session.add(scenario)
        session.commit()
        ev = ScenarioEvent(
            scenario_id=scenario.id,
            at_sim_time=at_sim_time,
            event_type="inject_signal",
            payload={},
            fired=0,
        )
        session.add(ev)
        session.commit()
        return ev.id
    finally:
        session.close()


def test_tick_jumps_over_closed_hours_and_skips_window(system):
    """From 82795 (22:59:55) one 15 sim-s tick at 1x lands at 08:00 next day
    (115200), NOT 82810 (23:00:10), and fires nothing inside the skipped
    23:00→08:00 window (§6.1)."""
    clock, orch, bus, session_factory = system

    # 5 sim-seconds before 23:00; one tick at 1x advances 15 sim-s.
    _set_clock(session_factory, 82795.0, speed=1.0, status="running")
    bus.sim_time = 82795.0

    fired = {"before_close": [], "in_window": [], "interval_in_window": []}

    # Deadline just before close (operating hours) — must still fire.
    orch.register(
        "deadline",
        lambda: fired["before_close"].append(clock.sim_time),
        due_at=82798.0,
    )
    # Deadline inside the closed window — must NOT fire on the jump tick.
    orch.register(
        "deadline",
        lambda: fired["in_window"].append(clock.sim_time),
        due_at=82810.0,
    )
    # Interval trigger whose next slot lands inside the closed window — the
    # in-window slot must be rolled past without firing.
    interval_trigger = orch.register(
        "interval",
        lambda: fired["interval_in_window"].append(clock.sim_time),
        interval_sim_s=300.0,
    )
    interval_trigger.next_due = 82810.0

    # Scenario events: one before close (fires), one inside the window (stays
    # pending so it takes effect on the next operating tick).
    ev_before = _add_scenario_event(session_factory, 82799.0)
    ev_in_window = _add_scenario_event(session_factory, 82850.0)

    events = orch.tick()

    # The clock jumped to 08:00 next day, never landing at 23:00:10.
    assert clock.sim_time == 115200.0
    assert clock.sim_time != 82810.0
    assert clock.day_number == 1

    # sim_tick reflects the landed (operating) time.
    sim_tick = events[0]
    assert sim_tick["event"] == "sim_tick"
    assert sim_tick["payload"]["sim_time"] == 115200.0
    assert sim_tick["payload"]["time_of_day"] == "08:00:00"

    # Operating-hours trigger fired; closed-window ones did not.
    assert fired["before_close"] == [115200.0]
    assert fired["in_window"] == []
    assert fired["interval_in_window"] == []

    # The in-window deadline stays pending (not consumed) for a later tick.
    in_window_deadline = next(t for t in orch.triggers if t.due_at == 82810.0)
    assert in_window_deadline.fired is False
    # The interval trigger's schedule rolled forward past the skipped window.
    assert interval_trigger.next_due > 115200.0

    # Scenario events: before-close fired (consumed), in-window still pending.
    session = session_factory()
    try:
        assert session.get(ScenarioEvent, ev_before).fired == 1
        assert session.get(ScenarioEvent, ev_in_window).fired == 0
    finally:
        session.close()


def test_run_loop_survives_tick_exception(system):
    """A tick that raises must be logged and skipped, not silently kill the
    loop task (which froze the sim at its last sim_time with status=running)."""
    import asyncio

    clock, orch, bus, session_factory = system
    _set_clock(session_factory, 30000.0, speed=1.0, status="running")

    calls = {"n": 0}
    real_tick = orch.tick

    def flaky_tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom (e.g. sqlite 'database is locked')")
        return real_tick()

    orch.tick = flaky_tick

    async def _run():
        task = asyncio.get_running_loop().create_task(orch.run_loop(lambda events: None))
        while calls["n"] < 2:
            await asyncio.sleep(0.01)
        orch.stop_loop()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(_run())
    assert calls["n"] >= 2, "loop must keep ticking after a failed tick"
    assert bus.sim_time > 30000.0, "the post-failure tick must still advance sim time"
