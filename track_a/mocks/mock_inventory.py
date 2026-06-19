"""Track A standalone mock for Track B inventory signals."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from core.models import MenuItem, Recipe, RecipeLine, SupplierCatalog
from core.signals import SignalType


class MockInventory:
    """Emits obviously fake inventory signals when DEMO_MODE=track_a."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.ws_broadcast = ws_broadcast
        self.disabled_item_id: Optional[int] = None
        self.disabled_at: Optional[float] = None

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            self.tick,
            interval_sim_s=3600.0,
            name="track_a_mock_inventory",
        )

    def tick(self) -> None:
        now = float(self.bus.sim_time)
        if self.disabled_item_id is not None and self.disabled_at is not None:
            if now - self.disabled_at >= 3600.0:
                self.bus.emit(
                    SignalType.MENU_TOGGLE,
                    {
                        "menu_item_id": self.disabled_item_id,
                        "action": "enable",
                        "reason": "mock restock complete",
                    },
                    source="track_a.mock_inventory",
                    dedup_key=f"mock-menu:{self.disabled_item_id}",
                )
                self.disabled_item_id = None
                self.disabled_at = None
            return

        actions = []
        session = self.db_session_factory()
        try:
            item = session.query(MenuItem).filter(MenuItem.active == 1).order_by(MenuItem.id.asc()).first()
            if item is None:
                return
            ingredient_id = self._first_ingredient_id(session, item.id)
            catalog = session.query(SupplierCatalog).filter(SupplierCatalog.ingredient_id == ingredient_id).first() if ingredient_id else None
            item_id = item.id
            self.disabled_item_id = item_id
            self.disabled_at = now
            actions.append(
                (
                    SignalType.MENU_TOGGLE,
                    {"menu_item_id": item_id, "action": "disable", "reason": "mock low stock"},
                    {"dedup_key": f"mock-menu:{item_id}"},
                )
            )
            if ingredient_id is not None:
                actions.append(
                    (
                        SignalType.STOCKOUT_RISK,
                        {
                            "ingredient_id": ingredient_id,
                            "on_hand": 1.0,
                            "projected_runout": now + 1800.0,
                            "affected_items": [item_id],
                        },
                        {"dedup_key": f"mock-stockout:{ingredient_id}"},
                    )
                )
            if catalog is not None:
                current_price = float(catalog.current_price or 0.0)
                actions.append(
                    (
                        SignalType.SUPPLIER_PRICE_UPDATE,
                        {
                            "supplier_id": catalog.supplier_id,
                            "ingredient_id": catalog.ingredient_id,
                            "old_price": current_price,
                            "new_price": round(current_price * 1.08, 4),
                            "availability": "limited",
                            "via": "market",
                        },
                        {"dedup_key": f"mock-price:{catalog.id}"},
                    )
                )
        finally:
            session.close()
        for signal_type, payload, kwargs in actions:
            self.bus.emit(
                signal_type,
                payload,
                source="track_a.mock_inventory",
                **kwargs,
            )

    @staticmethod
    def _first_ingredient_id(session: Any, menu_item_id: int) -> Optional[int]:
        recipe = session.query(Recipe).filter(Recipe.menu_item_id == menu_item_id).first()
        if recipe is None:
            return None
        line = session.query(RecipeLine).filter(RecipeLine.recipe_id == recipe.id).first()
        return line.ingredient_id if line is not None else None
