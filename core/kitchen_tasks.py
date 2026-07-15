"""Kitchen checklist tasks for the cook desk (opening / temp / cleaning / closing).

Templates are cuisine-keyed and materialized lazily into ``KitchenTask`` rows
once per sim day.  The cook confirms each task; any left pending past its due
time is surfaced to the manager desk as an ``ApprovalRequest`` of type
``kitchen_task`` (see :func:`reconcile`).

Task lists are derived from standard restaurant HACCP / food-safety opening &
closing routines (fridge/freezer temp logs, hot-holding checks, equipment
clean-down, FIFO rotation, sanitation, lock-up) plus a few cuisine-specific
station tasks.
"""
from __future__ import annotations

from typing import Any, Optional

from . import models

SECONDS_PER_DAY = 86400


def _hhmm(h: int, m: int = 0) -> int:
    """Seconds-into-day for a wall-clock hour:minute."""
    return h * 3600 + m * 60


# Each template: (key, title, category, station_name|None, due_seconds_into_day, [details])
# category ∈ opening | temp | cleaning | closing | prep | safety
_COMMON: list[tuple] = [
    ("open_equipment", "Turn on & preheat cooking equipment", "opening", None, _hhmm(8, 15),
     ["Switch on grill, ovens, fryers and hot-holding units", "Confirm each reaches service temperature"]),
    ("open_fridge_temp", "Record walk-in fridge & freezer temperatures", "temp", None, _hhmm(8, 30),
     ["Fridge must read ≤5°C", "Freezer must read ≤-18°C", "Log readings; report anything out of range"]),
    ("open_sanitize", "Sanitize all prep surfaces & stations", "cleaning", None, _hhmm(8, 30),
     ["Wipe down with food-safe sanitizer", "Fresh sanitizer buckets and cloths at each station"]),
    ("open_handwash", "Check handwashing stations stocked", "safety", None, _hhmm(8, 30),
     ["Soap, hot water and paper towels at every sink"]),
    ("open_mise", "Stock & date-label station mise en place", "prep", None, _hhmm(9, 0),
     ["Restock line to par", "Date-label all prepped items", "FIFO older stock to the front"]),
    ("mid_hothold", "Midday hot-holding temperature check", "temp", None, _hhmm(13, 0),
     ["Hot-held food must stay ≥63°C", "Discard anything held below temp for >2h"]),
    ("mid_fridge_temp", "Afternoon fridge temperature log", "temp", None, _hhmm(15, 0),
     ["Re-check and log all fridge/freezer temps"]),
    ("dinner_probe", "Cooked-food probe temperature spot check", "temp", None, _hhmm(19, 0),
     ["Probe cooked items to ≥75°C core", "Sanitize probe between checks"]),
    ("close_surfaces", "Wipe down & sanitize all surfaces", "cleaning", None, _hhmm(22, 30),
     ["Clear, clean and sanitize every work surface and station"]),
    ("close_fifo", "Rotate, cover & label all stored food", "safety", None, _hhmm(22, 30),
     ["Cover and date-label everything going into storage", "FIFO rotate; discard anything past its life"]),
    ("close_bins", "Empty & sanitize bins, take out trash", "cleaning", None, _hhmm(22, 45),
     ["Empty all bins", "Sanitize bin areas", "Take waste to the outside store"]),
    ("close_floors", "Sweep & mop the kitchen floors", "cleaning", None, _hhmm(22, 45),
     ["Sweep then mop with degreaser", "Wet-floor signs while drying"]),
    ("close_fridge_temp", "Record closing fridge & freezer temperatures", "temp", None, _hhmm(22, 45),
     ["Final temp log for the day"]),
    ("close_lockup", "Switch off & secure equipment, lock up", "closing", None, _hhmm(23, 0),
     ["Power down all equipment", "Check gas and extraction off", "Lock up and set the alarm"]),
]

_BY_CUISINE: dict[str, list[tuple]] = {
    "burger": [
        ("close_grill", "Scrape & clean the grill / flat-top", "cleaning", "Grill", _hhmm(22, 15),
         ["Scrape carbon, degrease and wipe down the flat-top", "Empty and clean the grease trap"]),
        ("close_fryer", "Filter & change fryer oil, clean fryer", "cleaning", "Fry", _hhmm(22, 15),
         ["Filter or change oil per schedule", "Boil-out and wipe down the fryer well"]),
    ],
    "italian": [
        ("close_pasta", "Drain, clean & descale the pasta cooker", "cleaning", "Pasta", _hhmm(22, 15),
         ["Drain and scrub the pasta boiler", "Descale and refill fresh water"]),
        ("close_oven", "Brush out & clean the pizza / pasta oven", "cleaning", "Grill", _hhmm(22, 15),
         ["Brush the stone / deck clean once cooled", "Wipe oven exterior and controls"]),
        ("close_slicer", "Break down & sanitize the deli slicer", "safety", "Cold", _hhmm(22, 30),
         ["Disassemble, wash and sanitize the slicer", "Guard reset before reassembly"]),
    ],
}


def _templates_for(cuisine: Optional[str]) -> list[tuple]:
    extra = _BY_CUISINE.get((cuisine or "").strip().lower(), [])
    return _COMMON + extra


def _station_id_by_name(session: Any) -> dict[str, int]:
    return {s.name: int(s.id) for s in session.query(models.Station).all()}


def ensure_tasks_for_day(session: Any, sim_day: int, cuisine: Optional[str]) -> None:
    """Materialize today's ``KitchenTask`` rows from templates if none exist yet.

    Idempotent: keyed on ``sim_day``.  Safe to call on every board read.
    """
    exists = (
        session.query(models.KitchenTask)
        .filter(models.KitchenTask.sim_day == sim_day)
        .first()
    )
    if exists is not None:
        return

    station_ids = _station_id_by_name(session)
    day_base = sim_day * SECONDS_PER_DAY
    for key, title, category, station_name, due_off, details in _templates_for(cuisine):
        session.add(models.KitchenTask(
            template_key=key,
            sim_day=sim_day,
            title=title,
            category=category,
            station_id=station_ids.get(station_name) if station_name else None,
            due_sim_time=float(day_base + due_off),
            details=list(details),
            status="pending",
            notified_manager=0,
        ))
    session.commit()


def _row_to_dict(t: models.KitchenTask, now: float, station_names: dict[int, str]) -> dict:
    overdue = t.status == "pending" and t.due_sim_time is not None and float(t.due_sim_time) < now
    return {
        "id": int(t.id),
        "template_key": t.template_key,
        "title": t.title,
        "category": t.category,
        "station_id": t.station_id,
        "station": station_names.get(int(t.station_id)) if t.station_id else None,
        "due_sim_time": float(t.due_sim_time) if t.due_sim_time is not None else None,
        "details": list(t.details or []),
        "status": t.status,
        "note": t.note,
        "severity": t.severity,
        "overdue": bool(overdue),
        "done_at": t.done_at,
        "done_by": t.done_by,
    }


def task_board(session: Any, *, now: float, cuisine: Optional[str]) -> dict:
    """Return today's task list with derived overdue state + counts."""
    sim_day = int(now // SECONDS_PER_DAY)
    ensure_tasks_for_day(session, sim_day, cuisine)

    rows = (
        session.query(models.KitchenTask)
        .filter(models.KitchenTask.sim_day == sim_day)
        .order_by(models.KitchenTask.due_sim_time.asc(), models.KitchenTask.id.asc())
        .all()
    )
    station_names = {int(s.id): s.name for s in session.query(models.Station).all()}
    tasks = [_row_to_dict(t, now, station_names) for t in rows]

    counts = {"done": 0, "pending": 0, "overdue": 0, "not_done": 0, "skipped": 0}
    for t in tasks:
        if t["status"] == "done":
            counts["done"] += 1
        elif t["status"] == "not_done":
            counts["not_done"] += 1
        elif t["status"] == "skipped":
            counts["skipped"] += 1
        elif t["overdue"]:
            counts["overdue"] += 1
        else:
            counts["pending"] += 1

    return {
        "generated_at_sim": now,
        "sim_day": sim_day,
        "counts": counts,
        "tasks": tasks,
    }


# Severity → ApprovalRequest.urgency (drives the manager notice's indicative UI).
_SEVERITY_URGENCY = {"high": "high", "medium": "normal", "low": "low"}


def _notify_not_done(approvals: Any, t: models.KitchenTask) -> None:
    """Raise a manager notice that a task was reported not done (with the reason)."""
    if approvals is None:
        return
    note = (t.note or "").strip()
    severity = t.severity or ("high" if t.category in ("temp", "safety") else "medium")
    approvals.create(
        type="kitchen_task",
        title=f"Task not done: {t.title}",
        summary=f"Kitchen reported this not done — {note}" if note else "Kitchen reported this task not done.",
        payload={"task_id": int(t.id), "category": t.category, "template_key": t.template_key,
                 "note": note, "outcome": "not_done", "severity": severity},
        urgency=_SEVERITY_URGENCY.get(severity, "normal"),
        ref_id=int(t.id),
    )


def set_outcome(
    session: Any,
    task_id: int,
    *,
    status: str,
    note: Optional[str] = None,
    severity: Optional[str] = None,
    by: str = "cook",
    now: float,
    approvals: Any = None,
) -> Optional[models.KitchenTask]:
    """Set a task's outcome: ``done`` | ``not_done`` | ``pending``.

    ``not_done`` stores the reason + ``severity`` and immediately raises a
    severity-graded manager notice (idempotent via ``notified_manager``).
    Returns the row, or None if missing.
    """
    t = session.get(models.KitchenTask, task_id)
    if t is None:
        return None
    if status == "done":
        t.status = "done"
        t.done_at = now
        t.done_by = by
        t.severity = None
        if note is not None:
            t.note = note
    elif status == "not_done":
        was_not_done = t.status == "not_done"
        t.status = "not_done"
        t.note = note
        t.severity = severity if severity in ("low", "medium", "high") else None
        t.done_at = now
        t.done_by = by
        t.notified_manager = 1  # also stops reconcile re-escalating it as overdue
        if not was_not_done:
            _notify_not_done(approvals, t)  # first transition → raise the reason once
    else:  # pending (revert)
        t.status = "pending"
        t.note = None
        t.severity = None
        t.done_at = None
        t.done_by = None
    session.commit()
    return t


def reconcile(session: Any, *, now: float, cuisine: Optional[str], approvals: Any) -> list[int]:
    """Raise a manager alert for every task past due and still pending.

    Idempotent per task via the ``notified_manager`` flag.  Returns the list of
    KitchenTask ids newly escalated.  ``approvals`` may be None (headless/tests).
    """
    sim_day = int(now // SECONDS_PER_DAY)
    ensure_tasks_for_day(session, sim_day, cuisine)

    overdue = (
        session.query(models.KitchenTask)
        .filter(
            models.KitchenTask.sim_day == sim_day,
            models.KitchenTask.status == "pending",
            models.KitchenTask.notified_manager == 0,
            models.KitchenTask.due_sim_time < now,
        )
        .all()
    )
    escalated: list[int] = []
    for t in overdue:
        t.notified_manager = 1
        escalated.append(int(t.id))
        if approvals is not None:
            mins_late = max(0, int((now - float(t.due_sim_time)) // 60))
            approvals.create(
                type="kitchen_task",
                title=f"Overdue kitchen task: {t.title}",
                summary=f"Not confirmed by the kitchen ({mins_late} min past due).",
                payload={"task_id": int(t.id), "category": t.category, "template_key": t.template_key},
                urgency="high" if t.category in ("temp", "safety") else "normal",
                ref_id=int(t.id),
            )
    session.commit()
    return escalated


def _demo() -> None:
    """Self-check: templates materialize once, overdue escalates once."""
    from . import db
    db.reset_db(keep_reference=False)
    session = db.new_session()
    try:
        session.add(models.Station(id=1, name="Grill"))
        session.add(models.Station(id=2, name="Fry"))
        session.commit()

        # Day 0, 08:00 — nothing overdue yet.
        board = task_board(session, now=_hhmm(8, 0), cuisine="burger")
        n = len(board["tasks"])
        assert n == len(_COMMON) + len(_BY_CUISINE["burger"]), n
        assert board["counts"]["overdue"] == 0

        # Idempotent: second call must not duplicate.
        board2 = task_board(session, now=_hhmm(8, 5), cuisine="burger")
        assert len(board2["tasks"]) == n, len(board2["tasks"])

        # After close — everything overdue; reconcile escalates once each.
        esc = reconcile(session, now=_hhmm(23, 30), cuisine="burger", approvals=None)
        assert len(esc) == n, (len(esc), n)
        esc2 = reconcile(session, now=_hhmm(23, 31), cuisine="burger", approvals=None)
        assert esc2 == [], esc2

        # Confirm one task clears its overdue state.
        tid = board["tasks"][0]["id"]
        set_outcome(session, tid, status="done", by="cook", now=_hhmm(23, 32))
        board3 = task_board(session, now=_hhmm(23, 33), cuisine="burger")
        assert board3["counts"]["done"] == 1, board3["counts"]

        # Report a task not done → status + note + one manager notice.
        class _Rec:
            def __init__(self): self.calls = []
            def create(self, **kw): self.calls.append(kw)
        rec = _Rec()
        tid2 = board["tasks"][1]["id"]
        set_outcome(session, tid2, status="not_done", note="ran out of degreaser",
                    by="cook", now=_hhmm(23, 34), approvals=rec)
        b4 = task_board(session, now=_hhmm(23, 35), cuisine="burger")
        t2 = next(t for t in b4["tasks"] if t["id"] == tid2)
        assert t2["status"] == "not_done" and t2["note"] == "ran out of degreaser", t2
        assert len(rec.calls) == 1 and rec.calls[0]["type"] == "kitchen_task", rec.calls
        # Idempotent: repeat doesn't re-notify.
        set_outcome(session, tid2, status="not_done", note="still no degreaser",
                    by="cook", now=_hhmm(23, 36), approvals=rec)
        assert len(rec.calls) == 1, rec.calls
        print("kitchen_tasks demo OK:", n, "tasks")
    finally:
        session.close()


if __name__ == "__main__":
    _demo()
