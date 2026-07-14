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
        elif signal.type in (
            SignalType.SUPPLIER_PRICE_UPDATE.value,
            SignalType.CALL_OUTCOME.value,
        ):
            # A supplier offer was applied (manually or auto) — re-cost the forward plan,
            # re-run the sourcing solver, then immediately place any now-due orders so
            # at-risk stock is secured without waiting for the next reorder sweep (§W1a).
            self.build_procurement_plan()
            self.run_sourcing_plan()
            self.execute_due_planned_orders()

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
        # B: Execute any overdue rows produced by the build just above (e.g. a
        # freshly-built at_risk row whose order_date is already < now).  A second
        # execute pass prevents those rows from sitting visible-and-unexecuted for
        # a full FORECAST_INTERVAL_SIM_S.  No recursion — execute never calls build.
        self.execute_due_planned_orders()

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

    def build_procurement_plan(
        self,
        horizon_days: Optional[float] = None,
        robust: Optional[bool] = None,
    ) -> int:
        """Build a time-phased forward procurement plan.

        Projects running inventory day-by-day using the DEMAND_FORECAST_HORIZON
        signal, schedules PlannedOrder rows for each ingredient that will dip
        below safety stock, and applies hysteresis to avoid churn.

        robust: when True, override the env default and run pass-3 hard-delay
                guarantee; when False, disable it regardless of env default;
                when None, use the config default (RELIABILITY_ROBUST_HARD_DELAY).

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
                    # C3: All-suppliers-out fallback — pick cheapest available anyway
                    # so we can still emit an at_risk planned order rather than silently
                    # dropping the ingredient from coverage tracking.
                    if specs:
                        candidate = min(
                            specs, key=lambda s: float(s.get("current_price") or 1e9)
                        )
                    if candidate is None:
                        continue

                lead_days = float(lead_by_supplier.get(candidate["supplier_id"], 1.0))
                safety_stock = float(level.safety_stock or 0.0)
                shelf_life = (
                    float(ing.shelf_life_days)
                    if ing.perishable and ing.shelf_life_days
                    else None
                )

                # Reconciled inventory snapshot: gather active lots and derive
                # on_hand from their sum (lot-sum is the single source of truth).
                # Falls back to on_hand_cached only when no lots exist at all.
                # This prevents the MILP and FEFO validator from using different
                # opening stocks (on_hand_cached can drift from lot-sum via the
                # expiry-scan race fixed in ledger._expire_lot).
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

                if lot_data:
                    # Lot-sum is the authoritative on-hand; cache may be stale.
                    on_hand = sum(lq for lq, _ in lot_data)
                    cached = float(level.on_hand_cached or 0.0)
                    if abs(on_hand - cached) > 1.0:
                        logger.warning(
                            "Inventory drift for ingredient %s: lot-sum=%.1f vs "
                            "on_hand_cached=%.1f — using lot-sum as truth.",
                            ing_id, on_hand, cached,
                        )
                else:
                    # No active lots: fall back to the cached scalar and synthesise
                    # a single never-expiring lot so both MILP and FEFO agree.
                    on_hand = float(level.on_hand_cached or 0.0)
                    if on_hand > 0:
                        lot_data = [[on_hand, 999999]]

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
        # scheduled_supplier_days: already-in-transit supplier-days whose delivery charge is sunk.
        # The MILP pre-opens those deliver binaries and skips charge/MOV for them so incremental
        # lines can piggyback without triggering a new delivery fee or MOV enforcement.
        scheduled_supplier_days: set = set()
        if ingredient_ids:
            from sqlalchemy import func as _sa_func  # local import; avoids top-level sqlalchemy dep
            session = self.db_session_factory()
            try:
                in_transit = (
                    session.query(
                        PurchaseOrderLine.ingredient_id,
                        PurchaseOrder.supplier_id,
                        PurchaseOrder.expected_delivery,
                        _sa_func.sum(PurchaseOrderLine.qty).label("total_qty"),
                    )
                    .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                    .filter(
                        PurchaseOrderLine.ingredient_id.in_(ingredient_ids),
                        PurchaseOrder.status.in_(("proposed", "approved", "placed")),
                        PurchaseOrder.expected_delivery.isnot(None),
                    )
                    .group_by(
                        PurchaseOrderLine.ingredient_id,
                        PurchaseOrder.supplier_id,
                        PurchaseOrder.expected_delivery,
                    )
                    .all()
                )
                for ing_id_t, sup_id_t, exp_delivery, total_qty in in_transit:
                    if exp_delivery is None:
                        continue
                    arr_day = int(float(exp_delivery) // SECONDS_PER_DAY) - now_day
                    # C1: Phantom-inventory fix — only credit POs expected to arrive
                    # within the horizon.  POs overdue by more than 1 day are treated
                    # as stuck (never delivered) so the planner re-orders instead of
                    # relying on stock that may never arrive.
                    if arr_day < -1:
                        logger.debug(
                            "Excluding overdue in-flight PO (arr_day=%d) for ingredient %s "
                            "from projection — treating as undelivered.",
                            arr_day, ing_id_t,
                        )
                        continue
                    arr_day = max(arr_day, 0)
                    if ing_id_t in inbound_by_day:
                        inbound_by_day[ing_id_t][arr_day] = (
                            inbound_by_day[ing_id_t].get(arr_day, 0.0) + float(total_qty or 0.0)
                        )
                    # Track the supplier-day as already scheduled
                    if sup_id_t is not None:
                        scheduled_supplier_days.add((int(sup_id_t), arr_day))

                # W4: Also credit POs awaiting approval (proposed/approved, expected_delivery=None).
                # estimate arrival = created_at + supplier lead_time so they count as pipeline
                # supply and the MILP doesn't re-order the same ingredient.
                null_transit = (
                    session.query(
                        PurchaseOrderLine.ingredient_id,
                        PurchaseOrder.supplier_id,
                        PurchaseOrder.created_at,
                        Supplier.lead_time_days,
                        _sa_func.sum(PurchaseOrderLine.qty).label("total_qty"),
                    )
                    .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                    .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
                    .filter(
                        PurchaseOrderLine.ingredient_id.in_(ingredient_ids),
                        PurchaseOrder.status.in_(("proposed", "approved")),
                        PurchaseOrder.expected_delivery.is_(None),
                    )
                    .group_by(
                        PurchaseOrderLine.ingredient_id,
                        PurchaseOrder.supplier_id,
                        PurchaseOrder.created_at,
                        Supplier.lead_time_days,
                    )
                    .all()
                )
                for ing_id_t, sup_id_t, created_at, lead_days, total_qty in null_transit:
                    lead = float(lead_days or 1.0)
                    est_delivery = float(created_at or now) + lead * SECONDS_PER_DAY
                    arr_day = int(est_delivery // SECONDS_PER_DAY) - now_day
                    if arr_day < -1:
                        continue
                    arr_day = max(arr_day, 0)
                    if ing_id_t in inbound_by_day:
                        inbound_by_day[ing_id_t][arr_day] = (
                            inbound_by_day[ing_id_t].get(arr_day, 0.0) + float(total_qty or 0.0)
                        )
                    if sup_id_t is not None:
                        scheduled_supplier_days.add((int(sup_id_t), arr_day))
            finally:
                session.close()

        # ----------------------------------------------------------------
        # Phase 2 + 2.5 (replaced): Time-phased cost-optimal plan via MILP.
        #
        # The old greedy projection + same-day consolidation is replaced by a
        # multi-period optimizer that jointly minimises goods cost + delivery
        # charges + discounts subject to coverage, lead-time feasibility, MOV,
        # and expiry constraints.  Falls back to a greedy projection
        # (plan_optimizer._solve_greedy) when PuLP is unavailable or the MILP
        # returns non-optimal.
        # ----------------------------------------------------------------
        new_orders: List[Dict] = []
        plan_method = "milp"
        plan_coverage_ok = True
        plan_total_short = 0.0
        _ing_name_by_id: Dict[int, str] = {}  # populated by MILP path; used by emit block
        plan_reliability_premium = 0.0
        plan_exposed_value_baseline = 0.0
        plan_exposed_value_protected = 0.0

        # -- Gather full catalog + all suppliers for every active ingredient --
        if ingredients_data:
            from track_b.procurement.plan_optimizer import (  # noqa: PLC0415
                solve_time_phased_plan as _solve_time_phased,
            )
            session = self.db_session_factory()
            try:
                _active_ing_ids = list(ingredients_data.keys())
                _all_cat_rows = (
                    session.query(SupplierCatalog)
                    .filter(SupplierCatalog.ingredient_id.in_(_active_ing_ids))
                    .all()
                )
                _all_sup_ids = {c.supplier_id for c in _all_cat_rows}
                _all_sup_rows = (
                    session.query(Supplier)
                    .filter(Supplier.id.in_(_all_sup_ids))
                    .all()
                )
                _ing_rows = (
                    session.query(Ingredient)
                    .filter(Ingredient.id.in_(_active_ing_ids))
                    .all()
                )
                _ing_name_by_id = {i.id: i.name for i in _ing_rows}
            finally:
                session.close()

            milp_ingredients = [
                {
                    "id": iid,
                    "perishable": 1 if data["shelf_life"] is not None else 0,
                    "shelf_life_days": data["shelf_life"],
                    "base_unit": data["candidate"].get("unit", "g"),
                    "name": _ing_name_by_id.get(iid, str(iid)),
                }
                for iid, data in ingredients_data.items()
            ]
            milp_catalog = [
                {
                    "supplier_id": int(c.supplier_id),
                    "ingredient_id": int(c.ingredient_id),
                    "current_price": float(c.current_price or 0.0),
                    "pack_size": float(c.pack_size or 1.0),
                    "unit": c.unit or "g",
                    "availability": c.availability or "in_stock",
                    "is_default": int(getattr(c, "is_default", 0) or 0),
                    "discount": (
                        c.discount
                        if hasattr(c, "discount") and c.discount
                        else None
                    ),
                }
                for c in _all_cat_rows
            ]
            milp_suppliers = [
                {
                    "id": int(s.id),
                    "name": s.name,
                    "lead_time_days": float(s.lead_time_days or 1.0),
                    "reliability_score": float(s.reliability_score or 1.0),
                    "min_order_value": float(s.min_order_value or 0.0),
                    "delivery_charge": float(s.delivery_charge or 0.0),
                    "volume_discount": (
                        s.volume_discount
                        if hasattr(s, "volume_discount") and s.volume_discount
                        else None
                    ),
                    "delivery_hour": float(getattr(s, "delivery_hour", None) or 8.0),
                }
                for s in _all_sup_rows
            ]
            _lead_by_sup = {int(s.id): float(s.lead_time_days or 1.0) for s in _all_sup_rows}
            _delivery_hour_by_sup = {
                int(s.id): float(getattr(s, "delivery_hour", None) or 8.0)
                for s in _all_sup_rows
            }

            # Robust demand floor: max(transient qty, steady-state baseline).
            _demand_map: Dict[int, Dict[int, float]] = {}
            for iid in ingredients_data:
                _demand_map[iid] = {
                    d: max(
                        per_day_demand.get(iid, {}).get(d, 0.0),
                        per_day_baseline.get(iid, {}).get(d, 0.0),
                    )
                    for d in range(n_days)
                }

            _on_hand_map = {iid: data["on_hand"] for iid, data in ingredients_data.items()}
            _safety_map = {iid: data["safety_stock"] for iid, data in ingredients_data.items()}
            # A9: pass dated opening lots so the solver's expiry model uses real
            # lot expiries (fresh-initial) rather than a single never-expiring lot.
            _lots_map = {iid: data["lots"] for iid, data in ingredients_data.items()}

            # Compute contribution margin per base unit for each active ingredient.
            # Margin = menu selling price − recipe cost (cheapest catalog price per
            # ingredient × recipe qty).  Used as weights in the pass-2 reliability
            # exposure lever; falls back to unit price in the solver when 0.
            _margin_by_ing: Dict[int, float] = {}
            try:
                # Cheapest available catalog price per ingredient.
                _cheap_price: Dict[int, float] = {}
                for _cc in milp_catalog:
                    _ciid = int(_cc["ingredient_id"])
                    _cp = float(_cc.get("current_price") or 0.0)
                    if _ciid not in _cheap_price or _cp < _cheap_price[_ciid]:
                        _cheap_price[_ciid] = _cp

                # Selling prices for menu items that appear in our recipe_qty map.
                _sess_m = self.db_session_factory()
                try:
                    _mid_ids = list(recipe_qty.keys())
                    _menu_price_rows = (
                        _sess_m.query(MenuItem.id, MenuItem.dine_in_price)
                        .filter(MenuItem.id.in_(_mid_ids))
                        .all()
                    ) if _mid_ids else []
                finally:
                    _sess_m.close()
                _price_by_mid: Dict[int, float] = {
                    int(_m): float(_p or 0.0) for _m, _p in _menu_price_rows
                }

                # Margin per base unit of each ingredient, averaged across dishes.
                _margin_sum: Dict[int, float] = {}
                _margin_cnt: Dict[int, int] = {}
                for _mid, _ing_map in recipe_qty.items():
                    _sell = _price_by_mid.get(_mid, 0.0)
                    if _sell <= 0:
                        continue
                    _rcost = sum(
                        float(_qty) * _cheap_price.get(_iid, 0.0)
                        for _iid, _qty in _ing_map.items()
                    )
                    _margin = max(0.0, _sell - _rcost)
                    if _margin <= 0:
                        continue
                    for _iid, _qty in _ing_map.items():
                        if (_qty or 0) <= 0:
                            continue
                        _mpunit = _margin / float(_qty)
                        _margin_sum[_iid] = _margin_sum.get(_iid, 0.0) + _mpunit
                        _margin_cnt[_iid] = _margin_cnt.get(_iid, 0) + 1
                _margin_by_ing = {
                    _iid: _margin_sum[_iid] / _margin_cnt[_iid]
                    for _iid in _margin_sum
                    if _margin_cnt.get(_iid, 0) > 0
                }
            except Exception:  # noqa: BLE001
                logger.warning(
                    "build_procurement_plan: margin_by_ing computation failed; "
                    "reliability pass will fall back to unit-price weights."
                )
                _margin_by_ing = {}

            _solution = _solve_time_phased(
                n_days=n_days,
                ingredients=milp_ingredients,
                catalog=milp_catalog,
                suppliers=milp_suppliers,
                demand_by_day=_demand_map,
                inbound_by_day=inbound_by_day,
                on_hand=_on_hand_map,
                safety_stock=_safety_map,
                params={
                    "spoilage_penalty_multiplier": 2.0,
                    "slack_penalty": 1000.0,
                    "safety_penalty_multiplier": 0.0,  # safety buffer is a reporting target only; never drives a goods purchase
                    "reorder_interval_days": config.REORDER_INTERVAL_DAYS,
                    "lots_by_ing": _lots_map,
                    "reliability_cash_tolerance": config.RELIABILITY_CASH_TOLERANCE,
                    "stress_enabled": config.RELIABILITY_STRESS_ENABLED,
                    "margin_by_ing": _margin_by_ing,
                    "production_start_hour": config.PRODUCTION_START_HOUR,
                    "scheduled_supplier_days": scheduled_supplier_days,
                    "robust_hard_delay": (
                        robust if robust is not None else config.RELIABILITY_ROBUST_HARD_DELAY
                    ),
                    "robust_min_reliability": config.RELIABILITY_ROBUST_MIN_RELIABILITY,
                },
            )
            plan_method = _solution.method
            plan_coverage_ok = bool(getattr(_solution, "coverage_ok", True))
            plan_total_short = float(getattr(_solution, "total_short", 0.0))
            plan_reliability_premium = float(getattr(_solution, "reliability_premium", 0.0))
            plan_exposed_value_baseline = float(getattr(_solution, "exposed_value_baseline", 0.0))
            plan_exposed_value_protected = float(getattr(_solution, "exposed_value_plan", 0.0))
            plan_robust_applied = bool(getattr(_solution, "robust_applied", False))
            plan_robust_status = str(getattr(_solution, "robust_status", ""))
            logger.info(
                "Time-phased plan: method=%s, %d orders, total_cost=%.2f, "
                "coverage_ok=%s, total_short=%.1f, robust=%s — %s",
                _solution.method,
                len(_solution.orders),
                _solution.total_cost,
                plan_coverage_ok,
                plan_total_short,
                plan_robust_status or "off",
                _solution.rationale,
            )
            if not plan_coverage_ok:
                logger.warning(
                    "Procurement plan leaves %.0f base units of forecasted demand "
                    "UNCOVERABLE (lead-time / supply infeasible).", plan_total_short,
                )

            # Map PlanSolution.orders → new_orders dict (Phase 3 schema)
            for _order in _solution.orders:
                _iid = _order.ingredient_id
                _s_id = _order.supplier_id
                _lead = _lead_by_sup.get(_s_id, 1.0)
                _dh = _delivery_hour_by_sup.get(_s_id, 8.0)
                _delivery_sim_s = float(
                    (now_day + _order.delivery_day) * SECONDS_PER_DAY + _dh * 3600.0
                )
                _order_sim_s = _delivery_sim_s - _lead * SECONDS_PER_DAY
                # C: distinguish genuinely uncoverable (no supply, qty==0 sentinel)
                # from late/expedite orders (buyable but order-by window has passed).
                if _order.at_risk and (_order.qty or 0) <= 0:
                    _status = "uncoverable"   # no lead-feasible / in-stock supply
                elif _order.at_risk or _order_sim_s < now:
                    _status = "at_risk"       # order window passed, but supply exists
                else:
                    _status = "planned"
                _covers_until = float(
                    (
                        now_day
                        + _order.delivery_day
                        + math.ceil(_lead + config.REORDER_INTERVAL_DAYS + 1)
                    )
                    * SECONDS_PER_DAY
                    + DAY_OPEN_OFFSET
                )
                # Convert per-line risk's latest_safe_arrival (horizon day offset)
                # to a sim-seconds timestamp using the supplier's delivery hour.
                _lsa_offset = getattr(_order, "latest_safe_arrival", -1)
                _lsa_sim_s: Optional[float] = None
                if _lsa_offset is not None and _lsa_offset >= 0:
                    _lsa_sim_s = float(
                        (now_day + _lsa_offset) * SECONDS_PER_DAY + _dh * 3600.0
                    )
                new_orders.append({
                    "ingredient_id": _iid,
                    "supplier_id": _s_id,
                    "qty": _order.qty,
                    "unit": _order.unit,
                    "unit_price": _order.unit_price,
                    "order_date": _order_sim_s,
                    "delivery_date": _delivery_sim_s,
                    "covers_from": _delivery_sim_s,
                    "covers_until": _covers_until,
                    "status": _status,
                    "reason": _order.reason,
                    "projected_stock_before": float(getattr(_order, "projected_stock_before", 0.0) or 0.0),
                    "qty_needed_before": float(getattr(_order, "qty_needed_before", 0.0) or 0.0),
                    "shortage_if_late": float(getattr(_order, "shortage_if_late", 0.0) or 0.0),
                    "latest_safe_arrival": _lsa_sim_s,
                })

        # Signal emission is deferred to after the FEFO recheck so it uses
        # per-ingredient coverage_by_ing (not the solver's delay metric).
        # Placeholder; actual emission happens after the FEFO recheck block.

        # ----------------------------------------------------------------
        # WS5: Anti-jitter filter for top-up orders.
        #
        # When PROCUREMENT_JITTER_FRACTION > 0 and there is existing pipeline
        # for an ingredient (inbound_by_day sum > 0), suppress the top-up order
        # if the unrounded shortfall is below JITTER_FRACTION × total_demand.
        # This prevents forecast noise from triggering churn on orders that are
        # already substantially covered.
        #
        # First-time orders (zero existing pipeline) are NEVER suppressed.
        # Default JITTER_FRACTION = 0.0 — disabled unless explicitly configured.
        # ----------------------------------------------------------------
        _jitter_frac = float(getattr(config, "PROCUREMENT_JITTER_FRACTION", 0.0))
        if _jitter_frac > 0 and new_orders:
            _filtered_orders: List[Dict] = []
            for _jod in new_orders:
                _jiid = int(_jod["ingredient_id"])
                _existing_pipeline = sum(inbound_by_day.get(_jiid, {}).values())
                if _existing_pipeline > 0:
                    # Top-up path: check anti-jitter threshold.
                    _total_demand_j = sum(
                        per_day_demand.get(_jiid, {}).get(d, 0.0) for d in range(n_days)
                    )
                    _on_hand_j = (ingredients_data.get(_jiid) or {}).get("on_hand", 0.0)
                    _unrounded_short = max(0.0, _total_demand_j - _on_hand_j - _existing_pipeline)
                    _jitter_threshold = _jitter_frac * max(1.0, _total_demand_j)
                    if _unrounded_short < _jitter_threshold:
                        logger.debug(
                            "build_procurement_plan: anti-jitter suppressing top-up for "
                            "ingredient %s (unrounded shortfall %.1f < threshold %.1f).",
                            _jiid, _unrounded_short, _jitter_threshold,
                        )
                        continue  # skip this top-up — within jitter tolerance
                _filtered_orders.append(_jod)
            if len(_filtered_orders) < len(new_orders):
                logger.info(
                    "build_procurement_plan: anti-jitter suppressed %d top-up order(s).",
                    len(new_orders) - len(_filtered_orders),
                )
            new_orders = _filtered_orders

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
                .filter(PlannedOrder.status.in_(["planned", "at_risk", "uncoverable"]))
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
                pack_size = (
                    ingredients_data[ing_id]["candidate"]["pack_size"]
                    if ing_id in ingredients_data
                    else 1.0
                )

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
                            "projected_stock_before": order_data.get("projected_stock_before", 0.0),
                            "qty_needed_before": order_data.get("qty_needed_before", 0.0),
                            "shortage_if_late": order_data.get("shortage_if_late", 0.0),
                            "latest_safe_arrival": order_data.get("latest_safe_arrival"),
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

            # B (R1): Reconciliation gate — never persist a new or kept planned row
            # when open real POs cover ADDITIONAL demand beyond what the solver already
            # credited via inbound_by_day.
            #
            # Netting rule: inbound_by_day already credited every non-overdue open PO
            # into the solver's arrivals, so plan-row quantities are already net of
            # pipeline.  R1 must compare against the RESIDUAL — PO qty not yet seen by
            # the solver — to avoid double-subtracting the same supply.
            #
            # residual[iid] = max(0, total_open_PO_qty[iid] - inbound_credited[iid])
            #   ≈ 0  for any PO credited by inbound_by_day (same status filter, same
            #         overdue exclusion) → R1 does not cancel the legitimate top-up.
            #   > 0  only when a brand-new PO was placed *after* inbound snapshot and
            #         before this R1 check → still suppresses genuine duplicates.
            #
            # Keyed on ingredient_id only (not ingredient+supplier) so a cross-supplier
            # PO still suppresses the plan row.
            try:
                from sqlalchemy import func as _sa_func_r1  # local import; avoids top-level dep
                _open_po_coverage: Dict[int, float] = {}   # {ingredient_id: total_qty}
                _open_po_rows = (
                    session.query(
                        PurchaseOrderLine.ingredient_id,
                        _sa_func_r1.sum(PurchaseOrderLine.qty).label("total_qty"),
                    )
                    .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                    .filter(PurchaseOrder.status.in_(("proposed", "approved", "placed")))
                    .group_by(PurchaseOrderLine.ingredient_id)
                    .all()
                )
                for _r1_ing, _r1_qty in _open_po_rows:
                    _open_po_coverage[int(_r1_ing)] = (
                        _open_po_coverage.get(int(_r1_ing), 0.0) + float(_r1_qty or 0.0)
                    )

                # How much of each ingredient the solver already credited (inbound_by_day
                # uses the same status/overdue filter as the open-PO query above).
                _inbound_credited: Dict[int, float] = {
                    iid: sum(day_map.values())
                    for iid, day_map in inbound_by_day.items()
                    if day_map
                }

                if _open_po_coverage:
                    _r1_filtered: List[Dict] = []
                    for _od in final_new_orders:
                        _r1_iid = int(_od["ingredient_id"])
                        _r1_need = float(_od["qty"] or 0.0)
                        _r1_gross = _open_po_coverage.get(_r1_iid, 0.0)
                        # Residual = PO qty the solver did NOT already net
                        _r1_residual = max(0.0, _r1_gross - _inbound_credited.get(_r1_iid, 0.0))
                        if _r1_residual >= _r1_need:
                            logger.info(
                                "R1: suppressing planned row ingredient=%s "
                                "(residual PO qty %.1f — gross %.1f minus credited %.1f — ≥ needed %.1f).",
                                _r1_iid, _r1_residual, _r1_gross,
                                _inbound_credited.get(_r1_iid, 0.0), _r1_need,
                            )
                        else:
                            _r1_filtered.append(_od)
                    final_new_orders = _r1_filtered

                    for _r1_row_id in list(kept_ids):
                        _r1_krow = session.get(PlannedOrder, _r1_row_id)
                        if _r1_krow is None:
                            continue
                        _r1_iid = int(_r1_krow.ingredient_id)
                        _r1_need = float(_r1_krow.qty or 0.0)
                        _r1_gross = _open_po_coverage.get(_r1_iid, 0.0)
                        _r1_residual = max(0.0, _r1_gross - _inbound_credited.get(_r1_iid, 0.0))
                        if _r1_residual >= _r1_need:
                            logger.info(
                                "R1: superseding kept planned row %s ingredient=%s "
                                "(residual PO qty %.1f covers needed %.1f).",
                                _r1_row_id, _r1_iid, _r1_residual, _r1_need,
                            )
                            kept_ids.discard(_r1_row_id)
                            superseded_ids.add(_r1_row_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "build_procurement_plan: R1 open-PO reconciliation query failed; skipping."
                )

            for row_id in superseded_ids:
                row = session.get(PlannedOrder, row_id)
                if row:
                    row.status = "superseded"

            n_new = len(final_new_orders)
            n_kept = len(kept_ids)
            n_superseded = len(superseded_ids)
            total_active = n_new + n_kept

            # WS2/WS3: Authoritative FEFO coverage check — replaces the aggregate
            # horizon-sum recompute with a day-by-day FEFO inventory simulation that
            # is independent of the solver's netting.  Inputs are deduped by identity:
            #   • open_po_arrivals = inbound_by_day (keyed by PO, already deduped)
            #   • plan_arrivals    = final_new_orders + kept rows (post-R1, post-hysteresis)
            # This correctly catches coverage gaps that the solver introduced or missed
            # during reconciliation (the mascarpone double-net → false coverage_ok=True).
            _reconciled_total_short = plan_total_short  # fallback
            _reconciled_coverage_ok = plan_coverage_ok
            _coverage_depends_on_planned = False
            _late_delivery_coverage_ok = (
                float(getattr(_solution, "unit_shortfall_if_1day_late", 0.0) or 0.0) <= config.COVERAGE_EPSILON
            )
            _fefo_result: Dict[str, Any] = {}  # populated inside inner try; used after it
            try:
                from track_b.procurement.plan_optimizer import (  # noqa: PLC0415
                    project_fefo_coverage as _fefo_check,
                )

                # Build plan_arrivals_by_day from final persisted orders.
                _plan_arr_by_day: Dict[int, Dict[int, float]] = {}
                for _pao in final_new_orders:
                    _pa_iid = int(_pao["ingredient_id"])
                    _pa_dd = int(float(_pao.get("delivery_date") or 0.0) // SECONDS_PER_DAY) - now_day
                    _pa_dd = max(_pa_dd, 0)
                    _plan_arr_by_day.setdefault(_pa_iid, {})
                    _plan_arr_by_day[_pa_iid][_pa_dd] = (
                        _plan_arr_by_day[_pa_iid].get(_pa_dd, 0.0)
                        + float(_pao.get("qty") or 0.0)
                    )
                for _kr_id in kept_ids:
                    _kr = session.get(PlannedOrder, _kr_id)
                    if _kr is not None:
                        _kr_iid = int(_kr.ingredient_id)
                        _kr_dd = int(float(_kr.delivery_date or 0.0) // SECONDS_PER_DAY) - now_day
                        _kr_dd = max(_kr_dd, 0)
                        _plan_arr_by_day.setdefault(_kr_iid, {})
                        _plan_arr_by_day[_kr_iid][_kr_dd] = (
                            _plan_arr_by_day[_kr_iid].get(_kr_dd, 0.0)
                            + float(_kr.qty or 0.0)
                        )

                # Ingredient shelf life map for arrival-lot expiry calculation.
                _fefo_sl: Dict[int, Optional[float]] = {
                    iid: data.get("shelf_life")
                    for iid, data in ingredients_data.items()
                }

                _fefo_result = _fefo_check(
                    n_days=n_days,
                    # Use the same robust demand the MILP optimised for (max of
                    # forecast and baseline) so the coverage flag and the solve
                    # measure the same demand.  per_day_demand (raw forecast) alone
                    # would be inconsistent with a plan that targets the robust max.
                    demand_by_day=_demand_map,
                    on_hand={iid: d["on_hand"] for iid, d in ingredients_data.items()},
                    lots_by_ing={iid: d["lots"] for iid, d in ingredients_data.items()},
                    open_po_arrivals_by_day=inbound_by_day,
                    plan_arrivals_by_day=_plan_arr_by_day,
                    safety_stock={iid: d["safety_stock"] for iid, d in ingredients_data.items()},
                    ingredient_shelf_life=_fefo_sl,
                )
                _reconciled_total_short = float(_fefo_result["total_short"])
                _reconciled_coverage_ok = bool(_fefo_result["coverage_ok"])
                _coverage_depends_on_planned = bool(_fefo_result["coverage_depends_on_planned"])
                _late_delivery_coverage_ok = bool(_fefo_result["late_delivery_coverage_ok"])

                if not _reconciled_coverage_ok:
                    _short_ings = _fefo_result.get("short_by_ing") or {}
                    for _si_iid, _si_qty in _short_ings.items():
                        logger.warning(
                            "FEFO coverage gap: ingredient %s is %.1f units short after "
                            "full reconciliation (on_hand + inbound + plan).",
                            _si_iid, _si_qty,
                        )
                    if plan_coverage_ok:
                        logger.warning(
                            "Coverage gap INTRODUCED by post-solver reconciliation: "
                            "%.1f units uncoverable after R1/hysteresis (solver claimed full coverage).",
                            _reconciled_total_short,
                        )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "build_procurement_plan: FEFO coverage check failed; "
                    "falling back to solver total_short=%.1f.", plan_total_short,
                )

            # ----------------------------------------------------------------
            # Post-FEFO: derive authoritative coverage_by_ing and update orders
            # ----------------------------------------------------------------
            _coverage_by_ing: Dict[int, Dict] = _fefo_result.get("coverage_by_ing", {})
            # Patch coverage fields into new_orders before persistence.
            for _od in final_new_orders:
                _co_iid = int(_od.get("ingredient_id", 0))
                _co = _coverage_by_ing.get(_co_iid, {})
                _od["coverage_status"] = _co.get("status", "covered")
                _od["short_nominal"] = float(_co.get("short_nominal", 0.0))
                _od["short_delayed"] = float(_co.get("short_delayed", 0.0))
                # Normalise unit to ingredient base_unit so qty and unit agree.
                _co_ing = session.get(Ingredient, _co_iid)
                if _co_ing and getattr(_co_ing, "base_unit", None):
                    _od["unit"] = _co_ing.base_unit

            # Anti-contradiction guard: coverage_ok cannot be True while any
            # ingredient is nominal_uncoverable.  Fail-safe toward showing the gap.
            _has_nominal_uncoverable = any(
                v.get("status") == "nominal_uncoverable"
                for v in _coverage_by_ing.values()
            )
            if _reconciled_coverage_ok and _has_nominal_uncoverable:
                _n_unc = sum(
                    1 for v in _coverage_by_ing.values()
                    if v.get("status") == "nominal_uncoverable"
                )
                logger.error(
                    "Contradiction: coverage_ok=True but %d ingredient(s) are nominal_uncoverable; "
                    "forcing coverage_ok=False as fail-safe.",
                    _n_unc,
                )
                _reconciled_coverage_ok = False

            # Emit INGREDIENT_UNCOVERABLE signals driven by FEFO coverage_by_ing.
            # Only nominal_uncoverable ingredients trigger the signal; delay-exposed
            # items do NOT — this prevents false "coffee short 135ml"-style warnings.
            # The short_qty is the per-ingredient nominal shortfall (not plan-wide total).
            try:
                from core.signals import IngredientUncoverablePayload  # noqa: PLC0415
                _emitted_iids: set = set()
                for _co_iid, _co_info in _coverage_by_ing.items():
                    if _co_info.get("status") != "nominal_uncoverable":
                        continue
                    if _co_iid in _emitted_iids:
                        continue
                    _emitted_iids.add(_co_iid)
                    _uo_name = _ing_name_by_id.get(_co_iid, str(_co_iid))
                    _uo_unit = next(
                        (_od.get("unit", "g") for _od in new_orders
                         if int(_od.get("ingredient_id", 0)) == _co_iid),
                        "g",
                    )
                    _sn = float(_co_info.get("short_nominal", 0.0))
                    self.emit(
                        SignalType.INGREDIENT_UNCOVERABLE,
                        IngredientUncoverablePayload(
                            ingredient_id=_co_iid,
                            ingredient_name=_uo_name,
                            short_qty=_sn,
                            unit=_uo_unit,
                            reason=(
                                f"No lead-feasible / in-stock supply: "
                                f"{_sn:.0f}{_uo_unit} short on time"
                            ),
                        ).model_dump(),
                        dedup_key=f"uncoverable:{_co_iid}",
                    )
            except Exception:  # noqa: BLE001
                logger.warning("build_procurement_plan: failed to emit INGREDIENT_UNCOVERABLE signals")

            run = ProcurementPlanRun(
                created_at=now,
                horizon_days=horizon_days,
                items_planned=total_active,
                method=plan_method,
                coverage_ok=1 if _reconciled_coverage_ok else 0,
                total_short=_reconciled_total_short,
                reliability_premium=plan_reliability_premium,
                exposed_value_baseline=plan_exposed_value_baseline,
                exposed_value_protected=plan_exposed_value_protected,
                coverage_depends_on_planned_orders=1 if _coverage_depends_on_planned else 0,
                late_delivery_coverage_ok=1 if _late_delivery_coverage_ok else 0,
                cash_optimal_cost=float(getattr(_solution, "cash_optimal_cost", 0.0) or 0.0),
                robust_requested=1 if bool(getattr(_solution, "robust_requested", False)) else 0,
                robust_applied=1 if bool(getattr(_solution, "robust_applied", False)) else 0,
                robust_status=str(getattr(_solution, "robust_status", "") or ""),
                robust_premium=float(getattr(_solution, "robust_premium", 0.0) or 0.0),
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
                    # Update coverage fields from FEFO result.
                    _co = _coverage_by_ing.get(int(row.ingredient_id), {})
                    row.coverage_status = _co.get("status", "covered")
                    row.short_nominal = float(_co.get("short_nominal", 0.0))
                    row.short_delayed = float(_co.get("short_delayed", 0.0))

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
                    PlannedOrder.status.in_(["planned", "at_risk", "uncoverable"]),
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
                    "order_date": po.order_date,        # W2: detect late/at-risk rows
                    "status": po.status,                # D: propagate urgency to PO
                    "plan_run_id": po.plan_run_id,      # A: net-once — find plan's created_at
                }
                for po in due
            ]
            # A: Batch-resolve plan_run created_at so we only net POs the plan
            # hadn't already credited.  One query across all distinct plan_run_ids.
            _run_ids = {d["plan_run_id"] for d in due_data if d["plan_run_id"]}
            _run_created_at: Dict[int, float] = {}
            if _run_ids:
                for _run in session.query(ProcurementPlanRun).filter(
                    ProcurementPlanRun.id.in_(_run_ids)
                ).all():
                    _run_created_at[_run.id] = float(_run.created_at or 0.0)
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
        # Planned rows whose need is already fully covered by an in-flight PO —
        # marked 'superseded' so they never linger as zombie at_risk duplicates.
        resolved_ids: List[int] = []
        for (supplier_id, _delivery_day), group in groups.items():
            # WS1/WS4/WS5: Identity-based netting + cross-sweep consolidation.
            #
            # NETTING RULE (replaces created_at >= plan_built_at):
            #   1. If PurchaseOrderLine.planned_order_id == this planned order's id,
            #      a PO line already exists for this exact row → resolved (FK check).
            #   2. Otherwise, only net POs created STRICTLY AFTER the plan was built
            #      (created_at > plan_built_at, not >=).  Same-tick POs were already
            #      credited by inbound_by_day in the MILP and must NOT be double-netted.
            #
            # CONSOLIDATION (WS4): before creating a new PO, look for an existing open
            # PO for the same (supplier_id, delivery_day).  If found, append lines to it
            # (no second delivery charge).  If not found, create a new PO with one fee.
            lines = []
            placed_ids = []
            for po_data in group:
                ing_id = po_data["ingredient_id"]
                planned_order_id = int(po_data["id"])

                # --- FK identity check (bulletproof, same-tick safe) ---
                session2 = self.db_session_factory()
                try:
                    _already_linked = (
                        session2.query(PurchaseOrderLine)
                        .filter(PurchaseOrderLine.planned_order_id == planned_order_id)
                        .first()
                    )
                finally:
                    session2.close()

                if _already_linked is not None:
                    # A PO line already exists for this planned order — no duplicate.
                    resolved_ids.append(planned_order_id)
                    logger.debug(
                        "execute_due_planned_orders: planned row %s already has a PO line "
                        "(po_id=%s) — resolving without new order.",
                        planned_order_id, _already_linked.po_id,
                    )
                    continue

                # --- Timestamp net for POs placed strictly AFTER the plan ran ---
                _run_id = po_data.get("plan_run_id")
                _plan_built_at = _run_created_at.get(_run_id) if _run_id else None

                session2b = self.db_session_factory()
                try:
                    _inbound_q = (
                        session2b.query(_sa_func.sum(PurchaseOrderLine.qty))
                        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                        .filter(
                            PurchaseOrderLine.ingredient_id == ing_id,
                            PurchaseOrder.status.in_(("proposed", "approved", "placed")),
                            # Null planned_order_id only — lines linked to a planned order
                            # are accounted via the FK check above.
                            PurchaseOrderLine.planned_order_id.is_(None),
                        )
                    )
                    if _plan_built_at is not None:
                        # Strict >: same-tick POs were already credited by inbound_by_day
                        # in the MILP; re-netting them here was the primary cause of the
                        # mascarpone drop (planned top-up order suppressed when plan-build
                        # and PO creation share a sim tick).
                        _inbound_q = _inbound_q.filter(
                            PurchaseOrder.created_at > _plan_built_at
                        )
                    inbound_qty_row = _inbound_q.scalar()
                finally:
                    session2b.close()
                inbound_qty = float(inbound_qty_row or 0.0)
                shortfall = float(po_data["qty"] or 0.0) - inbound_qty
                if shortfall <= 0:
                    # Covered by a truly-new post-plan PO (not from this planned row).
                    resolved_ids.append(planned_order_id)
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
                    "qty": shortfall,
                    "unit": unit,
                    "unit_price": float(unit_price),
                    "planned_order_id": planned_order_id,  # WS1: identity link
                })
                placed_ids.append(planned_order_id)

            if not lines:
                continue

            # W2: At-risk rows have order_date < now (the order-by window already
            # passed).  Pass planned_delivery=None so _place uses now + lead_time
            # (deliver-ASAP), rather than the stale future date the MILP assumed.
            # On-time rows keep their planned delivery date (A4 behaviour).
            group_late = any(
                float(d.get("order_date") or now + 1) < now for d in group
            )
            planned_delivery = None if group_late else min(
                float(d["delivery_date"] or 0.0) for d in group
            )
            dc = delivery_charges.get(supplier_id, 0.0)

            # D: Derive group urgency from source planned rows — "uncoverable" wins
            # over "at_risk"; stored on the PO so the Ordered section can badge it.
            _group_statuses = {d.get("status") for d in group}
            if "uncoverable" in _group_statuses:
                _group_urgency: Optional[str] = "uncoverable"
            elif "at_risk" in _group_statuses or group_late:
                _group_urgency = "at_risk"
            else:
                _group_urgency = None

            # WS4: Cross-sweep consolidation — if an open PO already exists for this
            # (supplier, delivery_day), merge into it rather than creating a new PO
            # (which would add a second delivery charge for the same arrival slot).
            _existing_po_id: Optional[int] = None
            try:
                _s_lookup = self.db_session_factory()
                try:
                    _existing_po = (
                        _s_lookup.query(PurchaseOrder)
                        .filter(
                            PurchaseOrder.supplier_id == supplier_id,
                            PurchaseOrder.status.in_(("proposed", "approved", "placed")),
                            PurchaseOrder.expected_delivery.isnot(None),
                        )
                        .all()
                    )
                    for _ep in _existing_po:
                        _ep_day = int(float(_ep.expected_delivery or 0.0) // SECONDS_PER_DAY)
                        if _ep_day == _delivery_day:
                            _existing_po_id = int(_ep.id)
                            break
                finally:
                    _s_lookup.close()
            except Exception:  # noqa: BLE001
                logger.warning("execute_due_planned_orders: consolidation lookup failed; will create new PO.")

            if _existing_po_id is not None:
                # Merge into existing PO — no second delivery charge.
                logger.info(
                    "execute_due_planned_orders: consolidating %d line(s) into existing PO #%s "
                    "(supplier %s, delivery_day %s) — no additional delivery fee.",
                    len(lines), _existing_po_id, supplier_id, _delivery_day,
                )
                merged_ok = self.procurement.add_lines_to_po(
                    po_id=_existing_po_id,
                    lines=lines,
                    created_by=self.name,
                )
                if not merged_ok:
                    logger.warning(
                        "execute_due_planned_orders: merge into PO #%s failed; falling back to new PO.",
                        _existing_po_id,
                    )
                    _existing_po_id = None  # fall through to create_po below

                if merged_ok:
                    # Mark source planned rows as placed (merged into existing PO).
                    for row_id in placed_ids:
                        _s_mark = self.db_session_factory()
                        try:
                            _row = _s_mark.get(PlannedOrder, row_id)
                            if _row:
                                _row.status = "placed"
                                _s_mark.commit()
                        except Exception:  # noqa: BLE001
                            _s_mark.rollback()
                        finally:
                            _s_mark.close()
                    count += 1
                    continue  # don't fall through to create_po

            try:
                created_po = self.procurement.create_po(
                    supplier_id=supplier_id,
                    lines=lines,
                    created_by=self.name,
                    planned_delivery=planned_delivery,  # A4/W2: ASAP for late, plan date for on-time
                    delivery_charge=dc,                 # include delivery fee in total_cost
                    urgency=_group_urgency,             # D: propagate at_risk/uncoverable label
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "execute_due_planned_orders: create_po failed for supplier %s", supplier_id
                )
                continue

            # Fix A: re-read the committed PO status before deciding what to do with
            # the source PlannedOrder rows.  create_po expunges the object before
            # _place runs, so the returned object may not reflect the final status.
            # When the PO went to approval (status="proposed", over the threshold) we
            # mark source rows "superseded" — a real pending PO now represents them, so
            # they must leave the planned list immediately.  Marking them "placed"
            # unconditionally was the primary cause of the at_risk+proposed-PO duplicate.
            target_status = "placed"
            if created_po is not None:
                session_chk = self.db_session_factory()
                try:
                    fresh_po = session_chk.get(PurchaseOrder, created_po.id)
                    if fresh_po is not None and fresh_po.status == "proposed":
                        target_status = "superseded"
                        logger.info(
                            "execute_due_planned_orders: PO #%s is pending approval → "
                            "marking source planned rows superseded (not placed).",
                            created_po.id,
                        )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "execute_due_planned_orders: could not re-read PO %s status; "
                        "defaulting to 'placed'.",
                        getattr(created_po, "id", "?"),
                    )
                finally:
                    session_chk.close()

            for row_id in placed_ids:
                session4 = self.db_session_factory()
                try:
                    row = session4.get(PlannedOrder, row_id)
                    if row:
                        row.status = target_status
                        session4.commit()
                except Exception:  # noqa: BLE001
                    session4.rollback()
                finally:
                    session4.close()

            count += 1  # one PO created per supplier-day group

        # Resolve planned rows already covered by in-flight POs (no zombie duplicates).
        for row_id in resolved_ids:
            session5 = self.db_session_factory()
            try:
                row = session5.get(PlannedOrder, row_id)
                if row and row.status in ("planned", "at_risk"):
                    row.status = "superseded"
                    session5.commit()
            except Exception:  # noqa: BLE001
                session5.rollback()
            finally:
                session5.close()

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
