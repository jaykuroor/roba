"""Supplier-term application into the procurement catalog/suppliers."""

from core.models import SupplierTerm
from track_b.procurement.terms import apply_supplier_terms


def _supplier(delivery_charge=12.0):
    return [{"id": 1, "delivery_charge": delivery_charge, "lead_time_days": 2.0}]


def test_free_delivery_zeroes_delivery_charge(session_factory):
    """A live free_delivery term waives the supplier's delivery charge (rather
    than the old bug of a €0 price on every item)."""
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
        assert applied.suppliers[0]["delivery_charge"] == 0.0
    finally:
        session.close()


def test_expired_free_delivery_leaves_charge(session_factory):
    """An order-limited free_delivery term with no orders left is not applied."""
    session = session_factory()
    try:
        session.add(SupplierTerm(
            supplier_id=1, ingredient_id=None, term_type="free_delivery",
            value=0.0, scope="all", effective_at=0.0, expiry_kind="orders",
            remaining_orders=0, status="active", created_at=0.0,
        ))
        session.commit()
        applied = apply_supplier_terms(session, [], _supplier(), now=1000.0)
        assert applied.suppliers[0]["delivery_charge"] == 12.0
    finally:
        session.close()
