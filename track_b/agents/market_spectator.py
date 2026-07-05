"""Market Spectator agent — supplier costs & negotiation (02 §B4.3).

Tracks supplier prices, negotiates via approval-gated voice calls (core call
subsystem §8), and reacts to spoilage. Writes ``supplier_catalog`` dynamic
fields, ``supplier_price_history`` and ``negotiations`` on agreed outcomes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core import config
from core.agent_base import BaseAgent
from core.models import (
    AppSettings,
    Call,
    Ingredient,
    InventoryLevel,
    InventoryOptimizerMemory,
    ManagerChange,
    Negotiation,
    Supplier,
    SupplierCatalog,
    SupplierPriceHistory,
)
from core.signals import SignalType

# Signal groups this agent listens to (02 §B4.3).
GROUPS = ["procurement", "inventory"]

# Price-above-median margin that triggers a negotiation consideration.
_NEGOTIATE_MARGIN = 0.15
# Repeated spoilage events for the same ingredient before reacting.
_SPOILAGE_THRESHOLD = 2
# Par reduction applied when repeated spoilage suggests over-ordering.
_PAR_REDUCTION = 0.10


class MarketSpectator(BaseAgent):
    """Supplier price monitoring + negotiation calls + spoilage reaction."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Any,
        name: str = "market_spectator",
        ws_broadcast: Any = None,
        calls: Any = None,
    ):
        super().__init__(bus, db_session_factory, name, ws_broadcast=ws_broadcast)
        self.subscribe(GROUPS)
        self.calls = calls
        # call_id -> ingredient_id, recorded at request time (CALL_OUTCOME's
        # outcome.ingredient_id is optional per §8.5's schema).
        self._call_ingredient: Dict[int, int] = {}
        # (supplier_id, ingredient_id) currently under negotiation, to avoid
        # re-requesting a call every price-review tick while one is pending.
        self._negotiating: set = set()
        # ingredient_id -> consecutive spoilage waste-event count.
        self._spoilage_counts: Dict[int, int] = {}

    def attach_calls(self, calls: Any) -> None:
        self.calls = calls

    def negotiate(self, supplier_id: int, ingredient_id: Optional[int] = None, note: str = "") -> None:
        """Presenter-triggered negotiation (the SupplierEditor "Negotiate"
        button, §B6) — same path the periodic price review uses.

        ``ingredient_id`` may be None (negotiate general supplier terms).
        ``note`` is an optional operator instruction forwarded to the AI caller.
        """
        if ingredient_id is not None and (supplier_id, ingredient_id) in self._negotiating:
            return
        self._start_negotiation(supplier_id, ingredient_id, note=note)

    # -- signal handling ----------------------------------------------------

    def on_signal(self, signal: Any) -> None:
        """React to ``CALL_OUTCOME`` (negotiation result) and
        ``WASTE_EVENT(spoilage)`` (§B4.3)."""
        if signal.type == SignalType.CALL_OUTCOME.value:
            self._on_call_outcome(signal.payload or {})
        elif signal.type == SignalType.WASTE_EVENT.value:
            payload = signal.payload or {}
            if payload.get("waste_type") == "spoilage":
                self._on_spoilage(payload)

    # -- price review / negotiation (§B4.3) ----------------------------------

    def review_prices(self) -> None:
        """Periodic price review against ``supplier_price_history`` (§B4.3).

        Only requests a negotiation when there is substantial evidence:
        - Price has risen sustainably above the historical median (not a one-off spike)
        - No active or recent-cooldown negotiation for this supplier+ingredient
        - Potential savings over the horizon are meaningful vs switching cost
        """
        if self.calls is None:
            return
        session = self.db_session_factory()
        try:
            rows = session.query(SupplierCatalog).all()
            specs = [
                {
                    "supplier_id": c.supplier_id,
                    "ingredient_id": c.ingredient_id,
                    "current_price": c.current_price,
                }
                for c in rows
            ]
            settings = session.query(AppSettings).filter(AppSettings.id == 1).first()
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
        finally:
            session.close()

        for spec in specs:
            key = (spec["supplier_id"], spec["ingredient_id"])
            if key in self._negotiating:
                continue

            # Cooldown check: skip if negotiated recently
            if self._in_cooldown(spec["supplier_id"], spec["ingredient_id"]):
                continue

            median = self._historical_median(spec["supplier_id"], spec["ingredient_id"])
            if median is None or spec["current_price"] is None:
                continue

            price_excess = spec["current_price"] - median
            if spec["current_price"] <= median * (1.0 + _NEGOTIATE_MARGIN):
                continue

            # Sustained-rise check: need at least 3 price history points above median
            if not self._is_sustained_rise(
                spec["supplier_id"], spec["ingredient_id"], median
            ):
                continue

            # Min-savings guard: potential savings over horizon vs switching cost
            # Estimate weekly demand from price history volume (fallback: 1 unit/day)
            weekly_demand = self._estimate_weekly_demand(spec["ingredient_id"])
            weekly_savings = price_excess * weekly_demand
            total_savings = weekly_savings * (horizon_days / 7.0)
            if total_savings <= switching_cost:
                self.log_event(
                    "negotiation_skipped",
                    f"Skipping negotiation for ingredient "
                    f"{spec['ingredient_id']} supplier {spec['supplier_id']}: "
                    f"potential savings {total_savings:.4f} <= switching cost "
                    f"{switching_cost:.2f}.",
                    {
                        "supplier_id": spec["supplier_id"],
                        "ingredient_id": spec["ingredient_id"],
                        "total_savings": total_savings,
                        "switching_cost": switching_cost,
                    },
                )
                continue

            self._start_negotiation(spec["supplier_id"], spec["ingredient_id"])

    def _historical_median(self, supplier_id: int, ingredient_id: int) -> Optional[float]:
        session = self.db_session_factory()
        try:
            prices = sorted(
                p[0]
                for p in session.query(SupplierPriceHistory.price)
                .filter(
                    SupplierPriceHistory.supplier_id == supplier_id,
                    SupplierPriceHistory.ingredient_id == ingredient_id,
                )
                .all()
                if p[0] is not None
            )
        finally:
            session.close()
        if not prices:
            return None
        mid = len(prices) // 2
        if len(prices) % 2:
            return float(prices[mid])
        return float(prices[mid - 1] + prices[mid]) / 2.0

    def _start_negotiation(self, supplier_id: int, ingredient_id: Optional[int], note: str = "") -> None:
        session = self.db_session_factory()
        try:
            ing_name = self._ingredient_name(session, ingredient_id) if ingredient_id else "general terms"
        finally:
            session.close()

        call = self.calls.request(
            agent=self.name,
            counterparty_type="supplier",
            counterparty_id=supplier_id,
            purpose=f"negotiate {ing_name} price",
            note=note,
            ingredient_id=ingredient_id,
        )
        if ingredient_id is not None:
            self._call_ingredient[call.id] = ingredient_id
        self._negotiating.add((supplier_id, ingredient_id))

        # Record cooldown timestamp so we don't re-request within NEGOTIATION_COOLDOWN_SIM_S
        self._record_cooldown(supplier_id, ingredient_id)

        self.log_event(
            "negotiation_requested",
            f"Requested negotiation call with supplier {supplier_id} for {ing_name}.",
            {"supplier_id": supplier_id, "ingredient_id": ingredient_id, "call_id": call.id},
        )

    @staticmethod
    def _ingredient_name(session: Any, ingredient_id: int) -> str:
        ing = session.get(Ingredient, ingredient_id)
        return ing.name if ing is not None else str(ingredient_id)

    # -- call outcome (§8.5) -------------------------------------------------

    def _on_call_outcome(self, payload: Dict[str, Any]) -> None:
        if payload.get("counterparty_type") != "supplier":
            return
        call_id = payload.get("call_id")
        outcome = payload.get("outcome") or {}
        if call_id is None:
            return

        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is None or call.agent != self.name:
                return
            supplier_id = call.counterparty_id
            transcript = list(call.transcript or [])
        finally:
            session.close()

        ingredient_id = outcome.get("ingredient_id") or self._call_ingredient.get(call_id)
        self._call_ingredient.pop(call_id, None)
        if ingredient_id is None:
            return
        self._negotiating.discard((supplier_id, ingredient_id))

        now = self.sim_time
        agreed = bool(outcome.get("agreed"))
        agreed_price = outcome.get("agreed_price")

        session = self.db_session_factory()
        try:
            catalog = (
                session.query(SupplierCatalog)
                .filter(
                    SupplierCatalog.supplier_id == supplier_id,
                    SupplierCatalog.ingredient_id == ingredient_id,
                )
                .first()
            )
            old_price = catalog.current_price if catalog is not None else 0.0
            new_price = float(agreed_price) if (agreed and agreed_price is not None) else old_price
            savings = float(old_price or 0.0) - float(new_price or 0.0)

            session.add(
                Negotiation(
                    supplier_id=supplier_id,
                    ingredient_id=ingredient_id,
                    call_id=call_id,
                    transcript=transcript,
                    outcome=outcome,
                    savings=savings,
                    sim_time=now,
                )
            )

            if agreed and catalog is not None and agreed_price is not None:
                catalog.current_price = new_price
                catalog.updated_at = now
                session.add(
                    SupplierPriceHistory(
                        supplier_id=supplier_id,
                        ingredient_id=ingredient_id,
                        price=new_price,
                        sim_time=now,
                    )
                )
            availability = catalog.availability if catalog is not None else "in_stock"
            session.commit()
        finally:
            session.close()

        if agreed and agreed_price is not None:
            self.emit(
                SignalType.SUPPLIER_PRICE_UPDATE,
                {
                    "supplier_id": supplier_id,
                    "ingredient_id": ingredient_id,
                    "old_price": float(old_price or 0.0),
                    "new_price": float(new_price),
                    "availability": availability,
                    "via": "call",
                },
            )
            self.broadcast(
                "supplier_price_updated",
                {"supplier_id": supplier_id, "ingredient_id": ingredient_id, "new_price": new_price},
            )
            self.log_event(
                "negotiation_agreed",
                f"Negotiated ingredient {ingredient_id} from supplier {supplier_id}: "
                f"{old_price:.2f} -> {new_price:.2f}.",
                {"supplier_id": supplier_id, "ingredient_id": ingredient_id, "savings": savings},
            )
            # Create a ManagerChange card for the price update
            change_id = self._create_call_outcome_change(
                call_id=call_id,
                supplier_id=supplier_id,
                ingredient_id=ingredient_id,
                old_price=float(old_price or 0.0),
                new_price=float(new_price),
                savings=savings,
            )
            if change_id is not None:
                self.broadcast("manager_change", {
                    "change_id": change_id,
                    "kind": "call_price",
                    "supplier_id": supplier_id,
                    "ingredient_id": ingredient_id,
                })
        else:
            self.log_event(
                "negotiation_no_deal",
                f"Negotiation with supplier {supplier_id} for ingredient {ingredient_id} did not reach agreement.",
                {"supplier_id": supplier_id, "ingredient_id": ingredient_id},
            )

    # -- spoilage reaction (§16 / §B4.3) -------------------------------------

    def _on_spoilage(self, payload: Dict[str, Any]) -> None:
        ingredient_id = payload.get("ingredient_id")
        if ingredient_id is None:
            return
        count = self._spoilage_counts.get(ingredient_id, 0) + 1
        self._spoilage_counts[ingredient_id] = count
        if count < _SPOILAGE_THRESHOLD:
            return

        self._spoilage_counts[ingredient_id] = 0
        session = self.db_session_factory()
        try:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            if level is not None and level.par_level:
                level.par_level = float(level.par_level) * (1.0 - _PAR_REDUCTION)
                session.commit()
        finally:
            session.close()

        self.log_event(
            "spoilage_pattern",
            f"Repeated spoilage for ingredient {ingredient_id}; recommend ordering "
            f"less/fresher (par reduced {int(_PAR_REDUCTION * 100)}%).",
            {"ingredient_id": ingredient_id},
        )

    # -- cooldown helpers ---------------------------------------------------

    def _cooldown_key(self, supplier_id: int, ingredient_id: int) -> str:
        return f"{supplier_id}:{ingredient_id}"

    def _record_cooldown(self, supplier_id: int, ingredient_id: int) -> None:
        """Persist a negotiation cooldown timestamp in InventoryOptimizerMemory."""
        now = self.sim_time
        key = self._cooldown_key(supplier_id, ingredient_id)
        session = self.db_session_factory()
        try:
            existing = (
                session.query(InventoryOptimizerMemory)
                .filter(
                    InventoryOptimizerMemory.scope_type == "negotiation_cooldown",
                    InventoryOptimizerMemory.scope_ref == key,
                )
                .first()
            )
            if existing is not None:
                existing.insight = {"last_negotiated_at": now}
                existing.last_seen_at = now
            else:
                session.add(InventoryOptimizerMemory(
                    scope_type="negotiation_cooldown",
                    scope_ref=key,
                    insight={"last_negotiated_at": now},
                    evidence=None,
                    confidence=1.0,
                    created_at=now,
                    last_seen_at=now,
                    valid_until=now + config.NEGOTIATION_COOLDOWN_SIM_S,
                    source="market_spectator",
                ))
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    def _in_cooldown(self, supplier_id: int, ingredient_id: int) -> bool:
        """Return True if a negotiation for this supplier+ingredient was requested recently."""
        now = self.sim_time
        key = self._cooldown_key(supplier_id, ingredient_id)
        session = self.db_session_factory()
        try:
            row = (
                session.query(InventoryOptimizerMemory)
                .filter(
                    InventoryOptimizerMemory.scope_type == "negotiation_cooldown",
                    InventoryOptimizerMemory.scope_ref == key,
                )
                .first()
            )
            if row is None:
                return False
            if row.valid_until is not None and float(row.valid_until) > now:
                return True
            return False
        finally:
            session.close()

    def _is_sustained_rise(
        self, supplier_id: int, ingredient_id: int, median: float
    ) -> bool:
        """Return True if the last 3+ price history points are all above median."""
        session = self.db_session_factory()
        try:
            recent = (
                session.query(SupplierPriceHistory.price)
                .filter(
                    SupplierPriceHistory.supplier_id == supplier_id,
                    SupplierPriceHistory.ingredient_id == ingredient_id,
                )
                .order_by(SupplierPriceHistory.sim_time.desc())
                .limit(3)
                .all()
            )
        finally:
            session.close()
        if len(recent) < 3:
            # Not enough history to confirm sustained rise — only require 1 if
            # price is dramatically above median (>40%) to catch new situations.
            if recent and float(recent[0][0] or 0.0) > median * 1.40:
                return True
            return False
        return all(float(r[0] or 0.0) > median for r in recent)

    def _estimate_weekly_demand(self, ingredient_id: int) -> float:
        """Very rough weekly demand estimate for the savings calculation.

        Uses InventoryLevel par_level as a proxy (par covers ~1 week including
        safety stock, so it's a reasonable upper-bound for weekly usage).
        Returns 1.0 as a fallback so the savings check is conservative.
        """
        session = self.db_session_factory()
        try:
            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient_id)
                .first()
            )
            if level is not None and level.par_level and float(level.par_level) > 0:
                return float(level.par_level) * 0.7  # ~70% of par ≈ weekly usage
            return 1.0
        finally:
            session.close()

    # -- ManagerChange creation (call outcome) --------------------------------

    def _create_call_outcome_change(
        self,
        call_id: int,
        supplier_id: int,
        ingredient_id: int,
        old_price: float,
        new_price: float,
        savings: float,
    ) -> Optional[int]:
        """Create a ManagerChange row for an agreed price change from a call.

        Returns the change id or None on failure.
        """
        now = self.sim_time
        session = self.db_session_factory()
        try:
            settings = session.query(AppSettings).filter(AppSettings.id == 1).first()
            auto_apply = bool(
                settings.auto_apply_supplier_changes if settings else
                config.AUTO_APPLY_SUPPLIER_CHANGES
            )

            ing = session.get(Ingredient, ingredient_id)
            ing_name = ing.name if ing is not None else str(ingredient_id)
            sup = session.get(Supplier, supplier_id)
            sup_name = sup.name if sup is not None else str(supplier_id)

            details = {
                "ingredient_id": ingredient_id,
                "ingredient_name": ing_name,
                "supplier_id": supplier_id,
                "supplier_name": sup_name,
                "call_id": call_id,
                "price_before": old_price,
                "price_after": new_price,
                "savings": savings,
            }

            change = ManagerChange(
                kind="call_price",
                status="applied" if auto_apply else "pending",
                auto_applied=1 if auto_apply else 0,
                summary=(
                    f"Price negotiated: {ing_name} from {sup_name}: "
                    f"{old_price:.4f} → {new_price:.4f}"
                ),
                details=details,
                created_at=now,
                resolved_at=now if auto_apply else None,
            )
            session.add(change)
            session.flush()
            change_id = change.id
            session.commit()
            return change_id
        except Exception:
            session.rollback()
            logger.exception("Failed to create call outcome ManagerChange.")
            return None
        finally:
            session.close()
