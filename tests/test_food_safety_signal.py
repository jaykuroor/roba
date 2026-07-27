"""FOOD_SAFETY_CHECK emission from the kitchen checklist (Phase 2).

There is no distinct "failed" task status — ``not_done`` plus ``severity`` *is*
the failure — so the signal carries an ``outcome`` instead. Two things raise it:
the cook reporting a temp/safety check not done, and one running past its final
overdue tier (nobody is going to do it now).

Gate:
- a not-done temp check emits once, with the cook's reason;
- a not-done *cleaning* check emits nothing (not a HACCP failure);
- escalation emits only past the final tier;
- the snapshot's `safety_issues` / `task_compliance` agree with the board;
- every SignalType is registered at all four points (§4's four-place gotcha).
"""

import pytest

from core import kitchen_tasks, models
from core.ops_snapshot import build_ops_snapshot
from core.signals import SIGNAL_PAYLOADS, SIGNAL_REGISTRY, SignalType

DAY = 0
# 08:30 is when the opening fridge/freezer temperature log is due.
FRIDGE_TEMP_DUE = 8 * 3600 + 30 * 60


def _seed_tasks(session_factory, now=FRIDGE_TEMP_DUE):
    session = session_factory()
    try:
        kitchen_tasks.ensure_tasks_for_day(session, DAY, "italian")
    finally:
        session.close()


def _task(session_factory, template_key):
    session = session_factory()
    try:
        return (
            session.query(models.KitchenTask)
            .filter(models.KitchenTask.template_key == template_key)
            .one()
        )
    finally:
        session.close()


def _signals(bus):
    return bus.live(type=SignalType.FOOD_SAFETY_CHECK)


def _report_not_done(session_factory, bus, task_id, *, note, severity, now):
    session = session_factory()
    try:
        return kitchen_tasks.set_outcome(
            session, task_id, status="not_done", note=note, severity=severity,
            by="cook", now=now, approvals=None, bus=bus,
        )
    finally:
        session.close()


# -- registration ----------------------------------------------------------


def test_every_signal_type_is_registered_at_all_four_points():
    """Omitting SIGNAL_REGISTRY is a hard KeyError at emit; omitting the payload
    silently skips validation. Guard the whole enum, not just the new type."""
    for sig_type in SignalType:
        assert sig_type in SIGNAL_REGISTRY, f"{sig_type} missing from SIGNAL_REGISTRY"
        assert sig_type in SIGNAL_PAYLOADS, f"{sig_type} missing from SIGNAL_PAYLOADS"


def test_food_safety_groups_have_a_real_subscriber():
    """A type whose groups nobody subscribes to dead-letters on every emit."""
    subscribed = {"forecasting", "sensing", "inventory", "procurement"}
    groups = set(SIGNAL_REGISTRY[SignalType.FOOD_SAFETY_CHECK]["groups"])
    assert groups & subscribed, groups


# -- emission --------------------------------------------------------------


def test_not_done_temp_check_emits_food_safety(bus, session_factory):
    _seed_tasks(session_factory)
    task = _task(session_factory, "open_fridge_temp")

    _report_not_done(session_factory, bus, task.id,
                     note="walk-in reading 9C", severity="high", now=FRIDGE_TEMP_DUE + 60)

    live = _signals(bus)
    assert len(live) == 1
    payload = live[0].payload
    assert payload["outcome"] == "not_done"
    assert payload["category"] == "temp"
    assert payload["severity"] == "high"
    assert payload["note"] == "walk-in reading 9C"
    assert payload["task_id"] == task.id


def test_not_done_cleaning_check_emits_nothing(bus, session_factory):
    """Mopping the floor late is not a food-safety incident."""
    _seed_tasks(session_factory)
    task = _task(session_factory, "close_floors")

    _report_not_done(session_factory, bus, task.id,
                     note="no mop head", severity="medium", now=FRIDGE_TEMP_DUE + 60)

    assert _signals(bus) == []


def test_repeat_not_done_does_not_pile_up_incidents(bus, session_factory):
    _seed_tasks(session_factory)
    task = _task(session_factory, "open_fridge_temp")

    for note in ("still 9C", "still 9C", "engineer called"):
        _report_not_done(session_factory, bus, task.id,
                         note=note, severity="high", now=FRIDGE_TEMP_DUE + 60)

    assert len(_signals(bus)) == 1


def test_safety_check_emits_only_past_the_final_overdue_tier(bus, session_factory):
    _seed_tasks(session_factory)
    tiers = kitchen_tasks._tiers_min()

    def reconcile_at(minutes_late):
        session = session_factory()
        try:
            return kitchen_tasks.reconcile(
                session, now=FRIDGE_TEMP_DUE + minutes_late * 60,
                cuisine="italian", approvals=None, bus=bus,
            )
        finally:
            session.close()

    for tier_minutes in tiers[:-1]:
        reconcile_at(tier_minutes)
        assert _signals(bus) == [], f"emitted at tier minute {tier_minutes}"

    reconcile_at(tiers[-1])
    live = _signals(bus)
    assert [s.payload["outcome"] for s in live] == ["overdue"] * len(live)
    # The 08:30 batch is the fridge log + sanitize + handwash; only the two
    # HACCP ones (temp / safety) are food-safety failures.
    assert {s.payload["category"] for s in live} <= {"temp", "safety"}
    assert any(s.payload["title"].startswith("Record walk-in fridge") for s in live)


# -- the snapshot the manager card reads -----------------------------------


def test_snapshot_reports_safety_issues_and_compliance(bus, session_factory):
    _seed_tasks(session_factory)
    fridge = _task(session_factory, "open_fridge_temp")
    handwash = _task(session_factory, "open_handwash")
    bus.sim_time = FRIDGE_TEMP_DUE + 60

    _report_not_done(session_factory, bus, fridge.id,
                     note="walk-in reading 9C", severity="high", now=bus.sim_time)
    session = session_factory()
    try:
        # Done exactly at its due time: `done_late` has NO grace window (unlike
        # the 5-min notice tiers), so a minute late would already count against
        # compliance — see docs/fable/progress.md §4.
        kitchen_tasks.set_outcome(session, handwash.id, status="done",
                                  by="cook", now=FRIDGE_TEMP_DUE, approvals=None, bus=bus)
    finally:
        session.close()

    snapshot = build_ops_snapshot(session_factory, None, bus=bus)

    assert len(snapshot["safety_issues"]) == 1
    failure = snapshot["safety_issues"][0]
    assert failure["outcome"] == "not_done"
    assert failure["note"] == "walk-in reading 9C"
    assert failure["title"].startswith("Record walk-in fridge")

    compliance = snapshot["task_compliance"]
    assert compliance["not_done"] == 1
    assert compliance["done"] == 1
    assert compliance["done_late"] == 0
    assert compliance["rate"] == pytest.approx(
        (compliance["done"] - compliance["done_late"]) / compliance["accountable"]
    )


def test_snapshot_compliance_rate_is_none_before_anything_is_due(bus, session_factory):
    """0% at 08:00 would be a lie — nothing has been missed yet."""
    _seed_tasks(session_factory)
    bus.sim_time = 8 * 3600  # 08:00, before the first task is due

    snapshot = build_ops_snapshot(session_factory, None, bus=bus)
    assert snapshot["safety_issues"] == []
    assert snapshot["task_compliance"]["rate"] is None
