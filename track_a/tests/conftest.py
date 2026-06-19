"""Track A test fixtures."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.db as db
from core.bus import SignalBus
from core.models import (
    BatchDefinition,
    Competitor,
    CompetitorOffer,
    MenuItem,
    OrderLine,
    Recipe,
    RecipeLine,
    SimSettings,
    Staff,
    StaffStation,
    Station,
    WeatherLog,
)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def bus(session_factory):
    bus = SignalBus(session_factory)
    bus.sim_time = 28800.0
    return bus


@pytest.fixture
def seeded(session_factory):
    session = session_factory()
    try:
        grill = Station(id=1, name="Grill")
        pasta = Station(id=2, name="Pasta")
        session.add_all([grill, pasta])
        pizza = MenuItem(
            id=1,
            name="Margherita Pizza",
            category="pizza",
            station_id=1,
            dine_in_price=12.0,
            online_price=14.0,
            prep_time_min=12.0,
            is_batchable=1,
            active=1,
            weather_tags=["comfort"],
            description="",
        )
        pasta_item = MenuItem(
            id=2,
            name="Pasta Pomodoro",
            category="pasta",
            station_id=2,
            dine_in_price=13.0,
            online_price=15.0,
            prep_time_min=10.0,
            is_batchable=1,
            active=1,
            weather_tags=["comfort"],
            description="",
        )
        session.add_all([pizza, pasta_item])
        session.add_all([Recipe(id=1, menu_item_id=1), Recipe(id=2, menu_item_id=2)])
        session.add_all([
            RecipeLine(id=1, recipe_id=1, ingredient_id=1, qty=1.0, unit="each", optional=0),
            RecipeLine(id=2, recipe_id=2, ingredient_id=2, qty=1.0, unit="each", optional=0),
        ])
        session.add_all([
            BatchDefinition(
                id=1,
                menu_item_id=1,
                applicable_menus=[],
                dayparts=["breakfast"],
                prep_lead_time_min=10.0,
                batch_size_min=4.0,
                batch_size_step=2.0,
                batch_size_max=20.0,
                decide_by_offset_min=15.0,
                prepared_shelf_life_min=120.0,
                station_id=1,
                required_skill="cook",
                default_cadence_min=60.0,
                historical_attach_rate=0.2,
            ),
            BatchDefinition(
                id=2,
                menu_item_id=2,
                applicable_menus=[],
                dayparts=["breakfast"],
                prep_lead_time_min=10.0,
                batch_size_min=4.0,
                batch_size_step=2.0,
                batch_size_max=20.0,
                decide_by_offset_min=15.0,
                prepared_shelf_life_min=120.0,
                station_id=2,
                required_skill="cook",
                default_cadence_min=60.0,
                historical_attach_rate=0.2,
            ),
        ])
        session.add(SimSettings(id=1, base_orders_per_day=100, velocity=1.0, dish_mix_weights={"1": 2.0, "2": 1.0}, daypart_curve=None, channel_mix={}, anomaly_injections=None))
        session.add(WeatherLog(id=1, sim_time=28800.0, source="override", temp_c=10.0, condition="rain", precip_mm=5.0, wind_kph=10.0, applied=1))
        session.add_all([
            Staff(id=1, name="Luca", role="cook", skill_level=3, hourly_cost=20.0, active=1),
            Staff(id=2, name="Sofia", role="cook", skill_level=3, hourly_cost=20.0, active=1),
            StaffStation(id=1, staff_id=1, station_id=1),
            StaffStation(id=2, staff_id=2, station_id=2),
        ])
        for day in range(-7, 0):
            session.add(OrderLine(order_id=1, menu_item_id=1, qty=10.0, unit_price=12.0, modifiers=[], discount=0.0, line_total=12.0, status="sold", sim_time=day * 86400 + 30000.0))
            session.add(OrderLine(order_id=1, menu_item_id=2, qty=5.0, unit_price=13.0, modifiers=[], discount=0.0, line_total=13.0, status="sold", sim_time=day * 86400 + 30000.0))
        session.add_all([
            Competitor(id=1, name="Mario", platform="google", cuisine=["italian"], distance_km=0.8, rating=4.0, is_open=1, price_tier="$$", updated_at=0.0),
            Competitor(id=2, name="Far Away", platform="google", cuisine=["italian"], distance_km=5.0, rating=5.0, is_open=1, price_tier="$$", updated_at=0.0),
            CompetitorOffer(id=1, competitor_id=1, dish_or_combo="Margherita Pizza", price=11.5, description="", updated_at=0.0),
        ])
        session.commit()
    finally:
        session.close()
