"""Tests for seeding / generation / validation (§12) — gate 1."""

import pytest

from core.llm import LLMProvider
from core.models import (
    Competitor,
    Ingredient,
    InventoryLevel,
    MenuItem,
    Order,
    OrderLine,
    RecipeLine,
    Review,
    SimSettings,
    SimState,
    Staff,
    Supplier,
    SupplierCatalog,
)
from core.seeding import Seeder, Validator


@pytest.fixture
def llm(monkeypatch):
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    provider = LLMProvider()
    provider._sleep = lambda *_a, **_k: None
    return provider


@pytest.mark.parametrize("preset", ["bellas_kitchen", "burger_joint"])
def test_load_preset_inserts_and_validates(llm, session_factory, preset):
    """Gate 1: load_preset inserts all rows and validator returns (True, [])."""
    seeder = Seeder(llm, session_factory)
    data = seeder.load_preset(preset)

    ok, violations = Validator().validate(data)
    assert ok, violations
    assert violations == []

    session = session_factory()
    try:
        assert session.query(Ingredient).count() == len(data["ingredients"])
        assert session.query(MenuItem).count() == len(data["menu_items"])
        assert session.query(RecipeLine).count() == len(data["recipe_lines"])
        assert session.query(Supplier).count() == len(data["suppliers"])
        assert session.query(SupplierCatalog).count() == len(data["supplier_catalog"])
        assert session.query(InventoryLevel).count() == len(data["inventory_levels"])
        assert session.query(Staff).count() == len(data["staff"])
        assert session.query(Competitor).count() == 5
        assert session.query(Review).count() == 10
        # 30 days of synthetic POS history was generated and inserted.
        assert session.query(Order).count() > 0
        assert session.query(OrderLine).count() > 0
        # Singletons present.
        assert session.get(SimState, 1) is not None
        assert session.get(SimSettings, 1) is not None
    finally:
        session.close()


def test_list_presets(llm, session_factory):
    seeder = Seeder(llm, session_factory)
    presets = seeder.list_presets()
    assert "bellas_kitchen" in presets
    assert "burger_joint" in presets


def test_validator_flags_and_repairs_missing_supplier():
    """An ingredient with no supplier_catalog row is auto-repaired (§12.3)."""
    data = {
        "ingredients": [{"id": 1, "name": "Salt"}],
        "stations": [{"id": 1, "name": "Line"}],
        "menu_items": [
            {"id": 1, "name": "Dish", "station_id": 1,
             "dine_in_price": 9.0, "online_price": 10.0, "is_batchable": 0}
        ],
        "recipe_lines": [{"id": 1, "recipe_id": 1, "ingredient_id": 1, "qty": 5}],
        "staff_stations": [{"id": 1, "staff_id": 1, "station_id": 1}],
        "supplier_catalog": [],  # ingredient 1 unsold -> repairable
        "batch_definitions": [],
        "competitor_offers": [],
        "competitors": [],
    }
    ok, violations = Validator().validate(data)
    assert ok, violations
    assert any(sc["ingredient_id"] == 1 for sc in data["supplier_catalog"])


def test_generate_offline_produces_valid_bundle(llm, session_factory):
    """generate() works offline (canned slice) and inserts a valid bundle."""
    seeder = Seeder(llm, session_factory)
    data = seeder.generate("cafe", {"menu_items": 2})
    ok, violations = Validator().validate(data)
    assert ok, violations
    session = session_factory()
    try:
        assert session.query(MenuItem).count() == len(data["menu_items"])
        assert session.query(InventoryLevel).count() == len(data["ingredients"])
    finally:
        session.close()
