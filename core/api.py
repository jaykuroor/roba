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
import math
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import config, db, models
from .approvals import ApprovalsHub
from .bus import SignalBus
from .calls import CallSubsystem
from .clock import SimClock, get_or_create_sim_state
from .formatter import DataFormatter, line_to_dict, order_to_dict
from .llm import LLMProvider
from .orchestrator import Orchestrator
from .pos_simulator import POSSimulator
from .runtime_policy import InventorySignalPolicy
from .scenarios import ScenarioEngine
from .seeding import Seeder
from .signals import SignalType
from .voice import VoiceProcessor
from .weather import WeatherProvider
from track_a.forecast_jobs import DETERMINISTIC_FORECAST, LLM_FINALIZER, ForecastJobRunner

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
        self.inventory_signal_policy: Optional[InventorySignalPolicy] = None
        self.forecast_jobs: Optional[ForecastJobRunner] = None
        self.hub: WebSocketHub = WebSocketHub()
        self.loop_task: Optional[asyncio.Task] = None
        self.track_a: Dict[str, Any] = {}
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
        try:
            session.commit()
            session.refresh(settings)
        except IntegrityError:
            session.rollback()
            settings = session.get(models.SimSettings, 1)
            if settings is None:
                raise
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
    llm = LLMProvider(db_session_factory=factory)
    inventory_signal_policy = InventorySignalPolicy(
        config.INVENTORY_SHORTAGE_SIGNALS_ENABLED
    )
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

    # Track A: Demand & Sensing. In DEMO_MODE=track_a this also wires the
    # Track A-owned MockInventory; in combined mode it stays disabled.
    try:
        from track_a import bootstrap_track_a

        ctx.track_a = bootstrap_track_a(
            bus=bus,
            db_session_factory=factory,
            orchestrator=orchestrator,
            formatter=formatter,
            calls=calls,
            llm=llm,
            ws_broadcast=sink,
        )
    except Exception:  # noqa: BLE001 - Track A must not prevent core startup.
        logger.exception("Track A failed to bootstrap")
        ctx.track_a = {}

    forecaster = ctx.track_a.get("forecaster")
    if forecaster is not None:
        forecast_jobs = ForecastJobRunner(
            bus=bus,
            db_session_factory=factory,
            forecaster=forecaster,
            approvals=approvals,
            ws_broadcast=sink,
        )
        forecaster.set_forecast_job_enqueue(
            lambda kind, reason: forecast_jobs.enqueue(
                kind,
                trigger_reason=reason,
                requested_by="signal" if reason.startswith("signal:") else "interval",
            )
        )
        bus.subscribe(SignalType.APPROVAL_RESOLVED, forecast_jobs.on_approval_resolved)
        ctx.forecast_jobs = forecast_jobs
    else:
        ctx.forecast_jobs = None

    # Track B: Inventory Management. Registers ledger, optimizer, market
    # spectator and (in track_b standalone mode) the MockForecaster.
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
        inventory_signal_policy=inventory_signal_policy,
    )

    ctx.bus = bus
    ctx.clock = clock
    ctx.orchestrator = orchestrator
    ctx.llm = llm
    ctx.inventory_signal_policy = inventory_signal_policy
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
    if ctx.forecast_jobs is not None:
        ctx.forecast_jobs.start()
    assert ctx.orchestrator is not None
    ctx.loop_task = asyncio.create_task(
        ctx.orchestrator.run_loop(ctx.hub.broadcast_events)
    )
    try:
        yield
    finally:
        if ctx.forecast_jobs is not None:
            ctx.forecast_jobs.stop()
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


class InventorySignalPolicyBody(BaseModel):
    shortage_signals_enabled: bool


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
    # Starting a fresh run after a stop clears the previous run's live orders so
    # the POS monitor starts clean rather than continuing onward. Resuming from
    # a pause keeps them. (The frontend clears its live buffer on the same
    # stopped→running transition.)
    if state["status"] == SimClock.STOPPED:
        _wipe_live_orders()
        ctx.hub.broadcast("pos_reset", {})
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
    ctx.hub.broadcast("pos_reset", {})
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


class SimConfigBody(BaseModel):
    """Editable sim_state config fields (not owned by the clock state machine)."""
    operating_window: Optional[Dict[str, Any]] = None
    skip_closed_hours: Optional[bool] = None
    call_mode: Optional[str] = None  # "freeze" | "slow"


@app.patch("/api/sim/state")
def patch_sim_state(body: SimConfigBody) -> Dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No sim config fields provided")
    with db.DB_LOCK:
        session = db.new_session()
        try:
            state = get_or_create_sim_state(session)
            for field, value in updates.items():
                setattr(state, field, value)
            session.commit()
        finally:
            session.close()
    result = ctx.clock.current_state()
    ctx.hub.broadcast("sim_state_changed", result)
    return result


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


@app.get("/api/runtime/inventory-signal-policy")
def get_inventory_signal_policy() -> Dict[str, Any]:
    if ctx.inventory_signal_policy is None:
        raise HTTPException(status_code=503, detail="Inventory signal policy unavailable")
    return ctx.inventory_signal_policy.snapshot().__dict__


@app.patch("/api/runtime/inventory-signal-policy")
def patch_inventory_signal_policy(body: InventorySignalPolicyBody) -> Dict[str, Any]:
    if ctx.inventory_signal_policy is None:
        raise HTTPException(status_code=503, detail="Inventory signal policy unavailable")
    snapshot = ctx.inventory_signal_policy.set_shortage_signals_enabled(
        body.shortage_signals_enabled
    )
    ctx.hub.broadcast("inventory_signal_policy_changed", snapshot.__dict__)
    return snapshot.__dict__


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



def _wipe_live_orders() -> None:
    """Delete the current run's orders/lines (``sim_time >= 0``), keeping the
    seeded historical rows (negative ``sim_time``) that back the forecast
    baseline. Used when starting a fresh run after a stop so the POS monitor
    starts clean instead of continuing onward."""
    with db.session_scope(coordinated=True) as session:
        session.query(models.OrderLine).filter(
            models.OrderLine.sim_time >= 0
        ).delete(synchronize_session=False)
        session.query(models.Order).filter(
            models.Order.sim_time >= 0
        ).delete(synchronize_session=False)


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



def _sync_bus_to_clock() -> Dict[str, Any]:
    """Keep SignalBus time aligned with the authoritative sim_state row."""
    state = ctx.clock.current_state()
    if ctx.bus is not None:
        ctx.bus.sim_time = float(state.get("sim_time") or 0.0)
    return state


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
        state = _sync_bus_to_clock()
    ctx.hub.broadcast("sim_state_changed", state)
    ctx.hub.broadcast("pos_reset", {})
    return {"preset_id": preset_id, "inserted": _bundle_summary(data), "sim_state": state}


@app.post("/api/seed/generate")
def seed_generate(body: GenerateBody) -> Dict[str, Any]:
    with db.DB_LOCK:
        ctx.clock.stop()
        _wipe_for_seed()
        data = ctx.seeder.generate(body.cuisine, body.size_params)
        _apply_bundle_singletons(data, None)
        ctx.scenarios.seed_default_scenario()
        state = _sync_bus_to_clock()
    ctx.hub.broadcast("sim_state_changed", state)
    ctx.hub.broadcast("pos_reset", {})
    return {"cuisine": body.cuisine, "inserted": _bundle_summary(data), "sim_state": state}


# ===========================================================================
# CRUD editing (§20) — GET list / POST create / PATCH {id} / DELETE {id}
# ===========================================================================

CRUD_RESOURCES = {
    "ingredients": models.Ingredient,
    "menu": models.MenuItem,
    "recipes": models.Recipe,
    "recipe-lines": models.RecipeLine,
    "staff": models.Staff,
    "suppliers": models.Supplier,
    "supplier-catalog": models.SupplierCatalog,
    "inventory": models.InventoryLevel,
    "competitors": models.Competitor,
    "reviews": models.Review,
    # scenario_events get full CRUD here; scenarios use custom GET (nested) below
    "scenario_events": models.ScenarioEvent,
    # Control page editors
    "stations": models.Station,
    "batch-definitions": models.BatchDefinition,
    "promotions": models.Promotion,
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


@app.get("/api/orders")
def read_orders(
    limit: int = Query(50),
    since: Optional[float] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    """Recent POS orders for the monitor's initial backfill.

    Returns newest-first ``[{order, lines}]`` mirroring the live
    ``order_created`` WS payload (minus the ephemeral ``velocity`` map, which
    the client recomputes from the streamed window). Lines are fetched in a
    single ``IN`` query to avoid N+1.
    """
    limit = max(1, min(limit, 200))
    query = db_session.query(models.Order)
    if since is not None:
        query = query.filter(models.Order.sim_time > since)
    orders = query.order_by(models.Order.sim_time.desc()).limit(limit).all()
    if not orders:
        return []
    order_ids = [o.id for o in orders]
    lines = (
        db_session.query(models.OrderLine)
        .filter(models.OrderLine.order_id.in_(order_ids))
        .all()
    )
    lines_by_order: Dict[int, List[Dict[str, Any]]] = {}
    for line in lines:
        lines_by_order.setdefault(line.order_id, []).append(line_to_dict(line))
    return [
        {"order": order_to_dict(o), "lines": lines_by_order.get(o.id, [])}
        for o in orders
    ]


@app.get("/api/pos/stats")
def read_pos_stats(
    since: float = Query(0.0),
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Aggregated POS statistics over ``(since, now]`` for the monitor's window
    selector (Today / last hour / 30m / 15m).

    Computed server-side so totals are accurate for any window regardless of the
    client's bounded live buffer. ``since`` is clamped to ``>= 0`` so the seeded
    negative-``sim_time`` history is never included. Also returns ~24 adaptive
    time buckets across the window for the orders-over-time chart.
    """
    since = max(float(since), 0.0)
    now = float(ctx.clock.sim_time)

    order_filter = models.Order.sim_time > since
    line_filter = models.OrderLine.sim_time > since

    orders = db_session.query(func.count(models.Order.id)).filter(order_filter).scalar() or 0
    revenue = (
        db_session.query(func.coalesce(func.sum(models.Order.total), 0.0))
        .filter(order_filter)
        .scalar()
        or 0.0
    )
    channel_split = {
        (channel or "unknown"): count
        for channel, count in db_session.query(
            models.Order.channel, func.count(models.Order.id)
        )
        .filter(order_filter)
        .group_by(models.Order.channel)
        .all()
    }
    lines = db_session.query(func.count(models.OrderLine.id)).filter(line_filter).scalar() or 0
    voided_lines = (
        db_session.query(func.count(models.OrderLine.id))
        .filter(line_filter, models.OrderLine.status == "voided")
        .scalar()
        or 0
    )
    top_items = [
        {"menu_item_id": menu_item_id, "qty": float(qty or 0.0)}
        for menu_item_id, qty in db_session.query(
            models.OrderLine.menu_item_id, func.sum(models.OrderLine.qty)
        )
        .filter(line_filter, models.OrderLine.status != "voided")
        .group_by(models.OrderLine.menu_item_id)
        .order_by(func.sum(models.OrderLine.qty).desc())
        .limit(8)
        .all()
    ]

    # ~24 adaptive buckets (min 60 sim-s wide) for the orders-over-time chart.
    span = max(now - since, 1.0)
    width = max(60.0, span / 24.0)
    bucket_count = int(math.ceil(span / width))
    counts = [0] * (bucket_count + 1)
    for (sim_time,) in db_session.query(models.Order.sim_time).filter(order_filter).all():
        idx = int((float(sim_time) - since) // width)
        if 0 <= idx < len(counts):
            counts[idx] += 1
    buckets = [
        {"t": since + i * width, "orders": count} for i, count in enumerate(counts)
    ]

    return {
        "since": since,
        "now": now,
        "orders": int(orders),
        "revenue": float(revenue),
        "lines": int(lines),
        "voided_lines": int(voided_lines),
        "channel_split": channel_split,
        "top_items": top_items,
        "buckets": buckets,
    }


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

# ===========================================================================
# Track A reads/actions
# ===========================================================================

class TrackAStaffBody(BaseModel):
    staff_id: Optional[int] = None
    station_id: Optional[int] = None
    daypart: Optional[str] = None
    status: str = "sick"
    reason: str = "called in sick"


class TrackAForecastAutoBody(BaseModel):
    enabled: bool


def _track_a_agent(name: str) -> Any:
    agent = ctx.track_a.get(name)
    if agent is None:
        raise HTTPException(status_code=503, detail=f"Track A agent {name!r} is not available")
    return agent


@app.get("/api/track-a/snapshot")
def track_a_snapshot(db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    track_groups = {"forecasting", "sensing", "human"}
    signals = _read_rows(
        db_session,
        models.Signal,
        order_by=models.Signal.created_at.desc(),
        predicate=lambda row: row.get("status") == "live"
        and bool(track_groups.intersection(set(row.get("groups") or []))),
    )
    forecaster = ctx.track_a.get("forecaster")
    return {
        "demo_mode": os.getenv("DEMO_MODE", "combined"),
        "sim_state": ctx.clock.current_state(),
        "menu_items": _read_rows(db_session, models.MenuItem, models.MenuItem.id.asc()),
        "forecasts": _read_rows(db_session, models.Forecast, models.Forecast.generated_at.desc(), 100),
        "batches": _read_rows(db_session, models.Batch, models.Batch.decided_at.desc(), 50),
        "demand_memory": _read_rows(
            db_session,
            models.DemandForecasterMemory,
            models.DemandForecasterMemory.last_seen_at.desc(),
            40,
        ),
        "forecast_overrides": _read_rows(
            db_session,
            models.ForecastOverride,
            models.ForecastOverride.created_at.desc(),
            50,
        ),
        "forecast_traces": _read_rows(
            db_session,
            models.ForecastTrace,
            models.ForecastTrace.created_at.desc(),
            100,
        ),
        "forecast_adjustments": _read_rows(
            db_session,
            models.ForecastAdjustment,
            models.ForecastAdjustment.created_at.desc(),
            300,
        ),
        "forecast_jobs": _read_rows(
            db_session,
            models.ForecastJob,
            models.ForecastJob.created_at.desc(),
            20,
        ),
        "forecast_reasoning": _read_rows(
            db_session,
            models.EventLog,
            models.EventLog.sim_time.desc(),
            80,
            predicate=lambda row: row.get("category") in {"forecast", "batch"},
        ),
        "competitors": _read_rows(db_session, models.Competitor, models.Competitor.id.asc()),
        "competitor_offers": _read_rows(db_session, models.CompetitorOffer, models.CompetitorOffer.id.asc()),
        "competitor_intel": _read_rows(db_session, models.CompetitorIntel, models.CompetitorIntel.sim_time.desc(), 50),
        "competitor_observations": _read_rows(
            db_session,
            models.CompetitorObservation,
            models.CompetitorObservation.sim_time.desc(),
            80,
        ),
        "competitor_menu_snapshots": _read_rows(
            db_session,
            models.CompetitorMenuSnapshot,
            models.CompetitorMenuSnapshot.fetched_at.desc(),
            30,
        ),
        "competitor_probe_results": _read_rows(
            db_session,
            models.CompetitorProbeResult,
            models.CompetitorProbeResult.sim_time.desc(),
            30,
        ),
        "reviews": _read_rows(db_session, models.Review, models.Review.sim_time.desc(), 50),
        "review_insights": _read_rows(db_session, models.ReviewInsight, models.ReviewInsight.sim_time.desc(), 50),
        "stations": _read_rows(db_session, models.Station, models.Station.id.asc()),
        "staff": _read_rows(db_session, models.Staff, models.Staff.id.asc()),
        "staff_stations": _read_rows(db_session, models.StaffStation, models.StaffStation.id.asc()),
        "attendance": _read_rows(db_session, models.Attendance, models.Attendance.sim_time.desc(), 50),
        "signals": signals,
        "events": _read_rows(db_session, models.EventLog, models.EventLog.sim_time.desc(), 50),
        "forecast_agent": {
            "llm_auto_mode": bool(getattr(forecaster, "llm_auto_mode", False)),
        },
    }


@app.post("/api/track-a/forecast/run")
def track_a_run_forecast() -> Dict[str, Any]:
    _track_a_agent("forecaster")
    if ctx.forecast_jobs is None:
        raise HTTPException(status_code=503, detail="Forecast job runner is not available")
    _sync_bus_to_clock()
    job = ctx.forecast_jobs.enqueue(
        DETERMINISTIC_FORECAST,
        trigger_reason="manual",
        requested_by="user",
    )
    return {"job_id": job["job_id"], "status": job["status"], "job": job}


@app.post("/api/track-a/forecast/finalize")
def track_a_finalize_forecast() -> Dict[str, Any]:
    _track_a_agent("forecaster")
    if ctx.forecast_jobs is None:
        raise HTTPException(status_code=503, detail="Forecast job runner is not available")
    _sync_bus_to_clock()
    job = ctx.forecast_jobs.enqueue(
        LLM_FINALIZER,
        trigger_reason="manual_llm_review",
        requested_by="user",
    )
    return {"job_id": job["job_id"], "status": job["status"], "job": job}


@app.post("/api/track-a/forecast/optimize")
def track_a_optimize_forecast() -> Dict[str, Any]:
    return track_a_finalize_forecast()


@app.post("/api/track-a/forecast/auto-mode")
def track_a_forecast_auto_mode(body: TrackAForecastAutoBody) -> Dict[str, Any]:
    forecaster = _track_a_agent("forecaster")
    with db.DB_LOCK:
        _sync_bus_to_clock()
        return forecaster.set_auto_mode(body.enabled)


def _expire_constraint_override(override: models.ForecastOverride, now: float) -> int:
    override.status = "expired"
    override.valid_until = min(float(override.valid_until or now), now)
    return int(override.id)


def _expire_constraint_signal(signal: models.Signal, now: float) -> str:
    signal.status = "expired"
    signal.expires_at = min(float(signal.expires_at or now), now)
    return str(signal.signal_id)


def _expire_overrides_for_signal(db_session: Any, signal_id: str, now: float) -> List[int]:
    expired: List[int] = []
    rows = db_session.query(models.ForecastOverride).filter(
        models.ForecastOverride.status == "active"
    ).all()
    for override in rows:
        evidence = override.evidence or {}
        if str(evidence.get("signal_id") or "") != signal_id:
            continue
        expired.append(_expire_constraint_override(override, now))
    return expired


@app.delete("/api/track-a/constraints/{kind}/{identifier}")
def track_a_delete_constraint(
    kind: str,
    identifier: str,
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    _sync_bus_to_clock()
    now = float(ctx.bus.sim_time if ctx.bus is not None else 0.0)
    normalized_kind = kind.strip().lower()
    expired_overrides: List[int] = []
    expired_signals: List[str] = []

    if normalized_kind == "override":
        try:
            override_id = int(identifier)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid override id") from exc
        with db.DB_LOCK:
            override = db_session.get(models.ForecastOverride, override_id)
            if override is None:
                raise HTTPException(status_code=404, detail=f"Constraint override {override_id} not found")
            expired_overrides.append(_expire_constraint_override(override, now))
            evidence = override.evidence or {}
            signal_id = evidence.get("signal_id")
            if signal_id:
                signal = db_session.get(models.Signal, str(signal_id))
                if signal is not None and signal.status == "live":
                    expired_signals.append(_expire_constraint_signal(signal, now))
            db_session.commit()
    elif normalized_kind == "signal":
        with db.DB_LOCK:
            signal = db_session.get(models.Signal, identifier)
            if signal is None:
                raise HTTPException(status_code=404, detail=f"Constraint signal {identifier} not found")
            expired_signals.append(_expire_constraint_signal(signal, now))
            expired_overrides.extend(_expire_overrides_for_signal(db_session, identifier, now))
            db_session.commit()
    else:
        raise HTTPException(status_code=422, detail="Constraint kind must be override or signal")

    ctx.hub.broadcast(
        "constraint_deleted",
        {
            "kind": normalized_kind,
            "identifier": identifier,
            "expired_overrides": expired_overrides,
            "expired_signals": expired_signals,
        },
    )
    return {
        "kind": normalized_kind,
        "identifier": identifier,
        "expired_overrides": expired_overrides,
        "expired_signals": expired_signals,
    }


@app.post("/api/track-a/reviews/process")
def track_a_process_reviews() -> Dict[str, Any]:
    review = _track_a_agent("review")
    rows = review.process_unprocessed()
    return {"created": len(rows)}


@app.post("/api/track-a/staff/recompute")
def track_a_staff_recompute() -> Dict[str, Any]:
    staff = _track_a_agent("staff")
    return {"coverage": staff.recompute()}


@app.post("/api/track-a/staff/call-in-sick")
def track_a_staff_call_in_sick(body: TrackAStaffBody) -> Dict[str, Any]:
    staff = _track_a_agent("staff")
    return staff.call_in_sick(
        staff_id=body.staff_id,
        station_id=body.station_id,
        daypart=body.daypart,
        status=body.status,
        reason=body.reason,
    )


@app.post("/api/track-a/competitors/{competitor_id}/research")
def track_a_competitor_research(competitor_id: int) -> Dict[str, Any]:
    session = db.new_session()
    try:
        if session.get(models.Competitor, competitor_id) is None:
            raise HTTPException(status_code=404, detail=f"Competitor {competitor_id} not found")
    finally:
        session.close()
    competitor = _track_a_agent("competitor")
    return competitor.request_research(competitor_id)


@app.post("/api/track-a/competitors/poll-aggregators")
def track_a_competitor_poll_aggregators() -> Dict[str, Any]:
    competitor = _track_a_agent("competitor")
    with db.DB_LOCK:
        _sync_bus_to_clock()
        observations = competitor.poll_aggregators()
    return {"created": len(observations), "observations": observations}


@app.post("/api/track-a/competitors/{competitor_id}/refresh-menu")
def track_a_competitor_refresh_menu(competitor_id: int) -> Dict[str, Any]:
    competitor = _track_a_agent("competitor")
    with db.DB_LOCK:
        _sync_bus_to_clock()
        return competitor.refresh_menu(competitor_id)


@app.post("/api/track-a/competitors/{competitor_id}/probe")
def track_a_competitor_probe(competitor_id: int) -> Dict[str, Any]:
    competitor = _track_a_agent("competitor")
    with db.DB_LOCK:
        _sync_bus_to_clock()
        return competitor.run_probe(competitor_id)


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
    _sync_bus_to_clock()
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
