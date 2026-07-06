"""VoiceActions — single dispatch seam for all voice agent tool calls.

Every tool the Gemini Live model can call routes here.  No logic lives in the
bridge (voice_live.py) except routing; no second LLM interprets the text.
Each method:
  • resolves names → ids (ingredient, dish, staff) deterministically,
  • returns ``{"need": "<field>", "question": "..."}`` when args are missing
    so the model re-asks rather than guessing,
  • performs the mutation through the existing agent / service,
  • broadcasts the right firehose event,
  • returns a concrete, truthful post-state the model reads back verbatim.

Reads never require confirmation.
Writes are staged or applied depending on the ``mode`` arg passed by the bridge
(respects the confirm/auto toggle).  Outbound calls are always staged.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class VoiceActions:
    """Stateful dispatch object wired up at API bootstrap.

    Holds references to every service needed so tools never open new sessions
    independently.  All methods are sync (called via asyncio.to_thread in the
    bridge), each opens/closes its own DB session as needed.
    """

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        hub_broadcast: Callable[[str, Dict], None],
        voice_processor: Any,           # VoiceProcessor (for read delegates + _record_*)
        *,
        ledger: Any = None,             # track_b InventoryLedger
        optimizer: Any = None,          # track_b InventoryOptimizer
        staff_agent: Any = None,        # track_a StaffAgent
        forecaster: Any = None,         # track_a DemandForecaster
        forecast_jobs: Any = None,      # ForecastJobRunner
        competitor_agent: Any = None,   # track_a CompetitorAgent
        review_agent: Any = None,       # track_a ReviewAgent
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.hub_broadcast = hub_broadcast
        self.vp = voice_processor       # VoiceProcessor

        self.ledger = ledger
        self.optimizer = optimizer
        self.staff_agent = staff_agent
        self.forecaster = forecaster
        self.forecast_jobs = forecast_jobs
        self.competitor_agent = competitor_agent
        self.review_agent = review_agent

        # Pending staged actions: action_id → {fn, human_readable}
        self._pending: Dict[str, Dict[str, Any]] = {}

    # -----------------------------------------------------------------------
    # Read tools (delegates to VoiceProcessor query methods)
    # -----------------------------------------------------------------------

    def get_inventory(
        self,
        item_name: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Current on-hand inventory with names and units.

        sort="expiring_soonest" returns the 5 lots expiring earliest.
        """
        if sort == "expiring_soonest":
            return self._get_expiring_soonest(5)
        return self.vp.query_inventory(item_name)

    def get_forecast(
        self,
        item_name: Optional[str] = None,
        daypart: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Demand forecasts.  Auto-runs the forecaster when none exist."""
        result = self.vp.query_forecast(item_name)
        if result.get("count", 0) == 0 and self.forecast_jobs is not None:
            # No forecast → trigger one and return the fresh results.
            try:
                self.forecast_jobs.enqueue(
                    "DETERMINISTIC_FORECAST",
                    trigger_reason="voice:no_forecast",
                    requested_by="voice",
                )
            except Exception:  # noqa: BLE001
                pass
            result = self.vp.query_forecast(item_name)
            result["auto_ran_forecast"] = True
        if daypart:
            rows = [r for r in result.get("forecasts", []) if r.get("daypart") == daypart]
            result = dict(result, forecasts=rows, count=len(rows))
        return result

    def get_batches(
        self,
        status: Optional[str] = None,
        dish: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upcoming and recent production batches."""
        from .models import Batch, MenuItem
        session = self.db_session_factory()
        try:
            item_names = {int(mi.id): str(mi.name or "") for mi in session.query(MenuItem).all()}
            q = session.query(Batch)
            if status:
                statuses = [s.strip() for s in status.split(",")]
                q = q.filter(Batch.status.in_(statuses))
            else:
                q = q.filter(Batch.status.in_(["decided", "approved", "ready"]))
            q = q.order_by(Batch.decided_at.desc())
            rows = []
            for b in q.limit(40).all():
                name = item_names.get(int(b.menu_item_id or 0), str(b.menu_item_id))
                if dish and dish.lower() not in name.lower():
                    continue
                from . import kitchen as _k
                state = _k._derive_state(b.decision, b.status, b.cooked_at)
                # Only run ingredient check for uncooked cook batches
                if b.decision == "cook" and b.cooked_at is None:
                    ing_block = _k.batch_ingredient_block(session, int(b.menu_item_id or 0))
                else:
                    ing_block = None
                rows.append({
                    "id": int(b.id),
                    "menu_item": name,
                    "decision": b.decision,
                    "status": b.status,
                    "state": state,
                    "feasible": ing_block is None,
                    "blocked_reason": ing_block,
                    "planned_qty": int(b.planned_qty or 0),
                    "actual_made_qty": float(b.actual_made_qty) if b.actual_made_qty is not None else None,
                    "serve_window": b.serve_window,
                    "cooked_at": float(b.cooked_at) if b.cooked_at is not None else None,
                })
        finally:
            session.close()
        return {"sim_time": float(self.bus.sim_time), "batches": rows, "count": len(rows)}

    def get_menu(self, filter: str = "disabled") -> Dict[str, Any]:
        """Which menu items are disabled (and why) or all items."""
        from .models import MenuItem, MenuToggle
        session = self.db_session_factory()
        try:
            items = session.query(MenuItem).order_by(MenuItem.id.asc()).all()
            rows = []
            for mi in items:
                if filter == "disabled" and mi.active:
                    continue
                # Find the most recent active block reason.
                block = (
                    session.query(MenuToggle)
                    .filter(
                        MenuToggle.menu_item_id == mi.id,
                        MenuToggle.action == "disable",
                        MenuToggle.active == 1,
                    )
                    .order_by(MenuToggle.sim_time.desc())
                    .first()
                )
                rows.append({
                    "id": int(mi.id),
                    "name": mi.name,
                    "active": bool(mi.active),
                    "disable_reason": block.reason_code if block else None,
                    "disable_text": block.reason if block else None,
                })
        finally:
            session.close()
        return {"items": rows, "count": len(rows)}

    def get_pos_stats(
        self,
        window: str = "3h",
        item_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POS top sellers and order counts in a time window."""
        from .models import MenuItem, Order, OrderLine
        now = float(self.bus.sim_time)
        window_s = _parse_window(window)
        since = now - window_s
        session = self.db_session_factory()
        try:
            item_names = {int(mi.id): str(mi.name or "") for mi in session.query(MenuItem).all()}
            lines = (
                session.query(OrderLine)
                .join(Order, Order.id == OrderLine.order_id)
                .filter(Order.sim_time >= since, OrderLine.status == "sold")
                .all()
            )
            counts: Dict[int, int] = {}
            for line in lines:
                mid = int(line.menu_item_id or 0)
                counts[mid] = counts.get(mid, 0) + int(line.qty or 1)
        finally:
            session.close()

        items_sorted = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        result_rows = []
        for mid, qty in items_sorted:
            name = item_names.get(mid, str(mid))
            if item_name and item_name.lower() not in name.lower():
                continue
            result_rows.append({"menu_item": name, "qty_sold": qty})

        if item_name and not result_rows:
            # Item was ordered but nothing found — check full range.
            return {
                "window": window,
                "since_sim_time": since,
                "items": [],
                "note": f"No sales of '{item_name}' in the last {window}.",
            }
        return {
            "window": window,
            "since_sim_time": since,
            "items": result_rows[:20],
            "count": len(result_rows),
        }

    def get_competitors(self) -> Dict[str, Any]:
        return self.vp.query_competitors()

    def get_reviews(self, sort: Optional[str] = None) -> Dict[str, Any]:
        result = self.vp.query_reviews()
        if sort == "most_hated":
            reviews = result.get("reviews", [])
            reviews.sort(key=lambda r: float(r.get("rating") or 5.0))
            result = dict(result, reviews=reviews[:10])
        return result

    def get_staff(self) -> Dict[str, Any]:
        return self.vp.query_staff()

    def get_supplier_prices(self, ingredient_name: Optional[str] = None) -> Dict[str, Any]:
        from .models import Ingredient, Supplier, SupplierCatalog
        from .formatter import format_price_per_unit
        session = self.db_session_factory()
        try:
            ingredients = {int(ing.id): ing for ing in session.query(Ingredient).all()}
            suppliers = {int(s.id): s for s in session.query(Supplier).all()}
            all_rows = []
            for cat in session.query(SupplierCatalog).all():
                ing = ingredients.get(int(cat.ingredient_id or 0))
                sup = suppliers.get(int(cat.supplier_id or 0))
                if not ing or not sup:
                    continue
                raw_price = float(cat.current_price) if cat.current_price is not None else 0.0
                cat_unit = cat.unit or "g"
                base_unit = str(ing.base_unit or "g")
                # Convert price to per-base-unit, then format for display
                if cat_unit == "kg" and base_unit == "g":
                    price_per_base = raw_price / 1000.0
                elif cat_unit == "g" and base_unit == "g":
                    price_per_base = raw_price
                else:
                    price_per_base = raw_price
                disp_price, disp_unit = format_price_per_unit(price_per_base, base_unit)
                all_rows.append({
                    "ingredient": ing.name,
                    "supplier": sup.name,
                    "price": raw_price,
                    "price_display": f"{disp_price} per {disp_unit}",
                    "unit": cat_unit,
                    "availability": cat.availability,
                    "is_default": bool(getattr(cat, "is_default", 0)),
                })
        finally:
            session.close()

        if ingredient_name:
            needle = ingredient_name.strip().lower()
            # 1. Try exact match first.
            exact = [r for r in all_rows if str(r.get("ingredient") or "").lower() == needle]
            if exact:
                rows = exact
            else:
                # 2. Fall back to substring match.
                rows = [r for r in all_rows if needle in str(r.get("ingredient") or "").lower()]
                if not rows:
                    rows = all_rows  # no match → return everything
        else:
            rows = all_rows
        return {"prices": rows, "count": len(rows)}

    def get_signals(self) -> Dict[str, Any]:
        return self.vp.query_signals()

    def get_kitchen_status(self, dish: Optional[str] = None, topic: str = "all") -> Dict[str, Any]:
        return self.vp.kitchen_status(dish=dish, topic=topic)

    # -----------------------------------------------------------------------
    # Write tools
    # -----------------------------------------------------------------------

    def disable_menu_item(
        self,
        item_name: str = "",
        reason: str = "voice request",
        *,
        category: Optional[str] = None,
        name_contains: Optional[str] = None,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Disable a menu item (sticky manual block).

        When ``category`` or ``name_contains`` is given, disables ALL active
        matching items in bulk (returns list of names affected).
        """
        # ── Bulk path ────────────────────────────────────────────────────────
        if category or name_contains:
            from .models import MenuItem, MenuToggle
            from .signals import SignalType

            session = self.db_session_factory()
            try:
                q = session.query(MenuItem).filter(MenuItem.active == 1)
                if category:
                    q = q.filter(MenuItem.category.ilike(f"%{category}%"))
                if name_contains:
                    q = q.filter(MenuItem.name.ilike(f"%{name_contains}%"))
                matched = q.all()
            finally:
                session.close()

            if not matched:
                return {"need": "item_name", "question": f"No active items found matching that filter."}

            item_ids = [(int(mi.id), str(mi.name)) for mi in matched]
            matched_names = [name for _, name in item_ids]

            def _apply_bulk():
                from .models import MenuItem, MenuToggle
                from .signals import SignalType
                now = float(self.bus.sim_time)
                session = self.db_session_factory()
                try:
                    for iid, iname in item_ids:
                        item = session.get(MenuItem, iid)
                        if item and item.active:
                            item.active = 0
                            session.add(MenuToggle(
                                menu_item_id=iid,
                                action="disable",
                                reason=reason,
                                reason_code="manual",
                                triggered_by="voice",
                                sim_time=now,
                                active=1,
                            ))
                    session.commit()
                finally:
                    session.close()
                for iid, _ in item_ids:
                    self.hub_broadcast("menu_toggled", {"menu_item_id": iid, "action": "disable"})
                return {"ok": True, "items": matched_names, "action": "disabled", "count": len(matched_names)}

            label = ", ".join(matched_names[:3]) + (f" and {len(matched_names)-3} more" if len(matched_names) > 3 else "")
            hr = f"Disable {len(matched_names)} item(s): {label}."
            return self._stage_or_apply(mode, _apply_bulk, human_readable=hr)

        # ── Single-item path ─────────────────────────────────────────────────
        item_id, resolved_name = self._resolve_menu_item(item_name)
        if item_id is None:
            return {"need": "item_name", "question": f"I couldn't find '{item_name}' on the menu. Which dish did you mean?"}

        def _apply():
            from .models import MenuItem, MenuToggle
            from .signals import SignalType
            now = float(self.bus.sim_time)
            session = self.db_session_factory()
            try:
                item = session.get(MenuItem, item_id)
                if item is None:
                    return {"error": "Item not found"}
                if not item.active:
                    return {"ok": True, "item": resolved_name, "was_already_disabled": True}
                item.active = 0
                session.add(MenuToggle(
                    menu_item_id=item_id,
                    action="disable",
                    reason=reason,
                    reason_code="manual",
                    triggered_by="voice",
                    sim_time=now,
                    active=1,
                ))
                session.commit()
            finally:
                session.close()
            self.hub_broadcast("menu_toggled", {"menu_item_id": item_id, "action": "disable"})
            try:
                self.bus.emit(
                    SignalType.MENU_TOGGLE,
                    {"menu_item_id": item_id, "action": "disable", "reason": reason},
                    source="voice",
                    dedup_key=f"toggle:{item_id}",
                )
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "item": resolved_name, "action": "disabled"}

        return self._stage_or_apply(
            mode,
            _apply,
            human_readable=f"Disable {resolved_name} on the menu.",
        )

    def enable_menu_item(
        self,
        item_name: str = "",
        *,
        category: Optional[str] = None,
        name_contains: Optional[str] = None,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Re-enable a menu item — clears all blocks including manual.

        When ``category`` or ``name_contains`` is given, enables ALL matching
        disabled items in bulk.
        """
        # ── Bulk path ────────────────────────────────────────────────────────
        if category or name_contains:
            from .models import MenuItem, MenuToggle

            session = self.db_session_factory()
            try:
                q = session.query(MenuItem).filter(MenuItem.active == 0)
                if category:
                    q = q.filter(MenuItem.category.ilike(f"%{category}%"))
                if name_contains:
                    q = q.filter(MenuItem.name.ilike(f"%{name_contains}%"))
                matched = q.all()
            finally:
                session.close()

            if not matched:
                return {"need": "item_name", "question": "No disabled items found matching that filter."}

            item_ids = [(int(mi.id), str(mi.name)) for mi in matched]
            matched_names = [name for _, name in item_ids]

            def _apply_bulk_enable():
                from .models import MenuItem, MenuToggle
                from .signals import SignalType
                now = float(self.bus.sim_time)
                session = self.db_session_factory()
                try:
                    for iid, iname in item_ids:
                        item = session.get(MenuItem, iid)
                        if item and not item.active:
                            item.active = 1
                            session.query(MenuToggle).filter(
                                MenuToggle.menu_item_id == iid,
                                MenuToggle.active == 1,
                            ).update({MenuToggle.active: 0})
                            session.add(MenuToggle(
                                menu_item_id=iid,
                                action="enable",
                                reason="manual voice enable",
                                reason_code="manual",
                                triggered_by="voice",
                                sim_time=now,
                                active=1,
                            ))
                    session.commit()
                finally:
                    session.close()
                for iid, _ in item_ids:
                    self.hub_broadcast("menu_toggled", {"menu_item_id": iid, "action": "enable"})
                return {"ok": True, "items": matched_names, "action": "enabled", "count": len(matched_names)}

            label = ", ".join(matched_names[:3]) + (f" and {len(matched_names)-3} more" if len(matched_names) > 3 else "")
            hr = f"Re-enable {len(matched_names)} item(s): {label}."
            return self._stage_or_apply(mode, _apply_bulk_enable, human_readable=hr)

        # ── Single-item path ─────────────────────────────────────────────────
        item_id, resolved_name = self._resolve_menu_item(item_name)
        if item_id is None:
            return {"need": "item_name", "question": f"I couldn't find '{item_name}' on the menu."}

        def _apply():
            from .models import MenuItem, MenuToggle
            from .signals import SignalType
            now = float(self.bus.sim_time)
            session = self.db_session_factory()
            try:
                item = session.get(MenuItem, item_id)
                if item is None:
                    return {"error": "Item not found"}
                if item.active:
                    return {"ok": True, "item": resolved_name, "was_already_enabled": True}
                item.active = 1
                # Clear ALL active blocks for a manual enable.
                session.query(MenuToggle).filter(
                    MenuToggle.menu_item_id == item_id,
                    MenuToggle.active == 1,
                ).update({MenuToggle.active: 0})
                session.add(MenuToggle(
                    menu_item_id=item_id,
                    action="enable",
                    reason="manual voice enable",
                    reason_code="manual",
                    triggered_by="voice",
                    sim_time=now,
                    active=1,
                ))
                session.commit()
            finally:
                session.close()
            self.hub_broadcast("menu_toggled", {"menu_item_id": item_id, "action": "enable"})
            try:
                self.bus.emit(
                    SignalType.MENU_TOGGLE,
                    {"menu_item_id": item_id, "action": "enable", "reason": "manual voice enable"},
                    source="voice",
                    dedup_key=f"toggle:{item_id}",
                )
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "item": resolved_name, "action": "enabled"}

        return self._stage_or_apply(
            mode,
            _apply,
            human_readable=f"Re-enable {resolved_name} on the menu.",
        )

    def adjust_inventory(
        self,
        ingredient_name: str,
        set_to: Optional[float] = None,
        delta: Optional[float] = None,
        unit: Optional[str] = None,
        reason: str = "voice adjustment",
        *,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Set or adjust inventory quantity for an ingredient."""
        if set_to is None and delta is None:
            return {"need": "set_to", "question": f"How much {ingredient_name} should I set the inventory to, or how much to add?"}
        ing_id, ing_name = self._resolve_ingredient(ingredient_name)
        if ing_id is None:
            return {"need": "ingredient_name", "question": f"I couldn't find '{ingredient_name}' in the ingredients. Did you mean something else?"}

        def _apply():
            from .models import InventoryLevel
            now = float(self.bus.sim_time)
            session = self.db_session_factory()
            try:
                level = session.query(InventoryLevel).filter(
                    InventoryLevel.ingredient_id == ing_id
                ).first()
                current = float(level.on_hand_cached or 0.0) if level else 0.0
                ing_unit = unit or (str(level.unit or "each") if level else "each")
            finally:
                session.close()

            if delta is not None:
                if self.ledger is None:
                    return {"error": "Ledger not available"}
                self.ledger.apply_receipt(
                    {
                        "ingredient_id": ing_id,
                        "ingredient_ref": ing_name,
                        "qty": delta,
                        "unit": ing_unit,
                        "reason": reason,
                    }
                )
                # Re-read on_hand after the direct write so the return value is accurate.
                session2 = self.db_session_factory()
                try:
                    from .models import InventoryLevel
                    level2 = session2.query(InventoryLevel).filter(
                        InventoryLevel.ingredient_id == ing_id
                    ).first()
                    new_qty = float(level2.on_hand_cached or 0.0) if level2 else max(0.0, current + delta)
                finally:
                    session2.close()
            else:
                new_qty = float(set_to)
                if self.ledger is None:
                    return {"error": "Ledger not available"}
                self.ledger.apply_count(
                    {
                        "ingredient_id": ing_id,
                        "ingredient_ref": ing_name,
                        "qty": new_qty,
                        "unit": ing_unit,
                        "raw_text": f"set {ing_name} to {new_qty} {ing_unit}",
                    }
                )
            # Cascade: disable/enable menu items whose ingredient is now at/below
            # threshold.  Mirror the pattern used by record_spoilage and
            # set_staff_attendance, which call recompute_availability directly at
            # the voice layer using self.hub_broadcast.  The ledger's internal
            # recompute (inside _apply_reported_count/_apply_reported_receipt) uses
            # a swallowed try/except, so its broadcast is unreliable under contention;
            # this direct call is the authoritative instant update.
            try:
                from .availability import recompute_availability
                recompute_availability(
                    self.db_session_factory,
                    self.bus,
                    self.hub_broadcast,
                    changed_ingredient_ids=[ing_id],
                    agent_name="voice_inventory",
                )
            except Exception:  # noqa: BLE001
                logger.warning("adjust_inventory recompute_availability failed", exc_info=True)
            from .formatter import format_qty
            return {
                "ok": True,
                "ingredient": ing_name,
                "on_hand_before": current,
                "on_hand_before_display": format_qty(current, ing_unit),
                "on_hand_after": new_qty,
                "on_hand_after_display": format_qty(new_qty, ing_unit),
                "unit": ing_unit,
            }

        op = f"Set {ing_name} to {set_to}" if set_to is not None else f"Add {delta} to {ing_name}"
        return self._stage_or_apply(mode, _apply, human_readable=f"{op} in inventory.")

    def record_spoilage(
        self,
        ingredient_name: str,
        qty: Optional[float] = None,
        all_stock: bool = False,
        *,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Mark ingredient(s) as spoiled: zero/reduce stock, log waste, cascade menu."""
        if qty is None and not all_stock:
            return {
                "need": "qty",
                "question": f"How much {ingredient_name} spoiled — a specific amount, or all of it?",
            }
        ing_id, ing_name = self._resolve_ingredient(ingredient_name)
        if ing_id is None:
            return {"need": "ingredient_name", "question": f"I couldn't find '{ingredient_name}'. Which ingredient spoiled?"}

        def _apply():
            if self.ledger is None:
                return {"error": "Ledger not available"}
            result = self.ledger.apply_ingredient_waste(
                ing_id,
                qty=qty,
                all_stock=all_stock,
                waste_type="spoilage",
                reason=f"voice: {ingredient_name} spoiled",
            )
            # Cascade: auto-disable dishes that now have no stock.
            disabled_items = []
            try:
                from .availability import recompute_availability
                changes = recompute_availability(
                    self.db_session_factory,
                    self.bus,
                    self.hub_broadcast,
                    changed_ingredient_ids=[ing_id],
                    agent_name="voice_spoilage",
                )
                disabled_items = [
                    c["menu_item_id"]
                    for c in changes
                    if c.get("action") == "disable" and c.get("reason_code") == "resolved"
                ]
                # Resolve item names for the response.
                from .models import MenuItem
                session = self.db_session_factory()
                try:
                    disabled_names = [
                        session.get(MenuItem, mid).name
                        for mid in disabled_items
                        if session.get(MenuItem, mid)
                    ]
                finally:
                    session.close()
            except Exception:  # noqa: BLE001
                logger.warning("recompute_availability failed", exc_info=True)
                disabled_names = []
            return {
                "ok": True,
                "ingredient": ing_name,
                "on_hand_before": result["on_hand_before"],
                "on_hand_after": result["on_hand_after"],
                "depleted": result["depleted"],
                "unit": result["unit"],
                "auto_disabled_items": disabled_names,
            }

        qty_str = "all" if all_stock else str(qty)
        return self._stage_or_apply(
            mode,
            _apply,
            human_readable=f"Mark {qty_str} {ing_name} as spoiled and update inventory.",
        )

    def confirm_batch_cooked(
        self,
        dish_or_batch: str,
        actual_qty: Optional[float] = None,
        *,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Record that a batch has been cooked."""
        from .models import Batch, MenuItem
        session = self.db_session_factory()
        try:
            batch, item_name = self._resolve_batch(session, dish_or_batch)
        finally:
            session.close()

        if batch is None:
            return {"need": "dish_or_batch", "question": f"I couldn't find a pending batch for '{dish_or_batch}'. Which dish batch was cooked?"}

        batch_id = batch["id"]
        menu_item_id = batch["menu_item_id"]
        planned_qty = batch["planned_qty"]
        effective_qty = actual_qty if actual_qty is not None else planned_qty

        def _apply():
            self.vp._record_batch_cooked(
                batch_id=batch_id,
                menu_item_id=menu_item_id,
                actual_made_qty=effective_qty,
                planned_qty=planned_qty,
            )
            self.hub_broadcast("batch_updated", {"batch_id": batch_id, "status": "ready"})
            return {
                "ok": True,
                "batch_id": batch_id,
                "dish": item_name,
                "actual_qty": effective_qty,
                "status": "cooked",
            }

        return self._stage_or_apply(
            mode,
            _apply,
            human_readable=f"Mark {item_name} batch as cooked ({effective_qty:.0f} made).",
        )

    def record_waste(
        self,
        item_name: str,
        qty: float,
        cause: str,
        *,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Record dish/overproduction waste (not ingredient spoilage)."""
        valid_causes = {"overproduction", "spoilage", "prep_error"}
        if cause not in valid_causes:
            cause = "overproduction"
        item_id, resolved_name = self._resolve_menu_item(item_name)
        if item_id is None:
            return {"need": "item_name", "question": f"Which dish was wasted?"}

        def _apply():
            self.vp._record_waste_event(
                menu_item_id=item_id,
                qty=qty,
                waste_type=cause,
                reason=f"voice: {qty} × {resolved_name} ({cause})",
            )
            return {"ok": True, "item": resolved_name, "qty": qty, "cause": cause}

        return self._stage_or_apply(
            mode,
            _apply,
            human_readable=f"Record {qty} × {resolved_name} as waste ({cause}).",
        )

    def set_staff_attendance(
        self,
        staff_name_or_role: str,
        status: str,
        daypart: Optional[str] = None,
        window: Optional[Dict[str, float]] = None,
        *,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Update a staff member's attendance and auto-resolve menu availability."""
        valid_statuses = {"sick", "leave", "present"}
        if status not in valid_statuses:
            return {"need": "status", "question": f"Should I mark them as sick, on leave, or present?"}

        # Resolve staff name or role → list of staff.
        staff_list = self._resolve_staff(staff_name_or_role)
        if not staff_list:
            return {
                "need": "staff_name_or_role",
                "question": f"I couldn't find '{staff_name_or_role}' in the staff roster. Which staff member do you mean?",
            }

        def _apply():
            if self.staff_agent is None:
                return {"error": "Staff agent not available"}
            results = []
            station_ids_affected = set()
            for s in staff_list:
                result = self.staff_agent.call_in_sick(
                    staff_id=s["id"],
                    status=status,
                    daypart=daypart,
                    reason=f"voice: {staff_name_or_role} marked {status}",
                )
                results.append({"staff": s["name"], **result})
                # Collect stations this person covers.
                station_ids_affected.update(s.get("station_ids", []))
            # Cascade: auto-resolve menu items for affected stations.
            disabled_names = []
            enabled_names = []
            try:
                from .availability import recompute_availability
                from .models import MenuItem
                changes = recompute_availability(
                    self.db_session_factory,
                    self.bus,
                    self.hub_broadcast,
                    changed_station_ids=list(station_ids_affected) if station_ids_affected else None,
                    agent_name="voice_attendance",
                )
                session = self.db_session_factory()
                try:
                    for c in changes:
                        if c.get("reason_code") != "resolved":
                            continue
                        mi = session.get(MenuItem, c["menu_item_id"])
                        name = mi.name if mi else str(c["menu_item_id"])
                        if c["action"] == "disable":
                            disabled_names.append(name)
                        elif c["action"] == "enable":
                            enabled_names.append(name)
                finally:
                    session.close()
            except Exception:  # noqa: BLE001
                logger.warning("recompute_availability failed", exc_info=True)
            return {
                "ok": True,
                "updated": results,
                "status": status,
                "auto_disabled_items": disabled_names,
                "auto_enabled_items": enabled_names,
            }

        names = ", ".join(s["name"] for s in staff_list)
        return self._stage_or_apply(
            mode,
            _apply,
            human_readable=f"Mark {names} as {status}.",
        )

    def forecast_demand(
        self,
        range: Optional[str] = None,          # "daypart" | "day" | "week" | "custom"
        daypart: Optional[str] = None,         # e.g. "dinner" — used when range="daypart"
        day_offset: int = 0,                   # 0=today, 1=tomorrow, …
        start: Optional[float] = None,         # absolute sim-second offset (range="custom")
        end: Optional[float] = None,
        item_name: Optional[str] = None,       # filter to a single menu item
    ) -> Dict[str, Any]:
        """Generate an on-demand interval forecast and return structured results.

        Resolves range → [start, end] in sim-seconds, runs forecast_interval
        synchronously, and returns the full breakdown dict.

        When item_name is provided the result is fully scoped to that one dish:
          • total_qty, by_day, by_daypart are computed for that dish only.
          • A top-level 'item' key provides a concise summary with disabled_reason
            so the voice agent can report latent demand for 86'd dishes.
        """
        if self.forecaster is None:
            return {"error": "Forecaster not available"}

        now = float(self.bus.sim_time)
        _SECONDS_PER_DAY = 86400.0

        # Resolve day-offset base
        day_base = now + day_offset * _SECONDS_PER_DAY

        range_key = (range or "day").lower()
        try:
            if range_key == "daypart":
                from track_a.agents.forecaster import DAYPART_SECONDS  # noqa: PLC0415
                dp = daypart or "dinner"
                if dp not in DAYPART_SECONDS:
                    return {"error": f"Unknown daypart: {dp!r}. Valid: {list(DAYPART_SECONDS)}"}
                import math as _math  # noqa: PLC0415
                day_idx = _math.floor(day_base / _SECONDS_PER_DAY)
                dp_start_s, dp_end_s, _ = DAYPART_SECONDS[dp]
                fs = day_idx * _SECONDS_PER_DAY + dp_start_s
                fe = day_idx * _SECONDS_PER_DAY + dp_end_s
            elif range_key in ("day", "today"):
                import math as _math  # noqa: PLC0415
                day_idx = _math.floor(day_base / _SECONDS_PER_DAY)
                fs = day_idx * _SECONDS_PER_DAY + 8 * 3600   # 08:00 open
                fe = day_idx * _SECONDS_PER_DAY + 23 * 3600  # 23:00 close
            elif range_key == "week":
                fs = now
                fe = now + 7 * _SECONDS_PER_DAY
            elif range_key == "custom":
                if start is None or end is None:
                    return {"error": "range='custom' requires explicit start and end offsets (in seconds from now)"}
                fs = now + float(start)
                fe = now + float(end)
            else:
                # Fallback to a full day
                import math as _math  # noqa: PLC0415
                day_idx = _math.floor(day_base / _SECONDS_PER_DAY)
                fs = day_idx * _SECONDS_PER_DAY + 8 * 3600
                fe = day_idx * _SECONDS_PER_DAY + 23 * 3600

            # --- Resolve the requested dish (incl. inactive) before running ---
            resolved_item = None
            disabled_reason: Optional[str] = None
            include_ids: Optional[list] = None
            if item_name:
                from .models import MenuItem, MenuToggle  # noqa: PLC0415
                from .kitchen import _resolve_menu_item  # noqa: PLC0415
                _session = self.db_session_factory()
                try:
                    resolved_item = _resolve_menu_item(_session, item_name)
                    if resolved_item is None:
                        return {
                            "error": f"No dish found matching '{item_name}'",
                            "need": "which dish?",
                        }
                    include_ids = [resolved_item.id]
                    if not resolved_item.active:
                        # Look up the disable reason from MenuToggle
                        block = (
                            _session.query(MenuToggle)
                            .filter(
                                MenuToggle.menu_item_id == resolved_item.id,
                                MenuToggle.action == "disable",
                                MenuToggle.active == 1,
                            )
                            .order_by(MenuToggle.sim_time.desc())
                            .first()
                        )
                        _reason_labels = {
                            "out_of_stock": "out of stock",
                            "station_unstaffed": "station unstaffed",
                            "manual": "manually disabled",
                        }
                        if block:
                            code = block.reason_code or "manual"
                            disabled_reason = _reason_labels.get(code, code)
                        else:
                            disabled_reason = "item disabled"
                    # Capture values before session closes
                    _item_id = int(resolved_item.id)
                    _item_name = str(resolved_item.name or "")
                    _item_active = bool(resolved_item.active)
                    _item_price = float(resolved_item.dine_in_price or 0)
                finally:
                    _session.close()

            result = self.forecaster.forecast_interval(
                fs,
                fe,
                trigger_reason="voice",
                source="voice",
                requested_by="voice",
                granularity="auto",
                persist=True,
                include_item_ids=include_ids,
            )

            if resolved_item is not None:
                # Scope the whole payload to the requested dish only
                needle_id = _item_id
                item_rows = [it for it in result.get("items", []) if it.get("menu_item_id") == needle_id]
                item_total_qty = sum(it.get("qty", 0) for it in item_rows)

                # Scope by_day to this dish and recompute day totals
                scoped_by_day = []
                for day in result.get("by_day", []):
                    day_items = [di for di in day.get("items", []) if di.get("menu_item_id") == needle_id]
                    day_qty = sum(di.get("qty", 0) for di in day_items)
                    day_baseline = sum(di.get("baseline", 0) for di in day_items)
                    scoped_by_day.append({
                        "day_index": day["day_index"],
                        "start": day["start"],
                        "end": day["end"],
                        "qty": day_qty,
                        "baseline": round(day_baseline, 2),
                    })

                # Recompute by_daypart from items breakdown (not directly available per item per daypart;
                # use the ratio: item share of day × original daypart totals approximation).
                # Simpler and correct: distribute item_total_qty proportionally across original by_daypart.
                original_by_daypart = result.get("by_daypart", {})
                original_total = result.get("total_qty", 1) or 1
                scoped_by_daypart = {
                    dp: {
                        "qty": round(item_total_qty * v["qty"] / original_total) if original_total else 0,
                        "baseline": round(item_total_qty * v["baseline"] / original_total, 2) if original_total else 0,
                    }
                    for dp, v in original_by_daypart.items()
                }

                result = dict(
                    result,
                    items=item_rows,
                    total_qty=item_total_qty,
                    by_day=scoped_by_day,
                    by_daypart=scoped_by_daypart,
                    # Concise top-level summary for the voice agent
                    item={
                        "menu_item_id": _item_id,
                        "name": _item_name,
                        "active": _item_active,
                        "disabled_reason": disabled_reason,
                        "total_qty": item_total_qty,
                        "price": _item_price,
                        "revenue_estimate": round(item_total_qty * _item_price, 2),
                        "per_day": scoped_by_day,
                        "per_daypart": scoped_by_daypart,
                    },
                )
            elif item_name:
                # item_name provided but resolve failed (shouldn't reach here, but guard anyway)
                needle = item_name.strip().lower()
                filtered = [
                    it for it in result.get("items", [])
                    if needle in str(it.get("name", "")).lower()
                ]
                result = dict(result, items=filtered)

            return result
        except Exception as e:  # noqa: BLE001
            return {"error": str(e), "status": "error"}

    def run_forecast(self) -> Dict[str, Any]:
        """Trigger the demand forecaster."""
        if self.forecast_jobs is None:
            return {"error": "Forecast jobs not available"}
        try:
            self.forecast_jobs.enqueue(
                "DETERMINISTIC_FORECAST",
                trigger_reason="voice:manual_run",
                requested_by="voice",
            )
            return {"ok": True, "message": "Forecast job enqueued. Results will be ready in a moment."}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def run_inventory_optimizer(self) -> Dict[str, Any]:
        """Trigger the inventory optimizer LLM pass."""
        if self.optimizer is None:
            return {"error": "Optimizer not available"}
        try:
            self.optimizer.llm_optimize()
            return {"ok": True, "message": "Inventory optimizer ran."}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def run_competitor_scan(self) -> Dict[str, Any]:
        """Trigger a competitor market poll."""
        if self.competitor_agent is None:
            return {"error": "Competitor agent not available"}
        try:
            self.competitor_agent.poll_aggregators()
            return {"ok": True, "message": "Competitor scan running."}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def process_reviews(self) -> Dict[str, Any]:
        """Trigger review processing."""
        if self.review_agent is None:
            return {"error": "Review agent not available"}
        try:
            self.review_agent.process_unprocessed()
            return {"ok": True, "message": "Review processing complete."}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def request_outbound_call(
        self,
        counterparty_type: str,
        purpose: str,
        target: str = "",
        ingredient: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """Stage an outbound call — always requires approval (never auto-applied).

        When ``ingredient`` is provided and counterparty_type is 'supplier',
        the system resolves the default supplier for that ingredient automatically.
        ``note`` is forwarded as operator instructions to the AI caller.
        """
        # Resolve ingredient → supplier when ingredient is given
        resolved_target = target
        ingredient_id: Optional[int] = None
        ingredient_name: str = ingredient or ""
        if counterparty_type == "supplier" and ingredient:
            ing_id, ing_name, sup_id, sup_name = self.vp._resolve_supplier_for_ingredient(ingredient)
            if sup_id:
                ingredient_id = ing_id
                ingredient_name = ing_name
                if not resolved_target:
                    resolved_target = sup_name

        def _apply():
            try:
                if self.vp.calls is not None:
                    cid = self.vp._resolve_counterparty_id(counterparty_type, resolved_target)
                    self.vp.calls.request(
                        agent="manager",
                        counterparty_type=counterparty_type,
                        counterparty_id=cid,
                        purpose=purpose,
                        note=note,
                        ingredient_id=ingredient_id,
                    )
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": True,
                "target": resolved_target,
                "type": counterparty_type,
                "ingredient": ingredient_name,
                "status": "approval_requested",
            }
        # Build a human-readable card label
        _label_parts = [f"Call {resolved_target or counterparty_type}"]
        if ingredient_name:
            _label_parts.append(f"re: {ingredient_name}")
        if purpose and purpose not in ("negotiate",):
            _label_parts.append(f"({purpose})")
        elif purpose == "negotiate":
            _label_parts.append("— negotiate price")
        if note:
            _label_parts.append(f"[note: {note[:60]}]")
        human_readable = " ".join(_label_parts) + "."
        # Always staged regardless of mode.
        return self._stage_or_apply("confirm", _apply, human_readable=human_readable)

    def consult_reasoner(self, question: str, context: Optional[str] = None) -> Dict[str, Any]:
        """Consult the background reasoning model for complex decisions.

        Always enriches the context with a live operational snapshot (per-dish
        forecasts, revenue, staff coverage, sole-cover flags) so the reasoner
        can quantify impact questions like 'how much will we lose if Marco is on leave?'
        """
        from . import reasoner  # noqa: PLC0415
        # Build operational snapshot and append to whatever context the caller provided.
        try:
            from .ops_snapshot import build_ops_snapshot  # noqa: PLC0415
            import json as _json  # noqa: PLC0415
            snapshot = build_ops_snapshot(
                self.db_session_factory,
                self.forecaster,
                bus=self.bus,
            )
            snapshot_text = _json.dumps(snapshot, indent=2, default=str)
            context_parts = []
            if context:
                context_parts.append(context)
            context_parts.append(f"OPERATIONAL SNAPSHOT:\n{snapshot_text}")
            context = "\n\n".join(context_parts)
        except Exception as _snap_ex:  # noqa: BLE001
            logger.warning("ops_snapshot failed, proceeding without: %s", _snap_ex)
            if not context:
                try:
                    context = f"Sim time: {self.bus.sim_time:.0f}s"
                except Exception:  # noqa: BLE001
                    pass
        return reasoner.consult(question, context)

    def register_demand_event(self, note: str, mode: str = "confirm") -> Dict[str, Any]:
        """Log an upcoming event or demand change that affects customer volume.

        Calls the demand synthesizer LLM to interpret the manager's statement, then
        emits a DEMAND_EVENT signal targeting the forecaster agent.
        """
        from . import reasoner as _reasoner  # noqa: PLC0415
        import json as _json  # noqa: PLC0415

        # Resolve sim day and weekday
        try:
            sim_time_f = float(self.bus.sim_time)
        except Exception:  # noqa: BLE001
            sim_time_f = 0.0
        _SECONDS_PER_DAY = 86400
        sim_day = int(sim_time_f // _SECONDS_PER_DAY)
        _WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekday_name = _WEEKDAY_NAMES[sim_day % 7]

        # Build a compact ops snapshot for context
        sim_context: dict = {}
        try:
            session = self.db_session_factory()
            try:
                from .models import Ingredient as _Ing, MenuItem as _MI  # noqa: PLC0415
                low_stock = session.query(_Ing).filter(_Ing.on_hand <= _Ing.safety_stock).all()
                sim_context["low_stock_ingredients"] = [i.name for i in low_stock if i.name]
                sim_context["menu_item_count"] = session.query(_MI).filter(_MI.active.is_(True)).count()
            finally:
                session.close()
        except Exception:  # noqa: BLE001
            pass

        synth = _reasoner.synthesize_demand_event(note, sim_day, weekday_name, sim_context)

        if synth.get("error") and not synth.get("event_ref"):
            return {
                "need": "clarification",
                "question": f"I couldn't understand the event from: {note!r}. Please be more specific.",
            }

        # Resolve the time window from day offsets
        from .clock import DAY_OPEN_OFFSET, DAY_CLOSE_OFFSET, SECONDS_PER_DAY  # noqa: PLC0415
        base_day = sim_day
        start_hour = synth.get("start_hour")
        end_hour = synth.get("end_hour")
        d_start = (base_day + synth["day_from"]) * SECONDS_PER_DAY + (
            start_hour * 3600 if start_hour is not None else DAY_OPEN_OFFSET
        )
        d_end = (base_day + synth["day_to"]) * SECONDS_PER_DAY + (
            end_hour * 3600 if end_hour is not None else DAY_CLOSE_OFFSET
        )
        window = {"start": d_start, "end": d_end}

        # Build human-readable multiplier string
        if synth.get("demand_multiplier"):
            mult_str = f"{synth['demand_multiplier']:.1f}×"
        elif synth.get("expected_attendance"):
            mult_str = f"{int(synth['expected_attendance'])} expected"
        else:
            mult_str = "~1.4×"

        day_from = synth.get("day_from", 0)
        day_to = synth.get("day_to", 0)
        hr = (
            f"Log demand event: '{synth['event_ref']}' — {mult_str} demand, "
            f"{day_from}–{day_to} days from now."
        )

        def _apply() -> Dict[str, Any]:
            payload_dict = {
                "event_ref": synth["event_ref"],
                "event_kind": synth.get("event_kind", "event"),
                "demand_multiplier": synth.get("demand_multiplier"),
                "expected_attendance": synth.get("expected_attendance"),
                "affected_categories": synth.get("affected_categories", []),
                "affected_menu_item_ids": synth.get("affected_menu_item_ids", []),
                "window": window,
                "raw_text": note,
                "confidence": float(synth.get("confidence", 0.7)),
            }
            from .signals import SignalType  # noqa: PLC0415
            ttl = max(window["end"] - float(self.bus.sim_time), 3600.0)
            self.bus.emit(
                SignalType.DEMAND_EVENT,
                payload_dict,
                source="voice",
                ttl=ttl,
                dedup_key=f"demand_event:{synth['event_ref']}",
                target_agents=["track_a.forecaster"],
            )
            return {"ok": True, "event_ref": synth["event_ref"], "window": window, "demand": mult_str}

        return self._stage_or_apply(mode, _apply, human_readable=hr)

    # -----------------------------------------------------------------------
    # Staging (confirm/auto mode)
    # -----------------------------------------------------------------------

    def execute_pending(self, action_id: str) -> Dict[str, Any]:
        """Execute a staged action by id (triggered by confirm_plan frame)."""
        entry = self._pending.pop(action_id, None)
        if entry is None:
            return {"error": f"No pending action {action_id}"}
        try:
            result = entry["fn"]()
            return {"plan_id": action_id, **result, "status": "applied"}
        except Exception as e:  # noqa: BLE001
            logger.exception("VoiceActions: execute_pending %s failed", action_id)
            return {"error": str(e), "plan_id": action_id}

    def cancel_pending(self, action_id: str) -> Dict[str, Any]:
        """Cancel a staged action."""
        entry = self._pending.pop(action_id, None)
        if entry is None:
            return {"error": f"No pending action {action_id}"}
        return {"status": "cancelled", "plan_id": action_id}

    def _stage_or_apply(
        self,
        mode: str,
        fn: Callable,
        *,
        human_readable: str,
    ) -> Dict[str, Any]:
        if mode == "auto":
            result = fn()
            return {"status": "applied", "summary": human_readable, **result}
        # Confirm mode: stage it.
        action_id = str(uuid.uuid4())
        self._pending[action_id] = {"fn": fn, "human_readable": human_readable}
        return {
            "status": "pending",
            "plan_id": action_id,
            "human_readable": human_readable,
            "requires_approval": True,
        }

    # -----------------------------------------------------------------------
    # Resolution helpers
    # -----------------------------------------------------------------------

    def _resolve_ingredient(self, name: str) -> tuple:
        """Returns (ingredient_id, resolved_name) or (None, None)."""
        from .models import Ingredient
        needle = name.strip().lower()
        session = self.db_session_factory()
        try:
            ings = session.query(Ingredient).all()
            # Exact match first, then contains.
            for ing in sorted(ings, key=lambda i: len(i.name or ""), reverse=True):
                if (ing.name or "").lower() == needle:
                    return int(ing.id), str(ing.name)
            for ing in sorted(ings, key=lambda i: len(i.name or ""), reverse=True):
                if needle in (ing.name or "").lower():
                    return int(ing.id), str(ing.name)
        finally:
            session.close()
        return None, None

    def _resolve_menu_item(self, name: str) -> tuple:
        """Returns (menu_item_id, resolved_name) or (None, None)."""
        from .models import MenuItem
        needle = name.strip().lower()
        session = self.db_session_factory()
        try:
            items = session.query(MenuItem).all()
            for mi in sorted(items, key=lambda i: len(i.name or ""), reverse=True):
                if (mi.name or "").lower() == needle:
                    return int(mi.id), str(mi.name)
            for mi in sorted(items, key=lambda i: len(i.name or ""), reverse=True):
                if needle in (mi.name or "").lower():
                    return int(mi.id), str(mi.name)
        finally:
            session.close()
        return None, None

    def _resolve_staff(self, name_or_role: str) -> List[Dict[str, Any]]:
        """Resolve one or more staff names/roles to a list of staff dicts.

        Accepts comma-separated, 'and'-separated, or '&'-separated lists.
        Returns deduplicated results across all fragments.
        """
        import re
        raw = name_or_role.strip()
        fragments = [
            f.strip()
            for f in re.split(r",\s*|\s+and\s+|\s*&\s*", raw, flags=re.IGNORECASE)
            if f.strip()
        ]

        if len(fragments) <= 1:
            # Single name or role — use original logic.
            return self._resolve_single_staff(raw)

        # Multiple fragments — resolve each and deduplicate by id.
        seen_ids: set = set()
        results = []
        for fragment in fragments:
            for staff_dict in self._resolve_single_staff(fragment):
                if staff_dict["id"] not in seen_ids:
                    seen_ids.add(staff_dict["id"])
                    results.append(staff_dict)
        return results

    def _resolve_single_staff(self, name_or_role: str) -> List[Dict[str, Any]]:
        """Resolve a single staff name or role to a list of staff dicts.

        Tries exact name match, then partial name, then role match.
        Returns a list so a role with multiple people works.
        """
        from .models import Staff, StaffStation
        needle = name_or_role.strip().lower()
        session = self.db_session_factory()
        try:
            all_staff = session.query(Staff).filter(Staff.active == 1).all()
            # Collect station ids per staff.
            station_map: Dict[int, List[int]] = {}
            for ss in session.query(StaffStation).all():
                station_map.setdefault(int(ss.staff_id), []).append(int(ss.station_id))

            def _to_dict(s: Any) -> Dict:
                return {
                    "id": int(s.id),
                    "name": str(s.name or ""),
                    "role": str(s.role or ""),
                    "station_ids": station_map.get(int(s.id), []),
                }

            # 1. Exact name.
            for s in all_staff:
                if (s.name or "").lower() == needle:
                    return [_to_dict(s)]
            # 2. Partial name.
            matched = [s for s in all_staff if needle in (s.name or "").lower()]
            if matched:
                return [_to_dict(s) for s in matched]
            # 3. Role match (can return multiple people with that role).
            role_matched = [s for s in all_staff if needle in (s.role or "").lower()]
            if role_matched:
                return [_to_dict(s) for s in role_matched]
        finally:
            session.close()
        return []

    def _resolve_batch(self, session: Any, dish_or_batch: str) -> tuple:
        """Returns (batch_dict, item_name) for the next pending batch matching the query."""
        from .models import Batch, MenuItem
        item_names = {int(mi.id): str(mi.name or "") for mi in session.query(MenuItem).all()}
        needle = dish_or_batch.strip().lower()

        # Try numeric batch id.
        if needle.lstrip("#").isdigit():
            batch_id = int(needle.lstrip("#"))
            b = session.get(Batch, batch_id)
            if b is not None:
                return (
                    {"id": int(b.id), "menu_item_id": int(b.menu_item_id or 0), "planned_qty": float(b.planned_qty or 0)},
                    item_names.get(int(b.menu_item_id or 0), str(b.menu_item_id)),
                )

        # Try dish name match → most recent approved/decided batch.
        for mid, name in item_names.items():
            if needle in name.lower():
                b = (
                    session.query(Batch)
                    .filter(
                        Batch.menu_item_id == mid,
                        Batch.status.in_(["decided", "approved"]),
                        Batch.decision == "cook",
                    )
                    .order_by(Batch.decided_at.desc())
                    .first()
                )
                if b is not None:
                    return (
                        {"id": int(b.id), "menu_item_id": mid, "planned_qty": float(b.planned_qty or 0)},
                        name,
                    )

        # Fallback: latest pending batch overall.
        b = (
            session.query(Batch)
            .filter(Batch.status.in_(["decided", "approved"]), Batch.decision == "cook")
            .order_by(Batch.decided_at.desc())
            .first()
        )
        if b is not None:
            return (
                {"id": int(b.id), "menu_item_id": int(b.menu_item_id or 0), "planned_qty": float(b.planned_qty or 0)},
                item_names.get(int(b.menu_item_id or 0), ""),
            )
        return None, None

    def _get_expiring_soonest(self, n: int = 5) -> Dict[str, Any]:
        from .models import Ingredient, InventoryLot
        session = self.db_session_factory()
        try:
            ings = {int(i.id): i for i in session.query(Ingredient).all()}
            lots = (
                session.query(InventoryLot)
                .filter(InventoryLot.status == "active", InventoryLot.qty_on_hand > 0)
                .order_by(InventoryLot.expiry_date.asc())
                .limit(n)
                .all()
            )
            rows = []
            for lot in lots:
                ing = ings.get(int(lot.ingredient_id or 0))
                rows.append({
                    "ingredient": ing.name if ing else str(lot.ingredient_id),
                    "qty": float(lot.qty_on_hand or 0),
                    "unit": lot.unit,
                    "expiry_sim_time": float(lot.expiry_date) if lot.expiry_date else None,
                })
        finally:
            session.close()
        return {"expiring_soonest": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_window(window: str) -> float:
    """Parse a window string like '3h', '30m', '1d' into seconds."""
    window = window.strip().lower()
    if window.endswith("d"):
        return float(window[:-1]) * 86400.0
    if window.endswith("h"):
        return float(window[:-1]) * 3600.0
    if window.endswith("m"):
        return float(window[:-1]) * 60.0
    return float(window)
