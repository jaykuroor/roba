"""Deterministic menu-item availability resolver.

Computes which menu items must be disabled based on two independent reasons:

  out_of_stock     — an ingredient used in the recipe has on_hand ≤ 0
  station_unstaffed — the station for this item has no available staff this daypart

Keeps the state as ``MenuToggle`` rows with ``reason_code`` set. The rules:

  • Auto-disable: write a MenuToggle(action="disable", reason_code=X, active=1)
                  and flip MenuItem.active = 0.
  • Auto-enable:  only when NO active system blocks (out_of_stock OR station_unstaffed)
                  remain for the item AND no active manual block exists.
  • Manual disables (reason_code="manual") are STICKY — auto-enable never clears them.
    They must be explicitly cleared by an enable_menu_item() call.
  • If an item was already disabled before this system existed (no reason_code), treat
    it as if it has reason_code="manual" for safety (don't auto-enable it).

Callers:
  • VoiceActions.record_spoilage()  → changed_ingredient_ids
  • VoiceActions.set_staff_attendance() → changed_station_ids
  • track_b.agents.optimizer (refactored) → changed_ingredient_ids
  • track_a.agents.staff.StaffAgent.recompute() → changed_station_ids
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Reason codes (stored in MenuToggle.reason_code)
RC_OUT_OF_STOCK = "out_of_stock"
RC_STATION_UNSTAFFED = "station_unstaffed"
RC_MANUAL = "manual"

_SYSTEM_REASON_CODES = {RC_OUT_OF_STOCK, RC_STATION_UNSTAFFED}


def _oos_threshold(level) -> float:
    """Return the quantity at or below which we treat this ingredient as 'out of stock'."""
    safety = float(level.safety_stock or 0.0)
    if safety > 0:
        return safety
    reorder = float(level.reorder_point or 0.0)
    if reorder > 0:
        return reorder
    return 0.0  # fallback: only truly zero


def recompute_availability(
    db_session_factory: Callable[[], Any],
    bus: Any,
    broadcast_fn: Optional[Callable[[str, Dict], None]],
    *,
    changed_ingredient_ids: Optional[List[int]] = None,
    changed_station_ids: Optional[List[int]] = None,
    agent_name: str = "availability",
) -> List[Dict[str, Any]]:
    """Resolve menu-item availability and apply any needed enable/disable changes.

    Parameters
    ----------
    db_session_factory:
        Callable that returns a fresh SQLAlchemy session.
    bus:
        The signal bus (for emitting MENU_TOGGLE signals).
    broadcast_fn:
        WS hub broadcast function ``(event_name, payload)`` → None.
    changed_ingredient_ids:
        Restrict the ingredient stock-rule check to these ids (pass None to check all).
    changed_station_ids:
        Restrict the station coverage check to these stations (pass None to check all).
    agent_name:
        Label written into ``triggered_by`` on MenuToggle rows.

    Returns
    -------
    List of change dicts, each with keys: menu_item_id, action, reason_code.
    """
    from .models import (
        Ingredient, InventoryLevel, MenuItem, MenuToggle, Recipe, RecipeLine,
        Staff, StaffStation, Station, Attendance,
    )
    from .signals import SignalType
    from track_a.agents.forecaster import current_daypart
    from core.clock import SECONDS_PER_DAY

    now = float(bus.sim_time)
    day = int(now // SECONDS_PER_DAY)
    daypart = current_daypart(now)

    changes: List[Dict[str, Any]] = []

    session = db_session_factory()
    try:
        # ------------------------------------------------------------------
        # 1. Compute which ingredient_ids are at zero (out_of_stock).
        # ------------------------------------------------------------------
        if changed_ingredient_ids is not None:
            ingredient_ids_to_check = set(changed_ingredient_ids)
        else:
            # All ingredients that have a level row.
            ingredient_ids_to_check = {
                int(lv.ingredient_id)
                for lv in session.query(InventoryLevel).all()
            }

        oos_ingredient_ids: Set[int] = set()
        for ing_id in ingredient_ids_to_check:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ing_id)
                .first()
            )
            if level is not None and float(level.on_hand_cached or 0.0) <= _oos_threshold(level):
                oos_ingredient_ids.add(ing_id)

        # Map ingredient_id → set of menu_item_ids that require it.
        ing_to_items: Dict[int, Set[int]] = {}
        if oos_ingredient_ids:
            for rl in (
                session.query(RecipeLine)
                .join(Recipe, Recipe.id == RecipeLine.recipe_id)
                .filter(RecipeLine.ingredient_id.in_(list(oos_ingredient_ids)))
                .all()
            ):
                recipe = session.get(Recipe, rl.recipe_id)
                if recipe is not None:
                    ing_to_items.setdefault(int(rl.ingredient_id), set()).add(
                        int(recipe.menu_item_id)
                    )

        # menu_item_ids that should be blocked for out_of_stock.
        oos_blocked_items: Set[int] = set()
        for items in ing_to_items.values():
            oos_blocked_items.update(items)

        # ------------------------------------------------------------------
        # 2. Compute which station_ids are uncovered → blocked item_ids.
        # ------------------------------------------------------------------
        if changed_station_ids is not None:
            station_ids_to_check = set(changed_station_ids)
        else:
            station_ids_to_check = {int(row.id) for row in session.query(Station).all()}

        def _staff_available(staff_id: int) -> bool:
            s = session.get(Staff, staff_id)
            if s is None or not s.active:
                return False
            rows = (
                session.query(Attendance)
                .filter(
                    Attendance.staff_id == staff_id,
                    Attendance.date_sim_day == day,
                )
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

        unstaffed_station_ids: Set[int] = set()
        for station_id in station_ids_to_check:
            links = (
                session.query(StaffStation)
                .filter(StaffStation.station_id == station_id)
                .all()
            )
            if not links:
                continue  # no staff ever assigned to this station → not managed
            covered = any(_staff_available(link.staff_id) for link in links)
            if not covered:
                unstaffed_station_ids.add(station_id)

        # Map station_id → set of menu_item_ids at that station.
        station_blocked_items: Set[int] = set()
        if unstaffed_station_ids:
            for mi in (
                session.query(MenuItem)
                .filter(MenuItem.station_id.in_(list(unstaffed_station_ids)))
                .all()
            ):
                station_blocked_items.add(int(mi.id))

        # ------------------------------------------------------------------
        # 3. Apply changes.
        # ------------------------------------------------------------------
        all_affected_item_ids = (
            oos_blocked_items | station_blocked_items
            | _items_with_active_system_block(session, MenuToggle)
        )

        for item_id in all_affected_item_ids:
            item = session.get(MenuItem, item_id)
            if item is None:
                continue

            should_oos_block = item_id in oos_blocked_items
            should_station_block = item_id in station_blocked_items

            # Apply new blocks.
            if should_oos_block and not _has_active_block(session, MenuToggle, item_id, RC_OUT_OF_STOCK):
                _write_toggle(session, item_id, "disable", RC_OUT_OF_STOCK, now, agent_name)
                changes.append({"menu_item_id": item_id, "action": "disable", "reason_code": RC_OUT_OF_STOCK})

            if should_station_block and not _has_active_block(session, MenuToggle, item_id, RC_STATION_UNSTAFFED):
                _write_toggle(session, item_id, "disable", RC_STATION_UNSTAFFED, now, agent_name)
                changes.append({"menu_item_id": item_id, "action": "disable", "reason_code": RC_STATION_UNSTAFFED})

            # Clear blocks that no longer apply.
            if not should_oos_block:
                if _has_active_block(session, MenuToggle, item_id, RC_OUT_OF_STOCK):
                    _clear_block(session, MenuToggle, item_id, RC_OUT_OF_STOCK)
                    changes.append({"menu_item_id": item_id, "action": "cleared", "reason_code": RC_OUT_OF_STOCK})

            if not should_station_block:
                if _has_active_block(session, MenuToggle, item_id, RC_STATION_UNSTAFFED):
                    _clear_block(session, MenuToggle, item_id, RC_STATION_UNSTAFFED)
                    changes.append({"menu_item_id": item_id, "action": "cleared", "reason_code": RC_STATION_UNSTAFFED})

            # Determine desired active state.
            any_block_active = _any_block_active(session, MenuToggle, item_id)
            desired_active = 0 if any_block_active else 1

            if int(item.active or 0) != desired_active:
                item.active = desired_active
                action_str = "enable" if desired_active else "disable"

                # Record the overall enable/disable in MenuToggle (no reason_code → summary row).
                session.add(MenuToggle(
                    menu_item_id=item_id,
                    action=action_str,
                    reason="availability resolver",
                    reason_code=None,
                    triggered_by=agent_name,
                    sim_time=now,
                    active=0,  # summary row, not a block itself
                ))
                changes.append({"menu_item_id": item_id, "action": action_str, "reason_code": "resolved"})

        session.commit()
    finally:
        session.close()

    # ------------------------------------------------------------------
    # 4. Broadcast and emit signals for any item whose active state changed.
    # ------------------------------------------------------------------
    resolved_actions = [c for c in changes if c.get("reason_code") == "resolved"]
    for change in resolved_actions:
        item_id = change["menu_item_id"]
        action = change["action"]
        if broadcast_fn is not None:
            try:
                broadcast_fn("menu_toggled", {"menu_item_id": item_id, "action": action})
            except Exception:  # noqa: BLE001
                pass
        if bus is not None:
            try:
                bus.emit(
                    SignalType.MENU_TOGGLE,
                    {"menu_item_id": item_id, "action": action, "reason": "availability"},
                    source=agent_name,
                    dedup_key=f"toggle:{item_id}",
                )
            except Exception:  # noqa: BLE001
                pass

    return changes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_active_block(session: Any, MenuToggle: Any, item_id: int, reason_code: str) -> bool:
    return session.query(MenuToggle).filter(
        MenuToggle.menu_item_id == item_id,
        MenuToggle.reason_code == reason_code,
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
    ).first() is not None


def _clear_block(session: Any, MenuToggle: Any, item_id: int, reason_code: str) -> None:
    session.query(MenuToggle).filter(
        MenuToggle.menu_item_id == item_id,
        MenuToggle.reason_code == reason_code,
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
    ).update({MenuToggle.active: 0})


def _write_toggle(
    session: Any,
    item_id: int,
    action: str,
    reason_code: str,
    now: float,
    agent_name: str,
) -> None:
    from .models import MenuToggle
    session.add(MenuToggle(
        menu_item_id=item_id,
        action=action,
        reason=f"{reason_code} block",
        reason_code=reason_code,
        triggered_by=agent_name,
        sim_time=now,
        active=1,
    ))


def _any_block_active(session: Any, MenuToggle: Any, item_id: int) -> bool:
    """True if any active disable block (system or manual) exists for item_id."""
    return session.query(MenuToggle).filter(
        MenuToggle.menu_item_id == item_id,
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
        # reason_code is None covers legacy rows (treat as manual/sticky).
    ).first() is not None


def _items_with_active_system_block(session: Any, MenuToggle: Any) -> Set[int]:
    """Return all item_ids that have an active system-managed block (to check if they should clear)."""
    rows = session.query(MenuToggle).filter(
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
        MenuToggle.reason_code.in_(list(_SYSTEM_REASON_CODES)),
    ).all()
    return {int(r.menu_item_id) for r in rows}
