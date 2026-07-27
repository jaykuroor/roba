"""Operational snapshot builder for the reasoning model.

Produces a compact, JSON-serialisable dict that captures the restaurant's
current state with real numbers:

  dishes   — per-item forecast demand + price → revenue estimate
  staff    — who is present / on leave, which stations they cover, sole-cover flags
  stations — which stations have at least one available staffer
  constraints — which items are blocked and why, which ingredients are at/below threshold

Used by VoiceActions.consult_reasoner() to give Gemini Pro real numbers for
impact / counterfactual questions like "how much will we lose if chef X is on leave?"
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# How many seconds of sim-time represent "today" for the forecast query
_TODAY_SECONDS = 86400.0


def build_ops_snapshot(
    db_session_factory: Callable[[], Any],
    forecaster: Optional[Any],
    *,
    bus: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return a compact operational snapshot dict.

    Falls back gracefully if any data source is unavailable.
    """
    from sqlalchemy import func  # noqa: PLC0415

    from .models import (  # noqa: PLC0415
        Attendance,
        Ingredient,
        InventoryLevel,
        MenuItem,
        MenuToggle,
        Order,
        RecipeLine,
        Recipe,
        Staff,
        Station,
        StaffStation,
        Forecast,
    )
    from .clock import SECONDS_PER_DAY  # noqa: PLC0415

    session = db_session_factory()
    try:
        now = float(bus.sim_time) if bus is not None else 0.0
        day = int(now // SECONDS_PER_DAY)

        # ------------------------------------------------------------------
        # 1. Menu items — price and active status
        # ------------------------------------------------------------------
        menu_items = session.query(MenuItem).order_by(MenuItem.id.asc()).all()
        station_map = {s.id: s.name for s in session.query(Station).all()}

        # ------------------------------------------------------------------
        # 2. Per-dish latest forecast qty (from most recent Forecast rows)
        # ------------------------------------------------------------------
        # Use the latest Forecast row per menu_item_id as a cheap "today" estimate.
        # If the forecaster is available and cheap, a quick interval call is skipped
        # here to keep snapshot latency low — the existing Forecast rows are sufficient
        # for the reasoner to quantify impacts.
        latest_forecast: Dict[int, int] = {}
        for fc in session.query(Forecast).order_by(Forecast.generated_at.desc()).limit(500).all():
            mid = int(fc.menu_item_id)
            if mid not in latest_forecast:
                latest_forecast[mid] = int(fc.forecast_qty or 0)

        # ------------------------------------------------------------------
        # 3. Staff availability and station coverage
        # ------------------------------------------------------------------
        all_staff = session.query(Staff).filter(Staff.active == 1).all()
        staff_station_links = session.query(StaffStation).all()
        attendance_today = (
            session.query(Attendance)
            .filter(Attendance.date_sim_day == day)
            .order_by(Attendance.sim_time.desc())
            .all()
        )

        # Determine each active staffer's attendance status
        # (last attendance record for today wins; default = present)
        staff_status: Dict[int, str] = {}
        for s in all_staff:
            staff_status[int(s.id)] = "present"
        for att in attendance_today:
            sid = int(att.staff_id)
            if sid in staff_status:
                existing = staff_status[sid]
                if existing == "present" and att.status in {"leave", "sick"}:
                    staff_status[sid] = att.status

        # Build station → available staff mapping
        station_to_available: Dict[int, List[int]] = {}
        station_to_all: Dict[int, List[int]] = {}
        for link in staff_station_links:
            sid = int(link.staff_id)
            stn = int(link.station_id)
            station_to_all.setdefault(stn, []).append(sid)
            if staff_status.get(sid) == "present":
                station_to_available.setdefault(stn, []).append(sid)

        # Sole-cover: a staffer is sole-cover for a station if they're the ONLY
        # available qualified person — removing them would unstuff the station.
        sole_cover_map: Dict[int, List[int]] = {}  # staff_id → [station_ids they solely cover]
        for stn, avail_staff in station_to_available.items():
            if len(avail_staff) == 1:
                sid = avail_staff[0]
                sole_cover_map.setdefault(sid, []).append(stn)

        # Build staff info dict
        staff_id_to_name: Dict[int, str] = {int(s.id): str(s.name or "") for s in all_staff}
        staff_id_to_role: Dict[int, str] = {int(s.id): str(s.role or "") for s in all_staff}

        # Dishes at each station (for "covers these dishes" field)
        station_to_dishes: Dict[int, List[str]] = {}
        for mi in menu_items:
            if mi.station_id:
                station_to_dishes.setdefault(int(mi.station_id), []).append(str(mi.name or ""))

        staff_list: List[Dict[str, Any]] = []
        for s in all_staff:
            sid = int(s.id)
            covered_stations = [stn for stn, sids in station_to_all.items() if sid in sids]
            covered_dishes: List[str] = []
            for stn in covered_stations:
                covered_dishes.extend(station_to_dishes.get(stn, []))
            sole_stns = sole_cover_map.get(sid, [])
            sole_dishes: List[str] = []
            for stn in sole_stns:
                sole_dishes.extend(station_to_dishes.get(stn, []))
            staff_list.append({
                "id": sid,
                "name": str(s.name or ""),
                "role": str(s.role or ""),
                "status": staff_status.get(sid, "present"),
                "covers_stations": [station_map.get(stn, str(stn)) for stn in covered_stations],
                "covers_dishes": list(set(covered_dishes)),
                "sole_cover_for_stations": [station_map.get(stn, str(stn)) for stn in sole_stns],
                "sole_cover_dishes_at_risk": list(set(sole_dishes)),
            })

        # ------------------------------------------------------------------
        # 4. Stations summary
        # ------------------------------------------------------------------
        stations_list: List[Dict[str, Any]] = []
        for stn_id, stn_name in station_map.items():
            avail = station_to_available.get(stn_id, [])
            stations_list.append({
                "station": stn_name,
                "covered": bool(avail),
                "available_staff": [staff_id_to_name.get(sid, str(sid)) for sid in avail],
                "dishes": station_to_dishes.get(stn_id, []),
            })

        # ------------------------------------------------------------------
        # 5. Per-dish summary with revenue estimate
        # ------------------------------------------------------------------
        # Look up disable reasons — use human-readable code, not the internal reason field
        _reason_labels = {
            "out_of_stock": "out of stock",
            "station_unstaffed": "station unstaffed",
            "manual": "manually disabled",
        }
        item_disable_reasons: Dict[int, str] = {}
        for mt in (
            session.query(MenuToggle)
            .filter(MenuToggle.action == "disable", MenuToggle.active == 1)
            .all()
        ):
            mid = int(mt.menu_item_id)
            if mid not in item_disable_reasons:
                code = mt.reason_code or "manual"
                label = _reason_labels.get(code, code)
                item_disable_reasons[mid] = label

        dishes_list: List[Dict[str, Any]] = []
        for mi in menu_items:
            mid = int(mi.id)
            fc_qty = latest_forecast.get(mid, 0)
            price = float(mi.dine_in_price or 0)
            dishes_list.append({
                "id": mid,
                "name": str(mi.name or ""),
                "active": bool(mi.active),
                "station": station_map.get(int(mi.station_id or 0), "unknown"),
                "price": price,
                "forecast_qty": fc_qty,
                "revenue_estimate": round(fc_qty * price, 2),
                "disabled_reason": item_disable_reasons.get(mid),
            })

        # ------------------------------------------------------------------
        # 6. Low-stock / OOS constraints
        # ------------------------------------------------------------------
        low_stock_items: List[Dict[str, Any]] = []
        from .formatter import format_qty as _fq
        for inv in session.query(InventoryLevel).all():
            oh = float(inv.on_hand_cached or 0)
            ss = float(inv.safety_stock or 0)
            if oh <= max(ss, 0):
                ing = session.get(Ingredient, inv.ingredient_id)
                base_unit = str(ing.base_unit or "g") if ing else "g"
                low_stock_items.append({
                    "ingredient": ing.name if ing else str(inv.ingredient_id),
                    "on_hand": oh,
                    "on_hand_display": _fq(oh, base_unit),
                    "safety_stock": ss,
                    "safety_stock_display": _fq(ss, base_unit),
                    "unit": base_unit,
                    "status": "depleted" if oh <= 0 else "below_safety_stock",
                })

        # ------------------------------------------------------------------
        # 7. Kitchen ticket backlog (docs/fable/portfolio-overview.md)
        # ------------------------------------------------------------------
        ticket_counts = dict(
            session.query(Order.kitchen_status, func.count(Order.id))
            .filter(Order.kitchen_status.in_(("queued", "cooking")))
            .group_by(Order.kitchen_status)
            .all()
        )
        # Rolling average over the last sim-hour: recent enough to react to a
        # cook going home, long enough not to swing on a single ticket.
        recently_served = (
            session.query(Order.sim_time, Order.served_at)
            .filter(Order.served_at.isnot(None), Order.served_at >= now - 3600.0)
            .all()
        )
        avg_ticket_minutes = (
            round(
                sum(float(sa) - float(st or 0.0) for st, sa in recently_served)
                / len(recently_served)
                / 60.0,
                1,
            )
            if recently_served
            else None
        )

    finally:
        session.close()

    return {
        "sim_time": now,
        "dishes": dishes_list,
        "staff": staff_list,
        "stations": stations_list,
        "low_stock_ingredients": low_stock_items,
        "queued_count": int(ticket_counts.get("queued", 0)),
        "cooking_count": int(ticket_counts.get("cooking", 0)),
        "avg_ticket_minutes": avg_ticket_minutes,
        "note": (
            "forecast_qty is the most recent per-item demand forecast. "
            "revenue_estimate = forecast_qty × price. "
            "sole_cover_dishes_at_risk lists dishes that would lose their only "
            "qualified cook if that staffer is absent. "
            "queued_count is the kitchen ticket backlog and avg_ticket_minutes "
            "the average order-to-served time over the last sim-hour."
        ),
    }
