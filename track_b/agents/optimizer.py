"""Inventory Optimizer agent — the decisions (02 §B4.2).

Consumes demand-driven threshold signals to size reorders, toggle menu items,
and turn near-expiry lots into promos (§18.8). Writes ``purchase_orders`` (via
Procurement), ``menu_toggles`` (+ ``menu_items.active``) and ``promotions``.

Stream E adds an LLM pass (``llm_optimize``) that reasons over the inventory
landscape and demand-forecast context to produce higher-quality decisions:
disabling lower-margin dishes when a shared ingredient is constrained, creating
deals for slow-movers/near-waste items, and deferring or accelerating reorders
based on demand patterns. Falls back to the deterministic path gracefully when
no LLM key is present.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional

from core import config
from core.agent_base import BaseAgent
from core.clock import DAY_OPEN_OFFSET, SECONDS_PER_DAY
from core.llm import CANNED_NOTE
from core.models import (
    AppSettings,
    Ingredient,
    InventoryLevel,
    InventoryLot,
    InventoryOptimizerMemory,
    ManagerChange,
    MenuItem,
    MenuToggle,
    OrderLine,
    PlannedOrder,
    ProcurementPlanRun,
    Promotion,
    PurchaseOrder,
    PurchaseOrderLine,
    Recipe,
    RecipeLine,
    SourcingRun,
    Supplier,
    SupplierCatalog,
    SupplierTerm,
)
from core.signals import SignalType

logger = logging.getLogger(__name__)

# Signal groups this agent listens to (02 §B4.2).
GROUPS = ["inventory", "procurement"]

# Lazy import; only pulled in when run_sourcing_plan executes so the solver
# dependency (PuLP) is optional for the rest of the optimizer.
def _import_solve_sourcing() -> Any:
    from track_b.procurement.sourcing import solve_sourcing  # noqa: PLC0415
    return solve_sourcing

# JSON schema for the LLM optimizer action list.
_OPTIMIZE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["toggle_item", "create_deal", "reorder",
                                        "defer_reorder", "request_negotiation"]},
                    "menu_item_id": {"type": "integer"},
                    "ingredient_id": {"type": "integer"},
                    "supplier_id": {"type": "integer"},
                    "toggle_direction": {"type": "string", "enum": ["disable", "enable"]},
                    "discount_pct": {"type": "number"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["action", "reason", "confidence"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["actions", "summary"],
}


class InventoryOptimizer(BaseAgent):
    """Reorder, menu-toggle, and expiry→promo decisions driven by demand.

    Stream E: an LLM reasoning pass (``llm_optimize``) runs on a longer
    cadence and augments the deterministic decisions with context-aware
    choices.  All LLM actions are mapped onto the same guarded executors
    (``_disable``, ``_propose_promo``, ``procurement.create_po``) so the
    APPROVAL_PO_THRESHOLD and all safety rails still apply.
    """

    def __init__(
        self,
        bus: Any,
        db_session_factory: Any,
        name: str = "optimizer",
        ws_broadcast: Any = None,
        procurement: Any = None,
        approvals: Any = None,
        llm: Any = None,
    ):
        super().__init__(bus, db_session_factory, name, ws_broadcast=ws_broadcast)
        self.subscribe(GROUPS)
        self.procurement = procurement
        self.approvals = approvals
        self.llm = llm
        # menu_item_id -> ingredient_id that triggered its disable, so the
        # reorder sweep knows which ingredient to watch for re-enabling
        # (menu_toggles has no ingredient_id column; this is in-process only).
        self._toggle_cause: Dict[int, int] = {}
        self._market_spectator: Any = None

    def attach_procurement(self, procurement: Any) -> None:
        self.procurement = procurement

    def attach_approvals(self, approvals: Any) -> None:
        self.approvals = approvals

    def attach_market_spectator(self, market_spectator: Any) -> None:
        self._market_spectator = market_spectator

    # -- signal handling ----------------------------------------------------

    def on_signal(self, signal: Any) -> None:
        """React to ``LOW_STOCK`` / ``STOCKOUT_RISK`` (toggle + reorder) and
        ``EXPIRY_RISK`` (promo proposal) (§B4.2)."""
        if signal.type in (SignalType.LOW_STOCK.value, SignalType.STOCKOUT_RISK.value):
            payload = signal.payload or {}
            ingredient_id = payload.get("ingredient_id")
            if ingredient_id is None:
                return
            self._maybe_toggle(ingredient_id, float(payload.get("projected_runout") or 0.0))
            self._maybe_reorder(ingredient_id)
            # Trigger LLM optimization when a shortage is signalled.
            if config.OPTIMIZER_LLM_AUTO_MODE and self.llm is not None:
                self.llm_optimize()
        elif signal.type == SignalType.EXPIRY_RISK.value:
            self._propose_promo(signal.payload or {})
            if config.OPTIMIZER_LLM_AUTO_MODE and self.llm is not None:
                self.llm_optimize()
        elif signal.type == SignalType.EXPIRY_USE_PRIORITY.value:
            payload = dict(signal.payload or {})
            ingredient_id = payload.get("ingredient_id") or self._resolve_ingredient_id(
                payload.get("ingredient_ref")
            )
            if ingredient_id is None:
                return
            payload["ingredient_id"] = ingredient_id
            self._propose_promo(payload)
        elif signal.type == SignalType.DEMAND_FORECAST_HORIZON.value:
            # Re-plan when the forecaster emits a fresh horizon (e.g. after a parade event).
            self.build_procurement_plan()
        elif signal.type == SignalType.WASTE_EVENT.value:
            # Sudden spoilage: stock dropped unexpectedly — re-plan immediately.
            self.build_procurement_plan()
        elif signal.type == SignalType.MENU_TOGGLE_REQUEST.value:
            payload = signal.payload or {}
            menu_item_id = payload.get("menu_item_id") or self._resolve_menu_item_id(
                payload.get("item_ref")
            )
            if menu_item_id is None:
                return
            self._manual_toggle(
                int(menu_item_id),
                str(payload.get("action") or "disable"),
                str(payload.get("reason") or payload.get("raw_text") or "manual voice request"),
            )

    # -- reorder (§18.8) ------------------------------------------------------

    def reorder_check(self) -> None:
        """Periodic reorder sweep: ``on_hand ≤ reorder_point`` → PO (§18.8)."""
        # Execute any planned orders whose order_date is now due.
        self.execute_due_planned_orders()

        session = self.db_session_factory()
        try:
            levels = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.reorder_point.isnot(None), InventoryLevel.reorder_point > 0)
                .all()
            )
            ingredient_ids = [lv.ingredient_id for lv in levels]
        finally:
            session.close()

        for ingredient_id in ingredient_ids:
            self._maybe_reorder(ingredient_id)

        for menu_item_id, ingredient_id in list(self._toggle_cause.items()):
            if self._on_hand_above_reorder(ingredient_id):
                self._reenable(menu_item_id, ingredient_id)

        # Rebuild the forward plan after each reactive sweep.
        self.build_procurement_plan()

    def _on_hand_above_reorder(self, ingredient_id: int) -> bool:
        session = self.db_session_factory()
        try:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            if level is None or level.reorder_point is None:
                return False
            return float(level.on_hand_cached or 0.0) > float(level.reorder_point)
        finally:
            session.close()

    # -- forward planner (WS4) -----------------------------------------------

    def build_procurement_plan(self, horizon_days: Optional[float] = None) -> int:
        """Build a time-phased forward procurement plan.

        Projects running inventory day-by-day using the DEMAND_FORECAST_HORIZON
        signal, schedules PlannedOrder rows for each ingredient that will dip
        below safety stock, and applies hysteresis to avoid churn.

        Returns the total count of active planned orders (new + kept).
        """
        if self.procurement is None:
            return 0

        # Fetch the latest DEMAND_FORECAST_HORIZON signal (pull-style).
        horizon_signals = self.bus.live(type=SignalType.DEMAND_FORECAST_HORIZON)
        days_payload = []
        if horizon_signals:
            payload = horizon_signals[0].payload or {}
            days_payload = payload.get("days") or []

        now = float(self.bus.sim_time)
        now_day = int(now // SECONDS_PER_DAY)

        # Derive n_days from the actual forecast span so we never project demand
        # beyond where we have real data (which caused back-half zero-demand rows
        # and under-sized orders).  If the caller provides an explicit horizon_days,
        # cap it at the forecast span; with no live forecast fall back gracefully.
        forecast_span = (max(d.get("day_index", 0) for d in days_payload) + 1) if days_payload else 0
        if forecast_span > 0:
            n_days = forecast_span if horizon_days is None else min(math.ceil(float(horizon_days)), forecast_span)
        else:
            n_days = math.ceil(float(horizon_days or 14.0))
        horizon_days = float(n_days)  # normalise for ProcurementPlanRun persistence

        # ----------------------------------------------------------------
        # Phase 1: Compute per-day per-ingredient demand and gather supply
        # ----------------------------------------------------------------
        per_day_demand: Dict[int, Dict[int, float]] = {}   # {ing_id: {day_idx: qty}}
        per_day_baseline: Dict[int, Dict[int, float]] = {} # {ing_id: {day_idx: baseline}}
        ingredients_data: Dict[int, Dict] = {}

        session = self.db_session_factory()
        try:
            catalog_entries = session.query(SupplierCatalog).all()
            ingredient_ids = list({c.ingredient_id for c in catalog_entries})

            for ing_id in ingredient_ids:
                per_day_demand[ing_id] = {}
                per_day_baseline[ing_id] = {}

            # Precompute recipe qty per (menu_item_id, ingredient_id) for efficiency.
            recipe_lines = (
                session.query(RecipeLine, Recipe)
                .join(Recipe, Recipe.id == RecipeLine.recipe_id)
                .filter(RecipeLine.ingredient_id.in_(ingredient_ids), RecipeLine.optional == 0)
                .all()
            )
            # {menu_item_id: {ingredient_id: raw_qty}}
            raw_recipe_qty: Dict[int, Dict[int, float]] = {}
            for rl, recipe in recipe_lines:
                mid = recipe.menu_item_id
                raw_recipe_qty.setdefault(mid, {})[rl.ingredient_id] = (
                    raw_recipe_qty.get(mid, {}).get(rl.ingredient_id, 0.0)
                    + float(rl.qty or 0.0)
                )

            # Yield-factor adjust.
            yield_factors: Dict[int, float] = {}
            for lv in session.query(InventoryLevel).filter(
                InventoryLevel.ingredient_id.in_(ingredient_ids)
            ):
                yield_factors[lv.ingredient_id] = max(float(lv.yield_factor or 1.0), 0.0001)
            recipe_qty: Dict[int, Dict[int, float]] = {}  # {menu_item_id: {ing_id: adj_qty}}
            for mid, ing_map in raw_recipe_qty.items():
                recipe_qty[mid] = {}
                for ing_id, raw_q in ing_map.items():
                    recipe_qty[mid][ing_id] = raw_q / yield_factors.get(ing_id, 1.0)

            # Accumulate per-day ingredient demand from horizon signal.
            for day in days_payload:
                day_idx = int(day.get("day_index") or 0)
                if day_idx >= n_days:
                    continue
                for item_entry in day.get("items") or []:
                    menu_item_id = item_entry.get("menu_item_id")
                    if menu_item_id is None:
                        continue
                    qty = float(item_entry.get("qty") or 0.0)
                    baseline = float(item_entry.get("baseline") or 0.0)
                    mid_recipe = recipe_qty.get(int(menu_item_id)) or {}
                    for ing_id, ing_qty in mid_recipe.items():
                        if ing_qty > 0:
                            per_day_demand[ing_id][day_idx] = (
                                per_day_demand[ing_id].get(day_idx, 0.0) + qty * ing_qty
                            )
                            per_day_baseline[ing_id][day_idx] = (
                                per_day_baseline[ing_id].get(day_idx, 0.0) + baseline * ing_qty
                            )

            # Gather inventory levels, supplier info, ingredient metadata.
            for ing_id in ingredient_ids:
                level = (
                    session.query(InventoryLevel)
                    .filter(InventoryLevel.ingredient_id == ing_id)
                    .first()
                )
                if level is None:
                    continue
                ing = session.get(Ingredient, ing_id)
                if ing is None:
                    continue

                catalog = (
                    session.query(SupplierCatalog)
                    .filter(SupplierCatalog.ingredient_id == ing_id)
                    .all()
                )
                specs = [
                    {
                        "supplier_id": c.supplier_id,
                        "current_price": float(c.current_price or 0.0),
                        "pack_size": float(c.pack_size or 1.0),
                        "availability": c.availability,
                        "unit": c.unit,
                        "is_default": int(getattr(c, "is_default", 0) or 0),
                    }
                    for c in catalog
                ]
                if not specs:
                    continue
                sup_ids = [s["supplier_id"] for s in specs]
                suppliers = (
                    session.query(Supplier)
                    .filter(Supplier.id.in_(sup_ids))
                    .all()
                )
                lead_by_supplier = {s.id: float(s.lead_time_days or 1.0) for s in suppliers}
                candidate = self._select_supplier(specs, lead_by_supplier)
                if candidate is None:
                    continue

                lead_days = float(lead_by_supplier.get(candidate["supplier_id"], 1.0))
                on_hand = float(level.on_hand_cached or 0.0)
                safety_stock = float(level.safety_stock or 0.0)
                shelf_life = (
                    float(ing.shelf_life_days)
                    if ing.perishable and ing.shelf_life_days
                    else None
                )

                # A9: Gather dated lots for expiry-aware projection.  Each lot is
                # stored as [qty, exp_day_offset] where exp_day_offset is days from
                # now_day until the lot expires (999999 = never expires).
                lots_raw = (
                    session.query(InventoryLot.qty_on_hand, InventoryLot.expiry_date)
                    .filter(
                        InventoryLot.ingredient_id == ing_id,
                        InventoryLot.status == "active",
                        InventoryLot.qty_on_hand > 0,
                    )
                    .all()
                )
                lot_data: List[List[float]] = []
                for lot_qty, lot_expiry in lots_raw:
                    if lot_expiry is not None:
                        exp_day = int(float(lot_expiry) // SECONDS_PER_DAY) - now_day
                    else:
                        exp_day = 999999  # never expires
                    lot_data.append([float(lot_qty or 0.0), float(exp_day)])

                ingredients_data[ing_id] = {
                    "candidate": candidate,
                    "lead_days": lead_days,
                    "on_hand": on_hand,
                    "safety_stock": safety_stock,
                    "shelf_life": shelf_life,
                    "lots": lot_data,
                }
        finally:
            session.close()

        # ----------------------------------------------------------------
        # A1: Build inbound_by_day — quantities already en-route bucketed by
        # arrival day so the projection never re-flags covered shortages.
        # ----------------------------------------------------------------
        inbound_by_day: Dict[int, Dict[int, float]] = {ing_id: {} for ing_id in ingredient_ids}
        if ingredient_ids:
            from sqlalchemy import func as _sa_func  # local import; avoids top-level sqlalchemy dep
            session = self.db_session_factory()
            try:
                in_transit = (
                    session.query(
                        PurchaseOrderLine.ingredient_id,
                        PurchaseOrder.expected_delivery,
                        _sa_func.sum(PurchaseOrderLine.qty).label("total_qty"),
                    )
                    .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                    .filter(
                        PurchaseOrderLine.ingredient_id.in_(ingredient_ids),
                        PurchaseOrder.status.in_(("proposed", "approved", "placed")),
                        PurchaseOrder.expected_delivery.isnot(None),
                    )
                    .group_by(PurchaseOrderLine.ingredient_id, PurchaseOrder.expected_delivery)
                    .all()
                )
                for ing_id_t, exp_delivery, total_qty in in_transit:
                    if exp_delivery is None:
                        continue
                    arr_day = int(float(exp_delivery) // SECONDS_PER_DAY) - now_day
                    # Overdue deliveries (arr_day < 0) are treated as arriving today.
                    arr_day = max(arr_day, 0)
                    if ing_id_t in inbound_by_day:
                        inbound_by_day[ing_id_t][arr_day] = (
                            inbound_by_day[ing_id_t].get(arr_day, 0.0) + float(total_qty or 0.0)
                        )
            finally:
                session.close()

        # ----------------------------------------------------------------
        # Phase 2: Day-by-day stock projection per ingredient
        # ----------------------------------------------------------------
        new_orders: List[Dict] = []

        _LOT_NEVER_EXPIRES = 999999.0  # sentinel exp_day for non-perishable lots

        for ing_id, data in ingredients_data.items():
            candidate = data["candidate"]
            lead_days = data["lead_days"]
            on_hand = data["on_hand"]
            safety_stock = data["safety_stock"]
            shelf_life = data["shelf_life"]
            pack_size = candidate["pack_size"]

            # A9: Seed the projection with the ingredient's real dated lots
            # (qty, exp_day_offset) so stock that expires mid-horizon isn't
            # counted as available coverage.  If lots are absent (edge-case),
            # fall back to on_hand as a non-expiring lump to stay safe.
            lots_proj: List[List[float]] = (
                [list(l) for l in data.get("lots", [])]
                or [[on_hand, _LOT_NEVER_EXPIRES]]
            )

            for d in range(n_days):
                delivery_sim_s = float((now_day + d) * SECONDS_PER_DAY + DAY_OPEN_OFFSET)
                # Day-0 opens at 08:00 but the sim may already be past that; clamp
                # so delivery is never scheduled in the past (avoids spurious at_risk).
                if d == 0:
                    delivery_sim_s = max(delivery_sim_s, now)

                # A1+A9: Credit inbound deliveries arriving today as a fresh lot
                # (expiry anchored to the ingredient's shelf life from arrival day).
                inbound_today = inbound_by_day[ing_id].get(d, 0.0)
                if inbound_today > 0:
                    inbound_exp = d + (shelf_life if shelf_life is not None else _LOT_NEVER_EXPIRES)
                    lots_proj.append([inbound_today, inbound_exp])

                # A9: Expire lots that spoil on or before today (exp_day <= d).
                lots_proj = [l for l in lots_proj if l[1] > d]

                # Sort soonest-expiry first for FIFO consumption.
                lots_proj.sort(key=lambda l: l[1])

                # Robust demand floor: max(transient qty, steady-state baseline).
                day_qty = per_day_demand[ing_id].get(d, 0.0)
                day_base = per_day_baseline[ing_id].get(d, 0.0)
                day_demand = max(day_qty, day_base)

                # Consume demand FIFO from soonest-expiry lot.
                rem_demand = day_demand
                for lot in lots_proj:
                    take = min(lot[0], rem_demand)
                    lot[0] -= take
                    rem_demand -= take
                    if rem_demand <= 0.0:
                        break
                lots_proj = [l for l in lots_proj if l[0] > 1e-9]

                running_stock = sum(l[0] for l in lots_proj)

                if running_stock < safety_stock:
                    order_date = delivery_sim_s - lead_days * SECONDS_PER_DAY

                    # Size: cover demand for lead_days + REORDER_INTERVAL_DAYS + 1 more days.
                    cover_days = lead_days + config.REORDER_INTERVAL_DAYS + 1
                    cover_end = min(n_days, d + math.ceil(cover_days))

                    demand_to_cover = sum(
                        max(
                            per_day_demand[ing_id].get(dd, 0.0),
                            per_day_baseline[ing_id].get(dd, 0.0),
                        )
                        for dd in range(d, cover_end)
                    )
                    needed = max(0.0, demand_to_cover + safety_stock - max(0.0, running_stock))

                    # A1 (sizing): subtract inbound scheduled within the coverage window.
                    # running_stock already accounts for inbound on days 0..d; subtract
                    # days d+1..cover_end so the order size reflects the true net shortfall.
                    inbound_in_window = sum(
                        inbound_by_day[ing_id].get(dd, 0.0) for dd in range(d + 1, cover_end)
                    )
                    needed = max(0.0, needed - inbound_in_window)

                    # Perishable ceiling: don't order more than can be consumed before expiry.
                    if shelf_life is not None:
                        expiry_end = min(n_days, d + math.ceil(min(shelf_life, cover_days)))
                        demand_before_expiry = sum(
                            max(
                                per_day_demand[ing_id].get(dd, 0.0),
                                per_day_baseline[ing_id].get(dd, 0.0),
                            )
                            for dd in range(d, expiry_end)
                        )
                        if 0 < demand_before_expiry < needed:
                            needed = demand_before_expiry

                    if needed <= 0:
                        continue

                    qty = math.ceil(needed / pack_size) * pack_size
                    # A9: Add the planned delivery as a fresh lot so subsequent
                    # days see the replenishment and don't double-plan.
                    order_exp = d + (shelf_life if shelf_life is not None else _LOT_NEVER_EXPIRES)
                    lots_proj.append([qty, order_exp])
                    running_stock += qty

                    covers_until = float(
                        (now_day + d + math.ceil(cover_days)) * SECONDS_PER_DAY + DAY_OPEN_OFFSET
                    )
                    status = "at_risk" if order_date < now else "planned"

                    new_orders.append({
                        "ingredient_id": ing_id,
                        "supplier_id": candidate["supplier_id"],
                        "qty": qty,
                        "unit": candidate["unit"],
                        "unit_price": candidate["current_price"],
                        "order_date": order_date,
                        "delivery_date": delivery_sim_s,
                        "covers_from": delivery_sim_s,
                        "covers_until": covers_until,
                        "status": status,
                        "reason": (
                            f"Projected stock below safety_stock ({safety_stock:.1f}) "
                            f"on sim day {now_day + d}"
                        ),
                    })

        # ----------------------------------------------------------------
        # Phase 2.5: MOV consolidation + delivery-charge attachment
        #
        # The per-ingredient projection above creates orders greedily; some
        # supplier-day groups may individually fall below a supplier's
        # min_order_value.  This pass:
        #   1. Loads each supplier's min_order_value, delivery_charge,
        #      pack_size-per-ingredient, and shelf_life-per-ingredient.
        #   2. Groups new_orders by (supplier_id, delivery_day_index).
        #   3. Merges a sub-MOV group with the nearest same-supplier group,
        #      guarded by perishable shelf-life (don't pull a perishable order
        #      so far forward that it expires before the original need date).
        #   4. If the surviving group is still < MOV, pads the ingredient with
        #      the longest shelf life in pack_size increments until MOV is met
        #      (never pads a short-shelf perishable).
        #   5. Attaches delivery_charge per order so cost totals are accurate.
        # ----------------------------------------------------------------
        if new_orders:
            session = self.db_session_factory()
            try:
                # Build supplier metadata map {supplier_id: {min_order_value, delivery_charge}}
                sup_ids_needed = {o["supplier_id"] for o in new_orders if o["supplier_id"]}
                sups = (
                    session.query(Supplier)
                    .filter(Supplier.id.in_(sup_ids_needed))
                    .all()
                )
                sup_meta: Dict[int, Dict] = {
                    s.id: {
                        "mov": float(s.min_order_value or 0.0),
                        "delivery_charge": float(s.delivery_charge or 0.0),
                    }
                    for s in sups
                }
                # Pack sizes per (supplier_id, ingredient_id) for padding
                cat_rows = (
                    session.query(SupplierCatalog)
                    .filter(SupplierCatalog.supplier_id.in_(sup_ids_needed))
                    .all()
                )
                pack_by: Dict[tuple, float] = {
                    (c.supplier_id, c.ingredient_id): float(c.pack_size or 1.0)
                    for c in cat_rows
                }
            finally:
                session.close()

            now_day_i = int(now // SECONDS_PER_DAY)

            def _delivery_day(o: Dict) -> int:
                return int(float(o["delivery_date"]) // SECONDS_PER_DAY)

            def _goods_value(orders: List[Dict]) -> float:
                return sum(float(o["unit_price"] or 0.0) * float(o["qty"]) for o in orders)

            # Group by (supplier_id, delivery_day)
            from collections import defaultdict as _dd
            groups: Dict[tuple, List[Dict]] = _dd(list)
            for o in new_orders:
                groups[(o["supplier_id"], _delivery_day(o))].append(o)

            # Sort groups by (supplier_id, delivery_day) for nearest-group search
            sorted_keys = sorted(groups.keys(), key=lambda k: (k[0], k[1]))

            # --- Merge sub-MOV groups ---
            for key in list(sorted_keys):
                if key not in groups:
                    continue
                sup_id, day = key
                meta = sup_meta.get(sup_id, {})
                mov = meta.get("mov", 0.0)
                if mov <= 0:
                    continue
                grp = groups[key]
                if _goods_value(grp) >= mov:
                    continue

                # Find nearest other group for same supplier (earlier preferred, then later)
                candidates = [
                    (abs(k[1] - day), k[1] - day, k)
                    for k in sorted_keys
                    if k != key and k[0] == sup_id and k in groups
                ]
                if not candidates:
                    continue
                candidates.sort()
                _, day_delta, target_key = candidates[0]

                # Shelf-life guard: if we're merging LATER into EARLIER (day_delta < 0 means
                # target is before us), check each perishable item in grp would still arrive
                # before it expires from the earlier delivery date.
                target_day = target_key[1]
                safe_to_merge = True
                if target_day < day:  # merging forward (earlier delivery)
                    earliest_order_shift = day - target_day  # days earlier we'd order
                    for o in grp:
                        ing_sl = ingredients_data.get(o["ingredient_id"], {}).get("shelf_life")
                        if ing_sl is not None:
                            # The lot would arrive earliest_order_shift days sooner and need
                            # to survive until the original need day + cover window.
                            # Reject if shelf_life < earliest_order_shift + 1 day buffer.
                            if ing_sl < earliest_order_shift + 1:
                                safe_to_merge = False
                                break

                if not safe_to_merge:
                    continue

                # Merge: absorb grp into target group, remove this group
                groups[target_key].extend(grp)
                del groups[key]

            # --- Pad groups still below MOV ---
            for key, grp in groups.items():
                sup_id, day = key
                meta = sup_meta.get(sup_id, {})
                mov = meta.get("mov", 0.0)
                if mov <= 0:
                    continue
                val = _goods_value(grp)
                if val >= mov:
                    continue

                # Find best candidate to pad: longest shelf life, not short-shelf perishable
                pad_candidates = []
                for o in grp:
                    ing_sl = ingredients_data.get(o["ingredient_id"], {}).get("shelf_life")
                    unit_price = float(o["unit_price"] or 0.0)
                    if unit_price <= 0:
                        continue
                    # Prefer non-perishable or long-shelf items; reject shelf < 2 days
                    if ing_sl is not None and ing_sl < 2:
                        continue
                    pad_candidates.append((
                        -(ing_sl if ing_sl is not None else 99999),  # sort: longer shelf first
                        o["ingredient_id"],
                        len(pad_candidates),  # stable tiebreaker; avoids comparing dicts
                        o,
                    ))

                if not pad_candidates:
                    continue  # can't pad safely; leave under MOV (will be flagged in plan)

                pad_candidates.sort()
                best_o = pad_candidates[0][3]
                ps = pack_by.get((sup_id, best_o["ingredient_id"]), float(best_o.get("qty") or 1.0))
                if ps <= 0:
                    ps = 1.0
                unit_p = float(best_o["unit_price"] or 0.0)
                if unit_p <= 0:
                    continue

                deficit = mov - val
                extra_packs = math.ceil(deficit / (ps * unit_p))
                best_o["qty"] = float(best_o["qty"]) + extra_packs * ps
                best_o["reason"] = best_o.get("reason", "") + " [qty padded to meet MOV]"

            # Flatten back to new_orders preserving insertion order as best we can
            new_orders = [o for grp in groups.values() for o in grp]

        # ----------------------------------------------------------------
        # Phase 3: Hysteresis + persist
        # ----------------------------------------------------------------
        session = self.db_session_factory()
        total_active = 0
        n_new = 0
        n_kept = 0
        n_superseded = 0
        try:
            existing_rows = (
                session.query(PlannedOrder)
                .filter(PlannedOrder.status.in_(["planned", "at_risk"]))
                .all()
            )
            existing_by_ingredient: Dict[int, List] = {}
            for row in existing_rows:
                existing_by_ingredient.setdefault(row.ingredient_id, []).append(row)

            final_new_orders: List[Dict] = []
            kept_ids: set = set()
            superseded_ids: set = set()
            # Maps kept row_id → the current plan data so we can refresh supplier,
            # qty, status, etc. on rows preserved by hysteresis (A2 supplier fix).
            kept_updates: Dict[int, Dict] = {}

            for order_data in new_orders:
                ing_id = order_data["ingredient_id"]
                new_delivery = order_data["delivery_date"]
                new_qty = order_data["qty"]
                pack_size = ingredients_data[ing_id]["candidate"]["pack_size"]

                matched = None
                for ex in existing_by_ingredient.get(ing_id, []):
                    if ex.id in kept_ids or ex.id in superseded_ids:
                        continue
                    if abs((ex.delivery_date or 0.0) - new_delivery) < SECONDS_PER_DAY:
                        matched = ex
                        break

                if matched is not None:
                    if (
                        abs(new_qty - float(matched.qty or 0.0)) <= pack_size
                        and abs(new_delivery - float(matched.delivery_date or 0.0)) < SECONDS_PER_DAY
                    ):
                        kept_ids.add(matched.id)
                        # A2/A4: Refresh mutable fields on kept rows so a stale row
                        # from an old plan run reflects the current MILP supplier,
                        # recalculated qty, and current timing — not the code from
                        # when the row was first created.
                        kept_updates[matched.id] = {
                            "supplier_id": order_data["supplier_id"],
                            "qty": order_data["qty"],
                            "unit": order_data["unit"],
                            "unit_price": order_data["unit_price"],
                            "order_date": order_data["order_date"],
                            "delivery_date": order_data["delivery_date"],
                            "covers_from": order_data.get("covers_from"),
                            "covers_until": order_data.get("covers_until"),
                            "status": order_data["status"],
                            "reason": order_data["reason"],
                        }
                        continue
                    else:
                        superseded_ids.add(matched.id)

                final_new_orders.append(order_data)

            # Any existing rows not matched → supersede.
            for rows in existing_by_ingredient.values():
                for row in rows:
                    if row.id not in kept_ids and row.id not in superseded_ids:
                        superseded_ids.add(row.id)

            for row_id in superseded_ids:
                row = session.get(PlannedOrder, row_id)
                if row:
                    row.status = "superseded"

            n_new = len(final_new_orders)
            n_kept = len(kept_ids)
            n_superseded = len(superseded_ids)
            total_active = n_new + n_kept

            run = ProcurementPlanRun(
                created_at=now,
                horizon_days=horizon_days,
                items_planned=total_active,
                method="projection",
            )
            session.add(run)
            session.flush()

            # Apply refreshed data to kept rows (supplier, qty, status, etc.)
            for row_id, updates in kept_updates.items():
                row = session.get(PlannedOrder, row_id)
                if row:
                    for field, value in updates.items():
                        setattr(row, field, value)
                    row.plan_run_id = run.id

            for order_data in final_new_orders:
                session.add(PlannedOrder(**order_data, plan_run_id=run.id, created_at=now))

            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("build_procurement_plan: DB write failed")
            return 0
        finally:
            session.close()

        self.broadcast("procurement_plan_updated", {"items_planned": total_active})
        logger.info(
            "Procurement plan: %d active (%d new, %d kept, %d superseded), horizon=%.1f d",
            total_active, n_new, n_kept, n_superseded, horizon_days,
        )
        return total_active

    def execute_due_planned_orders(self) -> int:
        """Convert planned orders whose order_date <= now into real POs.

        Called at the start of each reorder_check sweep.  Returns the count
        of orders executed.

        Groups due orders by (supplier_id, delivery_day) so that all items
        going to the same supplier on the same day form a single PO.  The
        supplier's delivery_charge is added once per PO so total_cost reflects
        true landed cost.
        """
        if self.procurement is None:
            return 0

        now = float(self.bus.sim_time)
        from sqlalchemy import func as _sa_func

        session = self.db_session_factory()
        try:
            due = (
                session.query(PlannedOrder)
                .filter(
                    PlannedOrder.status.in_(["planned", "at_risk"]),
                    PlannedOrder.order_date <= now,
                )
                .all()
            )
            # Snapshot all data we need before closing the session.
            due_data = [
                {
                    "id": po.id,
                    "ingredient_id": po.ingredient_id,
                    "supplier_id": po.supplier_id,
                    "qty": po.qty,
                    "unit": po.unit,
                    "unit_price": po.unit_price,
                    "delivery_date": po.delivery_date,  # A4: carry through for _place
                }
                for po in due
            ]
            # Load delivery charges for all relevant suppliers in one query.
            sup_ids = {d["supplier_id"] for d in due_data if d["supplier_id"]}
            delivery_charges: Dict[int, float] = {}
            if sup_ids:
                for sup in session.query(Supplier).filter(Supplier.id.in_(sup_ids)).all():
                    delivery_charges[sup.id] = float(sup.delivery_charge or 0.0)
        finally:
            session.close()

        # Group by (supplier_id, delivery_day) to batch into one PO per supplier-day.
        from collections import defaultdict as _dd
        groups: Dict[tuple, List[Dict]] = _dd(list)
        for po_data in due_data:
            if po_data["supplier_id"] is None:
                continue
            delivery_day = int(float(po_data["delivery_date"] or 0.0) // SECONDS_PER_DAY)
            groups[(po_data["supplier_id"], delivery_day)].append(po_data)

        count = 0
        for (supplier_id, _delivery_day), group in groups.items():
            # A5: For each ingredient, compute in-flight qty and only place the shortfall.
            lines = []
            placed_ids = []
            for po_data in group:
                ing_id = po_data["ingredient_id"]
                session2 = self.db_session_factory()
                try:
                    inbound_qty_row = (
                        session2.query(_sa_func.sum(PurchaseOrderLine.qty))
                        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                        .filter(
                            PurchaseOrderLine.ingredient_id == ing_id,
                            PurchaseOrder.status.in_(("proposed", "approved", "placed")),
                        )
                        .scalar()
                    )
                finally:
                    session2.close()
                inbound_qty = float(inbound_qty_row or 0.0)
                shortfall = float(po_data["qty"] or 0.0) - inbound_qty
                if shortfall <= 0:
                    continue

                # Fall back to catalog for unit / price if the PlannedOrder row lacks them.
                unit = po_data["unit"]
                unit_price = po_data["unit_price"]
                if unit is None or unit_price is None:
                    session3 = self.db_session_factory()
                    try:
                        cat = (
                            session3.query(SupplierCatalog)
                            .filter(
                                SupplierCatalog.supplier_id == supplier_id,
                                SupplierCatalog.ingredient_id == ing_id,
                            )
                            .first()
                        )
                        if cat is not None:
                            unit = unit or cat.unit
                            unit_price = unit_price or float(cat.current_price or 0.0)
                    finally:
                        session3.close()

                if unit is None:
                    unit = "g"
                if unit_price is None:
                    unit_price = 0.0

                lines.append({
                    "ingredient_id": ing_id,
                    "qty": shortfall,  # A5: only order the net shortfall
                    "unit": unit,
                    "unit_price": float(unit_price),
                })
                placed_ids.append(po_data["id"])

            if not lines:
                continue

            # Use the earliest delivery_date in the group as the planned delivery.
            planned_delivery = min(
                float(d["delivery_date"] or 0.0) for d in group
            )
            dc = delivery_charges.get(supplier_id, 0.0)

            try:
                self.procurement.create_po(
                    supplier_id=supplier_id,
                    lines=lines,
                    created_by=self.name,
                    planned_delivery=planned_delivery,  # A4: honour plan date
                    delivery_charge=dc,                 # include delivery fee in total_cost
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "execute_due_planned_orders: create_po failed for supplier %s", supplier_id
                )
                continue

            # Mark all PlannedOrder rows in the group as placed.
            for row_id in placed_ids:
                session4 = self.db_session_factory()
                try:
                    row = session4.get(PlannedOrder, row_id)
                    if row:
                        row.status = "placed"
                        session4.commit()
                except Exception:  # noqa: BLE001
                    session4.rollback()
                finally:
                    session4.close()

            count += 1  # one PO created per supplier-day group

        return count

    def _maybe_reorder(self, ingredient_id: int) -> None:
        if self.procurement is None:
            return
        session = self.db_session_factory()
        try:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            if level is None or level.reorder_point is None or level.reorder_point <= 0:
                return
            on_hand = float(level.on_hand_cached or 0.0)
            if on_hand > float(level.reorder_point):
                return
            par_level = float(level.par_level or 0.0)
            safety_stock = float(level.safety_stock or 0.0)

            # A5: Sum in-transit qty so we only order the shortfall, not the full
            # par/forecast target when part of it is already inbound.
            from sqlalchemy import func as _sa_func
            inbound_qty_row = (
                session.query(_sa_func.sum(PurchaseOrderLine.qty))
                .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                .filter(
                    PurchaseOrderLine.ingredient_id == ingredient_id,
                    PurchaseOrder.status.in_(("proposed", "approved", "placed")),
                )
                .scalar()
            )
            inbound_qty = float(inbound_qty_row or 0.0)

            catalog = (
                session.query(SupplierCatalog)
                .filter(SupplierCatalog.ingredient_id == ingredient_id)
                .all()
            )
            specs = [
                {
                    "supplier_id": c.supplier_id,
                    "current_price": c.current_price,
                    "pack_size": c.pack_size or 1.0,
                    "availability": c.availability,
                    "unit": c.unit,
                    "is_default": int(getattr(c, "is_default", 0) or 0),
                }
                for c in catalog
            ]
            lead_by_supplier = {
                s.id: float(s.lead_time_days or 1.0)
                for s in session.query(Supplier)
                .filter(Supplier.id.in_([c["supplier_id"] for c in specs]))
                .all()
            } if specs else {}

            # Load ingredient metadata for perishability check
            ing = session.get(Ingredient, ingredient_id)
        finally:
            session.close()

        # A2: Use _select_supplier (MILP default → heuristic fallback) — same as
        # build_procurement_plan so both paths always agree on who to buy from.
        candidate = self._select_supplier(specs, lead_by_supplier)
        if candidate is None:
            self.log_event(
                "reorder_failed",
                f"No available supplier for ingredient {ingredient_id}; reorder skipped.",
                {"ingredient_id": ingredient_id},
            )
            return

        lead_days = float(lead_by_supplier.get(candidate["supplier_id"], 1.0))

        # --- Forecast-aware sizing ---
        # Floor: cover demand over lead time + safety stock (avoid lost sales).
        # Degrades to par top-up when no horizon is live (identical to today's behavior).
        demand_lead = self._demand_over_lead(ingredient_id, lead_days)
        forecast_target = demand_lead + safety_stock - on_hand
        needed = max(par_level - on_hand, forecast_target)

        # Ceiling: for perishables, never order more than can be sold before expiry
        # (avoids wastage).  Skip ceiling when no horizon is live (demand_before_expiry→0
        # → ceiling doesn't bind because forecast_target also→0).
        if ing is not None and ing.perishable and ing.shelf_life_days:
            shelf_life = float(ing.shelf_life_days)
            expiry_demand = self._demand_before_expiry(ingredient_id, shelf_life)
            if expiry_demand > 0:  # only cap when we have forecast data
                demand_ceiling = max(0.0, expiry_demand - on_hand)
                # Cap at ceiling but never below the par floor
                if demand_ceiling < needed:
                    needed = max(par_level - on_hand, demand_ceiling)

        # A5: Subtract inbound supply — only order the net shortfall.
        needed = max(0.0, needed - inbound_qty)
        if needed <= 0:
            return
        pack_size = candidate["pack_size"] or 1.0
        qty = math.ceil(needed / pack_size) * pack_size

        self.procurement.create_po(
            supplier_id=candidate["supplier_id"],
            lines=[
                {
                    "ingredient_id": ingredient_id,
                    "qty": qty,
                    "unit": candidate["unit"],
                    "unit_price": candidate["current_price"],
                }
            ],
            created_by=self.name,
        )

    # -- recipe / yield helpers (mirrors ledger pattern; core.models only, no track_a import) -----

    def _yield_factor(self, session: Any, ingredient_id: int) -> float:
        """Return the yield factor for an ingredient (waste/prep loss adjustment)."""
        level = (
            session.query(InventoryLevel)
            .filter(InventoryLevel.ingredient_id == ingredient_id)
            .first()
        )
        if level is None or not level.yield_factor:
            return 1.0
        return float(level.yield_factor)

    def _ingredient_qty_for_menu_item(
        self,
        session: Any,
        menu_item_id: int,
        ingredient_id: int,
    ) -> float:
        """How much of ingredient_id is needed per 1 portion of menu_item_id.

        Copied verbatim from ledger._ingredient_qty_for_menu_item (core.models only).
        """
        rows = (
            session.query(RecipeLine.qty)
            .join(Recipe, Recipe.id == RecipeLine.recipe_id)
            .filter(
                Recipe.menu_item_id == menu_item_id,
                RecipeLine.ingredient_id == ingredient_id,
                RecipeLine.optional == 0,
            )
            .all()
        )
        if not rows:
            return 0.0
        return float(sum((row[0] or 0.0) for row in rows)) / max(
            self._yield_factor(session, ingredient_id), 0.0001
        )

    def _demand_over_lead(
        self,
        ingredient_id: int,
        lead_days: float,
    ) -> float:
        """Total ingredient demand over the next (lead_days + reorder_interval) days.

        Reads the latest DEMAND_FORECAST_HORIZON signal from the bus pull-style.
        Returns 0.0 when no horizon is available (formulae degrade to today's behavior).
        """
        horizon_signals = self.bus.live(type=SignalType.DEMAND_FORECAST_HORIZON)
        if not horizon_signals:
            return 0.0

        sig = horizon_signals[0]
        payload = sig.payload or {}
        days = payload.get("days") or []
        coverage_days = math.ceil(lead_days + config.REORDER_INTERVAL_DAYS)

        total_usage = 0.0
        session = self.db_session_factory()
        try:
            for day in days:
                day_idx = day.get("day_index", 999)
                if day_idx >= coverage_days:
                    break
                for item_entry in day.get("items") or []:
                    menu_item_id = item_entry.get("menu_item_id")
                    qty = float(item_entry.get("qty") or 0.0)
                    if qty <= 0 or menu_item_id is None:
                        continue
                    ing_qty = self._ingredient_qty_for_menu_item(session, int(menu_item_id), ingredient_id)
                    total_usage += qty * ing_qty
        finally:
            session.close()
        return total_usage

    def _demand_before_expiry(
        self,
        ingredient_id: int,
        shelf_life_days: float,
    ) -> float:
        """Total ingredient demand over the next shelf_life_days.

        Used as a perishability ceiling: never order more than can be sold.
        Returns 0.0 when no horizon is available.
        """
        horizon_signals = self.bus.live(type=SignalType.DEMAND_FORECAST_HORIZON)
        if not horizon_signals:
            return 0.0

        sig = horizon_signals[0]
        payload = sig.payload or {}
        days = payload.get("days") or []
        cap_days = math.ceil(shelf_life_days)

        total_usage = 0.0
        session = self.db_session_factory()
        try:
            for day in days:
                day_idx = day.get("day_index", 999)
                if day_idx >= cap_days:
                    break
                for item_entry in day.get("items") or []:
                    menu_item_id = item_entry.get("menu_item_id")
                    qty = float(item_entry.get("qty") or 0.0)
                    if qty <= 0 or menu_item_id is None:
                        continue
                    ing_qty = self._ingredient_qty_for_menu_item(session, int(menu_item_id), ingredient_id)
                    total_usage += qty * ing_qty
        finally:
            session.close()
        return total_usage

    def _net_demand_for_sourcing(
        self,
        ingredient_id: int,
        lead_days: float,
        horizon_days: float,
    ) -> float:
        """Net quantity to order for an ingredient so that at the end of the planning
        horizon there is exactly enough stock to cover demand.

        Formula:
          gross_need   = forecast consumption over [now, now + lead + horizon]
          usable_stock = portion of on_hand that can actually be consumed
                         before it expires (perishables capped by shelf life;
                         non-perishables use full on_hand)
          net          = max(0, gross_need − usable_stock)

        This guarantees "for the forecasted interval exactly that much is in
        inventory" (user requirement).  Units: grams (same as base_unit).
        """
        # --- gross need: consumption from now until end of coverage window ---
        total_window = lead_days + horizon_days
        gross_need = self._demand_over_lead(ingredient_id, total_window)
        if gross_need <= 0:
            return 0.0

        # --- usable stock ---
        session = self.db_session_factory()
        try:
            # On-hand stock
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            on_hand = float(level.on_hand_cached or 0.0) if level else 0.0

            # Is this ingredient perishable?
            ing = session.get(Ingredient, ingredient_id)
            perishable = bool(ing.perishable) if ing else False
            shelf_life = float(ing.shelf_life_days or 0.0) if ing else 0.0

            if not perishable or shelf_life <= 0:
                # Non-perishable: full on_hand is usable within the window
                usable_stock = on_hand
            else:
                # Perishable: only use stock that won't expire before it can be consumed
                # Determine how much of on_hand expires within the planning window
                # by querying actual lots (most accurate)
                now_sim = float(self.bus.sim_time)
                lots = (
                    session.query(InventoryLot)
                    .filter(
                        InventoryLot.ingredient_id == ingredient_id,
                        InventoryLot.qty_on_hand > 0,
                    )
                    .all()
                )
                if lots:
                    # Sum lot qty that hasn't expired and can be consumed in time
                    # "consumed in time" = expiry is within the total window from now
                    window_end = now_sim + total_window * 86400  # sim-seconds
                    usable_from_lots = 0.0
                    for lot in lots:
                        expiry = lot.expiry_date
                        if expiry is None:
                            # No expiry recorded — treat conservatively as usable
                            usable_from_lots += float(lot.qty_on_hand or 0.0)
                        elif expiry > now_sim:
                            # Lot hasn't expired; cap consumption by remaining shelf life
                            remaining_days = max(0.0, (expiry - now_sim) / 86400)
                            # fraction of gross_need attributable to these remaining days
                            daily_rate = gross_need / max(total_window, 1.0)
                            consumable_from_lot = min(
                                float(lot.qty_on_hand or 0.0),
                                daily_rate * remaining_days,
                            )
                            usable_from_lots += consumable_from_lot
                    usable_stock = min(on_hand, usable_from_lots)
                else:
                    # No lot data — fall back: only stock consumable before shelf_life expires
                    daily_rate = gross_need / max(total_window, 1.0)
                    usable_stock = min(on_hand, daily_rate * shelf_life)
        finally:
            session.close()

        return max(0.0, gross_need - usable_stock)

    # -- dynamic par recompute -----------------------------------------------

    def refresh_dynamic_pars(self) -> None:
        """Recompute par_level/reorder_point/safety_stock from the weekly horizon.

        Uses the robust daily baseline median (transient-free) so one-off event
        spikes don't contaminate the «normal week» par level.  Durable changes
        (e.g. competitor keeps prices elevated) are adopted after a few days as
        the rolling median shifts.

        Safe: only writes when a horizon is available and robust_usage > 0.
        Never zeroes out an existing par (guarded by `if robust_usage > 0`).
        """
        horizon_signals = self.bus.live(type=SignalType.DEMAND_FORECAST_HORIZON)
        if not horizon_signals:
            return  # no horizon yet; skip silently

        payload = horizon_signals[0].payload or {}
        item_baseline_median: Dict[str, float] = payload.get("item_daily_baseline_median") or {}
        if not item_baseline_median:
            return

        session = self.db_session_factory()
        try:
            levels = session.query(InventoryLevel).all()
            for level in levels:
                ingredient_id = int(level.ingredient_id)
                # Sum across all menu items: robust_daily_ingredient_usage
                robust_usage = 0.0
                for item_id_str, daily_baseline in item_baseline_median.items():
                    if float(daily_baseline) <= 0:
                        continue
                    try:
                        menu_item_id = int(item_id_str)
                    except (ValueError, TypeError):
                        continue
                    ing_qty = self._ingredient_qty_for_menu_item(session, menu_item_id, ingredient_id)
                    robust_usage += float(daily_baseline) * ing_qty

                if robust_usage <= 0:
                    continue  # no demand data for this ingredient; preserve existing pars

                ing = session.get(Ingredient, ingredient_id)
                lead_days = 1.0
                catalog = (
                    session.query(SupplierCatalog)
                    .filter(SupplierCatalog.ingredient_id == ingredient_id)
                    .all()
                )
                if catalog:
                    supplier_ids = [c.supplier_id for c in catalog]
                    suppliers = (
                        session.query(Supplier)
                        .filter(Supplier.id.in_(supplier_ids))
                        .all()
                    )
                    if suppliers:
                        lead_days = min(float(s.lead_time_days or 1.0) for s in suppliers)

                safety_stock = config.SAFETY_FRACTION * robust_usage * lead_days
                reorder_point = robust_usage * lead_days + safety_stock
                par_level = robust_usage * (lead_days + config.REORDER_INTERVAL_DAYS + config.SAFETY_DAYS)

                # Perishability cap on par
                if ing is not None and ing.perishable and ing.shelf_life_days:
                    shelf_cap = robust_usage * float(ing.shelf_life_days)
                    par_level = min(par_level, shelf_cap)

                # A6: EMA update — pars can now decrease as demand subsides instead
                # of permanently ratcheting to historical peaks.  α=0.3 adapts over
                # roughly 3 horizon readings while damping single-event spikes.
                _alpha = 0.3
                level.safety_stock = round(
                    max(0.0, float(level.safety_stock or 0.0) * (1 - _alpha) + safety_stock * _alpha), 4
                )
                level.reorder_point = round(
                    max(0.0, float(level.reorder_point or 0.0) * (1 - _alpha) + reorder_point * 0.5 * _alpha), 4
                )
                level.par_level = round(
                    max(0.0, float(level.par_level or 0.0) * (1 - _alpha) + par_level * 0.5 * _alpha), 4
                )

            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
        finally:
            session.close()

        self.log_event(
            "optimizer",
            "Dynamic pars refreshed from 7-day horizon (robust baseline).",
            {"signal_age": float((self.bus.live(type=SignalType.DEMAND_FORECAST_HORIZON) or [{}])[0].payload.get("generated_at", 0) if horizon_signals else 0)},
        )

    def _select_supplier(
        self,
        specs: List[Dict[str, Any]],
        lead_by_supplier: Dict[int, float],
    ) -> Optional[Dict[str, Any]]:
        """Single supplier-selection entry point used by **all** ordering paths.

        Prefers the MILP-chosen default supplier (``is_default == 1`` on
        ``SupplierCatalog``, written by ``run_sourcing_plan``); falls back to the
        heuristic scorer only when no default is available or in stock.  Using this
        consistently across ``build_procurement_plan`` and ``_maybe_reorder`` ensures
        the forward plan and the reactive reorder path always agree on who to buy from
        and with which lead time (§18.8, A2).
        """
        default_specs = [
            s for s in specs
            if s.get("is_default") == 1 and s.get("availability") != "out"
        ]
        if default_specs:
            return default_specs[0]
        return self._choose_supplier(specs, lead_by_supplier)

    @staticmethod
    def _choose_supplier(
        specs: List[Dict[str, Any]], lead_by_supplier: Dict[int, float]
    ) -> Optional[Dict[str, Any]]:
        """``score = availability_weight − price_norm − lead_norm`` (§18.8)."""
        usable = [s for s in specs if s["availability"] != "out"]
        if not usable:
            return None
        avail_weight = {"in_stock": 1.0, "limited": 0.5}
        max_price = max((s["current_price"] or 0.0) for s in usable) or 1.0
        max_lead = max((lead_by_supplier.get(s["supplier_id"], 1.0)) for s in usable) or 1.0

        best = None
        best_score = float("-inf")
        for s in usable:
            price_norm = (s["current_price"] or 0.0) / max_price
            lead_norm = lead_by_supplier.get(s["supplier_id"], 1.0) / max_lead
            score = avail_weight.get(s["availability"], 0.5) - price_norm - lead_norm
            if score > best_score:
                best_score = score
                best = s
        return best

    # -- menu toggle (§18.8) -------------------------------------------------

    def _maybe_toggle(self, ingredient_id: int, projected_runout: float) -> None:
        # Guard: if this is the only active dish using the ingredient, skip disabling —
        # leaving the restaurant with zero dishes to serve is worse than serving with
        # low stock.  This guard is optimizer-specific (projected shortages); voice
        # confirms (zero-stock commands) bypass this path entirely.
        session = self.db_session_factory()
        try:
            from core.models import MenuItem, Recipe, RecipeLine
            active_count = (
                session.query(MenuItem.id)
                .join(Recipe, Recipe.menu_item_id == MenuItem.id)
                .join(RecipeLine, RecipeLine.recipe_id == Recipe.id)
                .filter(RecipeLine.ingredient_id == ingredient_id, MenuItem.active == 1)
                .count()
            )
        finally:
            session.close()
        if active_count <= 1:
            return  # skip: would leave no active dishes for this ingredient

        # Delegate to the deterministic resolver, which disables ALL dishes using this
        # ingredient at/below threshold, not just the lowest-value one.
        try:
            from core.availability import recompute_availability
            recompute_availability(
                self.db_session_factory,
                self.bus,
                self.broadcast,
                changed_ingredient_ids=[ingredient_id],
                agent_name="optimizer",
            )
        except Exception as exc:
            logger.warning("optimizer _maybe_toggle cascade failed: %s", exc)

    def _margin_x_velocity(self, menu_item_id: int, price: float, ingredient_id: int) -> float:
        session = self.db_session_factory()
        try:
            recipe_cost = 0.0
            recipe = (
                session.query(Recipe).filter(Recipe.menu_item_id == menu_item_id).first()
            )
            if recipe is not None:
                lines = (
                    session.query(RecipeLine).filter(RecipeLine.recipe_id == recipe.id).all()
                )
                for rl in lines:
                    catalog = (
                        session.query(SupplierCatalog)
                        .filter(SupplierCatalog.ingredient_id == rl.ingredient_id)
                        .first()
                    )
                    unit_price = catalog.current_price if catalog is not None else 0.0
                    recipe_cost += float(rl.qty or 0.0) * float(unit_price or 0.0)

            velocity = (
                session.query(OrderLine)
                .filter(OrderLine.menu_item_id == menu_item_id, OrderLine.status == "sold")
                .count()
            )
        finally:
            session.close()

        margin = float(price or 0.0) - recipe_cost
        return margin * float(velocity)

    def _disable(self, menu_item_id: int, ingredient_id: int) -> None:
        """Disable a menu item by delegating to the deterministic resolver.

        Previously this wrote NULL-coded MenuToggle rows directly, bypassing the
        resolver and creating permanent locks.  Now it delegates to ``_maybe_toggle``
        (which calls ``recompute_availability``) so the block is idempotent and
        tracked with the correct ``out_of_stock`` reason_code.
        """
        self._toggle_cause[menu_item_id] = ingredient_id
        self._maybe_toggle(ingredient_id, projected_runout=float(self.sim_time))

    def _reenable(self, menu_item_id: int, ingredient_id: Optional[int] = None) -> None:
        # Delegate to the deterministic resolver; it auto-re-enables dishes when
        # on_hand > threshold and no other block remains.
        if ingredient_id is None:
            ingredient_id = self._toggle_cause.get(menu_item_id)
        if ingredient_id is not None:
            try:
                from core.availability import recompute_availability
                recompute_availability(
                    self.db_session_factory,
                    self.bus,
                    self.broadcast,
                    changed_ingredient_ids=[ingredient_id],
                    agent_name="optimizer",
                )
            except Exception as exc:
                logger.warning("optimizer _reenable cascade failed: %s", exc)
        self._toggle_cause.pop(menu_item_id, None)

    def _manual_toggle(self, menu_item_id: int, action: str, reason: str) -> None:
        now = self.sim_time
        action = "enable" if action == "enable" else "disable"
        session = self.db_session_factory()
        try:
            item = session.get(MenuItem, menu_item_id)
            if item is None:
                return
            desired_active = 1 if action == "enable" else 0
            if item.active == desired_active:
                return
            item.active = desired_active
            if action == "enable":
                (
                    session.query(MenuToggle)
                    .filter(MenuToggle.menu_item_id == menu_item_id, MenuToggle.active == 1)
                    .update({MenuToggle.active: 0})
                )
            session.add(
                MenuToggle(
                    menu_item_id=menu_item_id,
                    action=action,
                    reason=reason,
                    triggered_by=self.name,
                    sim_time=now,
                    active=1,
                )
            )
            session.commit()
        finally:
            session.close()

        self.emit(
            SignalType.MENU_TOGGLE,
            {"menu_item_id": menu_item_id, "action": action, "reason": reason},
            dedup_key=f"toggle:{menu_item_id}",
        )
        self.broadcast("menu_toggled", {"menu_item_id": menu_item_id, "action": action})
        self.log_event(
            "menu_toggle",
            f"{'Enabled' if action == 'enable' else 'Disabled'} menu item {menu_item_id}: {reason}.",
            {"menu_item_id": menu_item_id, "action": action, "reason": reason},
        )

    def _resolve_ingredient_id(self, ref: Any) -> Optional[int]:
        if not ref:
            return None
        session = self.db_session_factory()
        try:
            row = session.query(Ingredient).filter(Ingredient.name.ilike(str(ref))).first()
            if row is None:
                row = session.query(Ingredient).filter(Ingredient.name.ilike(f"{ref}%")).first()
            return int(row.id) if row is not None else None
        finally:
            session.close()

    def _resolve_menu_item_id(self, ref: Any) -> Optional[int]:
        if not ref:
            return None
        session = self.db_session_factory()
        try:
            row = session.query(MenuItem).filter(MenuItem.name.ilike(str(ref))).first()
            if row is None:
                row = session.query(MenuItem).filter(MenuItem.name.ilike(f"{ref}%")).first()
            return int(row.id) if row is not None else None
        finally:
            session.close()

    # -- expiry → promo (§18.8) ----------------------------------------------

    def _propose_promo(self, payload: Dict[str, Any]) -> None:
        ingredient_id = payload.get("ingredient_id")
        lot_id = payload.get("lot_id")
        if ingredient_id is None:
            return
        now = self.sim_time
        session = self.db_session_factory()
        try:
            ing = session.get(Ingredient, ingredient_id)
            ing_name = ing.name if ing is not None else str(ingredient_id)
            items = (
                session.query(MenuItem.id)
                .join(Recipe, Recipe.menu_item_id == MenuItem.id)
                .join(RecipeLine, RecipeLine.recipe_id == Recipe.id)
                .filter(RecipeLine.ingredient_id == ingredient_id, MenuItem.active == 1)
                .limit(3)
                .all()
            )
            menu_items = [r[0] for r in items]
            if not menu_items:
                return

            promo_type = "combo" if len(menu_items) > 1 else "discount"
            promo = Promotion(
                type=promo_type,
                menu_items=menu_items,
                trigger="expiry",
                discount_pct=float(config.PROMO_DISCOUNT_PCT),
                channel="both",
                status="proposed",
                approval_id=None,
                sim_time=now,
            )
            session.add(promo)
            session.commit()
            session.refresh(promo)
            promo_id = promo.id
        finally:
            session.close()

        if self.approvals is not None:
            approval = self.approvals.create(
                type="promo",
                title=f"Promo: near-expiry {ing_name}",
                summary=f"Discount {config.PROMO_DISCOUNT_PCT}% on menu items using {ing_name} (lot {lot_id} near expiry).",
                payload={"promo_id": promo_id, "ingredient_id": ingredient_id, "lot_id": lot_id},
                ref_id=promo_id,
            )
            session = self.db_session_factory()
            try:
                promo = session.get(Promotion, promo_id)
                if promo is not None:
                    promo.approval_id = approval.id
                    session.commit()
            finally:
                session.close()

        self.emit(
            SignalType.PROMO_PROPOSAL,
            {
                "promo_id": promo_id,
                "type": promo_type,
                "menu_items": menu_items,
                "discount_pct": float(config.PROMO_DISCOUNT_PCT),
                "channel": "both",
                "trigger": "expiry",
            },
            dedup_key=f"promo:{lot_id}",
        )
        self.log_event(
            "promo_proposal",
            f"Proposed {promo_type} promo for near-expiry {ing_name} ({config.PROMO_DISCOUNT_PCT}% off).",
            {"promo_id": promo_id, "ingredient_id": ingredient_id, "menu_items": menu_items},
        )

    # -- approval callbacks (called by the approval handlers) --------------

    def activate_promo(self, promo_id: int) -> None:
        """Mark an approved promotion ``active`` (§B4.5)."""
        session = self.db_session_factory()
        try:
            promo = session.get(Promotion, promo_id)
            if promo is None:
                return
            promo.status = "active"
            session.commit()
        finally:
            session.close()

        self.broadcast("promo_activated", {"promo_id": promo_id})
        self.log_event(
            "promo_activated", f"Promotion {promo_id} activated.", {"promo_id": promo_id}
        )

    # -- LLM optimization pass (Stream E) ------------------------------------

    def llm_optimize(self) -> None:
        """Run the LLM reasoning pass to produce augmented inventory decisions.

        Builds a rich context (inventory levels, near-expiry lots, shared-ingredient
        dish graph, demand forecasts, supplier catalog, memory) and asks the LLM
        for a structured action list.  Actions are mapped onto the guarded
        deterministic executors so all safety rails remain in effect.

        Falls back gracefully: if no LLM key is present or the response is canned,
        the call is a no-op (the deterministic path still runs on its own cadence).
        """
        if self.llm is None:
            return
        try:
            context = self._build_llm_context()
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are Roba's inventory optimizer AI. Based on the provided restaurant "
                        "inventory state, demand forecasts, supplier data, and optimization memory, "
                        "produce a list of inventory and menu actions.\n\n"
                        "AVAILABLE ACTIONS:\n"
                        "- toggle_item: disable/enable a menu item (menu_item_id, toggle_direction)\n"
                        "- create_deal: propose a discount promo (ingredient_id, discount_pct)\n"
                        "- reorder: trigger an immediate reorder (ingredient_id)\n"
                        "- defer_reorder: defer a pending reorder (ingredient_id)\n"
                        "- request_negotiation: request a supplier negotiation call "
                        "(supplier_id, ingredient_id). ONLY use when ALL criteria are met:\n"
                        "  (a) price has risen SUSTAINABLY (≥3 consecutive data points or "
                        "≥15% above historical median), AND\n"
                        "  (b) there is strong evidence of potential savings OR a cheaper "
                        "alternate supplier whose savings over the week exceed the switching "
                        "cost, AND\n"
                        "  (c) no negotiation with this supplier+ingredient pair is already in "
                        "progress or was completed recently (check optimizer_memory for "
                        "negotiation_cooldown entries), AND\n"
                        "  (d) total potential savings over the forecast horizon are meaningful "
                        "(not just cents).\n\n"
                        "FEW-SHOT EXAMPLES for request_negotiation:\n\n"
                        "EXAMPLE 1 — DO request negotiation:\n"
                        "Context: tomato price has risen from $0.003 to $0.0045/g over 3 weeks "
                        "(50% above median). We order 80g/day = ~560g/week. Potential weekly "
                        "savings if we get back to $0.003: ($0.0045-$0.003)*560=$0.84/week = "
                        "$3.36/month. Switching cost $5. No recent negotiation in memory.\n"
                        "→ ACTION: request_negotiation supplier_id=1 ingredient_id=3, "
                        "confidence=0.78. Reason: sustained 3-week price rise 50% above median; "
                        "meaningful monthly savings justify a call.\n\n"
                        "EXAMPLE 2 — DO NOT request negotiation (single spike):\n"
                        "Context: mozzarella spiked last Wednesday (weather event per notes), "
                        "price back near median the same week. Only 1 elevated price point.\n"
                        "→ NO action. Reason: single transient spike with no sustained trend.\n\n"
                        "EXAMPLE 3 — DO NOT request negotiation (savings too small):\n"
                        "Context: olive oil from SupplierA costs $0.0052/g, SupplierB offers "
                        "$0.0051/g. Weekly demand 20g. Potential savings: $0.02/week. "
                        "Switching cost $5.\n"
                        "→ NO action. Reason: switching cost ($5) vastly exceeds weekly savings "
                        "($0.02); a call would cost more in management time than it saves.\n\n"
                        "EXAMPLE 4 — DO request negotiation (alternate supplier much cheaper):\n"
                        "Context: cream from CurrentCo $0.008/g, AlternateDairy $0.0055/g. "
                        "Weekly demand 300g. Savings: ($0.008-$0.0055)*300=$0.75/week. "
                        "Over 4 weeks = $3.00 < switching cost $5 — marginal. But if the "
                        "optimizer_memory shows 3 late deliveries from CurrentCo in past 30 days "
                        "causing stockouts, the reliability cost also justifies switching.\n"
                        "→ ACTION: request_negotiation, confidence=0.72. Reason: sustained price "
                        "gap plus late-delivery pattern makes incumbent unreliable.\n\n"
                        "EXAMPLE 5 — DO NOT request negotiation (cooldown):\n"
                        "Context: optimizer_memory shows negotiation_cooldown entry for "
                        "supplier_id=2,ingredient_id=5 created 8 sim-days ago. "
                        "NEGOTIATION_COOLDOWN is ~7 sim-days.\n"
                        "→ NO action. Reason: negotiation already requested recently; cooldown "
                        "not yet expired.\n\n"
                        "EXAMPLE 6 — DO NOT request negotiation (no better alternative):\n"
                        "Context: basil from SingleSupplier is the only supplier in catalog. "
                        "Price is 10% above historical median but no alternate exists.\n"
                        "→ NO action. Reason: no competitive leverage; a negotiation call "
                        "without an alternative weakens our position. Flag for sourcing review.\n\n"
                        "Reason carefully about shared ingredients across dishes — disabling "
                        "a lower-margin dish can preserve a scarce ingredient for a higher-margin "
                        "dish. Propose deals (discounts) for items near waste/expiry. "
                        "Respond with JSON matching the schema: {actions: [{action, "
                        "menu_item_id?, ingredient_id?, supplier_id?, toggle_direction?, "
                        "discount_pct?, reason, confidence}], summary}."
                    ),
                },
                {"role": "user", "content": f"Inventory context:\n{context}"},
            ]
            result = self.llm.complete(
                messages,
                json_schema=_OPTIMIZE_SCHEMA,
                use_site="optimizer_optimization",
                temperature=0.1,
            )
            if not isinstance(result, dict) or result.get("note") == CANNED_NOTE:
                return
            self._apply_llm_actions(result.get("actions") or [], result.get("summary", ""))
        except Exception:  # noqa: BLE001
            logger.exception("Optimizer LLM pass failed; falling back to deterministic path")

    def _build_llm_context(self) -> str:
        """Build a compact JSON context for the LLM optimizer."""
        now = self.sim_time
        session = self.db_session_factory()
        try:
            # Inventory levels + near-expiry lots.
            levels = session.query(InventoryLevel).all()
            inventory = []
            for lv in levels:
                ing = session.get(Ingredient, lv.ingredient_id)
                if ing is None:
                    continue
                near_expiry_lots = (
                    session.query(InventoryLot)
                    .filter(
                        InventoryLot.ingredient_id == lv.ingredient_id,
                        InventoryLot.status == "active",
                        InventoryLot.expiry_date.isnot(None),
                        InventoryLot.expiry_date <= now + 172800.0,  # 2 sim-days
                    )
                    .all()
                )
                inventory.append({
                    "ingredient_id": int(lv.ingredient_id),
                    "name": ing.name,
                    "on_hand": float(lv.on_hand_cached or 0.0),
                    "par_level": float(lv.par_level or 0.0),
                    "reorder_point": float(lv.reorder_point or 0.0),
                    "near_expiry_qty": sum(float(lot.qty_on_hand or 0.0) for lot in near_expiry_lots),
                    "near_expiry_lots": len(near_expiry_lots),
                })

            # Menu items with margin × velocity and shared ingredient info.
            items = session.query(MenuItem).filter(MenuItem.active == 1).all()
            menu = []
            for item in items:
                score = self._margin_x_velocity(item.id, float(item.dine_in_price or 0.0), 0)
                recipe = session.query(Recipe).filter(Recipe.menu_item_id == item.id).first()
                ingredients = []
                if recipe:
                    for rl in session.query(RecipeLine).filter(RecipeLine.recipe_id == recipe.id).all():
                        ing = session.get(Ingredient, rl.ingredient_id)
                        ingredients.append({
                            "ingredient_id": int(rl.ingredient_id),
                            "name": ing.name if ing else str(rl.ingredient_id),
                            "qty": float(rl.qty or 0.0),
                        })
                menu.append({
                    "menu_item_id": int(item.id),
                    "name": item.name,
                    "margin_x_velocity": round(score, 2),
                    "price": float(item.dine_in_price or 0.0),
                    "ingredients": ingredients,
                })

            # Supplier catalog summary.
            suppliers = []
            for cat in session.query(SupplierCatalog).all():
                sup = session.get(Supplier, cat.supplier_id)
                suppliers.append({
                    "ingredient_id": int(cat.ingredient_id),
                    "supplier": sup.name if sup else str(cat.supplier_id),
                    "price": float(cat.current_price or 0.0),
                    "availability": cat.availability,
                    "lead_days": float(sup.lead_time_days or 2.0) if sup else 2.0,
                })

            # Memory from past optimizer decisions.
            memory = [
                {
                    "scope_type": m.scope_type,
                    "scope_ref": m.scope_ref,
                    "insight": m.insight,
                    "confidence": m.confidence,
                }
                for m in session.query(InventoryOptimizerMemory)
                .filter(
                    InventoryOptimizerMemory.valid_until.is_(None)
                    | (InventoryOptimizerMemory.valid_until > now)
                )
                .order_by(InventoryOptimizerMemory.last_seen_at.desc())
                .limit(20)
                .all()
            ]
        finally:
            session.close()

        # Live demand forecasts from the bus.
        demand = [
            {
                "menu_item_id": s.payload.get("menu_item_id"),
                "qty": s.payload.get("qty"),
                "daypart": s.payload.get("daypart"),
                "confidence": s.payload.get("confidence"),
            }
            for s in self.bus.live(type=SignalType.DEMAND_FORECAST)[:20]
        ]

        return json.dumps({
            "sim_time": now,
            "inventory": inventory,
            "menu": menu,
            "suppliers": suppliers,
            "demand_forecasts": demand,
            "optimizer_memory": memory,
        }, separators=(",", ":"))

    def _apply_llm_actions(self, actions: List[Dict[str, Any]], summary: str) -> None:
        """Map LLM-proposed actions onto the guarded deterministic executors."""
        applied: List[str] = []
        for action in actions:
            kind = str(action.get("action") or "")
            reason = str(action.get("reason") or "LLM optimizer recommendation")
            confidence = float(action.get("confidence") or 0.0)
            if confidence < 0.55:
                continue  # skip low-confidence actions
            try:
                if kind == "toggle_item":
                    menu_item_id = action.get("menu_item_id")
                    direction = str(action.get("toggle_direction") or "disable")
                    if menu_item_id is not None:
                        self._manual_toggle(int(menu_item_id), direction, f"[LLM] {reason}")
                        applied.append(f"toggle_item:{menu_item_id}:{direction}")
                elif kind == "create_deal":
                    ingredient_id = action.get("ingredient_id")
                    discount_pct = float(action.get("discount_pct") or config.PROMO_SLOW_MOVER_PCT)
                    if ingredient_id is not None:
                        self._propose_promo_llm(
                            int(ingredient_id),
                            discount_pct,
                            reason=f"[LLM] {reason}",
                            trigger="slow_mover",
                        )
                        applied.append(f"create_deal:ingredient:{ingredient_id}")
                elif kind == "reorder":
                    ingredient_id = action.get("ingredient_id")
                    if ingredient_id is not None:
                        self._maybe_reorder(int(ingredient_id))
                        applied.append(f"reorder:ingredient:{ingredient_id}")
                elif kind == "defer_reorder":
                    # Record this deferral decision in memory so future runs know.
                    ingredient_id = action.get("ingredient_id")
                    if ingredient_id is not None:
                        self._remember(
                            scope_type="ingredient",
                            scope_ref=str(ingredient_id),
                            insight={"action": "defer_reorder", "reason": reason},
                            confidence=confidence,
                            source="llm",
                        )
                        applied.append(f"defer_reorder:ingredient:{ingredient_id}")
                elif kind == "request_negotiation":
                    supplier_id = action.get("supplier_id")
                    ingredient_id = action.get("ingredient_id")
                    if (
                        supplier_id is not None
                        and ingredient_id is not None
                        and self._market_spectator is not None
                    ):
                        self._market_spectator.negotiate(
                            int(supplier_id), int(ingredient_id)
                        )
                        applied.append(
                            f"request_negotiation:s{supplier_id}:i{ingredient_id}"
                        )
            except Exception:  # noqa: BLE001
                logger.exception("Optimizer LLM action %s failed", kind)

        if applied:
            self.log_event(
                "llm_optimize",
                f"LLM optimizer applied {len(applied)} actions: {', '.join(applied[:5])}.",
                {"actions": actions, "summary": summary},
            )
            # Record a run-level memory insight.
            self._remember(
                scope_type="global",
                scope_ref="optimizer_run",
                insight={"summary": summary, "applied": applied},
                confidence=0.7,
                source="llm",
            )

    def _propose_promo_llm(
        self,
        ingredient_id: int,
        discount_pct: float,
        reason: str = "",
        trigger: str = "slow_mover",
    ) -> None:
        """Create a promo for an ingredient with a custom discount and trigger."""
        now = self.sim_time
        session = self.db_session_factory()
        try:
            ing = session.get(Ingredient, ingredient_id)
            ing_name = ing.name if ing is not None else str(ingredient_id)
            items = (
                session.query(MenuItem.id)
                .join(Recipe, Recipe.menu_item_id == MenuItem.id)
                .join(RecipeLine, RecipeLine.recipe_id == Recipe.id)
                .filter(RecipeLine.ingredient_id == ingredient_id, MenuItem.active == 1)
                .limit(3)
                .all()
            )
            menu_items = [r[0] for r in items]
            if not menu_items:
                return
            promo_type = "combo" if len(menu_items) > 1 else "discount"
            promo = Promotion(
                type=promo_type,
                menu_items=menu_items,
                trigger=trigger,
                discount_pct=float(discount_pct),
                channel="both",
                status="proposed",
                approval_id=None,
                sim_time=now,
            )
            session.add(promo)
            session.commit()
            session.refresh(promo)
            promo_id = promo.id
        finally:
            session.close()

        if self.approvals is not None:
            approval = self.approvals.create(
                type="promo",
                title=f"Promo: {trigger} {ing_name} ({discount_pct:.0f}% off)",
                summary=reason or f"LLM-recommended {trigger} deal for {ing_name}.",
                payload={"promo_id": promo_id, "ingredient_id": ingredient_id},
                ref_id=promo_id,
            )
            session = self.db_session_factory()
            try:
                promo_row = session.get(Promotion, promo_id)
                if promo_row is not None:
                    promo_row.approval_id = approval.id
                    session.commit()
            finally:
                session.close()

        self.emit(
            SignalType.PROMO_PROPOSAL,
            {
                "promo_id": promo_id,
                "type": promo_type,
                "menu_items": menu_items,
                "discount_pct": float(discount_pct),
                "channel": "both",
                "trigger": trigger,
            },
            dedup_key=f"promo_llm:{ingredient_id}:{trigger}",
        )
        self.log_event(
            "promo_proposal",
            f"[LLM] Proposed {trigger} promo for {ing_name} ({discount_pct:.0f}% off).",
            {"promo_id": promo_id, "ingredient_id": ingredient_id},
        )

    # -- sourcing plan (Phase 2b) ------------------------------------------------

    def run_sourcing_plan(self) -> None:
        """Run the MILP least-cost sourcing solve on the full ingredient catalog.

        Computes the optimal default-supplier assignment for every ingredient
        (accounting for item cost, delivery charge, switching cost, spoilage,
        volume discounts), writes a :class:`core.models.SourcingRun` audit row,
        updates :attr:`SupplierCatalog.is_default`, and for each changed default
        creates a :class:`core.models.ManagerChange` card + broadcasts
        ``manager_change`` on the operator WebSocket.

        Scheduled on a longer cadence via ``track_b.agents.__init__.register``
        and also callable on-demand via ``POST /api/track-b/optimizer/sourcing/run``.
        """
        solve_sourcing = _import_solve_sourcing()
        now = self.sim_time

        session = self.db_session_factory()
        try:
            ings = session.query(Ingredient).all()
            ingredients = [
                {
                    "id": int(i.id),
                    "name": i.name,
                    "perishable": bool(i.perishable),
                    "shelf_life_days": float(i.shelf_life_days or 0.0),
                    "current_default_supplier_id": None,
                }
                for i in ings
            ]
            ing_ids = [int(i.id) for i in ings]

            catalog_rows = session.query(SupplierCatalog).all()

            # Load active SupplierTerms and build quick lookup structures.
            # A term is "active" when status='active', not expired (date/orders).
            _active_terms = (
                session.query(SupplierTerm)
                .filter(SupplierTerm.status == "active")
                .all()
            )

            def _term_is_live(t: Any) -> bool:
                """Return True if the term is still in effect (not date/order-expired)."""
                if t.expiry_kind == "date" and t.expires_at is not None:
                    return float(t.expires_at) >= now
                if t.expiry_kind == "orders":
                    return (t.remaining_orders or 0) > 0
                return True  # expiry_kind="none" → permanent

            # Build {(supplier_id, ingredient_id_or_None): [SupplierTerm]}
            _term_map: Dict[tuple, list] = {}
            for t in _active_terms:
                if not _term_is_live(t):
                    continue
                key = (int(t.supplier_id), int(t.ingredient_id) if t.ingredient_id else None)
                _term_map.setdefault(key, []).append(t)

            def _is_unavailable(supplier_id: int, ingredient_id: int) -> bool:
                """Return True if any active unavailable term covers this supplier/ingredient."""
                applicable = (
                    _term_map.get((supplier_id, None), [])
                    + _term_map.get((supplier_id, ingredient_id), [])
                )
                return any(t.term_type == "unavailable" for t in applicable)

            def _effective_lead_days(supplier_id: int, base_days: float) -> float:
                """Return lead time in days, applying any lead_time_override term."""
                all_terms = _term_map.get((supplier_id, None), [])
                for t in all_terms:
                    if t.term_type == "lead_time_override":
                        return float(t.value)
                return base_days

            def _apply_terms(supplier_id: int, ingredient_id: int, base_price_g: float) -> float:
                """Apply any active SupplierTerms to the base per-gram price.

                Precedence: ingredient-specific terms override scope='all' terms.
                Multiple terms of the same type for the same row are rare (deduped at capture)
                but the last one wins to be safe.
                """
                price = base_price_g
                # Collect applicable terms: scope=all + scope=ingredient
                applicable = (
                    _term_map.get((supplier_id, None), [])          # scope=all
                    + _term_map.get((supplier_id, ingredient_id), [])  # scope=ingredient
                )
                for t in applicable:
                    if t.term_type == "price_override":
                        price = float(t.value)      # absolute per-gram price
                    elif t.term_type == "discount":
                        v = float(t.value)
                        if v < 0:                   # negative = price increase
                            price = price * (1.0 + abs(v))
                        else:
                            price = price * (1.0 - v)
                return max(0.0, price)

            def _price_per_gram(price: float, unit: str, pack_size: float) -> float:
                """Normalise a catalog price to per-gram so the MILP objective
                p·x (quantity in grams) is dimensionally consistent regardless
                of how the supplier quotes the price (per g / per kg / per pack)."""
                u = (unit or "g").strip().lower()
                if u == "kg":
                    return price / 1000.0
                if u in ("each", "pack", "unit"):
                    # price is per pack; pack_size is grams per pack
                    ps = pack_size if pack_size and pack_size > 0 else 1.0
                    return price / ps
                # "g" or unknown → price is already per gram
                return price

            catalog = []
            for c in catalog_rows:
                raw_price = float(c.current_price or 0.0)
                c_unit = c.unit or "g"
                c_pack = float(c.pack_size or 1.0)
                price_per_g = _price_per_gram(raw_price, c_unit, c_pack)
                # Apply any active SupplierTerms (discounts / price overrides)
                price_per_g = _apply_terms(int(c.supplier_id), int(c.ingredient_id), price_per_g)
                # Normalise per-item discount thresholds from catalog unit → grams
                disc = getattr(c, "discount", None)
                if disc:
                    normalised_disc = []
                    for tier in disc:
                        min_qty_g = _price_per_gram(1.0, c_unit, c_pack)  # unit factor
                        # tier min_qty is in catalog unit → convert to grams
                        tq = float(tier.get("min_qty") or 0.0)
                        # Convert: if unit is kg, min_qty is kg → multiply by 1000/factor
                        if c_unit.strip().lower() == "kg":
                            tq_g = tq * 1000.0
                        elif c_unit.strip().lower() in ("each", "pack", "unit"):
                            tq_g = tq * c_pack
                        else:
                            tq_g = tq  # already grams
                        disc_price_g = _price_per_gram(
                            float(tier.get("unit_price") or raw_price), c_unit, c_pack
                        )
                        normalised_disc.append({"min_qty": tq_g, "unit_price": disc_price_g})
                    disc = normalised_disc
                # Hard-exclude if an active unavailable term covers this supplier/ingredient
                effective_availability = c.availability or "in_stock"
                if _is_unavailable(int(c.supplier_id), int(c.ingredient_id)):
                    effective_availability = "out"

                catalog.append({
                    "id": int(c.id),
                    "supplier_id": int(c.supplier_id),
                    "ingredient_id": int(c.ingredient_id),
                    "current_price": price_per_g,   # normalised to per-gram (terms applied)
                    "original_price": raw_price,     # kept for human-readable display
                    "original_unit": c_unit,         # original catalog unit
                    "pack_size": c_pack,
                    "availability": effective_availability,
                    "unit": "g",                     # MILP always works in grams
                    "is_default": int(getattr(c, "is_default", 0) or 0),
                    "discount": disc,
                })

            default_sup: Dict[int, int] = {}
            for c in catalog:
                if c["is_default"]:
                    default_sup[c["ingredient_id"]] = c["supplier_id"]
            for i in ingredients:
                i["current_default_supplier_id"] = default_sup.get(i["id"])

            supplier_rows = session.query(Supplier).all()
            sup_name_map: Dict[int, str] = {int(s.id): s.name for s in supplier_rows}
            suppliers = [
                {
                    "id": int(s.id),
                    "name": s.name,
                    "delivery_charge": float(getattr(s, "delivery_charge", None) or 0.0),
                    # Use real DB values instead of hardcoded zeros/ones
                    "min_order_value": float(s.min_order_value or 0.0),
                    "lead_time_days": _effective_lead_days(int(s.id), float(s.lead_time_days or 1.0)),
                    "reliability_score": float(s.reliability_score or 1.0),
                    "volume_discount": getattr(s, "volume_discount", None),
                }
                for s in supplier_rows
            ]

            # Pull reliability scores from memory
            mem_rows = (
                session.query(InventoryOptimizerMemory)
                .filter(InventoryOptimizerMemory.scope_type == "supplier")
                .all()
            )
            for m in mem_rows:
                try:
                    s_id = int(str(m.scope_ref).split(":")[0])
                    if isinstance(m.insight, dict):
                        rel = float(m.insight.get("reliability_score", 1.0))
                        for s in suppliers:
                            if s["id"] == s_id:
                                s["reliability_score"] = rel
                except (ValueError, TypeError, AttributeError):
                    pass

            settings = (
                session.query(AppSettings)
                .filter(AppSettings.id == 1)
                .first()
            )
            auto_apply = bool(
                settings.auto_apply_supplier_changes if settings else
                config.AUTO_APPLY_SUPPLIER_CHANGES
            )
            switching_cost = float(
                settings.sourcing_switching_cost
                if (settings and hasattr(settings, "sourcing_switching_cost"))
                else config.SOURCING_SWITCHING_COST
            )
            horizon_days = float(
                settings.sourcing_horizon_days
                if (settings and hasattr(settings, "sourcing_horizon_days"))
                else config.SOURCING_HORIZON_DAYS
            )

            ing_name_map: Dict[int, str] = {int(i.id): i.name for i in ings}
        finally:
            session.close()

        # Compute net demand (shortfall) per ingredient: only order what we don't have
        # in usable stock after accounting for on-hand, expiry, and lead time.
        demand: Dict[int, float] = {}
        for iid in ing_ids:
            # Use the default supplier's lead_time_days as the planning lead
            lead = 1.0
            def_s_id = default_sup.get(iid)
            if def_s_id is not None:
                sup_data = next((s for s in suppliers if s["id"] == def_s_id), None)
                if sup_data:
                    lead = float(sup_data.get("lead_time_days") or 1.0)
            net = self._net_demand_for_sourcing(iid, lead, horizon_days)
            if net > 0:
                demand[iid] = net

        if not demand:
            self.log_event(
                "sourcing_plan_skipped",
                "Sourcing plan skipped: no net demand — all ingredients sufficiently stocked.",
                {},
            )
            return

        params = {
            "switching_cost": switching_cost,
            "horizon_days": horizon_days,
        }

        try:
            solution = solve_sourcing(ingredients, catalog, suppliers, demand, params)
        except Exception:
            logger.exception("Sourcing solve failed entirely; skipping this run.")
            return

        # Compute prev_cost on the same landed basis as solution.total_cost:
        # item cost (normalised per-gram × qty) + amortised delivery charge.
        # Group default suppliers to amortise delivery over all their ingredients.
        prev_sup_ingredients: Dict[int, List[int]] = {}  # sup_id → [ing_ids]
        for iid in demand:
            s_id = default_sup.get(iid)
            if s_id is not None:
                prev_sup_ingredients.setdefault(s_id, []).append(iid)

        prev_cost = 0.0
        for s_id, iids in prev_sup_ingredients.items():
            sup_data = next((s for s in suppliers if s["id"] == s_id), None)
            delivery = float(sup_data.get("delivery_charge") or 0.0) if sup_data else 0.0
            n = len(iids)
            for iid in iids:
                qty = demand.get(iid, 0.0)
                c = next(
                    (cc for cc in catalog
                     if cc["ingredient_id"] == iid and cc["supplier_id"] == s_id),
                    None,
                )
                if c:
                    prev_cost += float(c["current_price"] or 0.0) * qty
                    # Amortise delivery evenly across this supplier's ingredients
                    if n > 0:
                        prev_cost += delivery / n
        savings = round(prev_cost - solution.total_cost, 4)

        session = self.db_session_factory()
        try:
            run = SourcingRun(
                created_at=now,
                horizon_days=horizon_days,
                total_cost=solution.total_cost,
                prev_cost=round(prev_cost, 4),
                savings=savings,
                assignments=solution.assignments,
                method=solution.method,
                rationale=solution.rationale,
            )
            session.add(run)
            session.flush()
            run_id = run.id

            changes_created: List[Dict[str, Any]] = []

            for asgn in solution.assignments:
                iid = int(asgn["ingredient_id"])
                new_s_id = int(asgn["supplier_id"])

                # Clear old default, set new one for this ingredient
                for c_row in (
                    session.query(SupplierCatalog)
                    .filter(SupplierCatalog.ingredient_id == iid)
                    .all()
                ):
                    c_row.is_default = 1 if int(c_row.supplier_id) == new_s_id else 0

                if not asgn.get("is_default_change"):
                    continue

                old_s_id = default_sup.get(iid)
                old_cat = next(
                    (cc for cc in catalog
                     if cc["ingredient_id"] == iid and cc["supplier_id"] == (old_s_id or -1)),
                    None,
                )
                new_cat = next(
                    (cc for cc in catalog
                     if cc["ingredient_id"] == iid and cc["supplier_id"] == new_s_id),
                    None,
                )

                ing_name = ing_name_map.get(iid, str(iid))
                old_s_name = sup_name_map.get(old_s_id, str(old_s_id)) if old_s_id else "None"
                new_s_name = sup_name_map.get(new_s_id, str(new_s_id))

                details = {
                    "ingredient_id": iid,
                    "ingredient_name": ing_name,
                    "supplier_id_before": old_s_id,
                    "supplier_id_after": new_s_id,
                    "supplier_name_before": old_s_name,
                    "supplier_name_after": new_s_name,
                    "price_before": float(old_cat["current_price"]) if old_cat else 0.0,
                    "price_after": float(new_cat["current_price"]) if new_cat else 0.0,
                    "rationale": solution.rationale,
                    "run_id": run_id,
                    "estimated_savings": round(savings, 4),
                    "method": solution.method,
                }

                change = ManagerChange(
                    kind="sourcing_default",
                    status="applied" if auto_apply else "pending",
                    auto_applied=1 if auto_apply else 0,
                    summary=(
                        f"Default supplier for {ing_name}: "
                        f"{old_s_name} → {new_s_name}"
                    ),
                    details=details,
                    created_at=now,
                    resolved_at=now if auto_apply else None,
                )
                session.add(change)
                session.flush()
                changes_created.append(
                    {"change_id": change.id, "details": details}
                )

            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Sourcing plan DB writes failed.")
            return
        finally:
            session.close()

        for ch in changes_created:
            self.broadcast("manager_change", {
                "change_id": ch["change_id"],
                "details": ch["details"],
            })

        self.log_event(
            "sourcing_plan",
            (
                f"Sourcing plan ({solution.method}): "
                f"total_cost={solution.total_cost:.4f}, "
                f"prev_cost={prev_cost:.4f}, "
                f"savings={savings:.4f}, "
                f"changes={len(changes_created)}. "
                f"{solution.rationale}"
            ),
            {
                "method": solution.method,
                "total_cost": solution.total_cost,
                "prev_cost": round(prev_cost, 4),
                "savings": savings,
                "n_changes": len(changes_created),
                "run_id": run_id,
            },
        )

    def _remember(
        self,
        scope_type: str,
        scope_ref: str,
        insight: Any,
        confidence: float = 0.7,
        source: str = "llm",
        valid_until: Optional[float] = None,
    ) -> None:
        """Upsert an InventoryOptimizerMemory insight."""
        now = self.sim_time
        session = self.db_session_factory()
        try:
            existing = (
                session.query(InventoryOptimizerMemory)
                .filter(
                    InventoryOptimizerMemory.scope_type == scope_type,
                    InventoryOptimizerMemory.scope_ref == scope_ref,
                )
                .first()
            )
            if existing is not None:
                existing.insight = insight
                existing.confidence = confidence
                existing.last_seen_at = now
                existing.source = source
            else:
                session.add(InventoryOptimizerMemory(
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    insight=insight,
                    evidence=None,
                    confidence=confidence,
                    created_at=now,
                    last_seen_at=now,
                    valid_until=valid_until,
                    source=source,
                ))
            session.commit()
        finally:
            session.close()
