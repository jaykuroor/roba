"""Shared supplier-term application logic.

Loads active ``SupplierTerm`` rows from the database and overlays them onto
the raw ``milp_catalog`` and ``milp_suppliers`` structures used by both
``build_procurement_plan`` (forward planner) and ``run_sourcing_plan`` (least-
cost sourcing solver).  Extracting this into one place ensures both planners
see the same term-adjusted prices, availability flags, and lead times, and
that ``threshold_discount`` / ``free_goods`` offers reach the MILP objective.

Public API
----------
apply_supplier_terms(session, catalog, suppliers, now) -> AppliedTerms
    Adjust catalog and supplier dicts; return free_goods_offers for MILP use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class FreeGoodsOffer:
    """One ``free_goods`` SupplierTerm in a MILP-consumable form."""
    supplier_id: int
    free_ingredient_id: int   # ingredient the solver gets for free
    free_qty_g: float          # grams received free per qualifying order
    min_order_value: float     # € spend threshold; 0.0 = unconditional
    remaining_orders: Optional[int]  # None = no cap
    term_id: int


@dataclass
class AppliedTerms:
    catalog: List[Dict[str, Any]]           # milp_catalog with prices/availability adjusted
    suppliers: List[Dict[str, Any]]          # milp_suppliers with volume_discount extended
    free_goods_offers: List[FreeGoodsOffer] = field(default_factory=list)


def apply_supplier_terms(
    session: Any,
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    now: float,
) -> AppliedTerms:
    """Apply all active ``SupplierTerm`` rows to the given catalog and supplier lists.

    ``catalog`` and ``suppliers`` are the raw dicts built by ``build_procurement_plan``
    (``optimizer.py:580-613``).  This function returns *new* list objects so the caller
    can pass them to the MILP without mutating the originals.

    Term precedence (matches ``run_sourcing_plan._apply_terms``):
    - ingredient-specific terms override scope='all' terms.
    - price_override replaces the current price.
    - discount multiplies the current price.
    - threshold_discount is appended to volume_discount tiers.
    - free_goods is returned as FreeGoodsOffer objects.
    - free_delivery zeroes the supplier's delivery_charge while the term is live.
    - unavailable sets availability to 'out'.
    - lead_time_override replaces lead_time_days.
    """
    try:
        from core.models import SupplierTerm  # noqa: PLC0415  (avoid circular at module level)
    except ImportError:
        from models import SupplierTerm  # type: ignore[no-redef]

    # --- Load active terms ---------------------------------------------------
    active_terms = (
        session.query(SupplierTerm)
        .filter(SupplierTerm.status == "active")
        .all()
    )

    def _term_is_live(t: Any) -> bool:
        if t.expiry_kind == "date" and t.expires_at is not None:
            return float(t.expires_at) >= now
        if t.expiry_kind == "orders":
            return (t.remaining_orders or 0) > 0
        return True  # expiry_kind="none" → permanent

    # Build {(supplier_id, ingredient_id_or_None): [SupplierTerm]}
    term_map: Dict[Tuple[int, Optional[int]], List[Any]] = {}
    for t in active_terms:
        if not _term_is_live(t):
            continue
        key: Tuple[int, Optional[int]] = (
            int(t.supplier_id),
            int(t.ingredient_id) if t.ingredient_id else None,
        )
        term_map.setdefault(key, []).append(t)

    def _terms_for(supplier_id: int, ingredient_id: Optional[int]) -> List[Any]:
        return (
            term_map.get((supplier_id, None), [])
            + (term_map.get((supplier_id, ingredient_id), []) if ingredient_id else [])
        )

    # --- Adjust catalog rows -------------------------------------------------
    new_catalog: List[Dict[str, Any]] = []
    for c in catalog:
        s_id = int(c["supplier_id"])
        i_id = int(c["ingredient_id"])
        applicable = _terms_for(s_id, i_id)

        price = float(c.get("current_price") or 0.0)
        availability = c.get("availability", "in_stock")
        adjusted = dict(c)  # shallow copy — we only replace scalar fields

        for t in applicable:
            tt = t.term_type
            if tt == "price_override":
                price = float(t.value)
            elif tt == "discount":
                v = float(t.value)
                if v < 0:
                    price = price * (1.0 + abs(v))   # negative = price increase
                else:
                    price = price * (1.0 - v)
            elif tt == "unavailable":
                availability = "out"
            # threshold_discount and free_goods are handled below (supplier-level)

        adjusted["current_price"] = max(0.0, price)
        adjusted["availability"] = availability
        new_catalog.append(adjusted)

    # --- Adjust supplier rows ------------------------------------------------
    # Also collect threshold_discount terms as extra volume_discount tiers.
    # free_goods terms become FreeGoodsOffer objects returned separately.
    free_goods_offers: List[FreeGoodsOffer] = []
    new_suppliers: List[Dict[str, Any]] = []

    for s in suppliers:
        s_id = int(s["id"])
        adjusted_s = dict(s)
        base_lead = float(s.get("lead_time_days") or 1.0)
        vol_disc = list(s.get("volume_discount") or [])  # copy to extend

        # Collect all terms for this supplier (scope=all only — lead/threshold are supplier-level)
        all_terms = term_map.get((s_id, None), [])
        free_delivery = False
        for t in all_terms:
            tt = t.term_type
            if tt == "free_delivery":
                # Waive the delivery charge while the term is live. Expiry
                # (date / next-N-orders) is already enforced by _term_is_live,
                # and order-count is decremented on PO placement.
                # ponytail: waiver is unconditional while live; MOV-gating it to
                # orders ≥ min_order_value would need the free-goods big-M gate.
                free_delivery = True
            elif tt == "lead_time_override":
                base_lead = float(t.value)
            elif tt == "threshold_discount":
                # Append as a volume_discount tier so the existing MILP rebate loop handles it.
                disc_pct = round(float(t.value) * 100.0, 4)  # fraction → percent
                mov = float(t.min_order_value or 0.0)
                if disc_pct > 0 and mov > 0:
                    vol_disc.append({"min_value": mov, "discount_pct": disc_pct})
            elif tt == "free_goods":
                if t.free_ingredient_id and t.free_qty_g:
                    free_goods_offers.append(FreeGoodsOffer(
                        supplier_id=s_id,
                        free_ingredient_id=int(t.free_ingredient_id),
                        free_qty_g=float(t.free_qty_g),
                        min_order_value=float(t.min_order_value or 0.0),
                        remaining_orders=t.remaining_orders,
                        term_id=int(t.id),
                    ))

        adjusted_s["lead_time_days"] = base_lead
        adjusted_s["volume_discount"] = vol_disc if vol_disc else None
        if free_delivery:
            adjusted_s["delivery_charge"] = 0.0
        new_suppliers.append(adjusted_s)

    return AppliedTerms(
        catalog=new_catalog,
        suppliers=new_suppliers,
        free_goods_offers=free_goods_offers,
    )
