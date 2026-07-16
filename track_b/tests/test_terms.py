"""Supplier-term application into the procurement catalog/suppliers."""

from core.models import SupplierTerm
from track_b.procurement.terms import apply_supplier_terms


def _supplier(delivery_charge=12.0):
    return [{"id": 1, "delivery_charge": delivery_charge, "lead_time_days": 2.0}]


def test_free_delivery_becomes_mov_gated_offer(session_factory):
    """A live free_delivery term is returned as a MOV-gated FreeDeliveryOffer —
    NOT a pre-zeroed delivery charge (which the solver could exploit by opening a
    free delivery-day to harvest other promo benefits without buying)."""
    session = session_factory()
    try:
        session.add(SupplierTerm(
            supplier_id=1, ingredient_id=None, term_type="free_delivery",
            value=0.0, scope="all", effective_at=0.0, expiry_kind="orders",
            remaining_orders=2, status="active", min_order_value=100.0,
            created_at=0.0,
        ))
        session.commit()
        applied = apply_supplier_terms(session, [], _supplier(), now=1000.0)
        # Charge is left intact; the rebate is applied inside the MILP.
        assert applied.suppliers[0]["delivery_charge"] == 12.0
        assert len(applied.free_delivery_offers) == 1
        offer = applied.free_delivery_offers[0]
        assert offer.supplier_id == 1 and offer.min_order_value == 100.0
    finally:
        session.close()


def test_expired_free_delivery_yields_no_offer(session_factory):
    """An order-limited free_delivery term with no orders left produces no offer."""
    session = session_factory()
    try:
        session.add(SupplierTerm(
            supplier_id=1, ingredient_id=None, term_type="free_delivery",
            value=0.0, scope="all", effective_at=0.0, expiry_kind="orders",
            remaining_orders=0, status="active", created_at=0.0,
        ))
        session.commit()
        applied = apply_supplier_terms(session, [], _supplier(), now=1000.0)
        assert applied.free_delivery_offers == []
        assert applied.suppliers[0]["delivery_charge"] == 12.0
    finally:
        session.close()
