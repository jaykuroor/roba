"""Tests for the weather provider (§9) — gates 1, 2 & 3."""

import pytest

from core.clock import SimClock
from core.models import WeatherLog
from core.signals import SignalType
from core.weather import WeatherProvider, map_weather_code


@pytest.fixture
def provider(bus, session_factory):
    clock = SimClock(session_factory, bus)
    bus.sim_time = 1000.0
    return WeatherProvider(bus, session_factory, clock), session_factory


def test_map_weather_code():
    assert map_weather_code(0) == "clear"
    assert map_weather_code(1) == "clear"
    assert map_weather_code(2) == "clouds"
    assert map_weather_code(3) == "clouds"
    assert map_weather_code(61) == "rain"
    assert map_weather_code(80) == "rain"
    assert map_weather_code(71) == "snow"
    assert map_weather_code(85) == "snow"
    assert map_weather_code(95) == "storm"
    assert map_weather_code(99) == "storm"


def test_fetch_and_store_writes_row_and_emits(provider, monkeypatch):
    """Gate 1: fetch_and_store writes a WeatherLog row and emits WEATHER_UPDATE
    (verified against a live bus)."""
    weather, session_factory = provider

    def fake_get(url, params):
        return {
            "current": {
                "temperature_2m": 12.5,
                "precipitation": 0.4,
                "wind_speed_10m": 18.0,
                "weather_code": 61,  # → rain
            }
        }

    monkeypatch.setattr(weather, "_http_get", fake_get)
    row = weather.fetch_and_store()

    assert row.source == "api"
    assert row.condition == "rain"
    assert row.temp_c == 12.5

    session = session_factory()
    try:
        rows = session.query(WeatherLog).all()
        assert len(rows) == 1
        assert rows[0].source == "api"
    finally:
        session.close()

    live = weather.bus.live(type=SignalType.WEATHER_UPDATE)
    assert len(live) == 1
    payload = live[0].payload
    assert payload["condition"] == "rain"
    assert payload["source"] == "api"
    assert payload["wind_kph"] == 18.0


def test_override_writes_override_row(provider):
    """Gate 2: override writes a source='override' WeatherLog row."""
    weather, session_factory = provider
    row = weather.override(temp_c=5, condition="snow", precip_mm=2, wind_kph=30)

    assert row.source == "override"
    assert row.condition == "snow"
    assert row.temp_c == 5

    session = session_factory()
    try:
        overrides = (
            session.query(WeatherLog).filter(WeatherLog.source == "override").all()
        )
        assert len(overrides) == 1
        assert overrides[0].precip_mm == 2
        assert overrides[0].wind_kph == 30
    finally:
        session.close()

    # current() returns the latest row (the override).
    assert weather.current().source == "override"

    live = weather.bus.live(type=SignalType.WEATHER_UPDATE)
    assert live[-1].payload["source"] == "override"


def test_fetch_falls_back_to_default_without_network(provider, monkeypatch):
    """Gate 3: with no network, fetch_and_store falls back to the default
    without raising and still writes a row + emits."""
    weather, session_factory = provider

    def boom(url, params):
        raise OSError("no network")

    monkeypatch.setattr(weather, "_http_get", boom)
    row = weather.fetch_and_store()  # must not raise

    assert row.condition == "clear"
    assert row.temp_c == 20.0
    assert row.precip_mm == 0.0
    assert row.wind_kph == 10.0
    assert row.source == "api"

    live = weather.bus.live(type=SignalType.WEATHER_UPDATE)
    assert len(live) == 1
    assert live[0].payload["condition"] == "clear"


def test_fetch_error_reuses_last_row(provider, monkeypatch):
    """When the API errors but a prior row exists, the last row is reused (no
    duplicate default written)."""
    weather, session_factory = provider
    weather.override(temp_c=5, condition="snow", precip_mm=2, wind_kph=30)

    def boom(url, params):
        raise OSError("no network")

    monkeypatch.setattr(weather, "_http_get", boom)
    row = weather.fetch_and_store()

    # Reused the override row rather than writing a fresh default.
    assert row.condition == "snow"
    session = session_factory()
    try:
        assert session.query(WeatherLog).count() == 1
    finally:
        session.close()
