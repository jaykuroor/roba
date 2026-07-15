"""Staff shift check-in for the cook desk staff board.

Two modes (stored on ``SimSettings.staff_checkin_mode``):

  * ``sim_auto`` (default) — every active staff member is auto-marked *present*
    at shift start; the cook never has to check anyone in manually.
  * ``manual`` — everyone starts *absent* and stays that way until they check
    in.  Late (checked in after the grace period) and absent (never checked in
    by the cutoff) staff are surfaced to the manager desk.

Kept deliberately separate from ``Attendance``/``availability`` (which drive
leave/sick menu-disabling): a no-show here notifies the manager but does not
auto-disable the menu.
"""
from __future__ import annotations

from typing import Any, Optional

from . import models

SECONDS_PER_DAY = 86400
SHIFT_START_OFF = 28800   # 08:00 — restaurant open (matches clock.DAY_OPEN_OFFSET)
LATE_GRACE_S = 15 * 60    # checked in more than 15 min after start ⇒ late
ABSENT_CUTOFF_S = 60 * 60  # never checked in by start+60 min ⇒ absent alert

MODE_SIM_AUTO = "sim_auto"
MODE_MANUAL = "manual"


def get_mode(session: Any) -> str:
    s = session.get(models.SimSettings, 1)
    mode = getattr(s, "staff_checkin_mode", None) if s is not None else None
    return mode if mode in (MODE_SIM_AUTO, MODE_MANUAL) else MODE_SIM_AUTO


def set_mode(session: Any, mode: str, *, now: float) -> str:
    """Switch check-in mode and re-initialize today's rows for the new mode."""
    mode = mode if mode in (MODE_SIM_AUTO, MODE_MANUAL) else MODE_SIM_AUTO
    s = session.get(models.SimSettings, 1)
    if s is None:
        s = models.SimSettings(id=1)
        session.add(s)
    s.staff_checkin_mode = mode
    session.commit()
    _reset_day(session, int(now // SECONDS_PER_DAY), mode)
    return mode


def _active_staff(session: Any) -> list[models.Staff]:
    return session.query(models.Staff).filter(models.Staff.active == 1).all()


def _reset_day(session: Any, sim_day: int, mode: str) -> None:
    """Delete today's check-in rows and re-seed from the current mode."""
    session.query(models.ShiftCheckin).filter(
        models.ShiftCheckin.sim_day == sim_day
    ).delete(synchronize_session=False)
    session.commit()
    ensure_checkins_for_day(session, sim_day, mode)


def ensure_checkins_for_day(session: Any, sim_day: int, mode: str) -> None:
    """Create a ShiftCheckin row for every active staff member missing one today.

    ``sim_auto`` seeds rows as present (auto); ``manual`` seeds them as absent.
    Additive — safe on every board read, and picks up staff added mid-day.
    """
    shift_start = float(sim_day * SECONDS_PER_DAY + SHIFT_START_OFF)
    existing = {
        r.staff_id
        for r in session.query(models.ShiftCheckin.staff_id)
        .filter(models.ShiftCheckin.sim_day == sim_day)
        .all()
    }
    added = False
    for s in _active_staff(session):
        if s.id in existing:
            continue
        if mode == MODE_SIM_AUTO:
            row = models.ShiftCheckin(
                staff_id=s.id, sim_day=sim_day, status="present",
                shift_start=shift_start, checked_in_at=shift_start,
                source="auto", notified_manager=0,
            )
        else:
            row = models.ShiftCheckin(
                staff_id=s.id, sim_day=sim_day, status="absent",
                shift_start=shift_start, checked_in_at=None,
                source="manual", notified_manager=0,
            )
        session.add(row)
        added = True
    if added:
        session.commit()


def _display_status(row: models.ShiftCheckin, now: float) -> str:
    """Board-facing status: absent rows read 'upcoming' before shift start."""
    if row.status == "absent" and now < float(row.shift_start):
        return "upcoming"
    return row.status


def checkin(session: Any, staff_id: int, *, now: float) -> Optional[dict]:
    """Clock a staff member in.  Present, or late if past the grace period."""
    sim_day = int(now // SECONDS_PER_DAY)
    ensure_checkins_for_day(session, sim_day, get_mode(session))
    row = (
        session.query(models.ShiftCheckin)
        .filter(models.ShiftCheckin.staff_id == staff_id, models.ShiftCheckin.sim_day == sim_day)
        .first()
    )
    if row is None:
        return None
    late = now > float(row.shift_start) + LATE_GRACE_S
    row.status = "late" if late else "present"
    row.checked_in_at = now
    session.commit()
    return {"staff_id": staff_id, "status": row.status, "checked_in_at": now}


def undo_checkin(session: Any, staff_id: int, *, now: float) -> Optional[dict]:
    """Revert a check-in back to absent (manual-mode correction)."""
    sim_day = int(now // SECONDS_PER_DAY)
    row = (
        session.query(models.ShiftCheckin)
        .filter(models.ShiftCheckin.staff_id == staff_id, models.ShiftCheckin.sim_day == sim_day)
        .first()
    )
    if row is None:
        return None
    row.status = "absent"
    row.checked_in_at = None
    session.commit()
    return {"staff_id": staff_id, "status": "absent"}


def staff_board(session: Any, *, now: float) -> dict:
    """Return the staff check-in board for today: mode, per-staff rows, counts."""
    sim_day = int(now // SECONDS_PER_DAY)
    mode = get_mode(session)
    ensure_checkins_for_day(session, sim_day, mode)

    staff = {s.id: s for s in _active_staff(session)}
    rows = (
        session.query(models.ShiftCheckin)
        .filter(models.ShiftCheckin.sim_day == sim_day)
        .all()
    )
    out = []
    counts = {"present": 0, "late": 0, "absent": 0, "upcoming": 0}
    for r in rows:
        s = staff.get(r.staff_id)
        if s is None:
            continue  # deactivated mid-day — drop from the board
        disp = _display_status(r, now)
        counts[disp] = counts.get(disp, 0) + 1
        out.append({
            "staff_id": int(r.staff_id),
            "name": s.name,
            "role": s.role,
            "status": disp,
            "shift_start": float(r.shift_start),
            "checked_in_at": r.checked_in_at,
            "source": r.source,
        })
    out.sort(key=lambda x: (x["status"] != "absent", x["name"] or ""))
    return {
        "generated_at_sim": now,
        "sim_day": sim_day,
        "mode": mode,
        "counts": counts,
        "staff": out,
    }


def reconcile(session: Any, *, now: float, approvals: Any) -> list[int]:
    """Escalate late / absent staff to the manager desk (manual mode only).

    Idempotent per staff via ``notified_manager``.  Returns escalated staff ids.
    ``sim_auto`` never escalates.  ``approvals`` may be None (headless/tests).
    """
    sim_day = int(now // SECONDS_PER_DAY)
    mode = get_mode(session)
    ensure_checkins_for_day(session, sim_day, mode)
    if mode != MODE_MANUAL:
        return []

    staff = {s.id: s for s in _active_staff(session)}
    rows = (
        session.query(models.ShiftCheckin)
        .filter(
            models.ShiftCheckin.sim_day == sim_day,
            models.ShiftCheckin.notified_manager == 0,
        )
        .all()
    )
    escalated: list[int] = []
    for r in rows:
        s = staff.get(r.staff_id)
        if s is None:
            continue
        past_cutoff = now >= float(r.shift_start) + ABSENT_CUTOFF_S
        if r.status == "late":
            reason = "checked in late"
        elif r.status == "absent" and past_cutoff:
            reason = "has not checked in for their shift"
        else:
            continue  # present, or absent-but-still-within-cutoff
        r.notified_manager = 1
        escalated.append(int(r.staff_id))
        if approvals is not None:
            approvals.create(
                type="staff_shift",
                title=f"{s.name} {reason}",
                summary=f"{s.name} ({s.role}) {reason}.",
                payload={"staff_id": int(r.staff_id), "status": r.status, "role": s.role},
                urgency="normal",
                ref_id=int(r.staff_id),
            )
    session.commit()
    return escalated


def _demo() -> None:
    """Self-check: sim-auto = all present/no alerts; manual = late+absent escalate once."""
    from . import db
    db.reset_db(keep_reference=False)
    session = db.new_session()
    try:
        for i, name in enumerate(["Jake", "Maria", "Chen"], start=1):
            session.add(models.Staff(id=i, name=name, role="cook", active=1))
        session.add(models.SimSettings(id=1, staff_checkin_mode=MODE_SIM_AUTO))
        session.commit()

        # sim-auto: everyone present, nothing escalates.
        b = staff_board(session, now=float(SHIFT_START_OFF + 3600))
        assert b["counts"]["present"] == 3, b["counts"]
        assert reconcile(session, now=float(SHIFT_START_OFF + 3600), approvals=None) == []

        # switch to manual → all absent.
        set_mode(session, MODE_MANUAL, now=float(SHIFT_START_OFF - 600))
        b = staff_board(session, now=float(SHIFT_START_OFF - 600))
        assert b["counts"]["upcoming"] == 3, b["counts"]  # before shift start

        # Jake checks in on time, Maria late, Chen never.
        checkin(session, 1, now=float(SHIFT_START_OFF + 60))
        checkin(session, 2, now=float(SHIFT_START_OFF + LATE_GRACE_S + 300))
        b = staff_board(session, now=float(SHIFT_START_OFF + ABSENT_CUTOFF_S + 60))
        assert b["counts"]["present"] == 1 and b["counts"]["late"] == 1 and b["counts"]["absent"] == 1, b["counts"]

        esc = sorted(reconcile(session, now=float(SHIFT_START_OFF + ABSENT_CUTOFF_S + 60), approvals=None))
        assert esc == [2, 3], esc  # Maria (late) + Chen (absent), not Jake
        assert reconcile(session, now=float(SHIFT_START_OFF + ABSENT_CUTOFF_S + 120), approvals=None) == []
        print("staff_shifts demo OK")
    finally:
        session.close()


if __name__ == "__main__":
    _demo()
