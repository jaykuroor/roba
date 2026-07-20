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
    due = float(t.due_sim_time) if t.due_sim_time is not None else None
    late_min = 0
    if t.status == "done" and due is not None and t.done_at is not None:
        late_min = max(0, int((float(t.done_at) - due) // 60))
    return {
        "late": late_min > 0,
        "late_min": late_min,
        "overdue_min": max(0, int((now - due) // 60)) if overdue and due is not None else 0,
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

    counts = {"done": 0, "done_late": 0, "pending": 0, "overdue": 0, "not_done": 0, "skipped": 0}
    for t in tasks:
        if t["status"] == "done":
            counts["done"] += 1
            if t["late"]:
                counts["done_late"] += 1
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

# Escalation ladder for overdue/late notices. A task's single notice climbs
# this as it stays overdue (docs/fable/approvals.md): tier 1 at 5 min past
# due, tier 2 at 10, tier 3 at 15 (config.TASK_OVERDUE_NOTICE_TIERS_MIN).
_TIER_URGENCY = ["normal", "high", "critical"]


def _tiers_min() -> list[int]:
    from . import config
    return list(config.TASK_OVERDUE_NOTICE_TIERS_MIN)


def overdue_tier(overdue_min: float, tiers: Optional[list[int]] = None) -> int:
    """0 = within grace (no notice); 1..len(tiers) = escalation tier."""
    tiers = _tiers_min() if tiers is None else tiers
    return sum(1 for threshold in tiers if overdue_min >= threshold)


def tier_urgency(tier: int, category: Optional[str]) -> str:
    """Urgency for a tier; temp/safety tasks run one tier hotter."""
    idx = (tier - 1) + (1 if category in ("temp", "safety") else 0)
    return _TIER_URGENCY[max(0, min(idx, len(_TIER_URGENCY) - 1))]


def _find_pending_notice(session: Any, task_id: int) -> Optional[models.ApprovalRequest]:
    return (
        session.query(models.ApprovalRequest)
        .filter(
            models.ApprovalRequest.type == "kitchen_task",
            models.ApprovalRequest.ref_id == int(task_id),
            models.ApprovalRequest.status == "pending",
        )
        .order_by(models.ApprovalRequest.id.desc())
        .first()
    )


def _upsert_task_notice(
    approvals: Any,
    session: Any,
    t: models.KitchenTask,
    *,
    title: str,
    summary: str,
    urgency: str,
    extra_payload: dict,
) -> None:
    """One evolving notice per task: update the pending row if it exists,
    create it otherwise. ``approvals`` may be None (headless/tests)."""
    if approvals is None:
        return
    payload = {
        "task_id": int(t.id),
        "category": t.category,
        "template_key": t.template_key,
        **extra_payload,
    }
    existing = _find_pending_notice(session, int(t.id))
    if existing is not None:
        approvals.update(
            int(existing.id), title=title, summary=summary, urgency=urgency, payload=payload
        )
    else:
        approvals.create(
            type="kitchen_task",
            title=title,
            summary=summary,
            payload=payload,
            urgency=urgency,
            ref_id=int(t.id),
        )


def _notify_not_done(approvals: Any, session: Any, t: models.KitchenTask) -> None:
    """Raise/refresh the manager notice that a task was reported not done."""
    note = (t.note or "").strip()
    severity = t.severity or ("high" if t.category in ("temp", "safety") else "medium")
    _upsert_task_notice(
        approvals, session, t,
        title=f"Task not done: {t.title}",
        summary=f"Kitchen reported this not done — {note}" if note else "Kitchen reported this task not done.",
        urgency=_SEVERITY_URGENCY.get(severity, "normal"),
        extra_payload={"note": note, "outcome": "not_done", "severity": severity},
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
        # Done late past the first tier → the overdue notice (if any) flips to
        # "done late" at the lateness-matched urgency instead of going stale.
        late_min = (
            (now - float(t.due_sim_time)) / 60.0 if t.due_sim_time is not None else 0.0
        )
        tier = overdue_tier(late_min)
        if tier > 0:
            _upsert_task_notice(
                approvals, session, t,
                title=f"Task done late: {t.title}",
                summary=f"Completed by the kitchen {int(late_min)} min after its due time.",
                urgency=tier_urgency(tier, t.category),
                extra_payload={"outcome": "done_late", "late_min": int(late_min), "tier": tier},
            )
    elif status == "not_done":
        was_not_done = t.status == "not_done"
        t.status = "not_done"
        t.note = note
        t.severity = severity if severity in ("low", "medium", "high") else None
        t.done_at = now
        t.done_by = by
        t.notified_manager = len(_tiers_min())  # stops reconcile re-escalating it
        if not was_not_done:
            _notify_not_done(approvals, session, t)  # first transition → raise once
    else:  # pending (revert)
        t.status = "pending"
        t.note = None
        t.severity = None
        t.done_at = None
        t.done_by = None
        t.notified_manager = 0
    session.commit()
    return t


def reconcile(session: Any, *, now: float, cuisine: Optional[str], approvals: Any) -> list[int]:
    """Escalate every pending task past due through the notice tiers.

    Each task owns ONE notice that is created at the first tier (5 min past
    due) and updated in place as it crosses later tiers (10, 15 min) with
    climbing urgency. ``notified_manager`` stores the last-notified tier, so
    the sweep is idempotent per tier. Returns task ids escalated this call.
    ``approvals`` may be None (headless/tests) — tiers still advance.
    """
    sim_day = int(now // SECONDS_PER_DAY)
    ensure_tasks_for_day(session, sim_day, cuisine)

    tiers = _tiers_min()
    overdue = (
        session.query(models.KitchenTask)
        .filter(
            models.KitchenTask.sim_day == sim_day,
            models.KitchenTask.status == "pending",
            models.KitchenTask.notified_manager < len(tiers),
            models.KitchenTask.due_sim_time < now,
        )
        .all()
    )
    escalated: list[int] = []
    for t in overdue:
        overdue_min = (now - float(t.due_sim_time)) / 60.0
        tier = overdue_tier(overdue_min, tiers)
        if tier <= int(t.notified_manager or 0):
            continue
        t.notified_manager = tier
        escalated.append(int(t.id))
        _upsert_task_notice(
            approvals, session, t,
            title=f"Overdue kitchen task: {t.title}",
            summary=(
                f"Not confirmed by the kitchen — {int(overdue_min)} min past due "
                f"(escalation {tier}/{len(tiers)})."
            ),
            urgency=tier_urgency(tier, t.category),
            extra_payload={"outcome": "overdue", "overdue_min": int(overdue_min), "tier": tier},
        )
    session.commit()
    return escalated


class _DemoHub:
    """Minimal ApprovalsHub stand-in that persists real rows (for _demo/tests)."""

    def __init__(self, session: Any):
        self.session = session
        self.creates: list = []
        self.updates: list = []

    def create(self, **kw):
        row = models.ApprovalRequest(
            type=kw["type"], title=kw["title"], summary=kw["summary"],
            payload=kw.get("payload"), urgency=kw.get("urgency", "normal"),
            status="pending", created_at=0.0, ref_id=kw.get("ref_id"),
        )
        self.session.add(row)
        self.session.commit()
        self.creates.append(kw)
        return row

    def update(self, approval_id, **kw):
        row = self.session.get(models.ApprovalRequest, approval_id)
        for key in ("title", "summary", "urgency", "payload"):
            if kw.get(key) is not None:
                setattr(row, key, kw[key])
        self.session.commit()
        self.updates.append((approval_id, kw))
        return row


def _demo() -> None:
    """Self-check: templates materialize once; ONE notice per task escalates
    through tiers and flips to done-late; not_done notifies once."""
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

        hub = _DemoHub(session)
        tid = board["tasks"][0]["id"]  # "Turn on & preheat" — due 08:15, category=opening

        # 2 min past due: within grace, no notice.
        assert reconcile(session, now=_hhmm(8, 17), cuisine="burger", approvals=hub) == []
        # 6 min past due: tier 1 → ONE create, urgency normal.
        assert reconcile(session, now=_hhmm(8, 21), cuisine="burger", approvals=hub) == [tid]
        assert len(hub.creates) == 1 and hub.creates[0]["urgency"] == "normal", hub.creates
        # 11 min past due: tier 2 → same notice UPDATED to high, no new create.
        assert reconcile(session, now=_hhmm(8, 26), cuisine="burger", approvals=hub) == [tid]
        assert len(hub.creates) == 1 and len(hub.updates) == 1, (hub.creates, hub.updates)
        assert hub.updates[0][1]["urgency"] == "high", hub.updates
        # Cook completes it 17 min late → notice flips to done-late, critical.
        set_outcome(session, tid, status="done", by="cook", now=_hhmm(8, 32), approvals=hub)
        assert hub.updates[-1][1]["title"].startswith("Task done late"), hub.updates[-1]
        assert hub.updates[-1][1]["urgency"] == "critical", hub.updates[-1]
        pending = session.query(models.ApprovalRequest).filter_by(status="pending").count()
        assert pending == 1, pending  # one evolving notice, never a pile

        board3 = task_board(session, now=_hhmm(8, 33), cuisine="burger")
        t1 = next(t for t in board3["tasks"] if t["id"] == tid)
        assert t1["late"] and t1["late_min"] == 17, t1
        assert board3["counts"]["done_late"] == 1, board3["counts"]

        # After close — everything else escalates straight to top tier once.
        esc = reconcile(session, now=_hhmm(23, 30), cuisine="burger", approvals=None)
        assert len(esc) == n - 1, (len(esc), n)
        assert reconcile(session, now=_hhmm(23, 31), cuisine="burger", approvals=None) == []

        # Report a task not done → status + note + one manager notice.
        tid2 = board["tasks"][1]["id"]
        before = len(hub.creates)
        set_outcome(session, tid2, status="not_done", note="ran out of degreaser",
                    by="cook", now=_hhmm(23, 34), approvals=hub)
        b4 = task_board(session, now=_hhmm(23, 35), cuisine="burger")
        t2 = next(t for t in b4["tasks"] if t["id"] == tid2)
        assert t2["status"] == "not_done" and t2["note"] == "ran out of degreaser", t2
        assert len(hub.creates) == before + 1, hub.creates
        # Idempotent: repeat doesn't re-notify.
        set_outcome(session, tid2, status="not_done", note="still no degreaser",
                    by="cook", now=_hhmm(23, 36), approvals=hub)
        assert len(hub.creates) == before + 1, hub.creates
        print("kitchen_tasks demo OK:", n, "tasks")
    finally:
        session.close()


if __name__ == "__main__":
    _demo()
