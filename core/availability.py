"""Deterministic menu-item availability resolver.

Computes which menu items must be disabled based on three independent reasons:

  out_of_stock     — an ingredient used in the recipe is at/below its threshold
  station_unstaffed — the station for this item has no available staff this daypart
  equipment_down   — the station is out of service for a live EQUIPMENT_FAILURE
                     window (no Equipment table: the signal *is* the outage)

Keeps the state as ``MenuToggle`` rows with ``reason_code`` set. The invariant:

  • MenuItem.active == 1  iff  zero active disable-blocks exist for the item.
  • Block types: reason_code ∈ {"out_of_stock", "station_unstaffed",
    "equipment_down", "manual"}.
  • Auto-disable writes a MenuToggle(action="disable", reason_code=X, active=1)
    and flips MenuItem.active = 0.
  • Auto-enable: only when ALL active blocks (system + manual) are cleared.
  • Manual blocks (reason_code="manual") are STICKY — auto-enable never clears them.
    They must be explicitly cleared by an enable_menu_item() call.
  • ``_upsert_block`` deduplicates: only one active row per (item_id, reason_code).

The resolver is ALWAYS FULL-TRUTH: it recomputes ALL ingredients and ALL stations
on every call, ignoring the ``changed_ingredient_ids`` / ``changed_station_ids``
filter hints. Those params remain for API compatibility but are vestigial — callers
already invoke unconditionally, so scoping logic was the source of the headline bug
(an item blocked for ingredient Y was incorrectly re-enabled when ingredient X changed).

OOS mode (stored in SimSettings.availability_oos_mode):
  "threshold" — item disabled when on_hand ≤ safety_stock (or reorder_point if no
                safety_stock is set). This is the default.
  "zero"      — item disabled only when on_hand ≤ 0.

Callers:
  • VoiceActions.record_spoilage()  → changed_ingredient_ids (ignored as filter)
  • VoiceActions.set_staff_attendance() → changed_station_ids (ignored as filter)
  • track_b.agents.optimizer → changed_ingredient_ids (ignored as filter)
  • track_a.agents.staff.StaffAgent.recompute() → changed_station_ids (ignored as filter)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Reason codes (stored in MenuToggle.reason_code)
RC_OUT_OF_STOCK = "out_of_stock"
RC_STATION_UNSTAFFED = "station_unstaffed"
RC_EQUIPMENT_DOWN = "equipment_down"
RC_MANUAL = "manual"

_SYSTEM_REASON_CODES = {RC_OUT_OF_STOCK, RC_STATION_UNSTAFFED, RC_EQUIPMENT_DOWN}


# ---------------------------------------------------------------------------
# OOS mode helpers
# ---------------------------------------------------------------------------

def _get_oos_mode(session: Any) -> str:
    """Read the OOS mode from SimSettings; fall back to config default."""
    try:
        from .models import SimSettings
        settings = session.get(SimSettings, 1)
        if settings is not None:
            mode = getattr(settings, "availability_oos_mode", None)
            if mode in ("threshold", "zero"):
                return mode
    except Exception:  # noqa: BLE001
        pass
    from . import config as _cfg
    return getattr(_cfg, "AVAILABILITY_OOS_MODE", "threshold")


def _oos_threshold(level: Any, mode: str = "threshold") -> float:
    """Return the quantity at or below which we treat this ingredient as out-of-stock.

    mode="zero"      → 0.0 (item disabled only when truly at/below zero)
    mode="threshold" → safety_stock if set, else reorder_point if set, else 0.0
    """
    if mode == "zero":
        return 0.0
    safety = float(level.safety_stock or 0.0)
    if safety > 0:
        return safety
    reorder = float(level.reorder_point or 0.0)
    if reorder > 0:
        return reorder
    return 0.0


# ---------------------------------------------------------------------------
# Full-truth computation helpers
# ---------------------------------------------------------------------------

def _compute_oos_ingredient_ids(session: Any, mode: str) -> Set[int]:
    """Return all ingredient_ids whose on_hand is at/below threshold (full scan)."""
    from .models import InventoryLevel
    oos: Set[int] = set()
    for level in session.query(InventoryLevel).all():
        thresh = _oos_threshold(level, mode)
        if float(level.on_hand_cached or 0.0) <= thresh:
            oos.add(int(level.ingredient_id))
    return oos


def _items_for_ingredients(session: Any, ingredient_ids: Set[int]) -> Set[int]:
    """Map a set of ingredient_ids to menu_item_ids that use them (via RecipeLine → Recipe)."""
    from .models import Recipe, RecipeLine
    if not ingredient_ids:
        return set()
    items: Set[int] = set()
    for rl in (
        session.query(RecipeLine)
        .filter(RecipeLine.ingredient_id.in_(list(ingredient_ids)))
        .all()
    ):
        recipe = session.get(Recipe, rl.recipe_id)
        if recipe is not None:
            items.add(int(recipe.menu_item_id))
    return items


def _staff_available(session: Any, staff_id: int, day: int, daypart: str) -> bool:
    from .models import Attendance, Staff
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


def _compute_equipment_down_stations(bus: Any, now: float) -> Set[int]:
    """Station ids with a live EQUIPMENT_FAILURE window (docs/fable/incidents.md).

    There is no ``Equipment`` table by design: the live signal *is* the outage,
    and its TTL is the window, so a station re-opens when the bus sweeps the
    signal. ``until_sim`` is re-checked anyway so an un-swept signal cannot hold
    a station down past its window.
    """
    from .signals import SignalType
    down: Set[int] = set()
    try:
        live = bus.live(type=SignalType.EQUIPMENT_FAILURE)
    except Exception:  # noqa: BLE001 — availability must never break on this
        logger.warning("equipment-failure lookup failed", exc_info=True)
        return down
    for signal in live:
        payload = signal.payload or {}
        try:
            if float(payload.get("until_sim", 0.0)) <= now:
                continue
            down.add(int(payload["station_id"]))
        except (TypeError, ValueError, KeyError):
            continue
    return down


def _compute_unstaffed_stations(session: Any, day: int, daypart: str) -> Set[int]:
    """Return all station_ids that have assigned staff but none currently available."""
    from .models import StaffStation, Station
    unstaffed: Set[int] = set()
    for station in session.query(Station).all():
        links = (
            session.query(StaffStation)
            .filter(StaffStation.station_id == station.id)
            .all()
        )
        if not links:
            continue  # no staff ever assigned → not managed by the resolver
        covered = any(_staff_available(session, link.staff_id, day, daypart) for link in links)
        if not covered:
            unstaffed.add(int(station.id))
    return unstaffed


def _items_for_stations(session: Any, station_ids: Set[int]) -> Set[int]:
    """Return menu_item_ids assigned to any of the given station_ids."""
    from .models import MenuItem
    if not station_ids:
        return set()
    items: Set[int] = set()
    for mi in (
        session.query(MenuItem)
        .filter(MenuItem.station_id.in_(list(station_ids)))
        .all()
    ):
        items.add(int(mi.id))
    return items


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------

def recompute_availability(
    db_session_factory: Callable[[], Any],
    bus: Any,
    broadcast_fn: Optional[Callable[[str, Dict], None]],
    *,
    changed_ingredient_ids: Optional[List[int]] = None,  # vestigial — not used as filter
    changed_station_ids: Optional[List[int]] = None,     # vestigial — not used as filter
    agent_name: str = "availability",
) -> List[Dict[str, Any]]:
    """Resolve menu-item availability and apply any needed enable/disable changes.

    Always performs a full-truth recompute over ALL ingredients and ALL stations.
    The ``changed_ingredient_ids`` / ``changed_station_ids`` params are accepted for
    API compatibility but are ignored as scoping filters — the old scoped logic was
    the root cause of items being incorrectly re-enabled when an unrelated ingredient
    changed.

    Returns a list of change dicts with keys: menu_item_id, action, reason_code.
    Changes with ``reason_code="resolved"`` indicate a MenuItem.active flip.
    """
    from .models import MenuToggle, MenuItem
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
        # 1. Full-truth OOS: which ingredients are at/below their threshold?
        # ------------------------------------------------------------------
        oos_mode = _get_oos_mode(session)
        all_oos_ingredient_ids = _compute_oos_ingredient_ids(session, oos_mode)
        oos_blocked_items = _items_for_ingredients(session, all_oos_ingredient_ids)

        # ------------------------------------------------------------------
        # 2. Full-truth station coverage: which stations are unstaffed?
        # ------------------------------------------------------------------
        unstaffed_station_ids = _compute_unstaffed_stations(session, day, daypart)
        station_blocked_items = _items_for_stations(session, unstaffed_station_ids)

        # ------------------------------------------------------------------
        # 2b. Equipment outages: stations down for a scenario-driven window.
        # ------------------------------------------------------------------
        equipment_down_station_ids = _compute_equipment_down_stations(bus, now)
        equipment_blocked_items = _items_for_stations(session, equipment_down_station_ids)

        # ------------------------------------------------------------------
        # 3. Candidate items = items that currently have an auto-block
        #    OR that should now have one.
        # ------------------------------------------------------------------
        candidate_item_ids = (
            oos_blocked_items | station_blocked_items | equipment_blocked_items
            | _items_with_active_auto_block(session, MenuToggle)
        )

        for item_id in candidate_item_ids:
            item = session.get(MenuItem, item_id)
            if item is None:
                continue

            should_oos = item_id in oos_blocked_items
            should_station = item_id in station_blocked_items

            # Reconcile out_of_stock block.
            has_oos = _has_active_block(session, MenuToggle, item_id, RC_OUT_OF_STOCK)
            if should_oos and not has_oos:
                _upsert_block(session, MenuToggle, item_id, RC_OUT_OF_STOCK, now, agent_name)
                changes.append({"menu_item_id": item_id, "action": "disable", "reason_code": RC_OUT_OF_STOCK})
            elif not should_oos and has_oos:
                _clear_block(session, MenuToggle, item_id, RC_OUT_OF_STOCK)
                changes.append({"menu_item_id": item_id, "action": "cleared", "reason_code": RC_OUT_OF_STOCK})

            # Reconcile station_unstaffed block.
            has_station = _has_active_block(session, MenuToggle, item_id, RC_STATION_UNSTAFFED)
            if should_station and not has_station:
                _upsert_block(session, MenuToggle, item_id, RC_STATION_UNSTAFFED, now, agent_name)
                changes.append({"menu_item_id": item_id, "action": "disable", "reason_code": RC_STATION_UNSTAFFED})
            elif not should_station and has_station:
                _clear_block(session, MenuToggle, item_id, RC_STATION_UNSTAFFED)
                changes.append({"menu_item_id": item_id, "action": "cleared", "reason_code": RC_STATION_UNSTAFFED})

            # Reconcile equipment_down block — cleared once the outage window
            # ends (the EQUIPMENT_FAILURE signal expires or until_sim passes).
            should_equipment = item_id in equipment_blocked_items
            has_equipment = _has_active_block(session, MenuToggle, item_id, RC_EQUIPMENT_DOWN)
            if should_equipment and not has_equipment:
                _upsert_block(session, MenuToggle, item_id, RC_EQUIPMENT_DOWN, now, agent_name)
                changes.append({"menu_item_id": item_id, "action": "disable", "reason_code": RC_EQUIPMENT_DOWN})
            elif not should_equipment and has_equipment:
                _clear_block(session, MenuToggle, item_id, RC_EQUIPMENT_DOWN)
                changes.append({"menu_item_id": item_id, "action": "cleared", "reason_code": RC_EQUIPMENT_DOWN})

            # manual blocks are never touched here — they are sticky

            # Desired active = 1 iff NO active disable block of any type
            any_block = _any_block_active(session, MenuToggle, item_id)
            desired_active = 0 if any_block else 1
            current_active = int(item.active or 0)

            if current_active != desired_active:
                item.active = desired_active
                action_str = "enable" if desired_active else "disable"
                changes.append({"menu_item_id": item_id, "action": action_str, "reason_code": "resolved"})

        session.commit()
    finally:
        session.close()

    # ------------------------------------------------------------------
    # 4. Cancel pending batches for items that just became disabled.
    # ------------------------------------------------------------------
    resolved_actions = [c for c in changes if c.get("reason_code") == "resolved"]
    newly_disabled_ids = [c["menu_item_id"] for c in resolved_actions if c["action"] == "disable"]
    if newly_disabled_ids:
        try:
            from .kitchen import cancel_pending_batches_for_items  # noqa: PLC0415
            session2 = db_session_factory()
            try:
                cancelled = cancel_pending_batches_for_items(
                    session2,
                    newly_disabled_ids,
                    now=float(bus.sim_time),
                    reason="item auto-disabled (availability)",
                )
                session2.commit()
            finally:
                session2.close()
            # Broadcast batch_decided for each cancelled batch so the cook panel refreshes
            if broadcast_fn is not None and cancelled:
                for bid in cancelled:
                    try:
                        broadcast_fn("batch_decided", {"batch_id": bid, "decision": "skip"})
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as _ex:  # noqa: BLE001
            logger.warning("cancel_pending_batches_for_items failed: %s", _ex)

    # ------------------------------------------------------------------
    # 5. Broadcast and emit signals for items whose active state changed.
    # ------------------------------------------------------------------
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
# Block-table helpers (all pass MenuToggle as an argument to keep imports lazy)
# ---------------------------------------------------------------------------

def _upsert_block(
    session: Any,
    MenuToggle: Any,
    item_id: int,
    reason_code: str,
    now: float,
    agent_name: str,
) -> None:
    """Write an active disable block — deduplicates; never accumulates duplicate rows."""
    existing = session.query(MenuToggle).filter(
        MenuToggle.menu_item_id == item_id,
        MenuToggle.reason_code == reason_code,
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
    ).first()
    if existing is not None:
        return  # already blocked; do nothing
    session.add(MenuToggle(
        menu_item_id=item_id,
        action="disable",
        reason=f"{reason_code} block",
        reason_code=reason_code,
        triggered_by=agent_name,
        sim_time=now,
        active=1,
    ))
    session.flush()  # make the new block visible to same-txn reads;
                     # session is autoflush=False so _any_block_active would
                     # otherwise query the DB without seeing the pending add


def _has_active_block(session: Any, MenuToggle: Any, item_id: int, reason_code: str) -> bool:
    """True if an active disable row with the given reason_code exists for item_id."""
    return session.query(MenuToggle).filter(
        MenuToggle.menu_item_id == item_id,
        MenuToggle.reason_code == reason_code,
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
    ).first() is not None


def _clear_block(session: Any, MenuToggle: Any, item_id: int, reason_code: str) -> None:
    """Deactivate all active disable rows matching (item_id, reason_code)."""
    session.query(MenuToggle).filter(
        MenuToggle.menu_item_id == item_id,
        MenuToggle.reason_code == reason_code,
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
    ).update({MenuToggle.active: 0})


def _any_block_active(session: Any, MenuToggle: Any, item_id: int) -> bool:
    """True if any active disable block (system or manual) exists for item_id."""
    return session.query(MenuToggle).filter(
        MenuToggle.menu_item_id == item_id,
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
    ).first() is not None


def _items_with_active_auto_block(session: Any, MenuToggle: Any) -> Set[int]:
    """Return item_ids that have an active system-managed block (out_of_stock or station_unstaffed).

    Used to expand the candidate set for potential re-enabling — an item that
    previously had an auto-block must be rechecked even if it's no longer in the
    'should block' sets.
    """
    rows = session.query(MenuToggle).filter(
        MenuToggle.action == "disable",
        MenuToggle.active == 1,
        MenuToggle.reason_code.in_(list(_SYSTEM_REASON_CODES)),
    ).all()
    return {int(r.menu_item_id) for r in rows}
