"""Track A staff coverage agent."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from core.agent_base import BaseAgent
from core.clock import SECONDS_PER_DAY
from core.models import Attendance, MenuItem, Signal, Staff, StaffStation, Station
from core.signals import SignalType

from .forecaster import current_daypart


class StaffAgent(BaseAgent):
    """Computes station coverage from core staff/attendance tables."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        super().__init__(bus, db_session_factory, "track_a.staff")
        self.ws_broadcast = ws_broadcast
        self.subscribe(["forecasting"])

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            self.recompute,
            interval_sim_s=1800.0,
            name="track_a_staff_coverage",
        )

    def on_signal(self, signal: Signal) -> None:
        if signal.type == SignalType.USER_FACT.value:
            intent = (signal.payload or {}).get("intent")
            if intent in {"set_leave", "set_attendance"}:
                self.recompute()

    def recompute(self) -> List[Dict[str, Any]]:
        now = float(self.bus.sim_time)
        day = int(now // SECONDS_PER_DAY)
        daypart = current_daypart(now)
        emitted: List[Dict[str, Any]] = []
        session = self.db_session_factory()
        try:
            stations = session.query(Station.id, Station.name).order_by(Station.id.asc()).all()
            for station_id, station_name in stations:
                item_ids = [
                    row[0]
                    for row in session.query(MenuItem)
                    .with_entities(MenuItem.id)
                    .filter(MenuItem.station_id == station_id, MenuItem.active == 1)
                    .all()
                ]
                if not item_ids:
                    continue
                staff_links = (
                    session.query(StaffStation)
                    .with_entities(StaffStation.staff_id)
                    .filter(StaffStation.station_id == station_id)
                    .all()
                )
                available = [
                    link[0]
                    for link in staff_links
                    if self._is_staff_available(session, link[0], day, daypart)
                ]
                covered = len(available) > 0
                payload = {
                    "station_id": station_id,
                    "covered": covered,
                    "affected_items": [] if covered else item_ids,
                    "shortfall": 0.0 if covered else 1.0,
                }
                self.emit(
                    SignalType.STAFF_COVERAGE,
                    payload,
                    ttl=shift_ttl(now),
                    dedup_key=f"coverage:{station_id}",
                )
                self.log_event(
                    "staff",
                    f"{station_name} coverage {'restored' if covered else 'missing'}",
                    {**payload, "daypart": daypart, "available_staff": available},
                )
                emitted.append({**payload, "station": station_name, "available_staff": available})
        finally:
            session.close()
        self._broadcast("staff_coverage", {"coverage": emitted})
        return emitted

    @staticmethod
    def _is_staff_available(session: Any, staff_id: int, day: int, daypart: str) -> bool:
        staff = session.get(Staff, staff_id)
        if staff is None or not staff.active:
            return False
        rows = (
            session.query(Attendance)
            .filter(Attendance.staff_id == staff_id, Attendance.date_sim_day == day)
            .order_by(Attendance.sim_time.desc(), Attendance.id.desc())
            .all()
        )
        status = "present"
        for row in rows:
            if row.daypart not in (None, daypart):
                continue
            status = row.status or "present"
            break
        return status not in {"leave", "sick"}

    def call_in_sick(
        self,
        staff_id: Optional[int] = None,
        station_id: Optional[int] = None,
        daypart: Optional[str] = None,
        status: str = "sick",
        reason: str = "called in sick",
    ) -> Dict[str, Any]:
        now = float(self.bus.sim_time)
        day = int(now // SECONDS_PER_DAY)
        session = self.db_session_factory()
        try:
            resolved_staff_id = staff_id
            if resolved_staff_id is None and station_id is not None:
                link = (
                    session.query(StaffStation)
                    .filter(StaffStation.station_id == station_id)
                    .order_by(StaffStation.id.asc())
                    .first()
                )
                resolved_staff_id = link.staff_id if link is not None else None
            row = Attendance(
                staff_id=resolved_staff_id,
                date_sim_day=day,
                status=status,
                daypart=daypart,
                reason=reason,
                sim_time=now,
            )
            session.add(row)
            session.flush()
            attendance_id = row.id
            session.commit()
            result = {"attendance_id": attendance_id, "staff_id": resolved_staff_id, "status": status}
        finally:
            session.close()
        self.recompute()
        return result

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)


def shift_ttl(now: float) -> float:
    day_end = (int(now // SECONDS_PER_DAY) + 1) * SECONDS_PER_DAY
    return max(day_end - now, 1.0)
