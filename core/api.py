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

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from sqlalchemy import func, text
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
from .runtime_policy import ApprovalPolicy, InventorySignalPolicy
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
        self.voice_actions: Optional[Any] = None  # VoiceActions dispatch seam
        self.seeder: Optional[Seeder] = None
        self.weather: Optional[WeatherProvider] = None
        self.calls: Optional[CallSubsystem] = None
        self.approvals: Optional[ApprovalsHub] = None
        self.formatter: Optional[DataFormatter] = None
        self.pos: Optional[POSSimulator] = None
        self.scenarios: Optional[ScenarioEngine] = None
        self.inventory_signal_policy: Optional[InventorySignalPolicy] = None
        self.approval_policy: Optional[ApprovalPolicy] = None
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
            availability_oos_mode=config.AVAILABILITY_OOS_MODE,
            batch_auto_qty=0,
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


def _migrate_schema() -> None:
    """Apply additive column migrations that create_all() cannot handle on existing tables."""
    migrations = [
        ("sim_settings", "availability_oos_mode", "ALTER TABLE sim_settings ADD COLUMN availability_oos_mode TEXT DEFAULT 'threshold'"),
        ("menu_toggles", "reason_code", "ALTER TABLE menu_toggles ADD COLUMN reason_code TEXT"),
        ("sim_settings", "batch_auto_qty", "ALTER TABLE sim_settings ADD COLUMN batch_auto_qty INTEGER DEFAULT 0"),
    ]
    with db.engine.connect() as conn:
        for table, column, ddl in migrations:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            existing_cols = {r[1] for r in rows}
            if column not in existing_cols:
                conn.execute(text(ddl))
                conn.commit()


def _bootstrap() -> None:
    """Create tables + singletons and wire every core object (§ app bootstrap)."""
    db.create_all()
    _migrate_schema()

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
    approval_policy = ApprovalPolicy(config.APPROVAL_PO_THRESHOLD)
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
    # Wire calls and approvals into the voice processor for manager intents.
    voice.attach_approvals(approvals)
    voice.attach_calls(calls)

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
            approvals=approvals,
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
        approval_policy=approval_policy,
    )

    ctx.bus = bus
    ctx.clock = clock
    ctx.orchestrator = orchestrator
    ctx.llm = llm
    ctx.inventory_signal_policy = inventory_signal_policy
    ctx.approval_policy = approval_policy
    ctx.voice = voice
    ctx.seeder = seeder
    ctx.weather = weather
    ctx.approvals = approvals
    ctx.calls = calls
    ctx.formatter = formatter
    ctx.pos = pos
    ctx.scenarios = scenarios

    # Create the VoiceActions dispatch seam. Agent refs are optional; if a
    # track didn't register, the relevant VoiceActions methods return an error.
    try:
        from .voice_actions import VoiceActions
        track_b = ctx.tracks.get("track_b") or {}
        ctx.voice_actions = VoiceActions(
            bus=bus,
            db_session_factory=factory,
            hub_broadcast=sink,
            voice_processor=voice,
            ledger=track_b.get("ledger"),
            optimizer=track_b.get("optimizer"),
            staff_agent=ctx.track_a.get("staff"),
            forecaster=ctx.track_a.get("forecaster"),
            forecast_jobs=ctx.forecast_jobs,
            competitor_agent=ctx.track_a.get("competitor"),
            review_agent=ctx.track_a.get("reviews"),
        )
    except Exception:  # noqa: BLE001 — VoiceActions failure must not break startup
        logger.exception("VoiceActions failed to initialize — voice writes will be unavailable")


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
    availability_oos_mode: Optional[str] = None  # "threshold" | "zero"
    batch_auto_qty: Optional[bool] = None  # forecaster adjusts batch quantities without manager approval

    @field_validator("availability_oos_mode")
    @classmethod
    def _validate_oos_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("threshold", "zero"):
            raise ValueError("availability_oos_mode must be 'threshold' or 'zero'")
        return v


class InventorySignalPolicyBody(BaseModel):
    shortage_signals_enabled: bool


class ApprovalThresholdBody(BaseModel):
    approval_threshold: float


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
    oos_mode_changed = "availability_oos_mode" in updates
    with db.DB_LOCK:
        session = db.new_session()
        try:
            settings = _ensure_settings_singleton(session)
            for field, value in updates.items():
                setattr(settings, field, value)
            session.commit()
            session.refresh(settings)
            result = _row_to_dict(settings)
        finally:
            session.close()

    # When the OOS mode changes, recompute availability immediately so the
    # new mode is reflected without waiting for the next stock event.
    if oos_mode_changed and ctx.bus is not None:
        try:
            from .availability import recompute_availability
            recompute_availability(
                db.new_session,
                ctx.bus,
                ctx.hub.broadcast,
                agent_name="settings_oos_mode",
            )
        except Exception:  # noqa: BLE001
            logger.warning("availability recompute after oos_mode change failed", exc_info=True)

    return result


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


@app.get("/api/runtime/approval-threshold")
def get_approval_threshold() -> Dict[str, Any]:
    if ctx.approval_policy is None:
        raise HTTPException(status_code=503, detail="Approval policy unavailable")
    return ctx.approval_policy.snapshot().__dict__


@app.patch("/api/runtime/approval-threshold")
def patch_approval_threshold(body: ApprovalThresholdBody) -> Dict[str, Any]:
    if ctx.approval_policy is None:
        raise HTTPException(status_code=503, detail="Approval policy unavailable")
    snapshot = ctx.approval_policy.set_approval_threshold(body.approval_threshold)
    ctx.hub.broadcast("approval_threshold_changed", snapshot.__dict__)
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
    window: str = Query("day"),
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Aggregated POS statistics over ``(since, now]`` for the monitor's window
    selector (Today / last hour / 6h / This week).

    Computed server-side so totals are accurate for any window regardless of the
    client's bounded live buffer. ``since`` is clamped to ``>= 0`` so the seeded
    negative-``sim_time`` history is never included.

    Returns fixed, clock-aligned time buckets for the orders-over-time chart;
    bucket boundaries are multiples of the window's canonical bucket width so
    they never shift as ``now`` advances.
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

    # Fixed, clock-aligned time buckets for the orders-over-time chart.
    # Bucket width is a constant per window so boundaries never shift as
    # ``now`` advances (fixing the drifting X-axis bug).
    _BUCKET_WIDTHS: Dict[str, float] = {
        "1h": 300.0,       # 5-minute buckets
        "6h": 1800.0,      # 30-minute buckets
        "day": 3600.0,     # 1-hour buckets
        "week": 86400.0,   # 1-day buckets
    }
    bucket_width = _BUCKET_WIDTHS.get(window, 3600.0)
    # Anchor to the rounded boundary that contains ``since``.
    bucket_start = math.floor(since / bucket_width) * bucket_width
    # Build a fixed ordered list of bucket start times up to (and including)
    # the bucket that contains ``now``.
    bucket_starts: list[float] = []
    t = bucket_start
    while t <= now:
        bucket_starts.append(t)
        t += bucket_width
    # Count orders into each bucket.  Any t < since is excluded by order_filter.
    bucket_index: Dict[int, int] = {i: 0 for i in range(len(bucket_starts))}
    if bucket_starts:
        for (sim_time,) in db_session.query(models.Order.sim_time).filter(order_filter).all():
            raw_t = float(sim_time)
            idx = int((raw_t - bucket_start) / bucket_width)
            if 0 <= idx < len(bucket_starts):
                bucket_index[idx] = bucket_index.get(idx, 0) + 1
    buckets = [
        {"t": bucket_starts[i], "orders": bucket_index.get(i, 0)}
        for i in range(len(bucket_starts))
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
def read_purchase_orders(
    status: Optional[str] = Query(None, description="Filter by status: proposed|approved|placed|delivered|cancelled"),
    include_lines: bool = Query(False, description="Include PurchaseOrderLine rows"),
    limit: Optional[int] = Query(None, description="Max rows (for pagination)"),
    offset: int = Query(0, description="Row offset (for pagination)"),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    """List purchase orders with optional status filter, lines, and pagination."""
    from .formatter import format_qty as _fq  # noqa: PLC0415
    query = db_session.query(models.PurchaseOrder).order_by(models.PurchaseOrder.id.desc())
    if status:
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(models.PurchaseOrder.status.in_(statuses))
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    orders = query.all()
    # Pre-load supplier names for all orders in one query.
    sup_ids = {po.supplier_id for po in orders if po.supplier_id}
    sup_names: Dict[int, str] = {}
    if sup_ids:
        for s in db_session.query(models.Supplier).filter(models.Supplier.id.in_(sup_ids)).all():
            sup_names[s.id] = s.name
    result = []
    for po in orders:
        row = _row_to_dict(po)
        row["supplier_name"] = sup_names.get(po.supplier_id, str(po.supplier_id))
        row["urgency"] = getattr(po, "urgency", None)
        if include_lines:
            lines = (
                db_session.query(models.PurchaseOrderLine)
                .filter(models.PurchaseOrderLine.po_id == po.id)
                .all()
            )
            ing_ids = {int(l.ingredient_id) for l in lines if l.ingredient_id}
            ings = {int(i.id): i for i in db_session.query(models.Ingredient).filter(
                models.Ingredient.id.in_(ing_ids)
            ).all()} if ing_ids else {}
            row["lines"] = []
            for ln in lines:
                ing = ings.get(int(ln.ingredient_id or 0))
                base_unit = ing.base_unit if ing else (ln.unit or "g")
                row["lines"].append({
                    "id": ln.id,
                    "ingredient_id": ln.ingredient_id,
                    "ingredient_name": ing.name if ing else str(ln.ingredient_id),
                    "qty": ln.qty,
                    "qty_display": _fq(float(ln.qty or 0), base_unit),
                    "unit": ln.unit,
                    "unit_price": ln.unit_price,
                    "line_total": ln.line_total,
                })
        result.append(row)
    return result


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


@app.get("/api/calls/active")
def read_active_call(db_session: Any = Depends(db.get_db)) -> Optional[Dict[str, Any]]:
    """Return the single status='active' Call (with transcript), or null."""
    row = db_session.query(models.Call).filter(models.Call.status == "active").first()
    return _row_to_dict(row) if row is not None else None


@app.get("/api/calls")
def read_calls(
    status: Optional[str] = None,
    limit: int = 50,
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    q = db_session.query(models.Call)
    if status:
        statuses = [s.strip() for s in status.split(",")]
        q = q.filter(models.Call.status.in_(statuses))
    q = q.order_by(models.Call.id.desc()).limit(limit)
    return [_row_to_dict(r) for r in q.all()]


@app.get("/api/calls/recent")
def read_recent_call(db_session: Any = Depends(db.get_db)) -> Optional[Dict[str, Any]]:
    """Return the most recently completed call (for desk hydration on reconnect)."""
    row = (
        db_session.query(models.Call)
        .filter(models.Call.status.in_(["completed", "auto_resolved"]))
        .order_by(models.Call.id.desc())
        .first()
    )
    return _row_to_dict(row) if row is not None else None


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

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"present", "leave", "sick"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


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
        "horizon_forecasts": _read_rows(
            db_session,
            models.HorizonForecast,
            models.HorizonForecast.generated_at.desc(),
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


@app.post("/api/track-a/forecast/horizon")
def track_a_forecast_horizon(body: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an on-demand interval (day/week/custom) forecast synchronously."""
    forecaster = _track_a_agent("forecaster")
    _sync_bus_to_clock()
    now = float(ctx.bus.sim_time)
    _SECONDS_PER_DAY = 86400.0

    range_key = str(body.get("range") or "week").lower()
    day_offset = int(body.get("day_offset") or 0)
    day_base = now + day_offset * _SECONDS_PER_DAY

    import math as _math  # noqa: PLC0415
    if range_key == "week":
        fs = now
        fe = now + 7 * _SECONDS_PER_DAY
    elif range_key in ("day", "today"):
        day_idx = _math.floor(day_base / _SECONDS_PER_DAY)
        fs = day_idx * _SECONDS_PER_DAY + 8 * 3600
        fe = day_idx * _SECONDS_PER_DAY + 23 * 3600
    elif range_key == "custom":
        # Accept either explicit start/end sim-seconds OR day_offset + HH:MM time strings.
        if body.get("start_time") or body.get("end_time"):
            # Derive sim-seconds from day_offset + wall-clock time strings ("HH:MM").
            day_idx = _math.floor(day_base / _SECONDS_PER_DAY)
            day_base_s = float(day_idx * _SECONDS_PER_DAY)

            def _hhmm_to_s(t: str, fallback_s: float) -> float:
                """Convert "HH:MM" to seconds-of-day; return fallback_s on failure."""
                try:
                    parts = str(t).split(":")
                    return float(int(parts[0]) * 3600 + int(parts[1]) * 60)
                except Exception:  # noqa: BLE001
                    return fallback_s

            start_s = _hhmm_to_s(body.get("start_time", ""), 8 * 3600)
            end_s = _hhmm_to_s(body.get("end_time", ""), 23 * 3600)
            fs = day_base_s + start_s
            fe = day_base_s + end_s
            if fe <= fs:
                # Swap if user entered end before start
                fs, fe = fe, fs + _SECONDS_PER_DAY
        else:
            fs = float(body.get("start") or now)
            fe = float(body.get("end") or now + _SECONDS_PER_DAY)
    else:
        fs = float(body.get("start") or now)
        fe = float(body.get("end") or now + 7 * _SECONDS_PER_DAY)

    result = forecaster.forecast_interval(
        fs,
        fe,
        trigger_reason=str(body.get("trigger_reason") or "dashboard"),
        granularity=str(body.get("granularity") or "auto"),
        persist=True,
        requested_by=str(body.get("requested_by") or "dashboard"),
        source="dashboard",
    )
    return result


@app.get("/api/track-a/forecast/horizons")
def track_a_list_horizons(limit: int = 20, db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """List recent HorizonForecast headers."""
    rows = _read_rows(
        db_session,
        models.HorizonForecast,
        models.HorizonForecast.generated_at.desc(),
        limit,
    )
    return {"horizons": rows, "count": len(rows)}


@app.get("/api/track-a/forecast/horizon/{horizon_id}")
def track_a_get_horizon(horizon_id: int, db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Return a HorizonForecast header plus all its line rows (with item names)."""
    hf = db_session.get(models.HorizonForecast, horizon_id)
    if hf is None:
        return {"error": "not_found"}
    lines = (
        db_session.query(models.HorizonForecastLine)
        .filter(models.HorizonForecastLine.horizon_id == horizon_id)
        .order_by(models.HorizonForecastLine.day_index, models.HorizonForecastLine.daypart)
        .all()
    )
    item_ids = {int(l.menu_item_id) for l in lines if l.menu_item_id}
    items_map = {
        int(i.id): i
        for i in db_session.query(models.MenuItem).filter(models.MenuItem.id.in_(item_ids)).all()
    } if item_ids else {}
    return {
        "horizon": _row_to_dict(hf),
        "lines": [
            {
                **_row_to_dict(ln),
                "item_name": items_map[int(ln.menu_item_id)].name
                if ln.menu_item_id and int(ln.menu_item_id) in items_map
                else str(ln.menu_item_id),
            }
            for ln in lines
        ],
    }


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


# ---------------------------------------------------------------------------
# Voice plan/confirm API (Stream B)
# ---------------------------------------------------------------------------


class VoicePlanBody(BaseModel):
    text: str
    role: str = "manager"
    mode: Optional[str] = None  # None → use config default


class VoiceClarifyBody(BaseModel):
    plan_id: str
    answer: str


class VoiceSettingsBody(BaseModel):
    default_mode: str = "confirm"
    voice_model: Optional[str] = None


@app.post("/api/voice/plan")
def voice_plan(body: VoicePlanBody) -> Dict[str, Any]:
    """Compute a plan for the spoken input (plan/confirm flow, Stream B)."""
    _sync_bus_to_clock()
    return ctx.voice.plan(body.text, role=body.role, mode=body.mode)


@app.post("/api/voice/plan/{plan_id}/confirm")
def voice_plan_confirm(plan_id: str) -> Dict[str, Any]:
    """Apply a pending voice plan."""
    _sync_bus_to_clock()
    return ctx.voice.confirm(plan_id)


@app.post("/api/voice/plan/{plan_id}/cancel")
def voice_plan_cancel(plan_id: str) -> Dict[str, Any]:
    """Cancel a pending voice plan."""
    return ctx.voice.cancel(plan_id)


@app.post("/api/voice/clarify")
def voice_clarify(body: VoiceClarifyBody) -> Dict[str, Any]:
    """Re-plan with a clarification answer."""
    _sync_bus_to_clock()
    return ctx.voice.clarify(body.plan_id, body.answer)


@app.get("/api/settings/voice")
def get_voice_settings() -> Dict[str, Any]:
    return {
        "default_mode": config.VOICE_DEFAULT_MODE,
        "voice_model": config.GEMINI_LIVE_MODEL,
    }


@app.post("/api/settings/voice")
def set_voice_settings(body: VoiceSettingsBody) -> Dict[str, Any]:
    import core.config as _cfg
    from .voice_live import _ALLOWED_LIVE_MODELS
    _cfg.VOICE_DEFAULT_MODE = body.default_mode
    if body.voice_model and body.voice_model in _ALLOWED_LIVE_MODELS:
        _cfg.GEMINI_LIVE_MODEL = body.voice_model
    return {
        "default_mode": _cfg.VOICE_DEFAULT_MODE,
        "voice_model": _cfg.GEMINI_LIVE_MODEL,
    }


# ---------------------------------------------------------------------------
# Kitchen / Cook endpoints (Stream B3)
# ---------------------------------------------------------------------------


class BatchCookedBody(BaseModel):
    actual_made_qty: float


class KitchenWasteBody(BaseModel):
    menu_item_id: Optional[int] = None
    ingredient_id: Optional[int] = None
    qty: float
    waste_type: str = "overproduction"
    from_batch_id: Optional[int] = None
    reason: str = ""


@app.get("/api/kitchen/board")
def kitchen_board(
    window_hours: Optional[float] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Return the full batch board: counts + all batches with state derived.

    Used by the cook's rolling batch board and the voice agent context.
    """
    from . import kitchen as _kitchen
    now = float(ctx.clock.sim_time)
    window_sim_s = window_hours * 3600 if window_hours is not None else None
    return _kitchen.batch_board(db_session, now=now, window_sim_s=window_sim_s, limit=40)


@app.get("/api/kitchen/batches")
def kitchen_batches(
    status: Optional[str] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    """Return cook-facing batch queue: decided/approved 'cook' batches."""
    query = db_session.query(models.Batch).filter(models.Batch.decision == "cook")
    if status:
        # Accept comma-separated status values (e.g. "approved,decided")
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            query = query.filter(models.Batch.status == statuses[0])
        elif statuses:
            query = query.filter(models.Batch.status.in_(statuses))
        else:
            query = query.filter(models.Batch.status.in_(("decided", "approved")))
    else:
        query = query.filter(models.Batch.status.in_(("decided", "approved")))
    batches = query.order_by(models.Batch.decided_at.desc()).limit(20).all()
    result = []
    for b in batches:
        d = _row_to_dict(b)
        # Resolve menu item name.
        mi = db_session.get(models.MenuItem, b.menu_item_id) if b.menu_item_id else None
        d["menu_item_name"] = mi.name if mi else None
        result.append(d)
    return result


@app.post("/api/kitchen/batches/{batch_id}/cooked")
def kitchen_batch_cooked(batch_id: int, body: BatchCookedBody) -> Dict[str, Any]:
    """Cook marks a batch as cooked with the actual quantity made."""
    session = db.new_session()
    try:
        batch = session.get(models.Batch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
        now = float(ctx.clock.sim_time)
        planned = float(batch.planned_qty or 0.0)
        batch.actual_made_qty = body.actual_made_qty
        batch.status = "ready"
        batch.cooked_at = now
        session.commit()
    finally:
        session.close()

    ctx.bus.emit(
        "BATCH_PROGRESS",
        {
            "batch_id": batch_id,
            "menu_item_id": int(batch.menu_item_id or 0),
            "actual_made_qty": body.actual_made_qty,
            "planned_qty": planned,
            "status": "cooked",
            "source": "cook",
        },
        source="kitchen_api",
        target_agents=["forecaster", "ledger"],
    )
    return {"batch_id": batch_id, "status": "ready", "actual_made_qty": body.actual_made_qty}


@app.post("/api/kitchen/waste")
def kitchen_waste(body: KitchenWasteBody) -> Dict[str, Any]:
    """Cook reports a waste event."""
    now = float(ctx.clock.sim_time)
    session = db.new_session()
    try:
        we = models.WasteEvent(
            waste_type=body.waste_type,
            ingredient_id=body.ingredient_id,
            menu_item_id=body.menu_item_id,
            lot_id=None,
            qty=body.qty,
            unit="each",
            cost=None,
            reason=body.reason,
            sim_time=now,
            source="cook",
        )
        session.add(we)
        session.commit()
        session.refresh(we)
        we_id = we.id
    finally:
        session.close()

    ctx.bus.emit(
        "WASTE_EVENT",
        {
            "waste_event_id": we_id,
            "waste_type": body.waste_type,
            "menu_item_id": body.menu_item_id,
            "ingredient_id": body.ingredient_id,
            "qty": body.qty,
            "unit": "each",
            "cost": None,
            "reason": body.reason,
            "source": "cook",
        },
        source="kitchen_api",
        target_agents=["forecaster", "ledger"],
    )
    return {"waste_event_id": we_id, "status": "recorded"}


# ---------------------------------------------------------------------------
# Dev / demo helpers — batch schedule seeding and suggestion trigger
# ---------------------------------------------------------------------------

@app.post("/api/dev/seed-batches")
def dev_seed_batches() -> Dict[str, Any]:
    """Seed a full-day batch schedule from the current BatchDefinitions.

    Idempotent: clears today's batch rows first, then regenerates them.
    Useful to populate the cook's Batches panel immediately without waiting
    for the forecaster's daypart gate to open.
    """
    from .batch_schedule import seed_day_schedule
    now = float(ctx.clock.sim_time)
    session = db.new_session()
    try:
        created = seed_day_schedule(session, now=now, clear=True)
        session.commit()
        count = len(created)
    finally:
        session.close()

    ctx.hub.broadcast("batches_seeded", {"count": count, "sim_time": now})
    return {"seeded": count, "sim_time": now}


@app.post("/api/dev/suggest-batches")
async def dev_suggest_batches() -> Dict[str, Any]:
    """Manually trigger the start-of-day batch advisor (Gemini 2.5 Pro).

    Useful for testing without waiting for the daily clock boundary.
    Only runs if the forecaster agent is registered.
    """
    forecaster = ctx.track_a.get("forecaster") if ctx.track_a else None
    if forecaster is None:
        raise HTTPException(status_code=503, detail="Forecaster agent not available")
    try:
        import asyncio
        result = await asyncio.to_thread(forecaster.suggest_day_batches, force=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


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


# ---------------------------------------------------------------------------
# Call overhaul — new call/hint/parties endpoints
# ---------------------------------------------------------------------------

class StartCallBody(BaseModel):
    counterparty_type: str          # "supplier" | "competitor"
    counterparty_id: int
    purpose: str = ""
    agent: str = "user"
    note: str = ""
    ingredient_id: Optional[int] = None


class CallHintBody(BaseModel):
    text: str


@app.post("/api/calls")
def start_call(body: StartCallBody) -> Dict[str, Any]:
    """Start a call immediately (user-initiated; no approval card).

    Returns ``{call_id, status, counterparty_type, counterparty_id}`` so the
    browser can open ``/call?call_id=<id>`` in a new tab straight away.
    """
    _sync_bus_to_clock()
    call = ctx.calls.start_direct(
        agent=body.agent,
        counterparty_type=body.counterparty_type,
        counterparty_id=body.counterparty_id,
        purpose=body.purpose or f"User-initiated {body.counterparty_type} call",
        note=body.note,
        ingredient_id=body.ingredient_id,
    )
    return {
        "call_id": call.id,
        "status": call.status,
        "counterparty_type": call.counterparty_type,
        "counterparty_id": call.counterparty_id,
    }


@app.post("/api/calls/{call_id}/hint")
def call_hint(call_id: int, body: CallHintBody) -> Dict[str, Any]:
    """Enqueue a spectator coaching hint into the active live session."""
    from .voice_live import enqueue_call_hint
    queued = enqueue_call_hint(call_id, body.text)
    return {"call_id": call_id, "queued": queued}


@app.get("/api/presets/current/parties")
def get_current_parties(db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Return suppliers + competitors from the current restaurant preset.

    Used by the call page's direct-open chooser so the user can pick which
    party to role-play as.
    """
    suppliers = db_session.query(models.Supplier).order_by(models.Supplier.name).all()
    competitors = db_session.query(models.Competitor).order_by(models.Competitor.name).all()
    return {
        "suppliers": [
            {"id": s.id, "name": s.name, "phone": s.phone, "contact": s.contact,
             "delivery_charge": s.delivery_charge}
            for s in suppliers
        ],
        "competitors": [
            {"id": c.id, "name": c.name, "cuisine": c.cuisine, "phone": c.phone,
             "distance_km": c.distance_km, "rating": c.rating, "is_open": c.is_open}
            for c in competitors
        ],
    }


# ---------------------------------------------------------------------------
# Manager changes
# ---------------------------------------------------------------------------

def _change_to_dict(c: models.ManagerChange) -> Dict[str, Any]:
    return {
        "id": c.id,
        "kind": c.kind,
        "status": c.status,
        "auto_applied": bool(c.auto_applied),
        "summary": c.summary,
        "details": c.details,
        "created_at": c.created_at,
        "resolved_at": c.resolved_at,
    }


@app.get("/api/manager/changes")
def list_manager_changes(
    status: Optional[str] = Query(None),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    """Return manager change cards (for Manager Roba Desk)."""
    q = db_session.query(models.ManagerChange).order_by(
        models.ManagerChange.created_at.desc()
    )
    if status:
        q = q.filter(models.ManagerChange.status == status)
    return [_change_to_dict(c) for c in q.all()]


def _apply_change_to_db(change: models.ManagerChange, db_session: Any, direction: str = "after") -> None:
    """Write ``details[direction]`` state back to supplier tables."""
    details = change.details or {}
    state = details.get(direction, {})
    supplier_id = details.get("supplier_id")
    ingredient_id = details.get("ingredient_id")

    if change.kind in ("call_price", "sourcing_default"):
        if ingredient_id is not None and supplier_id is not None:
            # Update is_default for sourcing_default changes
            if change.kind == "sourcing_default":
                new_default = state.get("supplier_id", supplier_id)
                # Clear existing defaults for this ingredient
                db_session.query(models.SupplierCatalog).filter(
                    models.SupplierCatalog.ingredient_id == ingredient_id
                ).update({models.SupplierCatalog.is_default: 0})
                # Set new default
                db_session.query(models.SupplierCatalog).filter(
                    models.SupplierCatalog.supplier_id == new_default,
                    models.SupplierCatalog.ingredient_id == ingredient_id,
                ).update({models.SupplierCatalog.is_default: 1})
            # Update price for call_price changes
            if change.kind == "call_price":
                new_price = state.get("price")
                if new_price is not None and ingredient_id and supplier_id:
                    db_session.query(models.SupplierCatalog).filter(
                        models.SupplierCatalog.supplier_id == supplier_id,
                        models.SupplierCatalog.ingredient_id == ingredient_id,
                    ).update({models.SupplierCatalog.current_price: float(new_price)})
    elif change.kind == "supplier_term":
        # Apply / revert a SupplierTerm captured from a call.
        # Applying activates the term so it feeds the MILP; reverting deactivates it.
        term_id = details.get("supplier_term_id")
        if term_id is not None:
            term = db_session.get(models.SupplierTerm, int(term_id))
            if term is not None:
                if direction == "after":
                    term.status = "active"
                else:  # revert
                    term.status = "reverted"
    elif change.kind == "onboarding":
        # Apply / revert catalog entries from the onboarding outcome
        entries = state.get("catalog_entries", [])
        for entry in entries:
            s_id = entry.get("supplier_id") or supplier_id
            i_id = entry.get("ingredient_id")
            if not s_id or not i_id:
                continue
            existing = db_session.query(models.SupplierCatalog).filter(
                models.SupplierCatalog.supplier_id == s_id,
                models.SupplierCatalog.ingredient_id == i_id,
            ).first()
            if direction == "after":
                if existing:
                    existing.current_price = float(entry.get("price", existing.current_price))
                    existing.availability = entry.get("availability", existing.availability)
                else:
                    db_session.add(models.SupplierCatalog(
                        supplier_id=s_id, ingredient_id=i_id,
                        current_price=float(entry.get("price", 1.0)),
                        unit=entry.get("unit", "g"),
                        pack_size=float(entry.get("pack_size", 1.0)),
                        availability=entry.get("availability", "in_stock"),
                        updated_at=0.0, is_default=0,
                    ))
            else:  # revert ("before") — remove added entries
                if existing:
                    db_session.delete(existing)


def _replan_in_background() -> None:
    """Re-plan procurement after a supplier term is applied/reverted.

    Registered as a FastAPI BackgroundTask so it runs after the HTTP response is
    sent. Calls build + sourcing + execute so any now-due at-risk orders are placed
    immediately even while the sim is paused (§W1b).
    """
    optimizer = (ctx.tracks.get("track_b") or {}).get("optimizer")
    if optimizer is None:
        logger.warning("_replan_in_background: optimizer not found in ctx.tracks")
        return
    try:
        optimizer.build_procurement_plan()
        optimizer.run_sourcing_plan()
        optimizer.execute_due_planned_orders()
    except Exception as _exc:  # noqa: BLE001
        logger.warning("post-apply replan failed: %s", _exc)


@app.post("/api/manager/changes/{change_id}/apply")
def apply_manager_change(
    change_id: int,
    background_tasks: BackgroundTasks,
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Apply a pending manager change to the catalog."""
    change = db_session.get(models.ManagerChange, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"Change {change_id} not found")
    if change.status not in ("pending",):
        raise HTTPException(status_code=409, detail=f"Change is already {change.status}")
    now = float(ctx.bus.sim_time) if hasattr(ctx, "bus") else 0.0
    _apply_change_to_db(change, db_session, "after")
    change.status = "applied"
    change.resolved_at = now
    db_session.commit()
    ctx.hub.broadcast("manager_change", _change_to_dict(change))
    # Re-run the procurement planner when a supplier term or negotiated price is
    # applied so the optimizer picks up the new terms immediately (§W1b).
    if change.kind in ("supplier_term", "call_price"):
        background_tasks.add_task(_replan_in_background)
    return _change_to_dict(change)


@app.post("/api/manager/changes/{change_id}/revert")
def revert_manager_change(
    change_id: int,
    background_tasks: BackgroundTasks,
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Revert an applied manager change."""
    change = db_session.get(models.ManagerChange, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"Change {change_id} not found")
    if change.status not in ("applied",):
        raise HTTPException(status_code=409, detail=f"Cannot revert change with status {change.status!r}")
    now = float(ctx.bus.sim_time) if hasattr(ctx, "bus") else 0.0
    _apply_change_to_db(change, db_session, "before")
    change.status = "reverted"
    change.resolved_at = now
    db_session.commit()
    ctx.hub.broadcast("manager_change", _change_to_dict(change))
    # Re-plan after reverting a supplier term so the optimizer removes the benefit (§W1b).
    if change.kind in ("supplier_term", "call_price"):
        background_tasks.add_task(_replan_in_background)
    return _change_to_dict(change)


@app.post("/api/manager/changes/{change_id}/dismiss")
def dismiss_manager_change(change_id: int, db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Dismiss a pending manager change without applying it."""
    change = db_session.get(models.ManagerChange, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"Change {change_id} not found")
    now = float(ctx.bus.sim_time) if hasattr(ctx, "bus") else 0.0
    change.status = "dismissed"
    change.resolved_at = now
    db_session.commit()
    ctx.hub.broadcast("manager_change", _change_to_dict(change))
    return _change_to_dict(change)


# ---------------------------------------------------------------------------
# Auto-apply setting
# ---------------------------------------------------------------------------

class AutoApplyBody(BaseModel):
    auto_apply: bool


@app.get("/api/settings/auto-apply")
def get_auto_apply(db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Return the current auto-apply-supplier-changes setting."""
    settings = db_session.get(models.AppSettings, 1)
    enabled = bool(settings.auto_apply_supplier_changes) if settings else False
    return {"auto_apply": enabled}


@app.post("/api/settings/auto-apply")
def set_auto_apply(body: AutoApplyBody, db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Toggle auto-apply-supplier-changes."""
    settings = db_session.get(models.AppSettings, 1)
    if settings is None:
        settings = models.AppSettings(id=1, auto_apply_supplier_changes=int(body.auto_apply))
        db_session.add(settings)
    else:
        settings.auto_apply_supplier_changes = int(body.auto_apply)
    db_session.commit()
    ctx.hub.broadcast("auto_apply_changed", {"auto_apply": body.auto_apply})
    return {"auto_apply": body.auto_apply}


@app.get("/api/settings/identity")
def get_identity(db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Return the restaurant identity (title, location, phone)."""
    settings = db_session.get(models.AppSettings, 1)
    if settings is None:
        return {"title": "", "location": "", "phone": ""}
    return settings.get_identity()


class IdentityBody(BaseModel):
    title: str = ""
    location: str = ""
    phone: str = ""


@app.post("/api/settings/identity")
def set_identity(body: IdentityBody, db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Update the restaurant identity fields."""
    settings = db_session.get(models.AppSettings, 1)
    if settings is None:
        settings = models.AppSettings(
            id=1,
            restaurant_title=body.title or None,
            restaurant_location=body.location or None,
            restaurant_phone=body.phone or None,
        )
        db_session.add(settings)
    else:
        settings.restaurant_title = body.title or settings.restaurant_title
        settings.restaurant_location = body.location or settings.restaurant_location
        settings.restaurant_phone = body.phone or settings.restaurant_phone
    db_session.commit()
    return settings.get_identity()


# ---------------------------------------------------------------------------
# Sourcing run endpoint (trigger on-demand)
# ---------------------------------------------------------------------------

@app.post("/api/track-b/optimizer/sourcing/run")
def run_sourcing_plan() -> Dict[str, Any]:
    """Trigger an immediate sourcing solve (on-demand from Control)."""
    optimizer = (ctx.tracks.get("track_b") or {}).get("optimizer")
    if optimizer is None:
        raise HTTPException(status_code=503, detail="Optimizer not active")
    try:
        optimizer.run_sourcing_plan()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/track-b/sourcing/latest")
def get_latest_sourcing_run(db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Return the most recent SourcingRun row (for the Control defaults indicator)."""
    run = (
        db_session.query(models.SourcingRun)
        .order_by(models.SourcingRun.id.desc())
        .first()
    )
    if run is None:
        return {}
    return {
        "id": run.id,
        "created_at": run.created_at,
        "horizon_days": run.horizon_days,
        "total_cost": run.total_cost,
        "prev_cost": run.prev_cost,
        "savings": run.savings,
        "assignments": run.assignments,
        "method": run.method,
        "rationale": run.rationale,
    }


class SetDefaultBody(BaseModel):
    ingredient_id: int
    supplier_id: int


@app.post("/api/supplier-catalog/set-default")
def set_supplier_default(
    body: SetDefaultBody,
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Manually override the default supplier for an ingredient.

    Clears is_default on the current default row for that ingredient and sets
    it on the chosen catalog row; creates a ManagerChange(kind=sourcing_default)
    so the override is visible on the Roba Desk.
    """
    import time

    # Clear all existing defaults for this ingredient
    rows = (
        db_session.query(models.SupplierCatalog)
        .filter(models.SupplierCatalog.ingredient_id == body.ingredient_id)
        .all()
    )
    prev_default = next((r for r in rows if r.is_default), None)
    target_row = next((r for r in rows if r.supplier_id == body.supplier_id), None)
    if target_row is None:
        raise HTTPException(status_code=404, detail="Supplier does not supply that ingredient")

    if prev_default and prev_default.id == target_row.id:
        # No change needed
        return {"changed": False}

    prev_supplier_name = None
    if prev_default:
        prev_sup = db_session.get(models.Supplier, prev_default.supplier_id)
        prev_supplier_name = prev_sup.name if prev_sup else str(prev_default.supplier_id)

    new_sup = db_session.get(models.Supplier, body.supplier_id)
    new_supplier_name = new_sup.name if new_sup else str(body.supplier_id)

    ing = db_session.get(models.Ingredient, body.ingredient_id)
    ing_name = ing.name if ing else str(body.ingredient_id)

    for row in rows:
        row.is_default = 1 if row.supplier_id == body.supplier_id else 0
    db_session.commit()

    # Create ManagerChange so the desk card shows up
    change = models.ManagerChange(
        kind="sourcing_default",
        status="applied",
        auto_applied=0,
        summary=f"Manual override: {ing_name} → {new_supplier_name}",
        details={
            "ingredient_id": body.ingredient_id,
            "ingredient_name": ing_name,
            "supplier_id_before": prev_default.supplier_id if prev_default else None,
            "supplier_name_before": prev_supplier_name,
            "supplier_id_after": body.supplier_id,
            "supplier_name_after": new_supplier_name,
            "rationale": "Manual override via Control surface",
        },
        created_at=time.time(),
        resolved_at=time.time(),
    )
    db_session.add(change)
    db_session.commit()
    db_session.refresh(change)
    ctx.hub.broadcast("manager_change", {"change_id": change.id})

    return {"changed": True, "change_id": change.id}


@app.get("/api/track-b/suppliers/overview")
def get_suppliers_overview(db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Per-supplier overview for the Suppliers panel.

    Returns suppliers grouped into 'current' (≥1 default catalog row) and
    'alternate' (available but not currently default).  Each supplier entry
    includes its catalog items with planned-order quantities from the most
    recent SourcingRun, plus any in-flight PurchaseOrders (proposed/approved/placed).
    """
    from .formatter import format_qty as _fq, format_price_per_unit as _fpu  # noqa: PLC0415

    # Load all base data in bulk
    suppliers = db_session.query(models.Supplier).order_by(models.Supplier.name).all()
    catalog_rows = db_session.query(models.SupplierCatalog).all()
    ingredients = {int(i.id): i for i in db_session.query(models.Ingredient).all()}

    # Latest SourcingRun assignments → planned qty per (supplier_id, ingredient_id)
    planned: Dict[tuple, float] = {}
    latest_run = (
        db_session.query(models.SourcingRun)
        .order_by(models.SourcingRun.id.desc())
        .first()
    )
    if latest_run and latest_run.assignments:
        for asgn in (latest_run.assignments or []):
            s_id = asgn.get("supplier_id")
            i_id = asgn.get("ingredient_id")
            qty = asgn.get("qty", 0)
            if s_id and i_id:
                planned[(int(s_id), int(i_id))] = float(qty)

    # Active POs (proposed/approved/placed) lines per supplier
    active_pos = (
        db_session.query(models.PurchaseOrder)
        .filter(models.PurchaseOrder.status.in_(["proposed", "approved", "placed"]))
        .all()
    )
    po_lines_by_supplier: Dict[int, list] = {}
    if active_pos:
        po_ids = [po.id for po in active_pos]
        po_status_by_id = {po.id: po.status for po in active_pos}
        all_lines = (
            db_session.query(models.PurchaseOrderLine)
            .filter(models.PurchaseOrderLine.po_id.in_(po_ids))
            .all()
        )
        for ln in all_lines:
            po = next((p for p in active_pos if p.id == ln.po_id), None)
            if po is None:
                continue
            sup_id = int(po.supplier_id)
            po_lines_by_supplier.setdefault(sup_id, []).append({
                "ingredient_id": ln.ingredient_id,
                "qty": ln.qty,
                "unit": ln.unit,
                "unit_price": ln.unit_price,
                "line_total": ln.line_total,
                "po_status": po_status_by_id.get(po.id, po.status),
            })

    # Group catalog rows by supplier_id
    catalog_by_supplier: Dict[int, list] = {}
    for row in catalog_rows:
        catalog_by_supplier.setdefault(int(row.supplier_id), []).append(row)

    def _build_supplier_entry(sup: Any) -> Dict[str, Any]:
        sup_id = int(sup.id)
        cat_rows = catalog_by_supplier.get(sup_id, [])
        items = []
        for cat in sorted(cat_rows, key=lambda c: int(c.ingredient_id or 0)):
            ing_id = int(cat.ingredient_id)
            ing = ingredients.get(ing_id)
            base_unit = ing.base_unit if ing else "g"
            ing_name = ing.name if ing else str(ing_id)
            # Planned qty from latest SourcingRun
            p_qty = planned.get((sup_id, ing_id))
            # Price normalised for display (per kg / per L)
            price_disp_val, price_disp_unit = _fpu(
                float(cat.current_price or 0), base_unit
            )
            items.append({
                "catalog_id": cat.id,
                "ingredient_id": ing_id,
                "ingredient_name": ing_name,
                "is_default": bool(cat.is_default),
                "current_price": cat.current_price,
                "price_display": f"{price_disp_val:.2f} per {price_disp_unit}",
                "unit": cat.unit,
                "base_unit": base_unit,
                "availability": cat.availability,
                "planned_qty": p_qty,
                "planned_qty_display": _fq(p_qty, base_unit) if p_qty is not None else None,
            })
        # PO lines for this supplier (in-flight orders)
        po_items = []
        for ln in po_lines_by_supplier.get(sup_id, []):
            ing_id = int(ln["ingredient_id"] or 0)
            ing = ingredients.get(ing_id)
            base_unit = ing.base_unit if ing else (ln.get("unit") or "g")
            po_items.append({
                "ingredient_id": ing_id,
                "ingredient_name": ing.name if ing else str(ing_id),
                "qty": ln["qty"],
                "qty_display": _fq(float(ln["qty"] or 0), base_unit),
                "unit_price": ln["unit_price"],
                "line_total": ln["line_total"],
                "po_status": ln["po_status"],
            })
        is_current = any(c.is_default for c in cat_rows)
        return {
            "id": sup_id,
            "name": sup.name,
            "phone": sup.phone,
            "lead_time_days": sup.lead_time_days,
            "reliability_score": sup.reliability_score,
            "min_order_value": sup.min_order_value,
            "delivery_charge": sup.delivery_charge,
            "is_current": is_current,
            "items": items,
            "active_po_items": po_items,
        }

    current = []
    alternate = []
    for sup in suppliers:
        entry = _build_supplier_entry(sup)
        if entry["is_current"]:
            current.append(entry)
        else:
            alternate.append(entry)

    return {"current": current, "alternate": alternate}


class OnboardSupplierBody(BaseModel):
    name: str
    phone: Optional[str] = None
    lead_time_days: float = 3.0
    reliability_score: float = 0.9
    min_order_value: float = 0.0


@app.post("/api/suppliers/onboard")
def onboard_new_supplier(
    body: OnboardSupplierBody,
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Create a new supplier and stage an onboarding call approval card.

    Returns the new supplier row plus a ManagerChange id so the manager desk
    shows a 'Call to onboard' confirmation card immediately.
    """
    import time as _time  # noqa: PLC0415

    new_sup = models.Supplier(
        name=body.name,
        phone=body.phone,
        lead_time_days=body.lead_time_days,
        reliability_score=body.reliability_score,
        min_order_value=body.min_order_value,
        delivery_charge=0.0,
    )
    db_session.add(new_sup)
    db_session.flush()  # get id

    # Stage an onboarding call via CallSubsystem if available
    calls = getattr(ctx, "calls", None)
    call_id = None
    if calls is not None:
        try:
            call = calls.request(
                agent="manager",
                counterparty_type="supplier",
                counterparty_id=int(new_sup.id),
                purpose="onboarding",
                require_approval=True,
            )
            call_id = call.id
        except Exception:  # noqa: BLE001
            pass

    # Create a ManagerChange card so the desk feed shows the onboarding request
    change = models.ManagerChange(
        kind="onboarding",
        status="pending",
        auto_applied=0,
        summary=f"New supplier: call {body.name} to onboard",
        details={
            "supplier_id": int(new_sup.id),
            "supplier_name": body.name,
            "phone": body.phone,
            "call_id": call_id,
            "rationale": f"Onboarding call requested for new supplier '{body.name}'",
        },
        created_at=_time.time(),
        resolved_at=None,
    )
    db_session.add(change)
    db_session.commit()
    db_session.refresh(new_sup)
    db_session.refresh(change)
    ctx.hub.broadcast("manager_change", {"change_id": change.id})

    return {
        "supplier": _row_to_dict(new_sup),
        "change_id": change.id,
        "call_id": call_id,
    }


class NegotiateBody(BaseModel):
    supplier_id: int
    ingredient_id: Optional[int] = None
    note: Optional[str] = None


@app.post("/api/market/negotiate")
def market_negotiate(body: NegotiateBody) -> Dict[str, Any]:
    """Presenter-triggered negotiation — creates an approval-gated outbound call.

    Accepts an optional free-text ``note`` that is forwarded to the AI caller
    as operator instructions, and an optional ``ingredient_id`` to focus on.
    """
    market = (ctx.tracks.get("track_b") or {}).get("market_spectator")
    if market is None:
        raise HTTPException(status_code=503, detail="Market Spectator not active")
    market.negotiate(body.supplier_id, body.ingredient_id, note=body.note or "")
    return {"supplier_id": body.supplier_id, "ingredient_id": body.ingredient_id, "requested": True}


# ===========================================================================
# Track B: Inventory Optimizer LLM (Stream E)
# ===========================================================================


@app.get("/api/track-b/optimizer/insights")
def read_optimizer_insights(
    limit: int = Query(50),
    db_session: Any = Depends(db.get_db),
) -> List[Dict[str, Any]]:
    """Return recent InventoryOptimizerMemory insights."""
    rows = (
        db_session.query(models.InventoryOptimizerMemory)
        .order_by(models.InventoryOptimizerMemory.last_seen_at.desc())
        .limit(limit)
        .all()
    )
    return [_row_to_dict(r) for r in rows]


@app.post("/api/track-b/optimizer/auto-mode")
def set_optimizer_auto_mode(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Toggle the optimizer's LLM auto-mode at runtime."""
    import core.config as _cfg
    enabled = bool(body.get("enabled", True))
    _cfg.OPTIMIZER_LLM_AUTO_MODE = enabled
    optimizer = (ctx.tracks.get("track_b") or {}).get("optimizer")
    return {"optimizer_llm_auto_mode": enabled, "optimizer_active": optimizer is not None}


@app.post("/api/track-b/optimizer/run-llm")
def run_optimizer_llm(
    db_session: Any = Depends(db.get_db),
) -> Dict[str, Any]:
    """Trigger an immediate LLM optimization pass."""
    optimizer = (ctx.tracks.get("track_b") or {}).get("optimizer")
    if optimizer is None:
        raise HTTPException(status_code=503, detail="Optimizer not active")
    optimizer.llm_optimize()
    return {"status": "ok"}


# ===========================================================================
# Track B: Forward Procurement Plan (WS4)
# ===========================================================================


@app.get("/api/track-b/procurement/plan")
def get_procurement_plan(db_session: Any = Depends(db.get_db)) -> Dict[str, Any]:
    """Return all active (planned / at_risk) PlannedOrder rows with names.

    Each item includes delivery_charge_share (supplier delivery fee split evenly
    across items in the same supplier+delivery-day group) so the UI can show true
    landed cost per supplier group.
    """
    from .clock import SECONDS_PER_DAY as _SPD

    orders = (
        db_session.query(models.PlannedOrder)
        .filter(models.PlannedOrder.status.in_(["planned", "at_risk", "uncoverable"]))
        .order_by(models.PlannedOrder.order_date)
        .all()
    )

    latest_run = (
        db_session.query(models.ProcurementPlanRun)
        .order_by(models.ProcurementPlanRun.id.desc())
        .first()
    )

    def _day_label(sim_s: float) -> str:
        day = int(sim_s // _SPD)
        dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day % 7]
        return f"Day {day} ({dow})"

    def _day_time_label(sim_s: float, overdue: bool = False) -> str:
        """Concrete label: 'Day X (Dow) HH:MM' with optional '(overdue)' suffix."""
        day = int(sim_s // _SPD)
        dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day % 7]
        sod = int(sim_s) % _SPD
        hh, mm = divmod(sod // 60, 60)
        suffix = " (overdue)" if overdue else ""
        return f"Day {day} ({dow}) {hh:02d}:{mm:02d}{suffix}"

    # Current sim time (for overdue label).
    now_sim = float(ctx.bus.sim_time) if ctx.bus is not None else 0.0

    # Pre-load all referenced suppliers to compute delivery_charge_share.
    sup_ids = {o.supplier_id for o in orders if o.supplier_id}
    supplier_map: Dict[int, Any] = {}
    if sup_ids:
        for s in db_session.query(models.Supplier).filter(models.Supplier.id.in_(sup_ids)).all():
            supplier_map[s.id] = s

    # Count items per (supplier_id, delivery_day) group to split delivery charge.
    from collections import Counter as _Counter
    group_counts: Dict[tuple, int] = _Counter(
        (o.supplier_id, int(float(o.delivery_date or 0.0) // _SPD))
        for o in orders
        if o.supplier_id
    )

    result = []
    for o in orders:
        ing = db_session.get(models.Ingredient, o.ingredient_id)
        sup = supplier_map.get(o.supplier_id) if o.supplier_id else None
        order_date_s = float(o.order_date or 0.0)
        order_label = _day_time_label(
            order_date_s,
            overdue=(o.status == "at_risk" or order_date_s <= now_sim),
        )
        delivery_day_idx = int(float(o.delivery_date or 0.0) // _SPD)
        n_in_group = group_counts.get((o.supplier_id, delivery_day_idx), 1)
        dc = float(sup.delivery_charge or 0.0) if sup else 0.0
        result.append({
            "id": o.id,
            "ingredient_id": o.ingredient_id,
            "ingredient_name": ing.name if ing else str(o.ingredient_id),
            "supplier_id": o.supplier_id,
            "supplier_name": sup.name if sup else "Unknown",
            "qty": o.qty,
            "unit": o.unit or (ing.base_unit if ing else "g"),
            "unit_price": o.unit_price,
            "order_date": o.order_date,
            "order_date_label": order_label,
            "delivery_date": o.delivery_date,
            "delivery_date_label": _day_label(float(o.delivery_date or 0.0)),
            "covers_from": o.covers_from,
            "covers_until": o.covers_until,
            "status": o.status,
            "reason": o.reason or "",
            "delivery_charge": dc,
            "delivery_charge_share": round(dc / max(1, n_in_group), 4),
        })

    return {
        "plan_run_id": latest_run.id if latest_run else None,
        "generated_at": float(latest_run.created_at) if latest_run else 0.0,
        "coverage_ok": bool(getattr(latest_run, "coverage_ok", 1)) if latest_run else True,
        "total_short": float(getattr(latest_run, "total_short", 0.0) or 0.0) if latest_run else 0.0,
        "reliability_premium": float(getattr(latest_run, "reliability_premium", 0.0) or 0.0) if latest_run else 0.0,
        "exposed_value_baseline": float(getattr(latest_run, "exposed_value_baseline", 0.0) or 0.0) if latest_run else 0.0,
        "exposed_value_protected": float(getattr(latest_run, "exposed_value_protected", 0.0) or 0.0) if latest_run else 0.0,
        "items": result,
    }


@app.post("/api/track-b/procurement/plan/run")
def run_procurement_plan() -> Dict[str, Any]:
    """Manually trigger a procurement plan rebuild and execute any overdue orders."""
    optimizer = (ctx.tracks.get("track_b") or {}).get("optimizer")
    if optimizer is None:
        raise HTTPException(status_code=503, detail="Optimizer not active")
    # Execute overdue planned orders first (converts at_risk / past-due rows into POs),
    # then rebuild the plan so the UI reflects the updated state.
    executed = optimizer.execute_due_planned_orders()
    count = optimizer.build_procurement_plan()
    return {"ok": True, "items_planned": count, "orders_executed": executed}


@app.get("/api/track-b/procurement/warnings")
def get_procurement_warnings() -> List[Dict[str, Any]]:
    """Return live INGREDIENT_UNCOVERABLE signals — one per ingredient that the
    optimizer cannot source.

    This is the queryable warning surface for future features:
        bus.live(type=SignalType.INGREDIENT_UNCOVERABLE)

    The UI fetches this on mount; live updates arrive via the ``signal_emitted``
    WebSocket event (type == "INGREDIENT_UNCOVERABLE") which the ProcurementPanel
    already subscribes to.
    """
    if ctx.bus is None:
        return []
    signals = ctx.bus.live(type=SignalType.INGREDIENT_UNCOVERABLE)
    return [
        {
            "signal_id": s.signal_id,
            "ingredient_id": (s.payload or {}).get("ingredient_id"),
            "ingredient_name": (s.payload or {}).get("ingredient_name", ""),
            "short_qty": (s.payload or {}).get("short_qty", 0.0),
            "unit": (s.payload or {}).get("unit", ""),
            "reason": (s.payload or {}).get("reason", ""),
            "created_at": s.created_at,
        }
        for s in signals
    ]


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


@app.websocket("/ws/voice/live")
async def voice_live_endpoint(
    websocket: WebSocket,
    role: str = Query("manager"),
    mode: str = Query("confirm"),
    mic_mode: str = Query("ptt"),
    model: Optional[str] = Query(None),
    call_id: Optional[int] = Query(None),
) -> None:
    """Gemini Live API bridge (Stream B5).

    Browser connects, sends 16kHz PCM16 binary frames + JSON control frames;
    receives 24kHz PCM16 audio frames + JSON transcript/plan events back.

    mic_mode="ptt"          Automatic VAD disabled; turns are delimited by
                            explicit activity_start / activity_end frames.
    mic_mode="conversation" Default automatic VAD; Gemini detects turn ends.
    model=<id>              Override the live model (validated against allowlist).
    call_id=<int>           When set, bridges a specific call row — uses the
                            call-bound persona and mirrors turns to calls.add_turn.
    """
    await websocket.accept()
    try:
        from .voice_live import live_bridge
        await live_bridge(
            websocket,
            ctx.voice,
            role=role,
            mode=mode,
            mic_mode=mic_mode,
            model=model,
            voice_actions=ctx.voice_actions,
            call_id=call_id,
            calls=ctx.calls,
        )
    except Exception:  # noqa: BLE001
        logger.exception("voice live endpoint error")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
