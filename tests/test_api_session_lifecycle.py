"""Regression tests for API/session lifecycle under dashboard-style load."""

from concurrent.futures import ThreadPoolExecutor
import time

from fastapi.testclient import TestClient

import core.db as db
from core import api
from core import models
from core.clock import RUNNING, get_or_create_sim_state
from core.weather import WeatherProvider


def test_sessionlocal_returns_independent_sessions():
    first = db.SessionLocal()
    second = db.SessionLocal()
    try:
        assert first is not second
        assert first.identity_map is not second.identity_map
    finally:
        first.close()
        second.close()


def test_reset_db_completes_under_coordination_lock():
    db.reset_db(keep_reference=False)


def test_app_lifespan_starts_and_serves_basic_read():
    db.reset_db(keep_reference=False)
    with TestClient(api.app) as client:
        response = client.get("/api/sim/state")
    assert response.status_code == 200


def test_play_loop_self_heal_starts_missing_task(monkeypatch):
    class DoneTask:
        def done(self):
            return True

    class FakeOrchestrator:
        def run_loop(self, broadcast):
            return ("loop", broadcast)

    created = []

    def fake_create_task(coro):
        created.append(coro)
        return "new-task"

    monkeypatch.setattr(api.ctx, "orchestrator", FakeOrchestrator())
    monkeypatch.setattr(api.ctx, "loop_task", DoneTask())
    monkeypatch.setattr(api.asyncio, "create_task", fake_create_task)

    api._ensure_loop_task()

    assert created
    assert api.ctx.loop_task == "new-task"


def test_seed_route_completes_under_coordination_lock():
    db.reset_db(keep_reference=False)
    with TestClient(api.app) as client:
        response = client.post("/api/seed/preset/bellas_kitchen")
    assert response.status_code == 200, response.text


def test_seed_switch_deletes_in_fk_safe_order():
    db.reset_db(keep_reference=False)
    with TestClient(api.app) as client:
        first = client.post("/api/seed/preset/bellas_kitchen")
        assert first.status_code == 200, first.text

        second = client.post("/api/seed/preset/burger_joint")
        assert second.status_code == 200, second.text

        assert client.get("/api/menu").status_code == 200
        assert client.get("/api/inventory/lots").status_code == 200


def test_bootstrap_interval_triggers_start_from_persisted_sim_time():
    db.reset_db(keep_reference=False)
    session = db.new_session()
    try:
        state = get_or_create_sim_state(session)
        state.sim_time = 250000.0
        state.status = RUNNING
        session.commit()
    finally:
        session.close()

    api._bootstrap()

    now = api.ctx.clock.sim_time
    interval_triggers = [
        trigger for trigger in api.ctx.orchestrator.triggers
        if trigger.trigger_type == "interval" and trigger.next_due is not None
    ]
    assert interval_triggers
    assert all(trigger.next_due >= now for trigger in interval_triggers)


def test_play_route_allows_basic_read():
    db.reset_db(keep_reference=False)
    with TestClient(api.app) as client:
        assert client.post("/api/seed/preset/bellas_kitchen").status_code == 200
        assert client.post("/api/sim/play").status_code == 200
        response = client.get("/api/menu")
        client.post("/api/sim/pause")
    assert response.status_code == 200


def test_dashboard_reads_survive_running_sim_concurrently():
    db.reset_db(keep_reference=False)

    endpoints = [
        "/api/menu",
        "/api/weather",
        "/api/inventory/lots",
        "/api/scenarios",
        "/api/sim/pos",
    ]

    with TestClient(api.app) as client:
        seed = client.post("/api/seed/preset/bellas_kitchen")
        assert seed.status_code == 200, seed.text

        play = client.post("/api/sim/play")
        assert play.status_code == 200, play.text

        def fetch(path: str) -> int:
            response = client.get(path)
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(
                executor.map(fetch, endpoints * 3)
            )

        client.post("/api/sim/pause")

    assert all(status == 200 for status in statuses)


def test_running_sim_periodically_updates_core_feeds(monkeypatch):
    db.reset_db(keep_reference=False)
    monkeypatch.setattr(api.config, "TICK_REAL_MS", 10)
    monkeypatch.setattr(api.config, "WEATHER_FETCH_SIM_S", 30)

    def fake_weather(_self, _url, _params):
        return {
            "current": {
                "temperature_2m": 24.0,
                "precipitation": 1.0,
                "wind_speed_10m": 12.0,
                "weather_code": 61,
            }
        }

    monkeypatch.setattr(WeatherProvider, "_http_get", fake_weather)

    with TestClient(api.app) as client:
        seed = client.post("/api/seed/preset/bellas_kitchen")
        assert seed.status_code == 200, seed.text
        assert client.post("/api/sim/speed", json={"speed": 8}).status_code == 200
        assert client.post("/api/sim/play").status_code == 200

        deadline = time.monotonic() + 3.0
        saw_ledger = saw_order = saw_weather = False
        while time.monotonic() < deadline:
            # Track B's InventoryLedger depletes stock per order line (no
            # interval trigger needed — it reacts to ORDER_CREATED), so this
            # feed updates as soon as the POS sim produces an order.
            ledger = client.get("/api/inventory/ledger").json()
            weather = client.get("/api/weather").json()
            with db.DB_LOCK:
                session = db.new_session()
                try:
                    saw_order = (
                        session.query(models.Order)
                        .filter(models.Order.sim_time >= 0)
                        .count()
                    ) > 0
                finally:
                    session.close()
            saw_ledger = bool(ledger)
            saw_weather = bool(weather and weather["sim_time"] >= 0)
            if saw_ledger and saw_order and saw_weather:
                break
            time.sleep(0.05)

        client.post("/api/sim/pause")

    assert saw_ledger
    assert saw_order
    assert saw_weather


def test_restart_reseeds_active_preset_and_clears_runtime_rows():
    db.reset_db(keep_reference=False)
    with TestClient(api.app) as client:
        seed = client.post("/api/seed/preset/bellas_kitchen")
        assert seed.status_code == 200, seed.text

        menu = client.get("/api/menu").json()
        assert menu
        first_item = menu[0]
        changed = client.patch(
            f"/api/menu/{first_item['id']}",
            json={"name": "Changed By Test"},
        )
        assert changed.status_code == 200, changed.text

        # Seed a Track B intelligence-model runtime row (an INTELLIGENCE_MODELS
        # table, same wipe class as Forecast) the way a completed negotiation
        # would land via Market Spectator's CALL_OUTCOME handling.
        with db.DB_LOCK:
            session = db.new_session()
            try:
                supplier = session.query(models.Supplier).first()
                ingredient = session.query(models.Ingredient).first()
                session.add(models.Negotiation(
                    supplier_id=supplier.id,
                    ingredient_id=ingredient.id,
                    outcome={"result": "accepted"},
                    sim_time=0.0,
                ))
                session.commit()
            finally:
                session.close()
        assert client.get("/api/negotiations").json()

        restart = client.post("/api/sim/restart")
        assert restart.status_code == 200, restart.text
        assert restart.json()["status"] == "stopped"
        assert restart.json()["sim_time"] == 28800.0

        restored_menu = client.get("/api/menu").json()
        restored = next(item for item in restored_menu if item["id"] == first_item["id"])
        assert restored["name"] == first_item["name"]
        assert client.get("/api/negotiations").json() == []

        with db.DB_LOCK:
            session = db.new_session()
            try:
                state = session.get(models.SimState, 1)
                assert state.active_seed_id == "bellas_kitchen"
                assert session.query(models.Order).count() > 0
                assert session.query(models.Scenario).count() > 0
            finally:
                session.close()
