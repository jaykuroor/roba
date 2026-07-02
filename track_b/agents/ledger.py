"""Inventory Ledger agent — the source of truth for stock (02 §B4.1).

Maintains stock as an append-only ledger and raises stock / expiry / waste
signals. It is the **only** writer of ``inventory_ledger / inventory_lots /
inventory_levels`` (§19.4) and the single component that depletes inventory.

Depletion (§18.4, implemented exactly): for each sold order line / cooked
batch and each ``recipe_line``, ``used = qty × recipe_qty / yield_factor``;
deplete FIFO across ``inventory_lots`` (oldest ``expiry`` first); append
``inventory_ledger(reason=sale_depletion|batch_depletion, delta=-used,
ref_id, balance_after)``; update the lot + ``inventory_levels.on_hand_cached``;
broadcast ``inventory_updated``. ``on_hand`` is always the ledger sum (the
cache is a convenience kept in lockstep).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core import config
from core.agent_base import BaseAgent
from core.models import Ingredient
from core.models import InventoryLedger as InventoryLedgerRow
from core.models import (
    InventoryLevel,
    InventoryLot,
    MenuItem,
    PurchaseOrder,
    PurchaseOrderLine,
    Recipe,
    RecipeLine,
    Signal,
    Supplier,
    SupplierCatalog,
    WasteEvent,
)
from core.signals import SignalType

# Signal groups this agent listens to (02 §B4.1).
GROUPS = ["inventory"]


class InventoryLedger(BaseAgent):
    """Deterministic stock ledger; depletion, thresholds, receipts, waste."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Any,
        name: str = "ledger",
        ws_broadcast: Any = None,
        inventory_signal_policy: Any = None,
    ):
        super().__init__(bus, db_session_factory, name, ws_broadcast=ws_broadcast)
        self.subscribe(GROUPS)
        self.inventory_signal_policy = inventory_signal_policy
        # Rolling (sim_time, qty) usage ring buffer per ingredient, used only
        # to estimate ``projected_runout`` / ``projected_usage_before_expiry``
        # — a light local analogue of the formatter's item-velocity buffer,
        # scoped to ingredient depletion rather than dish sales.
        self._usage: Dict[int, List[Tuple[float, float]]] = {}

    # -- signal handling ----------------------------------------------------

    def on_signal(self, signal: Signal) -> None:
        """React to cooked batches and typed voice inventory routes (§18.4).

        PO deliveries are driven directly by Procurement calling
        :meth:`receive`, not via the bus.
        """
        if signal.type == SignalType.BATCH_DECISION.value:
            payload = signal.payload or {}
            if payload.get("decision") == "cook":
                self._deplete_for_recipe(
                    menu_item_id=payload["menu_item_id"],
                    qty=float(payload.get("qty") or 0.0),
                    reason="batch_depletion",
                    ref_id=payload.get("batch_definition_id"),
                )
            return

        if signal.type == SignalType.DEMAND_FORECAST.value:
            return

        if signal.type == SignalType.INVENTORY_RECEIPT_REPORTED.value:
            self._apply_reported_receipt(signal.payload or {})
            return

        if signal.type == SignalType.INVENTORY_COUNT_REPORTED.value:
            self._apply_reported_count(signal.payload or {})
            return

        if signal.type == SignalType.INGREDIENT_SHORTAGE_REPORTED.value:
            self._apply_reported_shortage(signal.payload or {})
            return

        if signal.type == SignalType.USER_FACT.value:
            payload = signal.payload or {}
            if payload.get("intent") in ("record_receipt", "add_inventory_count"):
                # Legacy migration path only; typed voice routes own durable
                # writes now, while old USER_FACT consumers keep a display shim.
                self._surface_user_fact(payload)
            return

    # -- order-line callback (§10) -----------------------------------------

    def handle_order_line(self, line: Any) -> None:
        """Deplete ingredients for one sold ``order_line`` (FIFO, §18.4).

        Registered with ``bus.register_order_line_handler`` so the core POS sim
        drives depletion without putting order lines on the signal bus.
        Voided lines never reach here (the formatter only forwards ``sold``
        lines; voided ones become a ``cancelled_order`` waste event instead).
        """
        if getattr(line, "status", "sold") != "sold":
            return
        self._deplete_for_recipe(
            menu_item_id=line.menu_item_id,
            qty=float(line.qty or 0.0),
            reason="sale_depletion",
            ref_id=line.id,
        )

    # -- depletion core (§18.4) ---------------------------------------------

    def _deplete_for_recipe(
        self, menu_item_id: int, qty: float, reason: str, ref_id: Optional[int]
    ) -> None:
        """Deplete every ``recipe_line`` ingredient for ``qty`` units of a dish."""
        if qty <= 0:
            return
        now = self.sim_time
        session = self.db_session_factory()
        try:
            recipe = (
                session.query(Recipe)
                .filter(Recipe.menu_item_id == menu_item_id)
                .first()
            )
            if recipe is None:
                return
            lines = (
                session.query(RecipeLine)
                .filter(RecipeLine.recipe_id == recipe.id)
                .all()
            )
            specs = [
                {"ingredient_id": rl.ingredient_id, "qty": rl.qty}
                for rl in lines
            ]
        finally:
            session.close()

        for spec in specs:
            level_yield = self._yield_factor(spec["ingredient_id"])
            used = qty * float(spec["qty"] or 0.0) / max(level_yield, 1e-9)
            if used <= 0:
                continue
            self._deplete_fifo(spec["ingredient_id"], used, reason, ref_id, now)
            try:
                from core.availability import recompute_availability
                recompute_availability(
                    self.db_session_factory,
                    self.bus,
                    self.broadcast,
                    changed_ingredient_ids=[spec["ingredient_id"]],
                    agent_name="ledger_sale",
                )
            except Exception as _exc:
                import logging
                logging.getLogger(__name__).warning("availability cascade failed: %s", _exc)

    def _yield_factor(self, ingredient_id: int) -> float:
        session = self.db_session_factory()
        try:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            if level is None or not level.yield_factor:
                return 1.0
            return float(level.yield_factor)
        finally:
            session.close()

    def _deplete_fifo(
        self,
        ingredient_id: int,
        qty: float,
        reason: str,
        ref_id: Optional[int],
        now: float,
    ) -> None:
        """Deplete ``qty`` of ``ingredient_id`` FIFO (oldest ``expiry`` first)."""
        session = self.db_session_factory()
        try:
            level = self._get_or_create_level(session, ingredient_id)
            remaining = qty
            lots = (
                session.query(InventoryLot)
                .filter(
                    InventoryLot.ingredient_id == ingredient_id,
                    InventoryLot.status == "active",
                )
                .order_by(InventoryLot.expiry_date.asc())
                .all()
            )
            balance_after = float(level.on_hand_cached or 0.0)
            for lot in lots:
                if remaining <= 0:
                    break
                take = min(lot.qty_on_hand, remaining)
                if take <= 0:
                    continue
                lot.qty_on_hand -= take
                if lot.qty_on_hand <= 1e-9:
                    lot.qty_on_hand = 0.0
                    lot.status = "depleted"
                remaining -= take
                balance_after -= take
                session.add(
                    InventoryLedgerRow(
                        ingredient_id=ingredient_id,
                        lot_id=lot.id,
                        delta_qty=-take,
                        reason=reason,
                        ref_id=ref_id,
                        sim_time=now,
                        balance_after=balance_after,
                    )
                )

            # Stockout: more demanded than any lot could supply — still record
            # the shortfall against the ledger (no lot to attribute it to) so
            # the on-hand cache and the ledger sum never diverge.
            if remaining > 1e-9:
                balance_after -= remaining
                session.add(
                    InventoryLedgerRow(
                        ingredient_id=ingredient_id,
                        lot_id=None,
                        delta_qty=-remaining,
                        reason=reason,
                        ref_id=ref_id,
                        sim_time=now,
                        balance_after=balance_after,
                    )
                )

            level.on_hand_cached = balance_after
            session.commit()
        finally:
            session.close()

        self._record_usage(ingredient_id, now, qty)
        self.broadcast(
            "inventory_updated", {"ingredient_id": ingredient_id, "on_hand": balance_after}
        )
        self._check_thresholds(ingredient_id, balance_after, now)

    @staticmethod
    def _get_or_create_level(session: Any, ingredient_id: int) -> InventoryLevel:
        level = (
            session.query(InventoryLevel)
            .filter(InventoryLevel.ingredient_id == ingredient_id)
            .first()
        )
        if level is None:
            level = InventoryLevel(
                ingredient_id=ingredient_id,
                par_level=0.0,
                reorder_point=0.0,
                safety_stock=0.0,
                yield_factor=1.0,
                on_hand_cached=0.0,
            )
            session.add(level)
            session.flush()
        return level

    # -- usage-rate tracking (for projected_runout / expiry estimates) -----

    def _record_usage(self, ingredient_id: int, sim_time: float, qty: float) -> None:
        buffer = self._usage.setdefault(ingredient_id, [])
        buffer.append((sim_time, qty))
        cutoff = sim_time - config.VELOCITY_WINDOW_SIM_S
        while buffer and buffer[0][0] < cutoff:
            buffer.pop(0)

    def _usage_rate(self, ingredient_id: int) -> float:
        """Ingredient usage rate (qty per sim-second) over the rolling window."""
        buffer = self._usage.get(ingredient_id)
        if not buffer:
            return 0.0
        total = sum(q for _t, q in buffer)
        return total / config.VELOCITY_WINDOW_SIM_S

    # -- threshold signals (§18.4 / §18.8) ----------------------------------

    def _check_thresholds(self, ingredient_id: int, on_hand: float, now: float) -> None:
        session = self.db_session_factory()
        try:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            safety_stock = float(level.safety_stock or 0.0) if level else 0.0
            unit = self._unit_for_session(session, ingredient_id)
            forecast_usage, affected, horizon_end = self._projected_remaining_usage(
                session, ingredient_id, now
            )
            resupply_at = self._fastest_resupply_at(session, ingredient_id, now)
        finally:
            session.close()

        rate = self._usage_rate(ingredient_id)
        far_future = now + config.EXPIRY_WINDOW_SIM_S * 2
        forecast_span = max(float(horizon_end or now) - now, 1.0)
        forecast_rate = forecast_usage / forecast_span if forecast_usage > 0 else 0.0
        projected_rate = max(rate, forecast_rate)
        projected_runout = (
            now + max(on_hand, 0.0) / projected_rate
            if projected_rate > 0
            else far_future
        )
        projected_balance = on_hand - forecast_usage

        if on_hand <= 0:
            affected = affected or self._affected_menu_items(ingredient_id)
            self._emit_shortage_signal(
                SignalType.STOCKOUT_RISK,
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "projected_runout": now,
                    "affected_items": affected,
                },
                f"stockout:{ingredient_id}",
            )
            self.log_event(
                "stockout_risk",
                f"Ingredient {ingredient_id} is at or below zero on-hand ({on_hand:.1f}).",
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "forecast_usage": forecast_usage,
                    "projected_balance": projected_balance,
                },
            )
        elif projected_balance <= 0 and projected_runout < resupply_at:
            affected = affected or self._affected_menu_items(ingredient_id)
            self._emit_shortage_signal(
                SignalType.STOCKOUT_RISK,
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "projected_runout": projected_runout,
                    "affected_items": affected,
                },
                f"stockout:{ingredient_id}",
            )
            self.log_event(
                "stockout_risk",
                f"Ingredient {ingredient_id} is projected to run out before resupply.",
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "forecast_usage": forecast_usage,
                    "projected_balance": projected_balance,
                    "projected_runout": projected_runout,
                    "resupply_at": resupply_at,
                },
            )
        elif safety_stock > 0 and projected_balance <= safety_stock:
            self._emit_shortage_signal(
                SignalType.LOW_STOCK,
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "threshold": safety_stock,
                    "projected_runout": projected_runout,
                    "unit": unit,
                },
                f"low_stock:{ingredient_id}",
            )
            self.log_event(
                "low_stock",
                f"Ingredient {ingredient_id} projected balance ({projected_balance:.1f}) at/below safety stock ({safety_stock:.1f}).",
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "safety_stock": safety_stock,
                    "forecast_usage": forecast_usage,
                    "projected_balance": projected_balance,
                },
            )

    def _emit_shortage_signal(
        self,
        signal_type: SignalType,
        payload: Dict[str, Any],
        dedup_key: str,
    ) -> None:
        enabled = True
        if self.inventory_signal_policy is not None:
            enabled = bool(self.inventory_signal_policy.shortage_signals_enabled)
        if enabled:
            self.emit(signal_type, payload, dedup_key=dedup_key)
            return
        self.log_event(
            "inventory_signal_muted",
            f"Muted {signal_type.value} for ingredient {payload.get('ingredient_id')}.",
            {
                "signal_type": signal_type.value,
                "dedup_key": dedup_key,
                "payload": payload,
            },
        )

    def _projected_remaining_usage(
        self,
        session: Any,
        ingredient_id: int,
        now: float,
    ) -> Tuple[float, List[int], float]:
        usage = 0.0
        affected: set[int] = set()
        horizon_end = now
        try:
            forecasts = self.bus.live(type=SignalType.DEMAND_FORECAST)
        except Exception:  # pragma: no cover - defensive for bus test doubles.
            forecasts = []
        for sig in forecasts:
            payload = sig.payload or {}
            window = payload.get("window") or {}
            try:
                end = float(window.get("end"))
                start = float(window.get("start", sig.created_at or now))
                menu_item_id = int(payload.get("menu_item_id"))
                demand_qty = float(payload.get("qty") or 0.0)
            except (TypeError, ValueError):
                continue
            if end <= now or demand_qty <= 0:
                continue
            fraction = 1.0
            if end > start and now > start:
                fraction = max(0.0, min(1.0, (end - now) / (end - start)))
            ingredient_qty = self._ingredient_qty_for_menu_item(
                session, menu_item_id, ingredient_id
            )
            if ingredient_qty <= 0:
                continue
            usage += demand_qty * fraction * ingredient_qty
            affected.add(menu_item_id)
            horizon_end = max(horizon_end, end)
        return usage, sorted(affected), horizon_end

    def _ingredient_qty_for_menu_item(
        self,
        session: Any,
        menu_item_id: int,
        ingredient_id: int,
    ) -> float:
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
            self._yield_factor(ingredient_id), 0.0001
        )

    def _fastest_resupply_at(self, session: Any, ingredient_id: int, now: float) -> float:
        candidates: List[float] = []
        outstanding = (
            session.query(PurchaseOrder.expected_delivery)
            .join(PurchaseOrderLine, PurchaseOrderLine.po_id == PurchaseOrder.id)
            .filter(
                PurchaseOrderLine.ingredient_id == ingredient_id,
                PurchaseOrder.status.in_(("approved", "placed")),
                PurchaseOrder.expected_delivery.isnot(None),
            )
            .all()
        )
        for (eta,) in outstanding:
            if eta is not None and float(eta) >= now:
                candidates.append(float(eta))

        catalog = (
            session.query(SupplierCatalog.supplier_id)
            .filter(
                SupplierCatalog.ingredient_id == ingredient_id,
                SupplierCatalog.availability != "out",
            )
            .all()
        )
        supplier_ids = [row[0] for row in catalog]
        if supplier_ids:
            suppliers = session.query(Supplier).filter(Supplier.id.in_(supplier_ids)).all()
            for supplier in suppliers:
                candidates.append(now + float(supplier.lead_time_days or 1.0) * 86400.0)

        return min(candidates) if candidates else now + config.EXPIRY_WINDOW_SIM_S * 2

    def _affected_menu_items(self, ingredient_id: int) -> List[int]:
        session = self.db_session_factory()
        try:
            rows = (
                session.query(MenuItem.id)
                .join(Recipe, Recipe.menu_item_id == MenuItem.id)
                .join(RecipeLine, RecipeLine.recipe_id == Recipe.id)
                .filter(RecipeLine.ingredient_id == ingredient_id, MenuItem.active == 1)
                .all()
            )
            return [r[0] for r in rows]
        finally:
            session.close()

    def _unit(self, ingredient_id: int) -> str:
        session = self.db_session_factory()
        try:
            return self._unit_for_session(session, ingredient_id)
        finally:
            session.close()

    @staticmethod
    def _unit_for_session(session: Any, ingredient_id: int) -> str:
        ing = session.get(Ingredient, ingredient_id)
        return ing.base_unit if ing is not None else "each"

    # -- expiry scan (§18.8) -------------------------------------------------

    def scan_expiry(self) -> None:
        """Expiry scan every ``EXPIRY_SCAN_SIM_S`` → ``EXPIRY_RISK`` / waste."""
        now = self.sim_time
        session = self.db_session_factory()
        try:
            lots = (
                session.query(InventoryLot)
                .filter(InventoryLot.status == "active")
                .all()
            )
            specs = [
                {
                    "id": lot.id,
                    "ingredient_id": lot.ingredient_id,
                    "qty_on_hand": lot.qty_on_hand,
                    "expiry_date": lot.expiry_date,
                    "purchase_price": lot.purchase_price,
                    "unit": lot.unit,
                }
                for lot in lots
            ]
        finally:
            session.close()

        for spec in specs:
            if spec["expiry_date"] is None:
                continue
            remaining = float(spec["expiry_date"]) - now
            if remaining <= 0:
                self._expire_lot(spec, now)
            elif remaining <= config.EXPIRY_WINDOW_SIM_S:
                self._raise_expiry_risk(spec, now, remaining)

    def _raise_expiry_risk(self, spec: Dict[str, Any], now: float, remaining: float) -> None:
        rate = self._usage_rate(spec["ingredient_id"])
        projected_usage = rate * remaining
        if projected_usage >= spec["qty_on_hand"]:
            return  # will be used up before it expires — no risk
        self.emit(
            SignalType.EXPIRY_RISK,
            {
                "ingredient_id": spec["ingredient_id"],
                "lot_id": spec["id"],
                "qty": spec["qty_on_hand"],
                "expiry": spec["expiry_date"],
                "projected_usage_before_expiry": projected_usage,
            },
            ttl=remaining,
            dedup_key=f"expiry:{spec['id']}",
        )

    def _expire_lot(self, spec: Dict[str, Any], now: float) -> None:
        qty = spec["qty_on_hand"]
        session = self.db_session_factory()
        try:
            lot = session.get(InventoryLot, spec["id"])
            if lot is None or lot.status != "active":
                return
            lot.status = "expired"
            level = self._get_or_create_level(session, spec["ingredient_id"])
            balance_after = float(level.on_hand_cached or 0.0) - qty
            level.on_hand_cached = balance_after
            session.add(
                InventoryLedgerRow(
                    ingredient_id=spec["ingredient_id"],
                    lot_id=spec["id"],
                    delta_qty=-qty,
                    reason="waste",
                    ref_id=spec["id"],
                    sim_time=now,
                    balance_after=balance_after,
                )
            )
            cost = qty * float(spec["purchase_price"] or 0.0)
            session.add(
                WasteEvent(
                    waste_type="expiry",
                    ingredient_id=spec["ingredient_id"],
                    menu_item_id=None,
                    lot_id=spec["id"],
                    qty=qty,
                    unit=spec["unit"],
                    cost=cost,
                    reason="lot expired unused",
                    sim_time=now,
                    source=self.name,
                )
            )
            session.commit()
        finally:
            session.close()

        self.broadcast(
            "inventory_updated", {"ingredient_id": spec["ingredient_id"], "on_hand": balance_after}
        )
        self.emit(
            SignalType.WASTE_EVENT,
            {
                "waste_type": "expiry",
                "ingredient_id": spec["ingredient_id"],
                "menu_item_id": None,
                "lot_id": spec["id"],
                "qty": qty,
                "unit": spec["unit"],
                "cost": cost,
                "reason": "lot expired unused",
            },
            groups=["inventory", "procurement", "human"],
        )
        self.log_event(
            "waste",
            f"Lot {spec['id']} expired unused ({qty:.1f} {spec['unit']}, cost {cost:.2f}).",
            {"ingredient_id": spec["ingredient_id"], "lot_id": spec["id"], "cost": cost},
        )

    # -- voice-driven ingredient waste (called by VoiceActions) -----------

    def apply_ingredient_waste(
        self,
        ingredient_id: int,
        qty: Optional[float] = None,
        *,
        all_stock: bool = False,
        waste_type: str = "spoilage",
        reason: str = "voice spoilage report",
    ) -> Dict[str, Any]:
        """Deplete ingredient stock and record waste.  Called by VoiceActions.

        Parameters
        ----------
        ingredient_id:
            The ingredient to deplete.
        qty:
            How much to deplete.  If None and ``all_stock=True``, depletes
            the entire on_hand_cached balance (i.e. sets to zero).
        all_stock:
            When True, interpret as "spoil all" — use the current on_hand balance.
        waste_type:
            One of "spoilage" | "overproduction" | "expiry" | "prep_error".
        reason:
            Human-readable reason stored in WasteEvent and ledger row.

        Returns
        -------
        dict with ingredient_id, on_hand_before, on_hand_after, depleted, unit.
        """
        now = self.sim_time
        session = self.db_session_factory()
        try:
            level = self._get_or_create_level(session, ingredient_id)
            on_hand_before = float(level.on_hand_cached or 0.0)
            ing = session.get(Ingredient, ingredient_id)
            unit = str(ing.base_unit or "each") if ing else "each"
            if all_stock or qty is None:
                depleted = on_hand_before
            else:
                depleted = min(float(qty), on_hand_before)
        finally:
            session.close()

        if depleted <= 0:
            return {
                "ingredient_id": ingredient_id,
                "on_hand_before": on_hand_before,
                "on_hand_after": on_hand_before,
                "depleted": 0.0,
                "unit": unit,
            }

        # Deplete FIFO via existing mechanism.
        self._deplete_fifo(ingredient_id, depleted, "waste", None, now)

        # Re-read balance after depletion.
        session = self.db_session_factory()
        try:
            level = self._get_or_create_level(session, ingredient_id)
            on_hand_after = float(level.on_hand_cached or 0.0)
            # Write a WasteEvent with ingredient_id set (unlike the dish-waste path).
            we = WasteEvent(
                waste_type=waste_type,
                ingredient_id=ingredient_id,
                menu_item_id=None,
                lot_id=None,
                qty=depleted,
                unit=unit,
                cost=None,
                reason=reason,
                sim_time=now,
                source="voice",
            )
            session.add(we)
            session.commit()
            we_id = we.id
        finally:
            session.close()

        self.broadcast(
            "inventory_updated", {"ingredient_id": ingredient_id, "on_hand": on_hand_after}
        )
        self.emit(
            SignalType.WASTE_EVENT,
            {
                "waste_type": waste_type,
                "ingredient_id": ingredient_id,
                "menu_item_id": None,
                "qty": depleted,
                "unit": unit,
                "reason": reason,
                "source": "voice",
                "waste_event_id": we_id,
            },
            source="voice",
            groups=["inventory", "procurement", "human"],
        )
        self.log_event(
            "waste",
            f"Voice spoilage: {depleted:.1f} {unit} of ingredient {ingredient_id} marked as {waste_type}.",
            {"ingredient_id": ingredient_id, "depleted": depleted, "on_hand_after": on_hand_after},
        )
        return {
            "ingredient_id": ingredient_id,
            "on_hand_before": on_hand_before,
            "on_hand_after": on_hand_after,
            "depleted": depleted,
            "unit": unit,
        }

    # -- voice-driven receipt (called by VoiceActions) ---------------------

    def apply_receipt(self, payload: Dict[str, Any]) -> None:
        """Record a voice-reported stock receipt directly, bypassing the bus.

        Delegates to ``_apply_reported_receipt`` which writes the lot + ledger
        row and then calls ``recompute_availability`` to cascade menu state.
        """
        self._apply_reported_receipt(payload)

    def apply_count(self, payload: Dict[str, Any]) -> None:
        """Record a voice-reported stock count (set-to) directly, bypassing the bus.

        Delegates to ``_apply_reported_count`` which writes the reconciliation
        ledger row and calls ``recompute_availability`` to cascade menu state.
        """
        self._apply_reported_count(payload)

    # -- receipts (called by Procurement) ----------------------------------

    def receive(self, po_id: int) -> None:
        """Create receipt lots + ledger entries for every line of a delivered
        PO (§B4.1 / §B4.4). ``po_id`` is the :class:`PurchaseOrder` id."""
        now = self.sim_time
        session = self.db_session_factory()
        try:
            po = session.get(PurchaseOrder, po_id)
            if po is None:
                return
            lines = (
                session.query(PurchaseOrderLine)
                .filter(PurchaseOrderLine.po_id == po_id)
                .all()
            )
            specs = [
                {
                    "ingredient_id": pl.ingredient_id,
                    "qty": pl.qty,
                    "unit": pl.unit,
                    "unit_price": pl.unit_price,
                }
                for pl in lines
            ]
            supplier_id = po.supplier_id
        finally:
            session.close()

        for spec in specs:
            self._receive_line(
                spec["ingredient_id"], spec["qty"], spec["unit"],
                spec["unit_price"], supplier_id, now, po_id,
            )

    def _receive_line(
        self,
        ingredient_id: int,
        qty: float,
        unit: str,
        unit_price: float,
        supplier_id: Optional[int],
        now: float,
        po_id: int,
    ) -> None:
        session = self.db_session_factory()
        try:
            ing = session.get(Ingredient, ingredient_id)
            shelf_life = float(ing.shelf_life_days or 5.0) if ing is not None else 5.0
            lot = InventoryLot(
                ingredient_id=ingredient_id,
                qty_on_hand=qty,
                unit=unit,
                purchase_price=unit_price,
                purchase_date=now,
                received_date=now,
                expiry_date=now + shelf_life * 86400.0,
                supplier_id=supplier_id,
                storage_location="main",
                status="active",
            )
            session.add(lot)
            session.flush()

            level = self._get_or_create_level(session, ingredient_id)
            balance_after = float(level.on_hand_cached or 0.0) + qty
            level.on_hand_cached = balance_after
            session.add(
                InventoryLedgerRow(
                    ingredient_id=ingredient_id,
                    lot_id=lot.id,
                    delta_qty=qty,
                    reason="receipt",
                    ref_id=po_id,
                    sim_time=now,
                    balance_after=balance_after,
                )
            )
            session.commit()
        finally:
            session.close()

        self.broadcast(
            "inventory_updated", {"ingredient_id": ingredient_id, "on_hand": balance_after}
        )
        self.log_event(
            "receipt",
            f"Received {qty:.1f} {unit} of ingredient {ingredient_id} (PO {po_id}).",
            {"ingredient_id": ingredient_id, "qty": qty, "po_id": po_id},
        )

    def _apply_reported_receipt(self, payload: Dict[str, Any]) -> None:
        ingredient_ref = str(payload.get("ingredient_ref") or "").strip()
        qty = float(payload.get("qty") or 0.0)
        unit = str(payload.get("unit") or "each")
        price = float(payload.get("price") or 0.0)
        now = self.sim_time
        if qty <= 0:
            return
        session = self.db_session_factory()
        try:
            ingredient = self._resolve_ingredient_from_payload(
                session, payload, unit=unit, create=True
            )
            if ingredient is None:
                return
            supplier = self._resolve_supplier_from_payload(session, payload, create=True)
            if supplier is not None:
                self._ensure_catalog(session, ingredient.id, supplier.id, unit, price)
            shelf_life = float(ingredient.shelf_life_days or 5.0)
            lot = InventoryLot(
                ingredient_id=ingredient.id,
                qty_on_hand=qty,
                unit=unit,
                purchase_price=price,
                purchase_date=now,
                received_date=now,
                expiry_date=now + shelf_life * 86400.0,
                supplier_id=supplier.id if supplier is not None else None,
                storage_location="main",
                status="active",
            )
            session.add(lot)
            session.flush()
            level = self._get_or_create_level(session, ingredient.id)
            balance_after = float(level.on_hand_cached or 0.0) + qty
            level.on_hand_cached = balance_after
            ledger = InventoryLedgerRow(
                ingredient_id=ingredient.id,
                lot_id=lot.id,
                delta_qty=qty,
                reason="receipt",
                ref_id=lot.id,
                sim_time=now,
                balance_after=balance_after,
            )
            session.add(ledger)
            session.commit()
            ingredient_id = int(ingredient.id)
        finally:
            session.close()
        self.broadcast("inventory_updated", {"ingredient_id": ingredient_id, "on_hand": balance_after})
        # Cascade: auto-enable dishes whose ingredients are now above threshold.
        try:
            from core.availability import recompute_availability
            recompute_availability(
                self.db_session_factory,
                self.bus,
                self.broadcast,
                changed_ingredient_ids=[ingredient_id],
                agent_name="ledger_receipt",
            )
        except Exception as _exc:
            import logging
            logging.getLogger(__name__).warning("availability cascade failed: %s", _exc)
        self.log_event(
            "receipt",
            f"Voice receipt recorded for {ingredient_ref or ingredient_id}; on-hand now {balance_after:.1f}.",
            {"ingredient_id": ingredient_id, "qty": qty, "unit": unit, "source": "voice"},
        )

    def _apply_reported_count(self, payload: Dict[str, Any]) -> None:
        qty = float(payload.get("qty") or 0.0)
        unit = str(payload.get("unit") or "each")
        now = self.sim_time
        session = self.db_session_factory()
        try:
            ingredient = self._resolve_ingredient_from_payload(
                session, payload, unit=unit, create=False
            )
            if ingredient is None:
                return
            level = self._get_or_create_level(session, ingredient.id)
            prior = float(level.on_hand_cached or 0.0)
            delta = qty - prior
            level.last_counted_at = now
            level.last_counted_qty = qty
            level.on_hand_cached = qty
            session.add(
                InventoryLedgerRow(
                    ingredient_id=ingredient.id,
                    lot_id=None,
                    delta_qty=delta,
                    reason="reconciliation",
                    ref_id=ingredient.id,
                    sim_time=now,
                    balance_after=qty,
                )
            )
            session.commit()
            ingredient_id = int(ingredient.id)
        finally:
            session.close()
        self.broadcast("inventory_updated", {"ingredient_id": ingredient_id, "on_hand": qty})
        # Cascade: auto-disable dishes whose ingredients are now at/below threshold.
        try:
            from core.availability import recompute_availability
            recompute_availability(
                self.db_session_factory,
                self.bus,
                self.broadcast,
                changed_ingredient_ids=[ingredient_id],
                agent_name="ledger_count",
            )
        except Exception as _exc:
            import logging
            logging.getLogger(__name__).warning("availability cascade failed: %s", _exc)
        self.log_event(
            "reconciliation",
            f"Voice count for ingredient {ingredient_id}: on-hand now {qty:.1f} (drift recorded).",
            {"ingredient_id": ingredient_id, "on_hand": qty, "delta": delta},
        )

    def _apply_reported_shortage(self, payload: Dict[str, Any]) -> None:
        session = self.db_session_factory()
        try:
            ingredient = self._resolve_ingredient_from_payload(
                session, payload, unit=payload.get("unit") or "each", create=False
            )
            if ingredient is None:
                return
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient.id)
                .first()
            )
            on_hand = float(level.on_hand_cached or 0.0) if level is not None else 0.0
            safety_stock = float(level.safety_stock or 0.0) if level is not None else 0.0
            unit = self._unit_for_session(session, ingredient.id)
            ingredient_id = int(ingredient.id)
        finally:
            session.close()
        severity = str(payload.get("severity") or "low")
        affected = self._affected_menu_items(ingredient_id)
        if severity in {"critical", "out", "stockout"} or on_hand <= 0:
            self._emit_shortage_signal(
                SignalType.STOCKOUT_RISK,
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "projected_runout": self.sim_time,
                    "affected_items": affected,
                },
                f"stockout:{ingredient_id}:voice",
            )
        else:
            self._emit_shortage_signal(
                SignalType.LOW_STOCK,
                {
                    "ingredient_id": ingredient_id,
                    "on_hand": on_hand,
                    "threshold": safety_stock,
                    "projected_runout": self.sim_time,
                    "unit": unit,
                },
                f"low_stock:{ingredient_id}:voice",
            )
        self.log_event(
            "manual_shortage",
            f"Voice shortage note for ingredient {ingredient_id}: {severity}.",
            {"ingredient_id": ingredient_id, "severity": severity, "payload": payload},
        )

    def _resolve_ingredient_from_payload(
        self,
        session: Any,
        payload: Dict[str, Any],
        unit: str,
        create: bool,
    ) -> Optional[Ingredient]:
        ingredient_id = payload.get("ingredient_id")
        if ingredient_id is not None:
            row = session.get(Ingredient, int(ingredient_id))
            if row is not None:
                return row
        ref = str(payload.get("ingredient_ref") or "").strip()
        if not ref:
            return None
        row = session.query(Ingredient).filter(Ingredient.name.ilike(ref)).first()
        if row is not None:
            return row
        row = session.query(Ingredient).filter(Ingredient.name.ilike(f"{ref}%")).first()
        if row is not None:
            return row
        if not create:
            return None
        row = Ingredient(
            name=ref,
            category="voice",
            base_unit=unit,
            perishable=1,
            shelf_life_days=5.0,
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _resolve_supplier_from_payload(
        session: Any,
        payload: Dict[str, Any],
        create: bool,
    ) -> Optional[Supplier]:
        supplier_id = payload.get("supplier_id")
        if supplier_id is not None:
            row = session.get(Supplier, int(supplier_id))
            if row is not None:
                return row
        ref = str(payload.get("supplier_ref") or "").strip()
        if not ref:
            return None
        row = session.query(Supplier).filter(Supplier.name.ilike(ref)).first()
        if row is not None:
            return row
        if not create:
            return None
        row = Supplier(
            name=ref,
            lead_time_days=1.0,
            reliability_score=0.8,
            min_order_value=0.0,
            contact="",
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _ensure_catalog(
        session: Any,
        ingredient_id: int,
        supplier_id: int,
        unit: str,
        price: float,
    ) -> None:
        existing = (
            session.query(SupplierCatalog)
            .filter(
                SupplierCatalog.ingredient_id == ingredient_id,
                SupplierCatalog.supplier_id == supplier_id,
            )
            .first()
        )
        if existing is not None:
            if price > 0:
                existing.current_price = price
            existing.availability = "in_stock"
            return
        session.add(
            SupplierCatalog(
                supplier_id=supplier_id,
                ingredient_id=ingredient_id,
                current_price=price,
                unit=unit,
                pack_size=1.0,
                availability="in_stock",
                updated_at=0.0,
            )
        )

    # -- voice-driven drift surfacing ---------------------------------------

    def _surface_user_fact(self, payload: Dict[str, Any]) -> None:
        """``record_receipt`` / ``add_inventory_count`` are already applied by
        ``core/voice.py`` directly against the ledger tables (§11) — surface
        the resulting on-hand / drift on the live dashboard without
        re-applying the write."""
        ing_name = payload.get("entity_ref")
        if not ing_name:
            return
        session = self.db_session_factory()
        try:
            ing = (
                session.query(Ingredient)
                .filter(Ingredient.name.ilike(str(ing_name)))
                .first()
            )
            if ing is None:
                return
            ingredient_id = ing.id
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            on_hand = float(level.on_hand_cached) if level is not None else 0.0
        finally:
            session.close()

        self.broadcast("inventory_updated", {"ingredient_id": ingredient_id, "on_hand": on_hand})
        if payload.get("intent") == "add_inventory_count":
            self.log_event(
                "reconciliation",
                f"Voice count for {ing_name}: on-hand now {on_hand:.1f} (drift recorded).",
                {"ingredient_id": ingredient_id, "on_hand": on_hand},
            )
        else:
            self.log_event(
                "receipt",
                f"Voice receipt recorded for {ing_name}; on-hand now {on_hand:.1f}.",
                {"ingredient_id": ingredient_id, "on_hand": on_hand},
            )
