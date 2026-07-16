"""Tests for the call subsystem + approvals hub (§8 / §19.4) — gates 4 & 5."""

import pytest

from core.approvals import ApprovalsHub
from core.calls import WAITING_NOTE, CallSubsystem
from core.clock import CALL_FROZEN, SimClock
from core.models import ApprovalRequest, Call
from core.signals import SignalType


class FakeLLM:
    """Deterministic stub: returns a concrete outcome dict for extraction and
    a scripted line for agent turns (so call.outcome is genuinely populated)."""

    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **kwargs):
        if use_site == "outcome_extraction":
            return {"agreed": True, "agreed_price": 1.8, "ingredient_id": 1}
        return "Scripted agent line."

    def canned(self, use_site):
        return "canned line"


@pytest.fixture
def wired(bus, session_factory):
    clock = SimClock(session_factory, bus)
    clock.play()  # RUNNING, so a freeze is observable and restorable
    bus.sim_time = 1000.0
    approvals = ApprovalsHub(bus, session_factory)
    calls = CallSubsystem(bus, session_factory, clock, FakeLLM())
    calls.attach_approvals(approvals)
    return calls, approvals, clock, session_factory


def test_full_call_flow(wired):
    """Gate 4: request → approve → frozen + CALL_STARTED → add_turn → end_call
    → CALL_OUTCOME emitted, outcome set, clock unfrozen."""
    calls, approvals, clock, session_factory = wired

    call = calls.request(
        agent="market_spectator",
        counterparty_type="supplier",
        counterparty_id=1,
        purpose="Negotiate tomato price",
    )
    assert call.status == "requested"
    assert call.approval_id is not None

    # A CALL_REQUEST and an APPROVAL_REQUEST were emitted.
    assert len(calls.bus.live(type=SignalType.CALL_REQUEST)) == 1
    assert len(calls.bus.live(type=SignalType.APPROVAL_REQUEST)) == 1

    # Approving the outbound_call resolves → the subsystem starts the call.
    approvals.approve(call.approval_id)

    assert clock.current_state()["status"] == CALL_FROZEN
    started = calls.bus.live(type=SignalType.CALL_STARTED)
    assert len(started) == 1
    assert started[0].payload["call_id"] == call.id

    session = session_factory()
    try:
        assert session.get(Call, call.id).status == "active"
    finally:
        session.close()

    calls.add_turn(call.id, "agent", "Can we improve the unit price?")
    calls.add_turn(call.id, "counterparty", "I can do 1.80 per kg.")

    calls.end_call(call.id)

    outcomes = calls.bus.live(type=SignalType.CALL_OUTCOME)
    assert len(outcomes) == 1
    assert outcomes[0].payload["call_id"] == call.id
    assert outcomes[0].payload["counterparty_type"] == "supplier"

    session = session_factory()
    try:
        row = session.get(Call, call.id)
        assert row.status == "completed"
        assert row.outcome is not None  # outcome is set (§8.5)
        assert row.outcome["agreed"] is True
        assert len(row.transcript) == 2
    finally:
        session.close()

    # Clock restored out of CALL_FROZEN (back to RUNNING).
    assert clock.current_state()["status"] != CALL_FROZEN


def test_resolution_dispatches_via_bus_to_all_subscribers(wired):
    """The call subsystem reacts to APPROVAL_RESOLVED through the bus, and a
    second, unrelated bus subscriber fires on the same resolution — proving
    Track B's PO/promo handlers will work off the same single dispatch path."""
    calls, approvals, clock, session_factory = wired

    # A stand-in for a Track B handler subscribing to the same signal type.
    seen = []
    calls.bus.subscribe(SignalType.APPROVAL_RESOLVED, lambda sig: seen.append(sig))

    call = calls.request("market_spectator", "supplier", 1, "Negotiate")
    approvals.approve(call.approval_id)

    # Track B's subscriber observed the resolution off the bus...
    assert len(seen) == 1
    assert seen[0].type == SignalType.APPROVAL_RESOLVED.value
    assert seen[0].payload["type"] == "outbound_call"
    assert seen[0].payload["decision"] == "approved"

    # ...and the call subsystem's own bus subscription started the call.
    assert clock.current_state()["status"] == CALL_FROZEN
    session = session_factory()
    try:
        assert session.get(Call, call.id).status == "active"
    finally:
        session.close()


def test_second_request_while_active_waits(wired):
    """Gate 5: a second request while one call is active shows a waiting note
    and does not start a second call."""
    calls, approvals, clock, session_factory = wired

    first = calls.request("market_spectator", "supplier", 1, "Negotiate")
    approvals.approve(first.approval_id)
    assert clock.current_state()["status"] == CALL_FROZEN

    # Second request arrives while the first is active.
    second = calls.request("competitor_intel", "competitor", 2, "Mystery shop")

    session = session_factory()
    try:
        approval = session.get(ApprovalRequest, second.approval_id)
        assert WAITING_NOTE in (approval.summary or "")
        assert approval.payload.get("note") == WAITING_NOTE

        # Exactly one active call (the first); the second has not started.
        active = session.query(Call).filter(Call.status == "active").all()
        assert len(active) == 1
        assert active[0].id == first.id
    finally:
        session.close()


def test_queued_call_starts_after_active_ends(wired):
    """Approving the second (waiting) call queues it; it starts only once the
    first call ends (§6.3)."""
    calls, approvals, clock, session_factory = wired

    first = calls.request("market_spectator", "supplier", 1, "Negotiate")
    approvals.approve(first.approval_id)

    second = calls.request("competitor_intel", "competitor", 2, "Mystery shop")
    approvals.approve(second.approval_id)  # queued, not started

    session = session_factory()
    try:
        assert session.get(Call, second.id).status == "approved"
        active = session.query(Call).filter(Call.status == "active").all()
        assert [c.id for c in active] == [first.id]
    finally:
        session.close()

    # Ending the first call lets the queued second call start.
    calls.end_call(first.id)

    session = session_factory()
    try:
        assert session.get(Call, second.id).status == "active"
    finally:
        session.close()
    assert clock.current_state()["status"] == CALL_FROZEN


def test_reject_marks_call_rejected(wired):
    """A rejected outbound_call approval sets call.status='rejected' and starts
    no call."""
    calls, approvals, clock, session_factory = wired

    call = calls.request("market_spectator", "supplier", 1, "Negotiate")
    approvals.reject(call.approval_id)

    session = session_factory()
    try:
        assert session.get(Call, call.id).status == "rejected"
        assert session.query(Call).filter(Call.status == "active").count() == 0
    finally:
        session.close()
    assert clock.current_state()["status"] != CALL_FROZEN


def test_auto_resolve_completes_without_roleplay(wired):
    """auto_resolve simulates a canned counterpart, completes the call as
    auto_resolved, and emits CALL_OUTCOME (§8.2 fallback)."""
    calls, approvals, clock, session_factory = wired

    call = calls.request("competitor_intel", "competitor", 2, "Mystery shop")
    approvals.approve(call.approval_id)
    calls.auto_resolve(call.id)

    session = session_factory()
    try:
        row = session.get(Call, call.id)
        assert row.status == "auto_resolved"
        # A canned counterparty turn + a generated agent turn were appended.
        roles = [t["role"] for t in row.transcript]
        assert "counterparty" in roles
        assert "agent" in roles
    finally:
        session.close()

    assert len(calls.bus.live(type=SignalType.CALL_OUTCOME)) == 1
    assert clock.current_state()["status"] != CALL_FROZEN


class _DiscountLLM:
    """Extraction stub returning the §8.5 supplier `updates` array — mirrors a
    real model capturing an explicit '20% off all items' offer."""

    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **kwargs):
        if use_site == "outcome_extraction":
            return {
                "updates": [
                    {
                        "update_type": "discount",
                        "scope": "all",
                        "amount": 20.0,
                        "amount_kind": "percent_discount",
                        "verbatim_quote": "20 percent off all items",
                        "confidence": 1.0,
                    }
                ]
            }
        return "Scripted agent line."

    def canned(self, use_site):
        return "canned line"


def test_supplier_discount_creates_active_term_and_replan_signal(bus, session_factory):
    """A supplier call stating a discount must end with an active SupplierTerm and
    a replan-triggering signal — the chain that turns 'informed the agent of a
    discount' into an applied procurement change (auto_apply on)."""
    from core.models import AppSettings, SupplierTerm

    clock = SimClock(session_factory, bus)
    clock.play()
    bus.sim_time = 1000.0
    session = session_factory()
    try:
        session.add(AppSettings(id=1, auto_apply_supplier_changes=1))
        session.commit()
    finally:
        session.close()

    approvals = ApprovalsHub(bus, session_factory)
    calls = CallSubsystem(bus, session_factory, clock, _DiscountLLM())
    calls.attach_approvals(approvals)

    call = calls.start_direct(
        agent="user",
        counterparty_type="supplier",
        counterparty_id=1,
        purpose="negotiate",
    )
    calls.add_turn(call.id, "counterparty", "20 percent off all items for two weeks.")
    calls.end_call(call.id)

    session = session_factory()
    try:
        terms = session.query(SupplierTerm).all()
        assert len(terms) == 1
        assert terms[0].term_type == "discount"
        assert terms[0].scope == "all"
        assert terms[0].status == "active"  # auto_apply → live for the optimizer
    finally:
        session.close()

    # scope='all' auto-apply emits CALL_OUTCOME so the optimizer re-plans.
    assert len(calls.bus.live(type=SignalType.CALL_OUTCOME)) >= 1


def test_expire_pending(bus, session_factory):
    """ApprovalsHub.expire_pending expires pending rows past the 6h TTL."""
    bus.sim_time = 0.0
    hub = ApprovalsHub(bus, session_factory)
    approval = hub.create(type="other", title="t", summary="s", payload={})

    # Before TTL: still pending.
    hub.expire_pending(now_sim=100.0)
    session = session_factory()
    try:
        assert session.get(ApprovalRequest, approval.id).status == "pending"
    finally:
        session.close()

    # After 6h + 1s: expired.
    hub.expire_pending(now_sim=21601.0)
    session = session_factory()
    try:
        assert session.get(ApprovalRequest, approval.id).status == "expired"
    finally:
        session.close()


class _PromoLLM:
    """Extraction stub for the real bug scenario: a supplier offering BOTH free
    delivery AND free 2kg tomatoes on orders ≥ €100 for the next two orders.
    A strong model returns exactly two clean updates (no phantom €0 price)."""

    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **kwargs):
        if use_site == "outcome_extraction":
            return {
                "updates": [
                    {
                        "update_type": "free_delivery",
                        "scope": "all",
                        "min_order_value": 100.0,
                        "expiry_kind": "for_n_orders",
                        "expires_hint": "for 2 orders",
                        "verbatim_quote": "free delivery",
                        "confidence": 1.0,
                    },
                    {
                        "update_type": "free_goods",
                        "scope": "ingredient",
                        "free_ingredient_name": "tomatoes",
                        "free_qty": 2.0,
                        "free_unit": "kg",
                        "min_order_value": 100.0,
                        "expiry_kind": "for_n_orders",
                        "expires_hint": "for 2 orders",
                        "verbatim_quote": "free 2kg of tomatoes",
                        "confidence": 1.0,
                    },
                ]
            }
        return "Scripted agent line."

    def canned(self, use_site):
        return "canned line"


class _BadDeliveryLLM:
    """Extraction stub for the pre-fix failure mode: 'free delivery' mislabelled
    as an absolute €0 price on all items. The zero-price guard must drop it."""

    def complete(self, messages, json_schema=None, max_tokens=800, use_site="", **kwargs):
        if use_site == "outcome_extraction":
            return {
                "updates": [
                    {
                        "update_type": "price_change",
                        "scope": "all",
                        "amount": 0.0,
                        "amount_kind": "absolute_price",
                        "unit": "per_kg",
                        "expiry_kind": "none",
                        "verbatim_quote": "free delivery",
                        "confidence": 0.9,
                    }
                ]
            }
        return "Scripted agent line."

    def canned(self, use_site):
        return "canned line"


def _run_supplier_call(bus, session_factory, llm, line):
    from core.models import AppSettings

    clock = SimClock(session_factory, bus)
    clock.play()
    bus.sim_time = 1000.0
    session = session_factory()
    try:
        session.add(AppSettings(id=1, auto_apply_supplier_changes=1))
        session.commit()
    finally:
        session.close()

    approvals = ApprovalsHub(bus, session_factory)
    calls = CallSubsystem(bus, session_factory, clock, llm)
    calls.attach_approvals(approvals)
    call = calls.start_direct(
        agent="user", counterparty_type="supplier", counterparty_id=1, purpose="promo"
    )
    calls.add_turn(call.id, "counterparty", line)
    calls.end_call(call.id)
    return calls


def test_compound_promo_yields_two_clean_terms(bus, session_factory):
    """The free-delivery + free-tomatoes promo must produce exactly two terms —
    one free_delivery, one free_goods — both limited to 2 orders and gated at
    €100, with NO phantom €0 price_override (the original bug)."""
    from core.models import SupplierTerm

    _run_supplier_call(
        bus, session_factory, _PromoLLM(),
        "Free delivery and free 2kg of tomatoes on any order above €100, for your next two orders.",
    )

    session = session_factory()
    try:
        terms = session.query(SupplierTerm).all()
        by_type = {t.term_type: t for t in terms}
        assert set(by_type) == {"free_delivery", "free_goods"}, [t.term_type for t in terms]
        assert "price_override" not in by_type  # no €0-on-all-items poison

        fd = by_type["free_delivery"]
        assert fd.expiry_kind == "orders" and fd.remaining_orders == 2
        assert fd.min_order_value == 100.0

        fg = by_type["free_goods"]
        assert fg.expiry_kind == "orders" and fg.remaining_orders == 2
        assert fg.free_qty_g == 2000.0 and fg.min_order_value == 100.0
    finally:
        session.close()


def test_mislabelled_free_delivery_price_is_dropped(bus, session_factory):
    """A 'free delivery' hallucinated as an absolute €0 price must never become a
    price_override term (which would make the supplier win every item at €0)."""
    from core.models import SupplierTerm

    _run_supplier_call(
        bus, session_factory, _BadDeliveryLLM(), "You get free delivery.",
    )

    session = session_factory()
    try:
        terms = session.query(SupplierTerm).all()
        assert all(t.term_type != "price_override" for t in terms), [t.term_type for t in terms]
    finally:
        session.close()
