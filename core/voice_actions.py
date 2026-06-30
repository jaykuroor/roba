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
                state = _k._derive_state(b)
                rows.append({
                    "id": int(b.id),
                    "menu_item": name,
                    "decision": b.decision,
                    "status": b.status,
                    "state": state,
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
        session = self.db_session_factory()
        try:
            ingredients = {int(ing.id): ing for ing in session.query(Ingredient).all()}
            suppliers = {int(s.id): s for s in session.query(Supplier).all()}
            rows = []
            for cat in session.query(SupplierCatalog).all():
                ing = ingredients.get(int(cat.ingredient_id or 0))
                sup = suppliers.get(int(cat.supplier_id or 0))
                if not ing or not sup:
                    continue
                if ingredient_name and ingredient_name.lower() not in str(ing.name or "").lower():
                    continue
                rows.append({
                    "ingredient": ing.name,
                    "supplier": sup.name,
                    "price": float(cat.current_price) if cat.current_price is not None else None,
                    "unit": cat.unit,
                    "availability": cat.availability,
                })
        finally:
            session.close()
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
        item_name: str,
        reason: str = "voice request",
        *,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Disable a menu item (sticky manual block)."""
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
            human_readable=f"Disable {resolved_name}?",
        )

    def enable_menu_item(
        self,
        item_name: str,
        *,
        mode: str = "confirm",
    ) -> Dict[str, Any]:
        """Re-enable a menu item — clears all blocks including manual."""
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
            human_readable=f"Re-enable {resolved_name}?",
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
                new_qty = max(0.0, current + delta)
                from .signals import SignalType
                self.bus.emit(
                    SignalType.INVENTORY_RECEIPT_REPORTED,
                    {
                        "ingredient_id": ing_id,
                        "ingredient_ref": ing_name,
                        "qty": delta,
                        "unit": ing_unit,
                        "reason": reason,
                    },
                    source="voice",
                    groups=["inventory"],
                )
            else:
                new_qty = float(set_to)
                from .signals import SignalType
                self.bus.emit(
                    SignalType.INVENTORY_COUNT_REPORTED,
                    {
                        "ingredient_id": ing_id,
                        "ingredient_ref": ing_name,
                        "qty": new_qty,
                        "unit": ing_unit,
                        "reason": reason,
                    },
                    source="voice",
                    groups=["inventory"],
                )
            return {
                "ok": True,
                "ingredient": ing_name,
                "on_hand_before": current,
                "on_hand_after": new_qty,
                "unit": ing_unit,
            }

        if set_to is not None:
            op = f"Set {ing_name} to {set_to} {unit or ''}".strip() + "?"
        else:
            direction = "Add" if (delta or 0) > 0 else "Remove"
            op = f"{direction} {abs(delta or 0)} {unit or ''} {ing_name}?".strip()
        return self._stage_or_apply(mode, _apply, human_readable=op)

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
            human_readable=f"Mark {qty_str} {ing_name} as spoiled?",
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
            human_readable=f"Mark {item_name} batch cooked — {effective_qty:.0f} made?",
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
            human_readable=f"Record {qty} × {resolved_name} as {cause}?",
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
                pass
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
            human_readable=f"Mark {names} as {status}?",
        )

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
        target: str,
        counterparty_type: str,
        purpose: str,
    ) -> Dict[str, Any]:
        """Stage an outbound call — always requires approval (never auto-applied)."""
        def _apply():
            try:
                if self.vp.calls is not None:
                    self.vp.calls.request(
                        agent="manager",
                        counterparty_type=counterparty_type,
                        counterparty_id=self.vp._resolve_counterparty_id(counterparty_type, target),
                        purpose=purpose,
                    )
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": True,
                "target": target,
                "type": counterparty_type,
                "status": "approval_requested",
            }
        # Always staged regardless of mode.
        return self._stage_or_apply("confirm", _apply, human_readable=f"Call {target} — {purpose}?")

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
            return {"status": "applied", "plan_id": action_id, **result}
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
            return {"status": "applied", **result}
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
        """Resolve a staff name or role to a list of staff dicts.

        Tries exact name match, then partial name, then role match.
        Returns a list so "all cooks" or a role with multiple people works.
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
