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


def _cat(price=1.0):
    return [{"supplier_id": 1, "ingredient_id": 7, "current_price": price,
             "availability": "in_stock"}]


def test_capped_discount_becomes_offer_not_price_cut(session_factory):
    """A percentage discount carrying max_discount_amount ("50% off up to €30")
    is returned as a CappedDiscountOffer and does NOT cut the per-item price — the
    cap can only be honoured as an order-level rebate in the MILP."""
    session = session_factory()
    try:
        session.add(SupplierTerm(
            supplier_id=1, ingredient_id=None, term_type="discount",
            value=0.5, scope="all", effective_at=0.0, expiry_kind="none",
            status="active", created_at=0.0, max_discount_amount=30.0,
        ))
        session.commit()
        applied = apply_supplier_terms(session, _cat(price=2.0), _supplier(), now=1000.0)
        # Price untouched (uncapped 50% cut would have halved it to 1.0).
        assert applied.catalog[0]["current_price"] == 2.0
        assert len(applied.capped_discount_offers) == 1
        offer = applied.capped_discount_offers[0]
        assert offer.supplier_id == 1
        assert offer.discount_frac == 0.5
        assert offer.cap_eur == 30.0
    finally:
        session.close()


def test_uncapped_discount_still_cuts_price(session_factory):
    """A plain discount (no cap) keeps the existing per-item price-cut behaviour
    and produces no capped offer."""
    session = session_factory()
    try:
        session.add(SupplierTerm(
            supplier_id=1, ingredient_id=None, term_type="discount",
            value=0.5, scope="all", effective_at=0.0, expiry_kind="none",
            status="active", created_at=0.0,
        ))
        session.commit()
        applied = apply_supplier_terms(session, _cat(price=2.0), _supplier(), now=1000.0)
        assert applied.catalog[0]["current_price"] == 1.0  # 50% off
        assert applied.capped_discount_offers == []
    finally:
        session.close()


def _supplier_mov(mov=100.0):
    return [{"id": 1, "delivery_charge": 0.0, "lead_time_days": 2.0, "min_order_value": mov}]


def test_min_order_override_changes_supplier_mov(session_factory):
    """A live min_order_override replaces the supplier's MOV the MILP constrains on."""
    session = session_factory()
    try:
        session.add(SupplierTerm(
            supplier_id=1, ingredient_id=None, term_type="min_order_override",
            value=50.0, scope="all", effective_at=0.0, expiry_kind="none",
            status="active", created_at=0.0,
        ))
        session.commit()
        applied = apply_supplier_terms(session, [], _supplier_mov(100.0), now=1000.0)
        assert applied.suppliers[0]["min_order_value"] == 50.0
    finally:
        session.close()


def test_expired_min_order_override_keeps_base_mov(session_factory):
    """A date-expired MOV promo reverts to the supplier's standing minimum."""
    session = session_factory()
    try:
        session.add(SupplierTerm(
            supplier_id=1, ingredient_id=None, term_type="min_order_override",
            value=50.0, scope="all", effective_at=0.0, expiry_kind="date",
            expires_at=500.0, status="active", created_at=0.0,
        ))
        session.commit()
        applied = apply_supplier_terms(session, [], _supplier_mov(100.0), now=1000.0)
        assert applied.suppliers[0]["min_order_value"] == 100.0  # now > expires_at → reverted
    finally:
        session.close()
