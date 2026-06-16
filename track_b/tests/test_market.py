"""Tests for the Market Spectator agent (02 §B9 / §B4.3)."""

from types import SimpleNamespace

import pytest

from core.models import (
    Call,
    Ingredient,
    InventoryLevel,
    Negotiation,
    Supplier,
    SupplierCatalog,
    SupplierPriceHistory,
)
from core.signals import SignalType
from track_b.agents.market_spectator import MarketSpectator


class _FakeCalls:
    def __init__(self):
        self.requested = []
        self._next_id = 1

    def request(self, agent, counterparty_type, counterparty_id, purpose):
        call = SimpleNamespace(id=self._next_id, agent=agent, counterparty_type=counterparty_type,
                                counterparty_id=counterparty_id, purpose=purpose)
        self._next_id += 1
        self.requested.append(call)
        return call


def _seed_ingredient(session_factory):
    session = session_factory()
    try:
        ing = Ingredient(name="tomato", category="produce", base_unit="g", perishable=1, shelf_life_days=5.0)
        session.add(ing)
        session.commit()
        return ing.id
    finally:
        session.close()


def _seed_supplier_catalog(session_factory, ingredient_id, current_price):
    session = session_factory()
    try:
        supplier = Supplier(name="GreenFarm", lead_time_days=2.0, reliability_score=0.9, min_order_value=0.0, contact="")
        session.add(supplier)
        session.flush()
        session.add(SupplierCatalog(supplier_id=supplier.id, ingredient_id=ingredient_id,
                                     current_price=current_price, unit="g", pack_size=50.0,
                                     availability="in_stock", updated_at=0.0))
        session.commit()
        return supplier.id
    finally:
        session.close()


def _seed_price_history(session_factory, supplier_id, ingredient_id, prices):
    session = session_factory()
    try:
        for i, p in enumerate(prices):
            session.add(SupplierPriceHistory(supplier_id=supplier_id, ingredient_id=ingredient_id,
                                               price=p, sim_time=float(i)))
        session.commit()
    finally:
        session.close()


@pytest.fixture
def market(bus, session_factory):
    calls = _FakeCalls()
    agent = MarketSpectator(bus, session_factory, calls=calls)
    return agent, calls


def test_review_prices_triggers_negotiation_when_above_median(market, session_factory):
    agent, calls = market
    ing_id = _seed_ingredient(session_factory)
    supplier_id = _seed_supplier_catalog(session_factory, ing_id, current_price=3.0)
    _seed_price_history(session_factory, supplier_id, ing_id, [1.0, 1.0, 1.0])  # median 1.0, current 3.0 -> negotiate

    agent.review_prices()

    assert len(calls.requested) == 1
    assert calls.requested[0].counterparty_id == supplier_id
    assert (supplier_id, ing_id) in agent._negotiating


def test_review_prices_skips_when_price_in_line(market, session_factory):
    agent, calls = market
    ing_id = _seed_ingredient(session_factory)
    supplier_id = _seed_supplier_catalog(session_factory, ing_id, current_price=1.05)
    _seed_price_history(session_factory, supplier_id, ing_id, [1.0, 1.0, 1.0])

    agent.review_prices()
    assert calls.requested == []


def test_call_outcome_agreed_updates_catalog_and_history(market, session_factory, bus):
    agent, calls = market
    ing_id = _seed_ingredient(session_factory)
    supplier_id = _seed_supplier_catalog(session_factory, ing_id, current_price=3.0)
    _seed_price_history(session_factory, supplier_id, ing_id, [1.0, 1.0, 1.0])

    agent.review_prices()
    call_id = calls.requested[0].id

    session = session_factory()
    try:
        session.add(Call(id=call_id, agent="market_spectator", counterparty_type="supplier",
                          counterparty_id=supplier_id, purpose="negotiate", status="active",
                          transcript=[{"role": "agent", "text": "hi"}], outcome=None,
                          started_at=0.0, ended_at=None, clock_action="freeze"))
        session.commit()
    finally:
        session.close()

    bus.sim_time = 50.0
    outcome_signal = SimpleNamespace(
        type=SignalType.CALL_OUTCOME.value,
        payload={"call_id": call_id, "counterparty_type": "supplier",
                  "outcome": {"ingredient_id": ing_id, "agreed_price": 1.5, "agreed": True}},
    )
    agent.on_signal(outcome_signal)

    session = session_factory()
    try:
        catalog = session.query(SupplierCatalog).filter(SupplierCatalog.ingredient_id == ing_id).first()
        assert catalog.current_price == pytest.approx(1.5)
        history = session.query(SupplierPriceHistory).filter(SupplierPriceHistory.price == 1.5).all()
        assert len(history) == 1
        negotiations = session.query(Negotiation).all()
        assert len(negotiations) == 1
        assert negotiations[0].savings == pytest.approx(3.0 - 1.5)
    finally:
        session.close()

    updates = bus.live(type=SignalType.SUPPLIER_PRICE_UPDATE)
    assert len(updates) == 1
    assert updates[0].payload["new_price"] == pytest.approx(1.5)
    assert (supplier_id, ing_id) not in agent._negotiating


def test_call_outcome_not_agreed_leaves_price_unchanged(market, session_factory, bus):
    agent, calls = market
    ing_id = _seed_ingredient(session_factory)
    supplier_id = _seed_supplier_catalog(session_factory, ing_id, current_price=3.0)

    session = session_factory()
    try:
        session.add(Call(id=1, agent="market_spectator", counterparty_type="supplier",
                          counterparty_id=supplier_id, purpose="negotiate", status="active",
                          transcript=[], outcome=None, started_at=0.0, ended_at=None, clock_action="freeze"))
        session.commit()
    finally:
        session.close()
    agent._call_ingredient[1] = ing_id

    outcome_signal = SimpleNamespace(
        type=SignalType.CALL_OUTCOME.value,
        payload={"call_id": 1, "counterparty_type": "supplier", "outcome": {"agreed": False}},
    )
    agent.on_signal(outcome_signal)

    session = session_factory()
    try:
        catalog = session.query(SupplierCatalog).filter(SupplierCatalog.ingredient_id == ing_id).first()
        assert catalog.current_price == pytest.approx(3.0)
    finally:
        session.close()
    assert bus.live(type=SignalType.SUPPLIER_PRICE_UPDATE) == []


def test_repeated_spoilage_reduces_par(market, session_factory):
    agent, _calls = market
    ing_id = _seed_ingredient(session_factory)
    session = session_factory()
    try:
        session.add(InventoryLevel(ingredient_id=ing_id, par_level=100.0, reorder_point=20.0,
                                    safety_stock=10.0, yield_factor=1.0, on_hand_cached=50.0))
        session.commit()
    finally:
        session.close()

    waste_signal = SimpleNamespace(
        type=SignalType.WASTE_EVENT.value,
        payload={"waste_type": "spoilage", "ingredient_id": ing_id},
    )
    agent.on_signal(waste_signal)
    agent.on_signal(waste_signal)  # second occurrence crosses the threshold

    session = session_factory()
    try:
        level = session.query(InventoryLevel).filter(InventoryLevel.ingredient_id == ing_id).first()
        assert level.par_level == pytest.approx(90.0)
    finally:
        session.close()
