"""FastAPI application — all REST routes (§20) + the WebSocket hub (§21).

This is the Layer-4 shell that fronts ``core``. On startup it wires the whole
simulation together (clock, bus, orchestrator, POS sim, formatter, voice,
weather, calls, approvals, seeding, scenarios, LLM) behind one app, starts the
orchestrator's ``run_loop`` as a background task, and exposes:

- the **WebSocket hub** at ``/ws`` (one connection; server pushes ``{event,
  payload}``; §21), with a thread-safe ``enqueue`` so synchronous producers
  (the formatter / weather / approvals / calls broadcast sinks, and the
  per-tick orchestrator output) can publish from any thread/loop context;
- the full **REST contract** under ``/api`` (§20): sim control, seeding,
  CRUD editing, weather, reads, actions, and scenarios.

All routes return proper HTTP status codes via :class:`HTTPException` (404 for
missing rows, 422 for bad input, 409 for conflicts such as starting an
already-running sim). CORS is wide open for local dev (the Vite proxy handles it
under Docker).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, db, models
from .approvals import ApprovalsHub
from .bus import SignalBus
from .calls import CallSubsystem
from .clock import SimClock, get_or_create_sim_state
from .formatter import DataFormatter
from .llm import LLMProvider
from .orchestrator import Orchestrator
from .pos_simulator import POSSimulator
from .scenarios import ScenarioEngine
from .seeding import Seeder
from .signals import SignalType
from .voice import VoiceProcessor
from .weather import WeatherProvider

logger = logging.getLogger(__name__)

# The scenario engine must process due events before the orchestrator's own
# per-tick scenario bookkeeping marks them ``fired``, so it is registered as an
# interval trigger fine-grained enough to fire on *every* tick at any speed:
# the slowest per-tick sim-advance is ``60 × min(SPEEDS) × 0.25``.
SCENARIO_TICK_INTERVAL_SIM_S = 60.0 * min(config.SPEEDS) * 0.25


# ===========================================================================
# WebSocket hub (§21)
# ===========================================================================

class WebSocketHub:
    """Maintains the set of active WS connections and broadcasts to all of them.

    The orchestrator's ``broadcast_fn`` and every synchronous broadcast sink
    funnel through :meth:`enqueue`, which is safe to call from any thread (route
    handlers run in a threadpool; the tick loop runs on the event loop). A
    single async drain task serialises the queued messages out to all sockets.
    """

    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._drain_task: Optional[asyncio.Task] = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._drain_task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        self._loop = None
        self.connections.clear()

    # -- connections --------------------------------------------------------

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    # -- publishing ---------------------------------------------------------

    def enqueue(self, message: Dict[str, Any]) -> None:
        """Queue one ``{event, payload}`` message for broadcast (thread-safe)."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._queue.put_nowait, message)
        except RuntimeError:
            # Loop is closing / closed — drop the message.
            pass

    def broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        """Sink shape ``fn(event, payload)`` used by the core broadcast hooks."""
        self.enqueue({"event": event, "payload": payload})

    def broadcast_events(self, events: List[Dict[str, Any]]) -> None:
        """Orchestrator ``broadcast_fn``: enqueue each per-tick WS event."""
        for event in events:
            self.enqueue(event)

    async def _drain(self) -> None:
        while True:
            message = await self._queue.get()
            await self._send(message)

    async def _send(self, message: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        for websocket in list(self.connections):
            try:
                await websocket.send_json(message)
            except Exception:  # noqa: BLE001 — a dead socket must not stop others.
                dead.append(websocket)
        for websocket in dead:
            self.connections.discard(websocket)


# ===========================================================================
# Application context (singletons wired at startup)
# ===========================================================================

class AppContext:
    """Holds the wired core singletons for the lifetime of the process."""

    def __init__(self) -> None:
        self.bus: Optional[SignalBus] = None
        self.clock: Optional[SimClock] = None
        self.orchestrator: Optional[Orchestrator] = None
        self.llm: Optional[LLMProvider] = None
        self.voice: Optional[VoiceProcessor] = None
        self.seeder: Optional[Seeder] = None
        self.weather: Optional[WeatherProvider] = None
        self.calls: Optional[CallSubsystem] = None
        self.approvals: Optional[ApprovalsHub] = None
        self.formatter: Optional[DataFormatter] = None
        self.pos: Optional[POSSimulator] = None
        self.scenarios: Optional[ScenarioEngine] = None
        self.hub: WebSocketHub = WebSocketHub()
        self.loop_task: Optional[asyncio.Task] = None
        # Per-track component dicts returned by each track's register(), e.g.
        # ctx.tracks["track_b"]["market_spectator"] — used by REST endpoints
        # that need to call into a track's agents (e.g. the Negotiate button).
        self.tracks: Dict[str, Dict[str, Any]] = {}


ctx = AppContext()


def _ensure_settings_singleton(session: Any) -> models.SimSettings:
    """Create the ``sim_settings`` singleton (id=1) with §22 defaults if absent."""
    settings = session.get(models.SimSettings, 1)
    if settings is None:
        settings = models.SimSettings(
            id=1,
            base_orders_per_day=config.BASE_ORDERS_PER_DAY,
            velocity=1.0,
            dish_mix_weights={},
            daypart_curve=None,
            channel_mix=dict(config.CHANNEL_MIX),
            anomaly_injections=None,
        )
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings


def _bootstrap() -> None:
    """Create tables + singletons and wire every core object (§ app bootstrap)."""
    db.create_all()

    factory = db.new_session

    # Singletons: sim_state (id=1) + sim_settings (id=1) if absent.
    session = factory()
    try:
        get_or_create_sim_state(session)
        _ensure_settings_singleton(session)
    finally:
        session.close()

    bus = SignalBus(factory)
    clock = SimClock(factory, bus)
    # Docker persists sim_state across container restarts. Seed the bus clock
    # before registering interval triggers so their first next_due is relative
    # to the restored sim time, not zero. Otherwise a running persisted sim can
    # spend startup catching up every historical interval.
    bus.sim_time = clock.sim_time
    orchestrator = Orchestrator(clock, bus, factory)
    orchestrator.coordinator = db.DB_LOCK
    llm = LLMProvider()
    voice = VoiceProcessor(llm, bus, factory)
    seeder = Seeder(llm, factory)
    weather = WeatherProvider(bus, factory, clock)
    approvals = ApprovalsHub(bus, factory)
    calls = CallSubsystem(bus, factory, clock, llm)
    formatter = DataFormatter(bus, factory)
    pos = POSSimulator(bus, factory, clock, formatter, weather=weather)
    scenarios = ScenarioEngine(bus, factory, clock, pos, weather)

    # Wire the WS broadcast sinks (§21). order_created flows through the
    # formatter; the POS simulator publishes via that same sink (was a no-op
    # stub in Session 4 — connected here).
    sink = ctx.hub.broadcast
    formatter.set_ws_broadcast(sink)
    weather.set_ws_broadcast(sink)
    approvals.set_ws_broadcast(sink)
    calls.set_ws_broadcast(sink)
    calls.attach_approvals(approvals)

    # §14.4 agent-group routing: every signal type fans out to
    # ``orchestrator.on_signal`` (which dispatches to each registered agent
    # whose subscribed groups intersect the signal's groups). Wired via the
    # same generic ``bus.subscribe`` dispatch path used by the call subsystem
    # / approvals — fires once per genuine new emit, never on a dedup-refresh.
    for _sig_type in SignalType:
        bus.subscribe(_sig_type, orchestrator.on_signal)

    # Register the §17 triggers core owns: the weather fetch, the POS arrival
    # generator, the approvals-expiry sweep, and the scenario engine.
    weather.register(orchestrator)
    pos.register(orchestrator)
    approvals.register(orchestrator)
    orchestrator.register(
        "interval",
        lambda: scenarios.tick(clock.sim_time),
        interval_sim_s=SCENARIO_TICK_INTERVAL_SIM_S,
        name="scenario_engine",
    )

    # Ship the flagship Friday Rush scenario on first run.
    scenarios.seed_default_scenario()

    # Wire the track agents/mocks for the active DEMO_MODE (§3.2 / §26.2). The
    # backend imports both tracks; DEMO_MODE selects which mocks run. Track
    # registration is best-effort: a track that hasn't shipped its wiring yet
    # (no ``register``) or that raises must never prevent core from starting.
    demo_mode = os.getenv("DEMO_MODE", "combined")
    _register_tracks(
        demo_mode,
        bus=bus,
        orchestrator=orchestrator,
        db_session_factory=factory,
        llm=llm,
        calls=calls,
        approvals=approvals,
        ws_broadcast=sink,
    )

    ctx.bus = bus
    ctx.clock = clock
    ctx.orchestrator = orchestrator
    ctx.llm = llm
    ctx.voice = voice
    ctx.seeder = seeder
    ctx.weather = weather
    ctx.approvals = approvals
    ctx.calls = calls
    ctx.formatter = formatter
    ctx.pos = pos
    ctx.scenarios = scenarios


def _register_tracks(demo_mode: str, **ctx_kwargs: Any) -> None:
    """Call each active track's ``agents.register(...)`` for ``demo_mode``.

    ``track_b`` / ``track_a`` run their real agents in ``combined`` and in their
    own standalone mode; the standalone mode additionally selects that track's
    mocks (the other track's signals). Each call is isolated so one track's
    wiring problem cannot take down the app."""
    targets = []
    if demo_mode in ("track_b", "combined"):
        targets.append("track_b")
    if demo_mode in ("track_a", "combined"):
        targets.append("track_a")

    for pkg_name in targets:
        try:
            module = __import__(f"{pkg_name}.agents", fromlist=["register"])
        except Exception:  # noqa: BLE001 — a missing/broken track must not crash core.
            logger.exception("Could not import %s.agents", pkg_name)
            continue
        register = getattr(module, "register", None)
        if register is None:
            logger.info("%s.agents has no register() yet — skipping", pkg_name)
            continue
        try:
            ctx.tracks[pkg_name] = register(demo_mode=demo_mode, **ctx_kwargs) or {}
        except Exception:  # noqa: BLE001 — isolate track wiring failures.
            logger.exception("%s.agents.register failed", pkg_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bootstrap on startup; start the tick loop; clean up on shutdown."""
    _bootstrap()
    await ctx.hub.start()
    assert ctx.orchestrator is not None
    ctx.loop_task = asyncio.create_task(
        ctx.orchestrator.run_loop(ctx.hub.broadcast_events)
    )
    try:
        yield
    finally:
        if ctx.orchestrator is not None:
            ctx.orchestrator.stop_loop()
        if ctx.loop_task is not None:
            ctx.loop_task.cancel()
            try:
                await ctx.loop_task
            except asyncio.CancelledError:
                pass
        await ctx.hub.stop()


app = FastAPI(title="Restaurant Multi-Agent Demo — core", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# Helpers
# ===========================================================================

def _row_to_dict(obj: Any) -> Dict[str, Any]:
    """Serialize any ORM row into a plain dict of its column values."""
    data = {col.key: getattr(obj, col.key) for col in obj.__table__.columns}
    if isinstance(obj, models.Forecast):
        data["forecast_qty"] = int(round(float(data.get("forecast_qty") or 0)))
    elif isinstance(obj, models.Batch):
        data["planned_qty"] = int(round(float(data.get("planned_qty") or 0)))
    return data


def _rows_to_dict(rows: List[Any]) -> List[Dict[str, Any]]:
    """Serialize ORM rows immediately while their owning session is open."""
    return [_row_to_dict(row) for row in rows]


def _read_rows(
    db_session: Any,
    model: Any,
    order_by: Optional[Any] = None,
    limit: Optional[int] = None,
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> List[Dict[str, Any]]:
    with db.DB_LOCK:
        query = db_session.query(model)
        if order_by is not None:
            query = query.order_by(order_by)
        if limit is not None:
            query = query.limit(limit)
        rows = _rows_to_dict(query.all())
    if predicate is not None:
        rows = [row for row in rows if predicate(row)]
    return rows


def _coerce_columns(model: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only keys that are real (non-pk) columns on ``model``."""
    cols = set(model.__table__.columns.keys())
    return {k: v for k, v in body.items() if k in cols and k != "id"}


# ===========================================================================
# Sim control (§20)
# ===========================================================================

@app.get("/api/health")
def health() -> Dict[str, Any]:
    """Cheap readiness probe for Docker/Vite startup coordination."""
    return {"ok": True, "sim": ctx.clock.current_state()}


class SpeedBody(BaseModel):
    speed: float


class PosBody(BaseModel):
    base_orders_per_day: Optional[int] = None
    velocity: Optional[float] = None
    dish_mix_weights: Optional[Dict[str, Any]] = None
    channel_mix: Optional[Dict[str, Any]] = None
    daypart_curve: Optional[Dict[str, Any]] = None
    anomaly_injections: Optional[List[Dict[str, Any]]] = None


def _ensure_loop_task() -> None:
    """Start the orchestrator loop if it is absent or previously died."""
    if ctx.orchestrator is None:
        return
    if ctx.loop_task is not None and not ctx.loop_task.done():
        return
    ctx.loop_task = asyncio.create_task(
        ctx.orchestrator.run_loop(ctx.hub.broadcast_events)
    )


@app.post("/api/sim/play")
async def sim_play() -> Dict[str, Any]:
    state = ctx.clock.current_state()
    if state["status"] == SimClock.RUNNING:
        raise HTTPException(status_code=409, detail="Simulation is already running")
    _ensure_loop_task()
    result = ctx.clock.play()
    ctx.hub.broadcast("sim_state_changed", result)
    return result


@app.post("/api/sim/pause")
def sim_pause() -> Dict[str, Any]:
    state = ctx.clock.current_state()
    if state["status"] == SimClock.STOPPED:
        raise HTTPException(status_code=409, detail="Cannot pause a stopped simulation")
    result = ctx.clock.pause()
    ctx.hub.broadcast("sim_state_changed", result)
    return result


@app.post("/api/sim/stop")
def sim_stop() -> Dict[str, Any]:
    result = ctx.clock.stop()
    ctx.hub.broadcast("sim_state_changed", result)
    return result


@app.post("/api/sim/restart")
def sim_restart() -> Dict[str, Any]:
    with db.DB_LOCK:
        # Halt the tick loop before the destructive reseed so it cannot write to
        # tables mid-wipe.
        ctx.clock.stop()

        # Capture the active seed before wiping everything.
        session = db.new_session()
        try:
            state = session.get(models.SimState, 1)
            active_seed = state.active_seed_id if state is not None else None
        finally:
            session.close()

        def _reseed() -> None:
            _wipe_for_seed()
            if active_seed:
                try:
                    data = ctx.seeder.load_preset(active_seed)
                    _apply_bundle_singletons(data, active_seed)
                except FileNotFoundError:
                    logger.warning("Restart: active seed %r not found", active_seed)
            # The wipe cleared scenarios - re-ship the flagship.
            ctx.scenarios.seed_default_scenario()

        result = ctx.clock.restart(_reseed)
    ctx.hub.broadcast("sim_state_changed", result)
    return result


@app.post("/api/sim/step")
def sim_step() -> Dict[str, Any]:
    result = ctx.clock.step()
    ctx.hub.broadcast("sim_state_changed", result)
    return result


@app.post("/api/sim/jump-next")
def sim_jump_next() -> Dict[str, Any]:
    result = ctx.clock.jump_to_next_event()
    ctx.hub.broadcast("sim_state_changed", result)
    return result


@app.post("/api/sim/speed")
def sim_speed(body: SpeedBody) -> Dict[str, Any]:
    try:
        result = ctx.clock.set_speed(body.speed)
        ctx.hub.broadcast("sim_state_changed", result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/sim/state")
def sim_state() -> Dict[str, Any]:
    return ctx.clock.current_state()


@app.get("/api/sim/pos")
def get_sim_pos() -> Dict[str, Any]:
    with db.DB_LOCK:
        session = db.new_session()
        try:
            settings = _ensure_settings_singleton(session)
            return _row_to_dict(settings)
        finally:
            session.close()


@app.patch("/api/sim/pos")
def sim_pos(body: PosBody) -> Dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No POS settings provided")
    with db.DB_LOCK:
        session = db.new_session()
        try:
            settings = _ensure_settings_singleton(session)
            for field, value in updates.items():
                setattr(settings, field, value)
            session.commit()
            session.refresh(settings)
            return _row_to_dict(settings)
        finally:
            session.close()


# ===========================================================================
# Seeding / generation (§20)
# ===========================================================================

class GenerateBody(BaseModel):
    cuisine: str
    size_params: Dict[str, Any] = {}

# Tables wiped on reseed — everything except the sim_state / sim_settings
# singletons, which are kept in place (and updated from the bundle) so the
# background tick loop never observes a missing singleton and re-creates it,
# which would race the bundle insert.
_WIPE_MODELS = (
    models.INTELLIGENCE_MODELS
    + models.TRANSACTIONAL_MODELS
    + [models.ScenarioEvent, models.Scenario, models.EventLog]
    + models.REFERENCE_MODELS
)

# sim_state fields the bundle may carry (all but id / active_seed_id).
_SIM_STATE_FIELDS = (
    "sim_time", "day_number", "day_of_week", "speed", "status",
    "operating_window", "skip_closed_hours", "call_mode",
)
_SIM_SETTINGS_FIELDS = (
    "base_orders_per_day", "velocity", "dish_mix_weights", "daypart_curve",
    "channel_mix", "anomaly_injections",
)


def _wipe_for_seed() -> None:
    """Delete every row except the sim singletons (idempotent reseed prep)."""
    with db.session_scope(coordinated=True) as session:
        keep = {models.SimState.__tablename__, models.SimSettings.__tablename__}
        for table in reversed(db.Base.metadata.sorted_tables):
            if table.name in keep:
                continue
            session.execute(table.delete())


def _apply_bundle_singletons(data: Dict[str, Any], preset_id: Optional[str]) -> None:
    """Apply a bundle's ``sim_state`` / ``sim_settings`` onto the kept
    singletons (in-place update, not insert) and stamp the active seed."""
    with db.session_scope(coordinated=True) as session:
        state = get_or_create_sim_state(session)
        bundle_state = data.get("sim_state") or {}
        for field in _SIM_STATE_FIELDS:
            if field in bundle_state:
                setattr(state, field, bundle_state[field])
        state.active_seed_id = preset_id

        settings = _ensure_settings_singleton(session)
        bundle_settings = data.get("sim_settings") or {}
        for field in _SIM_SETTINGS_FIELDS:
            if field in bundle_settings:
                setattr(settings, field, bundle_settings[field])


def _bundle_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a per-table row count for an inserted seed bundle."""
    return {
        key: len(value)
        for key, value in data.items()
        if isinstance(value, list)
    }


@app.get("/api/seed/presets")
def seed_presets() -> List[str]:
    return ctx.seeder.list_presets()


@app.post("/api/seed/preset/{preset_id}")
def seed_preset(preset_id: str) -> Dict[str, Any]:
    if preset_id not in ctx.seeder.list_presets():
        raise HTTPException(status_code=404, detail=f"Unknown preset {preset_id!r}")
    with db.DB_LOCK:
        # Halt the loop, then wipe (keeping the singletons) so reseeding is
        # idempotent without racing the loop on the sim_state singleton.
        ctx.clock.stop()
        _wipe_for_seed()
        try:
            data = ctx.seeder.load_preset(preset_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _apply_bundle_singletons(data, preset_id)
        ctx.scenarios.seed_default_scenario()
    return {"preset_id": preset_id, "inserted": _bundle_summary(data)}


@app.post("/api/seed/generate")
def seed_generate(body: GenerateBody) -> Dict[str, Any]:
    with db.DB_LOCK:
        ctx.clock.stop()
        _wipe_for_seed()
        data = ctx.seeder.generate(body.cuisine, body.size_params)
        _apply_bundle_singletons(data, None)
        ctx.scenarios.seed_default_scenario()
    return {"cuisine": body.cuisine, "inserted": _bundle_summary(data)}


# ===========================================================================
# CRUD editing (§20) — GET list / POST create / PATCH {id} / DELETE {id}
# ===========================================================================

CRUD_RESOURCES = {
    "ingredients": models.Ingredient,
    "menu": models.MenuItem,
    "recipes": models.Recipe,
    "staff": models.Staff,
    "suppliers": models.Supplier,
    "supplier-catalog": models.SupplierCatalog,
    "inventory": models.InventoryLevel,
    "competitors": models.Competitor,
    "reviews": models.Review,
    # scenario_events get full CRUD here; scenarios use custom GET (nested) below
    "scenario_events": models.ScenarioEvent,
}


def _register_crud(prefix: str, model: Any) -> None:
    tag = prefix

    def list_rows(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
        return _read_rows(db_session, model)

    def create_row(
        body: Dict[str, Any] = Body(...),
        db_session: Any = Depends(db.get_db),
    ) -> Dict[str, Any]:
        data = _coerce_columns(model, body)
        if not data:
            raise HTTPException(status_code=422, detail="No valid fields provided")
        try:
            obj = model(**data)
            db_session.add(obj)
            db_session.commit()
            db_session.refresh(obj)
        except Exception as exc:  # noqa: BLE001 — bad input -> 422
            db_session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _row_to_dict(obj)

    def patch_row(
        item_id: int,
        body: Dict[str, Any] = Body(...),
        db_session: Any = Depends(db.get_db),
    ) -> Dict[str, Any]:
        obj = db_session.get(model, item_id)
        if obj is None:
            raise HTTPException(status_code=404, detail=f"{tag} {item_id} not found")
        data = _coerce_columns(model, body)
        try:
            for field, value in data.items():
                setattr(obj, field, value)
            db_session.commit()
            db_session.refresh(obj)
        except Exception as exc:  # noqa: BLE001 — bad input -> 422
            db_session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _row_to_dict(obj)

    def delete_row(
        item_id: int,
        db_session: Any = Depends(db.get_db),
    ) -> Dict[str, Any]:
        obj = db_session.get(model, item_id)
        if obj is None:
            raise HTTPException(status_code=404, detail=f"{tag} {item_id} not found")
        db_session.delete(obj)
        db_session.commit()
        return {"deleted": item_id}

    app.add_api_route(f"/api/{prefix}", list_rows, methods=["GET"], name=f"{tag}_list")
    app.add_api_route(f"/api/{prefix}", create_row, methods=["POST"], name=f"{tag}_create")
    app.add_api_route(
        f"/api/{prefix}/{{item_id}}", patch_row, methods=["PATCH"], name=f"{tag}_patch"
    )
    app.add_api_route(
        f"/api/{prefix}/{{item_id}}", delete_row, methods=["DELETE"], name=f"{tag}_delete"
    )


for _prefix, _model in CRUD_RESOURCES.items():
    _register_crud(_prefix, _model)


# ===========================================================================
# Weather (§20)
# ===========================================================================

class WeatherOverrideBody(BaseModel):
    temp_c: float
    condition: str
    precip_mm: float
    wind_kph: float


@app.get("/api/weather")
def weather_current() -> Optional[Dict[str, Any]]:
    with db.DB_LOCK:
        row = ctx.weather.current()
    return _row_to_dict(row) if row is not None else None


@app.post("/api/weather/override")
def weather_override(body: WeatherOverrideBody) -> Dict[str, Any]:
    row = ctx.weather.override(
        temp_c=body.temp_c,
        condition=body.condition,
        precip_mm=body.precip_mm,
        wind_kph=body.wind_kph,
    )
    return _row_to_dict(row)


# ===========================================================================
# Read endpoints (§20)
# ===========================================================================

@app.get("/api/forecasts")
def read_forecasts(
    menu_item_id: Optional[int] = Query(None),
    since: Optional[float] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    query = db_session.query(models.Forecast)
    if menu_item_id is not None:
        query = query.filter(models.Forecast.menu_item_id == menu_item_id)
    if since is not None:
        query = query.filter(models.Forecast.generated_at >= since)
    return [_row_to_dict(r) for r in query.order_by(models.Forecast.generated_at.asc()).all()]


@app.get("/api/inventory/ledger")
def read_inventory_ledger(
    ingredient_id: Optional[int] = Query(None),
    since: Optional[float] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    query = db_session.query(models.InventoryLedger)
    if ingredient_id is not None:
        query = query.filter(models.InventoryLedger.ingredient_id == ingredient_id)
    if since is not None:
        query = query.filter(models.InventoryLedger.sim_time >= since)
    return [_row_to_dict(r) for r in query.order_by(models.InventoryLedger.sim_time.asc()).all()]


@app.get("/api/signals")
def read_signals(
    status: Optional[str] = Query(None),
    group: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    query = db_session.query(models.Signal)
    if status is not None:
        query = query.filter(models.Signal.status == status)
    if type is not None:
        query = query.filter(models.Signal.type == type)
    rows = query.order_by(models.Signal.created_at.asc()).all()
    result = [_row_to_dict(r) for r in rows]
    if group is not None:
        result = [r for r in result if group in (r.get("groups") or [])]
    return result


@app.get("/api/approvals")
def read_approvals(
    status: Optional[str] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    query = db_session.query(models.ApprovalRequest)
    if status is not None:
        query = query.filter(models.ApprovalRequest.status == status)
    return [_row_to_dict(r) for r in query.order_by(models.ApprovalRequest.created_at.asc()).all()]


@app.get("/api/events")
def read_events(
    since: Optional[float] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    query = db_session.query(models.EventLog)
    if since is not None:
        query = query.filter(models.EventLog.sim_time >= since)
    return [_row_to_dict(r) for r in query.order_by(models.EventLog.sim_time.asc()).all()]


@app.get("/api/batches")
def read_batches(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
    return [_row_to_dict(r) for r in db_session.query(models.Batch).all()]


@app.get("/api/waste")
def read_waste(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
    return [
        _row_to_dict(r)
        for r in db_session.query(models.WasteEvent).order_by(models.WasteEvent.sim_time.asc()).all()
    ]


@app.get("/api/purchase-orders")
def read_purchase_orders(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
    return [_row_to_dict(r) for r in db_session.query(models.PurchaseOrder).all()]


@app.get("/api/inventory/lots")
def read_inventory_lots(
    ingredient_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    query = db_session.query(models.InventoryLot)
    if ingredient_id is not None:
        query = query.filter(models.InventoryLot.ingredient_id == ingredient_id)
    if status is not None:
        query = query.filter(models.InventoryLot.status == status)
    return [_row_to_dict(r) for r in query.order_by(models.InventoryLot.expiry_date.asc()).all()]


@app.get("/api/promotions")
def read_promotions(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
    return [_row_to_dict(r) for r in db_session.query(models.Promotion).order_by(models.Promotion.sim_time.asc()).all()]


@app.get("/api/negotiations")
def read_negotiations(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
    return [_row_to_dict(r) for r in db_session.query(models.Negotiation).order_by(models.Negotiation.sim_time.asc()).all()]


@app.get("/api/competitor-intel")
def read_competitor_intel(
    competitor_id: Optional[int] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    query = db_session.query(models.CompetitorIntel)
    if competitor_id is not None:
        query = query.filter(models.CompetitorIntel.competitor_id == competitor_id)
    return [_row_to_dict(r) for r in query.order_by(models.CompetitorIntel.sim_time.asc()).all()]


@app.get("/api/calls")
def read_calls(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
    return [_row_to_dict(r) for r in db_session.query(models.Call).all()]


# ===========================================================================
# Action endpoints (§20)
# ===========================================================================

class VoiceBody(BaseModel):
    text: str


class CallTurnBody(BaseModel):
    role: str
    text: str


@app.post("/api/approvals/{approval_id}/approve")
def approve_approval(approval_id: int) -> Dict[str, Any]:
    row = ctx.approvals.approve(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    return _row_to_dict(row)


@app.post("/api/approvals/{approval_id}/reject")
def reject_approval(approval_id: int) -> Dict[str, Any]:
    row = ctx.approvals.reject(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    return _row_to_dict(row)


@app.post("/api/voice/transcript")
def voice_transcript(body: VoiceBody) -> Dict[str, Any]:
    return ctx.voice.process(body.text)


@app.post("/api/calls/{call_id}/turn")
def call_turn(call_id: int, body: CallTurnBody) -> Dict[str, Any]:
    session = db.new_session()
    try:
        if session.get(models.Call, call_id) is None:
            raise HTTPException(status_code=404, detail=f"Call {call_id} not found")
    finally:
        session.close()
    return ctx.calls.add_turn(call_id, body.role, body.text)


@app.post("/api/calls/{call_id}/end")
def call_end(call_id: int) -> Dict[str, Any]:
    session = db.new_session()
    try:
        if session.get(models.Call, call_id) is None:
            raise HTTPException(status_code=404, detail=f"Call {call_id} not found")
    finally:
        session.close()
    outcome = ctx.calls.end_call(call_id)
    return {"call_id": call_id, "outcome": outcome}


class NegotiateBody(BaseModel):
    supplier_id: int
    ingredient_id: int


@app.post("/api/market/negotiate")
def market_negotiate(body: NegotiateBody) -> Dict[str, Any]:
    """Presenter-triggered negotiation (SupplierEditor's "Negotiate" button,
    02 §B6) — routes into Track B's Market Spectator, which creates the
    approval-gated outbound call (§8.2)."""
    market = (ctx.tracks.get("track_b") or {}).get("market_spectator")
    if market is None:
        raise HTTPException(status_code=503, detail="Market Spectator not active")
    market.negotiate(body.supplier_id, body.ingredient_id)
    return {"supplier_id": body.supplier_id, "ingredient_id": body.ingredient_id, "requested": True}


# ===========================================================================
# Scenarios (§20)
# ===========================================================================


@app.post("/api/scenarios")
def create_scenario(
    body: Dict[str, Any] = Body(...),
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    data = _coerce_columns(models.Scenario, body)
    if not data:
        raise HTTPException(status_code=422, detail="No valid fields provided")
    try:
        obj = models.Scenario(**data)
        db_session.add(obj)
        db_session.commit()
        db_session.refresh(obj)
    except Exception as exc:  # noqa: BLE001
        db_session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _row_to_dict(obj)


@app.patch("/api/scenarios/{scenario_id}")
def update_scenario(
    scenario_id: int,
    body: Dict[str, Any] = Body(...),
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    obj = db_session.get(models.Scenario, scenario_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    data = _coerce_columns(models.Scenario, body)
    try:
        for field, value in data.items():
            setattr(obj, field, value)
        db_session.commit()
        db_session.refresh(obj)
    except Exception as exc:  # noqa: BLE001
        db_session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _row_to_dict(obj)


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(
    scenario_id: int,
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    obj = db_session.get(models.Scenario, scenario_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    db_session.delete(obj)
    db_session.commit()
    return {"deleted": scenario_id}


@app.get("/api/scenarios")
def read_scenarios(db_session: Any = Depends(db.get_db)) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for scenario in db_session.query(models.Scenario).all():
        data = _row_to_dict(scenario)
        data["events"] = [
            _row_to_dict(ev)
            for ev in db_session.query(models.ScenarioEvent)
            .filter(models.ScenarioEvent.scenario_id == scenario.id)
            .order_by(models.ScenarioEvent.at_sim_time.asc())
            .all()
        ]
        out.append(data)
    return out


@app.post("/api/scenarios/{scenario_id}/activate")
def activate_scenario(scenario_id: int) -> Dict[str, Any]:
    row = ctx.scenarios.activate(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return _row_to_dict(row)


@app.post("/api/scenarios/{scenario_id}/deactivate")
def deactivate_scenario(scenario_id: int) -> Dict[str, Any]:
    row = ctx.scenarios.deactivate(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return _row_to_dict(row)


# ===========================================================================
# WebSocket hub endpoint (§21)
# ===========================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await ctx.hub.connect(websocket)
    try:
        while True:
            # The frontend is a pure consumer (§21); we only read to detect
            # disconnects and keep the socket open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        ctx.hub.disconnect(websocket)
    except Exception:  # noqa: BLE001 — any socket error ends this connection.
        ctx.hub.disconnect(websocket)
