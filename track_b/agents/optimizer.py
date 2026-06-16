"""Inventory Optimizer agent — the decisions (02 §B4.2).

Consumes demand-driven threshold signals to size reorders, toggle menu items,
and turn near-expiry lots into promos (§18.8). Writes ``purchase_orders`` (via
Procurement), ``menu_toggles`` (+ ``menu_items.active``) and ``promotions``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from core import config
from core.agent_base import BaseAgent
from core.models import (
    Ingredient,
    InventoryLevel,
    MenuItem,
    MenuToggle,
    OrderLine,
    Promotion,
    PurchaseOrder,
    PurchaseOrderLine,
    Recipe,
    RecipeLine,
    Supplier,
    SupplierCatalog,
)
from core.signals import SignalType

# Signal groups this agent listens to (02 §B4.2).
GROUPS = ["inventory", "procurement"]


class InventoryOptimizer(BaseAgent):
    """Reorder, menu-toggle, and expiry→promo decisions driven by demand."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Any,
        name: str = "optimizer",
        ws_broadcast: Any = None,
        procurement: Any = None,
        approvals: Any = None,
    ):
        super().__init__(bus, db_session_factory, name, ws_broadcast=ws_broadcast)
        self.subscribe(GROUPS)
        self.procurement = procurement
        self.approvals = approvals
        # menu_item_id -> ingredient_id that triggered its disable, so the
        # reorder sweep knows which ingredient to watch for re-enabling
        # (menu_toggles has no ingredient_id column; this is in-process only).
        self._toggle_cause: Dict[int, int] = {}

    def attach_procurement(self, procurement: Any) -> None:
        self.procurement = procurement

    def attach_approvals(self, approvals: Any) -> None:
        self.approvals = approvals

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
        elif signal.type == SignalType.EXPIRY_RISK.value:
            self._propose_promo(signal.payload or {})

    # -- reorder (§18.8) ------------------------------------------------------

    def reorder_check(self) -> None:
        """Periodic reorder sweep: ``on_hand ≤ reorder_point`` → PO (§18.8)."""
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
                self._reenable(menu_item_id)

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

            # Don't pile on another PO while one is already in flight for this
            # ingredient (proposed/approved/placed, i.e. not yet delivered).
            outstanding = (
                session.query(PurchaseOrderLine)
                .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
                .filter(
                    PurchaseOrderLine.ingredient_id == ingredient_id,
                    PurchaseOrder.status.in_(("proposed", "approved", "placed")),
                )
                .first()
            )
            if outstanding is not None:
                return

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
                }
                for c in catalog
            ]
            lead_by_supplier = {
                s.id: float(s.lead_time_days or 1.0)
                for s in session.query(Supplier)
                .filter(Supplier.id.in_([c["supplier_id"] for c in specs]))
                .all()
            } if specs else {}
        finally:
            session.close()

        candidate = self._choose_supplier(specs, lead_by_supplier)
        if candidate is None:
            self.log_event(
                "reorder_failed",
                f"No available supplier for ingredient {ingredient_id}; reorder skipped.",
                {"ingredient_id": ingredient_id},
            )
            return

        needed = par_level - on_hand
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
        session = self.db_session_factory()
        try:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            supplier_leads = [
                float(s.lead_time_days or 1.0)
                for s in session.query(Supplier)
                .join(SupplierCatalog, SupplierCatalog.supplier_id == Supplier.id)
                .filter(SupplierCatalog.ingredient_id == ingredient_id)
                .all()
            ]
            resupply_eta_s = (min(supplier_leads) if supplier_leads else 2.0) * 86400.0

            items = (
                session.query(MenuItem)
                .join(Recipe, Recipe.menu_item_id == MenuItem.id)
                .join(RecipeLine, RecipeLine.recipe_id == Recipe.id)
                .filter(RecipeLine.ingredient_id == ingredient_id, MenuItem.active == 1)
                .all()
            )
            item_specs = [
                {"id": mi.id, "dine_in_price": mi.dine_in_price or 0.0} for mi in items
            ]
        finally:
            session.close()

        if len(item_specs) < 2:
            return  # nothing to conserve by disabling the only dish using it
        if projected_runout - self.sim_time >= resupply_eta_s:
            return  # resupply will land before it actually runs out

        scored = [
            (self._margin_x_velocity(spec["id"], spec["dine_in_price"], ingredient_id), spec["id"])
            for spec in item_specs
        ]
        scored.sort(key=lambda t: t[0])
        target_id = scored[0][1]
        self._disable(target_id, ingredient_id)

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
        now = self.sim_time
        session = self.db_session_factory()
        try:
            item = session.get(MenuItem, menu_item_id)
            if item is None or item.active == 0:
                return
            item.active = 0
            session.add(
                MenuToggle(
                    menu_item_id=menu_item_id,
                    action="disable",
                    reason=f"ingredient {ingredient_id} at risk of running out before resupply",
                    triggered_by=self.name,
                    sim_time=now,
                    active=1,
                )
            )
            session.commit()
        finally:
            session.close()

        self._toggle_cause[menu_item_id] = ingredient_id
        self.emit(
            SignalType.MENU_TOGGLE,
            {
                "menu_item_id": menu_item_id,
                "action": "disable",
                "reason": f"ingredient {ingredient_id} at risk of running out before resupply",
            },
            dedup_key=f"toggle:{menu_item_id}",
        )
        self.broadcast("menu_toggled", {"menu_item_id": menu_item_id, "action": "disable"})
        self.log_event(
            "menu_toggle",
            f"Disabled menu item {menu_item_id}: ingredient {ingredient_id} at risk.",
            {"menu_item_id": menu_item_id, "ingredient_id": ingredient_id},
        )

    def _reenable(self, menu_item_id: int) -> None:
        now = self.sim_time
        session = self.db_session_factory()
        try:
            item = session.get(MenuItem, menu_item_id)
            if item is None or item.active == 1:
                self._toggle_cause.pop(menu_item_id, None)
                return
            item.active = 1
            (
                session.query(MenuToggle)
                .filter(MenuToggle.menu_item_id == menu_item_id, MenuToggle.active == 1)
                .update({MenuToggle.active: 0})
            )
            session.add(
                MenuToggle(
                    menu_item_id=menu_item_id,
                    action="enable",
                    reason="resupplied above reorder point",
                    triggered_by=self.name,
                    sim_time=now,
                    active=1,
                )
            )
            session.commit()
        finally:
            session.close()

        self._toggle_cause.pop(menu_item_id, None)
        self.emit(
            SignalType.MENU_TOGGLE,
            {"menu_item_id": menu_item_id, "action": "enable", "reason": "resupplied above reorder point"},
            dedup_key=f"toggle:{menu_item_id}",
        )
        self.broadcast("menu_toggled", {"menu_item_id": menu_item_id, "action": "enable"})
        self.log_event(
            "menu_toggle",
            f"Re-enabled menu item {menu_item_id}: stock recovered above reorder point.",
            {"menu_item_id": menu_item_id},
        )

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
