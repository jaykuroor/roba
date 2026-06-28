# 05 — Complete Codebase Analysis

### Restaurant Multi-Agent System — Demand Forecasting & Inventory Optimization

> **Branch coverage:** `main` (core scaffold), `track_a` (Demand & Sensing), `track_b` (Inventory & Procurement).
> **Date:** 2026-06-18. All `file:line` references verified against the committed code.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [The Three-Branch / Two-Track Architecture](#2-the-three-branch--two-track-architecture)
3. [The Layered Architecture](#3-the-layered-architecture)
4. [Core Walkthrough — Module by Module](#4-core-walkthrough--module-by-module)
5. [The Data Model — All 38 Tables](#5-the-data-model--all-38-tables)
6. [The Signal Bus & Signal Taxonomy](#6-the-signal-bus--signal-taxonomy)
7. [The Deterministic Algorithms](#7-the-deterministic-algorithms)
8. [Track A — Demand & Sensing](#8-track-a--demand--sensing)
9. [Track B — Inventory & Procurement](#9-track-b--inventory--procurement)
10. [What a Runtime Looks Like](#10-what-a-runtime-looks-like)
11. [Frontend](#11-frontend)
12. [How to Run It](#12-how-to-run-it)
13. [Spec-vs-Implementation Gap Analysis](#13-spec-vs-implementation-gap-analysis)
14. [Glossary](#14-glossary)

---

## 1. Executive Summary

**Roba** is a demo-grade restaurant agent-orchestration system built to prove one thesis: *autonomous agents can make correct, explainable decisions in response to live operational signals, with humans in the loop only where it genuinely matters.*

The system has a **dual mandate**:

1. **Demand forecasting** — rolling, per-dish forecasts driven by history, weather, competitor intelligence, reviews, staff coverage, events, and live sales velocity; the Forecaster agent decides whether to pre-cook "batches" ahead of service windows.
2. **Inventory optimization** — theoretical-inventory tracking via an append-only ledger, automatic reorders, menu toggling when stock will run out before resupply, expiry-to-promo conversion, and supplier-price negotiation by voice.

Because there is no real POS system or supplier API, the demo **simulates a full restaurant operating day (08:00–23:00) in compressed time** — one sim-day = 15 real minutes at default speed. A presenter can perturb the simulated world (velocity sliders, weather override, call-in-sick buttons, voice facts, pre-built scenarios) and watch agents react and explain every decision in plain English via an activity log.

Two headline demo moments: a presenter **plays a supplier** by voice while the Market Spectator agent negotiates ingredient prices; and a presenter **plays a competitor** while the Competitor Intelligence agent runs an undercover phone-research call.

The codebase is split into three git branches representing **three build phases**:
- **`main`** — the Phase-0 `core` scaffold: simulation engine, signal bus, database, POS simulator, voice, weather, calls, LLM, seeding, REST/WS API, and a React shell. This is fully implemented.
- **`track_a`** — "Demand & Sensing": four agents (Forecaster, Competitor Intelligence, Review, Staff) and five React panels. Substantially implemented; a few LLM paths are stubbed to keyword heuristics.
- **`track_b`** — "Inventory & Procurement": three agents (Ledger, Optimizer, Market Spectator), a Procurement service, and four React panels. The most complete track — nearly full conformance with the spec.

The intended next step (Phase 2) is merging both tracks onto `main` with `DEMO_MODE=combined` so they interact via real cross-track signals. The wiring already exists; it only requires the Track A LLM gaps to be completed.

> **Note:** `docs/03_BUILD_PLAN_A.md` (the Track A build-execution playbook) is referenced in the main architecture doc but is **absent from every branch** — it was never created. `04_BUILD_PLAN_B.md` exists and guided the Track B implementation.

---

## 2. The Three-Branch / Two-Track Architecture

### 2.1 The Independence Model

The project's single most important architectural rule is in `docs/00_ARCHITECTURE.md §2`:

> *"Agents never call each other directly. All inter-component communication is signals on the bus (Layer 2) plus the REST/WS API (Layer 4 ↔ frontend)."*

This creates two independently buildable tracks that communicate exclusively through:
- **Signals on the bus** — typed, grouped, expiring messages in the `signals` table.
- **Reads of shared `core` tables** — reference data (ingredients, menu items, recipes, staff, suppliers) that both tracks query but only `core` seeds.

A track may **never** read another track's private tables directly. Track A learns about inventory only via `MENU_TOGGLE`, `STOCKOUT_RISK`, and `SUPPLIER_PRICE_UPDATE` signals. Track B learns about demand only via `DEMAND_FORECAST` and `BATCH_DECISION` signals.

### 2.2 Three Zones and Write Ownership

| Zone | Branch | What it owns |
|------|---------|-------------|
| **`core`** | `main` | Clock, orchestrator, bus, ALL DB models, POS sim, voice, calls, weather, seeding, LLM, FastAPI+WS, React shell + Approval Inbox, Demo Control Bar |
| **`track_a`** | `track_a` | `forecasts`, `batches`, `competitor_offers`, `competitor_intel`, `review_insights`; writes `batch_definition` decisions; React: Forecast, Competitors, Reviews, Staff, Signal Feed |
| **`track_b`** | `track_b` | `inventory_ledger/lots/levels`, `waste_events`, `purchase_orders(_lines)`, `menu_toggles` (+`menu_items.active`), `promotions`, `negotiations`, `supplier_price_history`, dynamic fields of `supplier_catalog`; React: Inventory, Expiry, Suppliers, Activity Log |

**Shared append-only tables** (both tracks write, no contention): `signals`, `event_log`, `approval_requests`.

### 2.3 DEMO_MODE and the Mock Contract

`DEMO_MODE` (env var, default `combined`) controls which side's signals are mocked:

| Mode | `core` | `track_a` | `track_b` |
|------|--------|-----------|-----------|
| `track_a` | Real | Real | `MockInventory` emits `MENU_TOGGLE`, `STOCKOUT_RISK`, `SUPPLIER_PRICE_UPDATE` |
| `track_b` | Real | `MockForecaster` emits `DEMAND_FORECAST`, `BATCH_DECISION` | Real |
| `combined` | Real | Real | Real (both mocks off) |

Wiring lives in `core/api.py:_register_tracks` (line ~278): it imports `{pkg}.agents` and calls `register(demo_mode=..., bus, orchestrator, ...)`. The mocks are instantiated only when their mode matches, and are automatically idle in `combined`. **No code changes are needed to switch modes** — only the env var.

### 2.4 Build Phases

- **Phase 0** — Build `core` (done; all on `main`). Output: working clock, bus, DB, POS sim, voice, calls, weather, LLM, API shell, React shell with empty agent stubs.
- **Phase 1** — Build the two tracks in parallel against frozen `core` interfaces (done; `track_a` and `track_b` branches).
- **Phase 2** — Merge. Set `DEMO_MODE=combined`; both tracks run against each other with zero glue code. Run the flagship "Friday Rush" scenario end-to-end. *(Not yet done.)*

---

## 3. The Layered Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Layer 4 — Human-in-the-Loop                                                │
│  Approval Inbox (approve/reject POs, promos, calls)                         │
│  Live dashboards (Forecast, Inventory, Competitors, Reviews, Staff, Expiry) │
│  Demo Control Bar (play/pause/speed/voice/scenario)                         │
└────────────────────────────┬───────────────────────────────────────────────┘
                             │ REST + WebSocket  (/api/*, /ws)
┌────────────────────────────▼───────────────────────────────────────────────┐
│  Layer 3 — Agents                                                           │
│  Track A: DemandForecaster | CompetitorIntelligence | Review | Staff        │
│  Track B: InventoryLedger  | InventoryOptimizer     | MarketSpectator       │
│  (agents only emit/consume signals + read core tables; never touch each     │
│   other's private tables)                                                   │
└────────────────────────────┬───────────────────────────────────────────────┘
                             │ emit / live / consume / sweep
┌────────────────────────────▼───────────────────────────────────────────────┐
│  Layer 2 — Signal Bus  (core/bus.py + signals table in SQLite)              │
│  Typed · Grouped · Expiring · Deduped · Cascade-safe                       │
│  21 signal types across 7 groups: forecasting | inventory | procurement     │
│  kitchen | sensing | human | frontend                                        │
└───────────┬──────────────────────────────────────┬─────────────────────────┘
            │ notify_order_line callback            │ signals
┌───────────▼──────────────────┐       ┌───────────▼──────────────────────────┐
│  Layer 1 — Data Formatter    │       │  Layer 0 — Raw Inputs                │
│  (core/formatter.py)         │       │  POS simulator (orders/lines)        │
│  Velocity ring buffer        │       │  Voice facts (presenter speech/text) │
│  Wastage relay & routing     │       │  Weather (Open-Meteo or override)    │
└──────────────────────────────┘       │  Inventory lots (seeded + receipts)  │
                                       │  Staff attendance (voice or UI)      │
                                       │  Competitors (seeded + discovery)    │
                                       │  Reviews (seeded or injected)        │
                                       └──────────────────────────────────────┘

Cross-cutting: SimClock + Orchestrator (tick loop) | LLM Provider | Call Subsystem
```

---

## 4. Core Walkthrough — Module by Module

### 4.1 `core/config.py` — All Constants (§22)

Single source of truth for every threshold, rate, and default. No magic numbers in agent logic. Key values:

| Constant | Value | Meaning |
|----------|-------|---------|
| `TICK_REAL_MS` | 250 | Real-time tick cadence (ms) |
| `REAL_MINUTES_PER_DAY_1X` | 15 | Default: one sim-day = 15 real minutes |
| `SPEEDS` | [0.25, 0.5, 1, 2, 4, 8] | Allowed speed multipliers |
| `DAYPARTS` | breakfast/lunch/afternoon/dinner/late | (start, end, weight); weights sum to 1.00 |
| `BASE_ORDERS_PER_DAY` | 300 | Poisson order arrival baseline |
| `CANCEL_RATE` | 0.03 | Probability of a line being voided |
| `VELOCITY_WINDOW_SIM_S` | 1800 | Rolling velocity window (30 sim-min) |
| `FORECAST_INTERVAL_SIM_S` | 1800 | Forecaster fires every 30 sim-min |
| `HISTORY_DAYS` | 30 | Days of seeded POS history |
| `EVENT_MULT` | 1.35 | Demand multiplier for a voice-added event |
| `STAFF_CAP_FACTOR` | 0.5 | Demand cap when a station is unstaffed |
| `SAFETY_DAYS` | 0.5 | Stock coverage = safety days × daily usage |
| `PAR_DAYS` | 3 | Par-level target days of coverage |
| `EXPIRY_SCAN_SIM_S` | 3600 | Ledger scans for expiry every sim-hour |
| `EXPIRY_WINDOW_SIM_S` | 172800 | Alert 2 sim-days before expiry |
| `APPROVAL_PO_THRESHOLD` | 200 | PO total above this requires human approval |
| `SIGNAL_COOLDOWN_SIM_S` | 1800 | Min interval before a same-key signal refreshes |
| `MAX_CASCADE_DEPTH` | 5 | Max signal chain depth before drop |
| `COMPETITOR_RADIUS_KM` | 3 | Radius for competitor discovery |

### 4.2 `core/db.py` — Database Layer

- **Engine:** single SQLite file at `DB_PATH` (from env, default `demo.db`), `check_same_thread=False`, `pool_pre_ping=True`.
- **Session:** `SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)`. Each call site creates a fresh session and closes it when done.
- **`create_all()`** creates all tables from `models.py`.
- **`reset_db(keep_reference=True)`** (db.py) drops and recreates `TRANSACTIONAL_MODELS + INTELLIGENCE_MODELS` while leaving reference data (ingredients, menu, staff, etc.) intact. The API's live restart path uses a separate `_wipe_for_seed()` function in `api.py` instead.

> **Local uncommitted edit (stashed as "track_b: core/db.py scoped_session→sessionmaker"):** The committed code on all branches uses `scoped_session`. The stashed working-tree change switches to plain `sessionmaker` with an explanatory comment about identity-map races under concurrent async/thread callers. This is a pending improvement.

### 4.3 `core/models.py` — All 38 Tables (§19)

All 38 tables from the spec are implemented. See [§5 The Data Model](#5-the-data-model--all-38-tables) for the full schema.

Conventions (`models.py:1–14`):
- `id INTEGER PRIMARY KEY AUTOINCREMENT` on every table except `signals` (which uses `signal_id TEXT`).
- All `*_at`/`*_time`/`expiry`/`expires_at` columns are `Float` (sim-seconds since epoch).
- JSON columns use `sqlalchemy.JSON` (TEXT under SQLite).
- Booleans stored as Integer 0/1.

### 4.4 `core/clock.py` — SimClock (§6)

**The clock owns controls; the orchestrator owns per-tick advancement.** This separation is explicit in the module docstring (`clock.py:1–14`).

#### Tick Math (§6.1)
`Δsim_per_tick = 60 × speed × 0.25`. At 1× and 250ms tick interval: `60 × 1 × 0.25 = 15 sim-seconds per real-second`, giving 54 000 sim-s / 3600 real-s = **15 real minutes per sim-day**. The orchestrator performs this calculation (`orchestrator.py:198`):
```python
delta = 60.0 * float(speed) * 0.25
```

**Closed-hours jump** (`orchestrator.py:201–203`): when `candidate ≥ day_close (23:00)`, `now` jumps to `next_day × 86400 + 28800 (08:00)`, skipping the 23:00→08:00 dead zone entirely in one tick. Triggers scheduled inside the skipped window are not fired; they roll forward.

#### State Machine (§6.2)
States: `STOPPED → RUNNING ⇄ PAUSED`; transient `CALL_FROZEN`.

| Control | Implementation | Effect |
|---------|---------------|--------|
| `play()` | `clock.py:147` | Status → RUNNING |
| `pause()` | `clock.py:159` | Status → PAUSED |
| `stop()` | `clock.py:170` | Reset sim_time to start-of-current-day; `bus.sweep(SWEEP_ALL_NOW)` clears all live signals |
| `restart()` | `clock.py:190` | Optional re-seed; reset to day-0 start, STOPPED |
| `set_speed()` | `clock.py:209` | Validates against `config.SPEEDS`; changes multiplier live |
| `step()` | `clock.py:255` | Advances to `orchestrator.next_due_at(now)`; then PAUSED |
| `jump_to_next_event()` | `clock.py:280` | Queries earliest unfired `scenario_events.at_sim_time`; then PAUSED |

#### Call Freeze (§6.3)
`freeze_for_call()` (`clock.py:226`): saves `(prior_status, prior_speed)`, sets `CALL_FROZEN`. The orchestrator's `run_loop` only calls `tick()` when status is `RUNNING`, so the call freeze effectively stops sim time. `unfreeze_from_call()` (`clock.py:240`) restores both fields.

> **Gap:** The `call_mode="slow"` option (§6.3) — which would clamp speed to 0.1× instead of freezing — is **not implemented**. `freeze_for_call` always freezes regardless of `call_mode`. The field is stored and stamped on `Call.clock_action` but has no effect on behavior.

### 4.5 `core/bus.py` — SignalBus (§14)

The signal bus is the system's nervous system. Every inter-agent communication is a row in the `signals` table.

**`emit(type, payload, source, groups, priority, ttl, dedup_key, correlation_id, now)`** (`bus.py:105`):
1. Looks up registry defaults for groups/priority/TTL (`signals.py:SIGNAL_REGISTRY`).
2. Validates payload against the registered pydantic model (`_validate_payload`, `bus.py:36`).
3. Checks cascade depth guard: if `correlation_id` suffix `:N > MAX_CASCADE_DEPTH`, **drop the emit silently** and return `None` (`bus.py:137–144`).
4. If a `dedup_key` is given, looks for an existing live row with the same key (`bus.py:150–155`):
   - **Identical payload** → no-op; returns existing signal. Also logs "cooldown" if within `SIGNAL_COOLDOWN_SIM_S`.
   - **Changed payload** → updates the existing row in place (refresh `expires_at`, replace `payload`). Queues a WS re-broadcast but **does not** re-dispatch subscribers (prevents double-acting on logical duplicates). (`bus.py:176–191`)
5. Otherwise: insert a new `Signal` row, notify WS queue, dispatch subscribers.

**`live(groups, type)`** (`bus.py:217`): returns all `status='live'` signals, optionally filtered by group intersection or type.

**`consume(signal_id)`** (`bus.py:246`): marks a signal `status='consumed'`.

**`sweep(now)`** (`bus.py:261`): bulk-updates `live → expired` where `expires_at ≤ now`. Called every tick by the orchestrator.

**Order-line callback** (`bus.py:316–323`): `register_order_line_handler(fn)` / `notify_order_line(line)` — a plain list of callbacks invoked for every sold order line. This is how the POS simulator drives Track B's Ledger **without** flooding the `signals` table with thousands of order-line rows.

**Subscribe / dispatch** (`bus.py:280–312`): `subscribe(signal_type, callback)` registers an in-process callback fired on every genuine new emit. A dedup-refresh does not re-fire subscribers. Failures in one subscriber are caught and logged without breaking others.

### 4.6 `core/signals.py` — Signal Taxonomy (§15)

`SignalType` enum with **21 types** (`signals.py:26–49`). `SIGNAL_REGISTRY` maps each type to its default groups, priority, and TTL (`signals.py:57–163`). One pydantic `*Payload` model per type validates fields at emit time (`signals.py:171–362`).

See [§6 The Signal Bus & Signal Taxonomy](#6-the-signal-bus--signal-taxonomy) for the full table.

### 4.7 `core/orchestrator.py` — Orchestrator (§17)

The orchestrator drives the tick loop and routes signals to agents.

**Trigger kinds** (`orchestrator.py:43`): `interval | deadline | signal_driven | threshold | manual`. `register()` validates the kind and computes initial `next_due` for interval triggers (`orchestrator.py:122–153`).

> **Note:** `signal_driven`, `threshold`, and `manual` are registry-accepted kinds but **are not auto-dispatched by the time scheduler**. They are registered so the `Trigger` object exists for reference/querying, but their wake path is via the bus subscriber callbacks and REST endpoints respectively. Only `interval` and `deadline` triggers fire from the tick loop.

**`tick()`** (`orchestrator.py:187`) — executed every 250ms real while `RUNNING`:
1. Advance `sim_time` by `Δsim = 60 × speed × 0.25`; handle closed-hours jump.
2. Write new `sim_time` + derived `day_number`/`day_of_week` back to `sim_state`.
3. Publish new `sim_time` onto the bus (`bus.sim_time = now`).
4. Fire due **interval** triggers, catching up across jumps (`orchestrator.py:273–286`).
5. Fire due **deadline** triggers (one-shot; sets `fired=True`) (`orchestrator.py:289–301`).
6. `bus.sweep(now)` — expire stale signals.
7. Fire due **scenario events** from `scenario_events` table (marks `fired=1`) (`orchestrator.py:303–322`).
8. Assemble and return WS events: `sim_tick` first, then all `signal_emitted` payloads drained from `bus.pending_broadcasts()`.

**Agent routing** (`orchestrator.py:177–183`): `on_signal(signal)` fans a signal out to every registered agent whose `subscribed_groups` intersect the signal's `groups`. This is the group-visibility rule (§14.4) — agents only receive signals in their subscribed groups.

**`run_loop(broadcast_fn)`** (`orchestrator.py:334`): async loop, ticks every `TICK_REAL_MS/1000` seconds, only advances when status is `RUNNING`. The loop calls `broadcast_fn(events)` which the API wires to the WebSocket hub.

### 4.8 `core/formatter.py` — Data Formatter & Wastage Relay (§16)

**Velocity ring buffer** (`formatter.py:60–84`): per-`menu_item_id`, stores `(sim_time, qty)` tuples for the last `VELOCITY_WINDOW_SIM_S` (30 sim-min). `item_velocity(id)` returns items-per-sim-second. The Forecaster calls this to compute the `recent_velocity` multiplier.

**`on_order(order, lines)`** (`formatter.py:88`): called by the POS simulator for each created order.
- Voided lines → `emit_waste(waste_type="cancelled_order")`.
- Non-voided lines → `_record_sale()` + `bus.notify_order_line(line)` (drives Ledger depletion).
- Broadcasts `order_created` WS event with the per-item velocity dict.

**Wastage relay** (`formatter.py:128`): `emit_waste(...)` writes a `waste_events` row, then emits `WASTE_EVENT` with groups from the routing table (`formatter.py:26–32`):

| `waste_type` | Signal groups |
|-------------|---------------|
| `overproduction` | inventory, forecasting, procurement, human |
| `spoilage` | inventory, procurement, human |
| `expiry` | inventory, procurement, human |
| `cancelled_order` | inventory, human |
| `prep_error` | inventory, human |

### 4.9 `core/pos_simulator.py` — POS Simulator (§10)

Generates `orders` + `order_lines` during `RUNNING`. Registered as an interval trigger (fires every tick).

**Arrivals**: Poisson; inter-arrival = `Exponential(1/rate)` where `rate = base_orders_per_day × velocity × daypart_weight(t) / 54000`. Active `anomaly_injections` velocity-mult windows scale the rate during surges. If `rate = 0` (outside operating hours, wrong daypart) returns `inf` — no order is generated, no crash.

**Per order**: `n_lines ~ {1: 0.5, 2: 0.3, 3: 0.2}`; each line samples `menu_items` by `dish_mix_weights` (restricted to `active=1` items); `channel` from `channel_mix`; price = `dine_in_price` or `online_price` by channel. Weather channel-shift applied from `config.WEATHER_CHANNEL_SHIFT` (e.g. rain → delivery up, dine-in down).

**Cancellations**: with probability `CANCEL_RATE` (0.03), a line status = `voided` → formatter routes it to `cancelled_order` waste event.

POS-editable live: `base_orders_per_day`, `velocity`, `dish_mix_weights`, `channel_mix`, `daypart_curve`.

### 4.10 `core/voice.py` — Voice Intake (§11)

Pipeline: `text → LLM extraction (JSON) → regex fallback → deterministic apply → persist → emit USER_FACT`.

**Extraction**: `llm.complete` with a JSON schema targeting `{intent, entity_type, entity_ref, attribute, value, effective_window?, confidence}`. If the LLM is unavailable, `_regex_extract` covers the four demo-critical intents with keyword patterns.

**Intents and apply steps**:
| Intent | Apply action |
|--------|-------------|
| `set_leave` | Write `attendance` rows (one per sim-day in window, `status='leave'/'sick'`) |
| `record_receipt` | Create `inventory_lot` + `inventory_ledger(receipt, +qty)` |
| `add_event` | Store `USER_FACT` with `effective_window` + demand multiplier tag |
| `add_inventory_count` | Write `inventory_levels.last_counted_*` + reconciliation ledger delta |
| `add_menu_item` | Create `menu_items` row; LLM drafts a recipe |
| `set_competitor`, `add_review`, `set_attendance` | Direct table writes |

**All paths**: persist a `user_facts` row; emit `USER_FACT` to all groups; **never bypass validation**.

**Worked examples** (all implemented):
- "Ansi is on leave the whole next week." → 7 attendance rows (`leave`), `USER_FACT(set_leave)`.
- "We received 20 kg of tomatoes from GreenFarm at 2 dollars a kilo." → lot + receipt ledger.
- "Add a Margherita pizza for 12 dollars." → `menu_items` row + LLM recipe.
- "There's a parade on our street this Monday." → `USER_FACT(add_event)` with Monday window + demand bump.

### 4.11 `core/calls.py` — Interactive Call Subsystem (§8)

Two agents make outbound approval-gated calls: **Market Spectator** (negotiates supplier prices) and **Competitor Intelligence** (undercover research). Both use this single subsystem.

**Lifecycle** (`calls.py`):
```
agent.calls.request(...)
  → Call row (status=requested)
  → ApprovalRequest (type=outbound_call)
  → CALL_REQUEST signal
  → bus subscriber: _on_approval_resolved
      rejected → Call status=rejected; agent gets CALL_OUTCOME with empty outcome
      approved → _start_call:
          if another call active → queue (FIFO)
          else:
              clock.freeze_for_call() → CALL_FROZEN
              CALL_STARTED signal; WS: call_started; UI opens roleplay console
              → turn loop (add_turn / generate_agent_turn via LLM)
                  each turn → calls.transcript appended; WS: call_turn
              → presenter clicks "End call" → calls/{id}/end → _finalize:
                  LLM parses transcript → outcome JSON → calls.outcome
                  CALL_OUTCOME signal → initiating agent's groups
                  clock.unfreeze_from_call() → restores prior status/speed
                  if next call pending → _start_call
```

**Fallback**: if the presenter declines roleplay or STT fails, `auto_resolve()` calls the LLM to simulate a counterparty response seeded from the catalog/competitor data. The call completes as `status=auto_resolved`.

**One active call at a time**: a second `request()` while one is active enters a FIFO queue; the approval card shows it waiting.

**Outcome extraction** (`calls.py`): after hangup, core sends the transcript to the LLM with the §8.5 JSON schema (`{agreed?, agreed_price?, popular_dishes?, price_points?}`). Core writes `calls.outcome` and emits `CALL_OUTCOME`. **Core writes no track-specific tables** — the initiating agent consumes `CALL_OUTCOME` and writes its own domain records.

### 4.12 `core/weather.py` — Weather Provider (§9)

**Fetch**: Open-Meteo API every `WEATHER_FETCH_SIM_S` (10800 sim-s = 3 sim-hours). `map_weather_code` maps WMO codes to the 5 canonical conditions (`clear|clouds|rain|storm|snow`).

**Fallback chain**: API response → last `weather_log` row → `DEFAULT_WEATHER` (clear, 20°C, 0 precip). Never raises.

**Override**: `POST /api/weather/override` writes a `weather_log` row with `source=override`; overrides win until changed. Emits `WEATHER_UPDATE` on every new row.

### 4.13 `core/seeding.py` — Seeding & Generation (§12)

**Mode A — Presets** (`/data/bellas_kitchen.json`, `/data/burger_joint.json`): complete curated JSON bundles loaded via `POST /api/seed/preset/{id}`. Each bundle contains the full seed graph: ingredients, menu, recipes, batch definitions, stations, staff, suppliers, inventory lots/levels, historical orders, competitors, reviews.

**Mode B — LLM Generation** (`POST /api/seed/generate`): LLM generates qualitative content (dish names, ingredient lists, staff names) → **validator** → **numeric layer** (code computes all consistency-critical numbers):
- `daily_usage = Σ(qty × seed_daily_item_sales)` per ingredient
- `safety_stock = SAFETY_DAYS × daily_usage`
- `par_level = PAR_DAYS × daily_usage`
- `reorder_point = supplier_lead_days × daily_usage + safety_stock`
- Initial lots at ~80% of par; staggered expiry dates based on `shelf_life_days`
- 30 days of synthetic POS history distributed by daypart curve + dish-mix weights

**Validator** (`seeding.py`): referential-integrity rules (every recipe ingredient exists, every station has ≥1 staff, every ingredient has ≥1 supplier, all prices > 0). ≤2 auto-repair passes; then surface the error. On failure the chain never raises silently.

### 4.14 `core/llm.py` — LLM Provider (§13)

**Fallback chain** (`llm.py`): `complete(messages, json_schema, max_tokens, use_site)` tries:
```
Gemini 2.0 Flash → Groq (llama-3.3-70b) → OpenRouter (mistral-7b-instruct:free) → canned
```

Per-provider: exponential backoff, 3 retries, base 1.5s. 429/5xx/timeout → retryable. Missing API key / 4xx → skip provider immediately. After switching providers, sleep `LLM_INTER_CALL_SLEEP_S` (2s) to protect free-tier RPM.

**Cache**: in-process `sha256(messages + schema)` dict. Generation calls (`use_site="generation"`) are never cached.

**JSON mode**: when `json_schema` is given, build a pydantic model dynamically, validate the response. One re-ask on parse failure, then fall through to canned.

**Canned fallbacks** per use-site (marker `CANNED_NOTE`, `llm.py:40`): `voice`, `review`, `call_competitor`, `call_supplier`, `generation`, `forecaster_suggestion`, `outcome_extraction`. Every call site ships a deterministic fallback — the demo never hard-stops without API keys.

### 4.15 `core/agent_base.py` — BaseAgent

Abstract base for all seven agents (`agent_base.py`):

```python
class BaseAgent(ABC):
    def subscribe(self, groups: List[str]) -> None   # record listened groups
    def on_signal(self, signal: Signal) -> None      # abstract — override
    def emit(self, type, payload, source=None, **kwargs) -> Optional[Signal]  # → bus.emit
    def log_event(self, category, summary, detail) -> EventLog  # → event_log table
    def sim_time -> float  # delegates to bus.sim_time (set by clock each tick)
```

All agents call `self.subscribe(groups)` in `__init__`. The orchestrator fans signals to agents whose `subscribed_groups` intersect the signal's `groups` (`orchestrator.py:180–183`).

### 4.16 `core/approvals.py` — ApprovalsHub (§19.4)

**Core owns the approval queue**; tracks only *act on* resolutions for their own types. `ApprovalsHub`:
- `create(type, title, summary, payload, ref_id)` → writes `ApprovalRequest(status=pending)`, emits `APPROVAL_REQUEST`, broadcasts `approval_created` WS event.
- `approve(id)` / `reject(id)` → update row status, emit `APPROVAL_RESOLVED`, broadcast `approval_resolved`.
- `expire_pending(now)` → marks 6h-old pending requests as `expired`.

Track B's approval handlers subscribe to `APPROVAL_RESOLVED` via `bus.subscribe()` and act on their own types (`purchase_order` → place PO; `promo` → activate). The core calls subsystem handles `outbound_call` resolutions.

### 4.17 `core/scenarios.py` — Scenario Engine (§18.8/18.9)

Eight event types: `inject_signal`, `change_setting`, `inject_review`, `set_competitor`, `call_in_sick`, `supplier_change`, `weather_set`, `velocity_mult`. Events fire when `at_sim_time ≤ now` and `fired = 0`; the orchestrator fires them each tick.

**"Friday Rush" scenario** (seeded inactive, activate via UI or `POST /api/scenarios/{id}/activate`):

| Sim-time | Event | Effect |
|----------|-------|--------|
| 11:30 | `velocity_mult ×1.6` for 45min | Lunch rush surge → Forecaster spikes |
| 12:15 | `call_in_sick` grill cook | `STAFF_COVERAGE(covered=false, station=grill)` → Forecaster caps grill items, skips their batches |
| 13:00 | `supplier_change` tomato `availability=out` | No reorder possible → `LOW_STOCK` → pasta `MENU_TOGGLE(disable)` → Track A stops forecasting/batching pasta |
| 15:00 | `weather_set` rain | `WEATHER_UPDATE(rain)` → delivery demand up, dine-in down; channel-mix shift in POS sim |
| 18:00 | `velocity_mult ×1.4` for 4h | Dinner rush |
| 21:30 | `inject_signal EXPIRY_RISK` mozzarella | → `PROMO_PROPOSAL(combo, 20%)` → Approval inbox |

This single run exercises every agent and the full cross-track cascade.

### 4.18 `core/api.py` — REST API + WebSocket Hub (§20/§21)

**WebSocket hub** (`api.py:~1018`): single `/ws` endpoint; thread-safe broadcast queue drained by an async task. Events pushed to the frontend:

`sim_tick` | `order_created` | `signal_emitted` | `sim_state_changed` | `weather_updated` | `approval_created` | `approval_resolved` | `call_started` | `call_turn` | `call_ended` | `event_logged` | `forecast_updated` | `batch_decided` | `inventory_updated` | `menu_toggled` | `supplier_price_updated`

**REST routes** (`/api` prefix, all JSON):

| Category | Routes |
|----------|--------|
| Sim control | `POST /sim/play\|pause\|stop\|restart\|step\|jump-next\|speed`, `GET /sim/state`, `GET+PATCH /sim/pos` |
| Seeding | `GET /seed/presets`, `POST /seed/preset/{id}`, `POST /seed/generate` |
| CRUD | `/menu`, `/recipes`, `/staff`, `/suppliers`, `/supplier-catalog`, `/inventory`, `/competitors`, `/reviews` — GET list, POST create, PATCH/{id}, DELETE/{id} |
| Weather | `GET /weather`, `POST /weather/override` |
| Reads | `/forecasts`, `/inventory/ledger`, `/signals`, `/approvals`, `/events`, `/batches`, `/waste`, `/purchase-orders`, `/inventory/lots`, `/promotions`, `/negotiations`, `/competitor-intel`, `/calls` |
| Actions | `POST /approvals/{id}/approve\|reject`, `POST /voice/transcript`, `POST /calls/{id}/turn`, `POST /calls/{id}/end`, `POST /market/negotiate` |
| Scenarios | `GET+POST /scenarios`, `POST /scenarios/{id}/activate\|deactivate` |

**Bootstrap** (`api.py:lifespan`): `create_all()` → create clock, bus, orchestrator, formatter, weather, seeding, LLM, voice, calls, approvals, scenarios → `_register_tracks(demo_mode)` → wire WS sinks → start `orchestrator.run_loop()`.

**DEMO_MODE wiring** (`api.py:_register_tracks`): imports `{pkg}.agents` and calls `register(demo_mode, bus, orchestrator, db_session_factory, llm, calls, approvals, ws_broadcast)` for each mode-matching track. Track-specific REST endpoints (e.g. `POST /api/track-a/forecast/run`, `POST /api/market/negotiate`) use the component dict returned by `register()`.

---

## 5. The Data Model — All 38 Tables

### Conventions
- Sim-time columns are `Float` (sim-seconds since epoch = 00:00 of day 0).
- JSON columns: `sqlalchemy.JSON` (TEXT holding JSON under SQLite).
- Boolean columns: `Integer` 0/1.
- FK names: `<entity>_id`.
- Single exception: `signals.signal_id` is `TEXT PRIMARY KEY` (all others are `INTEGER AUTOINCREMENT`).

### §19.1 — Reference / Config (seeded; survive `reset_db(keep_reference=True)`)

| Table | Key columns | Notes |
|-------|-------------|-------|
| `ingredients` | `name, category, base_unit (g\|ml\|each), perishable, shelf_life_days, allergen_flags JSON, weather_tags JSON` | Weather tags drive the weather multiplier |
| `stations` | `name` | Kitchen stations (grill, pasta, etc.) |
| `menu_items` | `name, category, station_id→stations, dine_in_price, online_price, prep_time_min, is_batchable, active=1, weather_tags JSON` | `active` toggled by Track B's Optimizer |
| `recipes` | `menu_item_id→menu_items` | One recipe per dish |
| `recipe_lines` | `recipe_id, ingredient_id, qty, unit, optional=0` | Ingredients + quantities per dish |
| `batch_definitions` | `menu_item_id, dayparts JSON, prep_lead_time_min, batch_size_min/step/max, decide_by_offset_min, station_id` | Controls pre-cook decisions |
| `staff` | `name, role, skill_level, hourly_cost, active` | Kitchen and FOH staff |
| `staff_stations` | `staff_id, station_id` | M:N station coverage mapping |
| `staff_dish_skills` | `staff_id, menu_item_id` | Dish-level skill exceptions |
| `suppliers` | `name, lead_time_days, reliability_score, min_order_value` | Supplier master |
| `supplier_catalog` | `supplier_id, ingredient_id, current_price, unit, pack_size, availability (in_stock\|limited\|out), updated_at` | Core seeds; Track B writes `current_price/availability/updated_at` |

### §19.2 — State / Transactional (cleared on `reset_db` transactional wipe)

| Table | Key columns | Notes |
|-------|-------------|-------|
| `inventory_lots` | `ingredient_id, qty_on_hand, purchase_price, expiry_date, status (active\|depleted\|expired)` | FIFO depleted oldest-expiry-first |
| `inventory_ledger` | `ingredient_id, lot_id, delta_qty, reason (receipt\|sale_depletion\|batch_depletion\|waste\|reconciliation), ref_id, sim_time, balance_after` | **Append-only source of truth; `on_hand = ledger sum`** |
| `inventory_levels` | `ingredient_id UNIQUE, par_level, reorder_point, safety_stock, yield_factor, on_hand_cached, last_counted_at, last_counted_qty` | `on_hand_cached` kept in lockstep with ledger |
| `orders` | `sim_time, service_mode, channel, total, status (open\|closed\|cancelled)` | One per customer visit |
| `order_lines` | `order_id, menu_item_id, qty, unit_price, status (sold\|voided\|comped), sim_time` | High-volume; not on signal bus (callback path) |
| `batches` | `batch_definition_id, menu_item_id, serve_window JSON, decision (cook\|skip), planned_qty, status (decided\|prepping\|ready\|served\|expired)` | Written by Track A Forecaster |
| `waste_events` | `waste_type (overproduction\|spoilage\|cancelled_order\|prep_error\|expiry), ingredient_id?, menu_item_id?, lot_id?, qty, cost, sim_time` | Feeds waste-cost dashboard |
| `purchase_orders` | `supplier_id, status (proposed\|approved\|placed\|delivered\|cancelled), expected_delivery, total_cost, approval_id?` | |
| `purchase_order_lines` | `po_id, ingredient_id, qty, unit_price, line_total` | |
| `menu_toggles` | `menu_item_id, action (disable\|enable), reason, triggered_by, sim_time, active` | History of all toggles |
| `attendance` | `staff_id, date_sim_day, status (present\|leave\|sick), daypart? (null=whole day)` | Written by voice `set_leave`; read by Staff agent for coverage |

### §19.3 — Intelligence / Agent I/O (cleared on reset)

| Table | Key columns | Notes |
|-------|-------------|-------|
| `forecasts` | `menu_item_id, window JSON, daypart, forecast_qty, baseline_qty, multipliers JSON, confidence, generated_at, trigger_reason` | Written by Track A Forecaster |
| `signals` | `signal_id TEXT PK, type, source, groups JSON, priority, payload JSON, created_at, expires_at, dedup_key, status (live\|consumed\|expired), correlation_id` | Indexes on `status` and `(dedup_key, status)` |
| `competitors` | `name, cuisine JSON, distance_km, rating, is_open, price_tier` | Treated as reference (survives reset) |
| `competitor_offers` | `competitor_id, dish_or_combo, price, description` | Treated as reference |
| `competitor_intel` | `competitor_id, method (call\|aggregator\|discovery), popular_dishes JSON, price_points JSON, call_id?` | Written by Track A |
| `reviews` | `source, rating, text, dish_mentions JSON, sentiment, sim_time, processed` | Seeded + injected; `processed` flag for Track A Review agent |
| `review_insights` | `review_id?, insight_type, summary, suggested_action, severity, sim_time` | Written by Track A Review agent |
| `supplier_price_history` | `supplier_id, ingredient_id, price, sim_time` | Written by Track B Market Spectator |
| `negotiations` | `supplier_id, ingredient_id, call_id?, transcript JSON, outcome JSON, savings, sim_time` | Written by Track B Market Spectator |
| `approval_requests` | `type (purchase_order\|menu_change\|promo\|outbound_call\|other), title, summary, payload JSON, urgency, status (pending\|approved\|rejected\|expired), created_at, resolved_at, ref_id` | Written by core ApprovalsHub |
| `promotions` | `type (combo\|discount), menu_items JSON, trigger, discount_pct, channel, status (proposed\|approved\|active\|expired), approval_id?` | Written by Track B Optimizer |
| `user_facts` | `raw_text, source (voice\|text), extracted JSON, applied, resulting_writes JSON, sim_time` | Written by core voice pipeline |
| `weather_log` | `sim_time, source (api\|override), temp_c, condition (clear\|clouds\|rain\|storm\|snow), precip_mm, wind_kph, applied` | |
| `calls` | `agent (market_spectator\|competitor_intel), counterparty_type (supplier\|competitor), status, transcript JSON, outcome JSON, started_at, ended_at, clock_action` | |

### §19.4 — Simulation / Control

| Table | Key columns | Notes |
|-------|-------------|-------|
| `sim_state` | `id=1 singleton; sim_time, day_number, day_of_week, speed, status, operating_window JSON, skip_closed_hours, call_mode, active_seed_id` | Single row; clock + orchestrator are the only writers |
| `sim_settings` | `id=1 singleton; base_orders_per_day=300, velocity=1.0, dish_mix_weights JSON, daypart_curve JSON, channel_mix JSON, anomaly_injections JSON` | POS control; editable live |
| `scenarios` | `name, description, is_active` | |
| `scenario_events` | `scenario_id, at_sim_time, event_type, payload JSON, fired=0` | |
| `event_log` | `sim_time, category, actor, summary, detail JSON` | **The activity-log / narrative feed; append-only** |

### The Seed Graph

When a preset loads, the tables are populated in this FK-respecting order:
```
ingredients → stations → menu_items → recipes → recipe_lines
→ batch_definitions → staff → staff_stations → staff_dish_skills
→ suppliers → supplier_catalog → inventory_lots → inventory_levels
→ orders (historical) → order_lines (historical)
→ competitors → competitor_offers → reviews
→ supplier_price_history → weather_log → sim_state → sim_settings
```

Historical `order_lines` use **negative `sim_time`** values (pre-day-0) so the Forecaster's baseline query (`_history_average`) can find them by querying all lines regardless of sign.

---

## 6. The Signal Bus & Signal Taxonomy

### 6.1 Signal Envelope (§14.1)

Every signal stored in the `signals` table and emitted over WebSocket:

```json
{
  "signal_id": "uuid",
  "type": "LOW_STOCK",
  "source": "ledger",
  "groups": ["procurement", "inventory", "human"],
  "priority": 3,
  "payload": { "ingredient_id": 3, "on_hand": 1.2, "threshold": 2.0, "projected_runout": 43200.0, "unit": "kg" },
  "created_at": 51230.0,
  "expires_at": 65630.0,
  "dedup_key": "low_stock:3",
  "status": "live",
  "correlation_id": "abc:2"
}
```

### 6.2 The Seven Groups

| Group | Who listens | Typical subscribers |
|-------|------------|---------------------|
| `forecasting` | Track A | Forecaster, Staff |
| `inventory` | Track B | Ledger, Optimizer |
| `procurement` | Track B | Optimizer, Market Spectator, Approval handlers |
| `kitchen` | Future / human | Batch execution |
| `sensing` | Track A | Competitor Intelligence, Review |
| `human` | Frontend / Approval Inbox | All approval-relevant signals |
| `frontend` | Frontend direct | STOCKOUT_RISK, MENU_TOGGLE |

### 6.3 All 21 Signal Types

`→` = crosses the track boundary; both tracks must mock these for standalone runs.

| Type | Groups | Prio | TTL (sim-s) | Payload highlights |
|------|--------|------|-------------|-------------------|
| `DEMAND_FORECAST` → | forecasting, inventory, human | 2 | until window end | `menu_item_id, window{start,end}, daypart, qty, baseline, multipliers{}, confidence` |
| `BATCH_DECISION` → | kitchen, inventory, human | 3 | 14400 (4h) | `batch_definition_id, menu_item_id, serve_window, decision (cook\|skip), qty, by` |
| `WASTE_EVENT` | inventory, forecasting, procurement, human | 3 | 21600 (6h) | `waste_type, ingredient_id?, menu_item_id?, lot_id?, qty, unit, cost, reason` |
| `LOW_STOCK` → | procurement, inventory, human | 3 | 14400 (4h) | `ingredient_id, on_hand, threshold, projected_runout, unit` |
| `STOCKOUT_RISK` → | procurement, inventory, human, **frontend** | 4 | 14400 (4h) | `ingredient_id, on_hand, projected_runout, affected_items[]` |
| `EXPIRY_RISK` | inventory, procurement, human | 3 | until expiry | `ingredient_id, lot_id, qty, expiry, projected_usage_before_expiry` |
| `MENU_TOGGLE` → | forecasting, kitchen, human, **frontend** | 3 | 86400 (24h) | `menu_item_id, action (disable\|enable), reason` |
| `REORDER_PLACED` | procurement, human | 2 | 86400 (24h) | `po_id, supplier_id, lines[], total, eta` |
| `SUPPLIER_PRICE_UPDATE` → | inventory, procurement, forecasting | 2 | 86400 (24h) | `supplier_id, ingredient_id, old_price, new_price, availability, via (market\|call)` |
| `COMPETITOR_UPDATE` → | forecasting, human | 1 | 43200 (12h) | `competitor_id, is_open, offers_changed, summary` |
| `COMPETITOR_INTEL` → | forecasting, human | 2 | 86400 (24h) | `competitor_id, popular_dishes[], price_points{}, method (call\|aggregator), call_id?` |
| `REVIEW_INSIGHT` → | forecasting, human | 2 | 43200 (12h) | `review_id?, severity, summary, suggested_action, dish_mentions[]` |
| `STAFF_COVERAGE` → | forecasting, human | 3 | until shift end | `station_id, covered, affected_items[], shortfall` |
| `PROMO_PROPOSAL` | human | 3 | until expiry | `promo_id, type, menu_items[], discount_pct, channel, trigger` |
| `APPROVAL_REQUEST` | human | 4 | 21600 (6h) | `approval_id, type, title, summary, payload{}, urgency` |
| `APPROVAL_RESOLVED` → | human, procurement, inventory, kitchen | 3 | 7200 (2h) | `approval_id, type, decision (approved\|rejected), ref_id, payload{}` |
| `WEATHER_UPDATE` | forecasting | 1 | 10800 (3h) | `temp_c, condition, precip_mm, wind_kph, source` |
| `CALL_REQUEST` | human | 4 | 3600 (1h) | `call_id, agent, counterparty_type, counterparty_id, purpose` |
| `CALL_STARTED` | human, frontend | 2 | call len | `call_id` |
| `CALL_OUTCOME` → | forecasting, procurement, inventory, human | 2 | 43200 (12h) | `call_id, counterparty_type, outcome{}` |
| `USER_FACT` | forecasting, inventory, procurement, human | 2 | per fact | `intent, entity_type, entity_ref, attribute, value, effective_window?, raw_text` |

### 6.4 Safety Rules

- **Dedup** (`bus.py:149–191`): a `dedup_key` collapses duplicates into a single live row. Changed payload → refresh in place (no duplicate row). Identical payload within cooldown → no-op.
- **Cooldown** (`SIGNAL_COOLDOWN_SIM_S = 1800`): an identical-payload emit within the cooldown window is a logged no-op.
- **Cascade depth** (`MAX_CASCADE_DEPTH = 5`): `correlation_id` suffix `:N` parsed; emit dropped silently when N > 5.
- **Subscriber isolation** (`bus.py:303–312`): a callback exception never breaks other subscribers or the emit path.
- **Dedup-refresh does not re-fire subscribers** (`bus.py:190`): prevents double-acting on logically-duplicate emits.

---

## 7. The Deterministic Algorithms

These algorithms are core decisions that never call the LLM. They are the system's mathematical backbone.

### 7.1 Baseline Demand (§18.1)
```
baseline(item, daypart, dow) =
  mean of historical order_line qty for (item, daypart, dow) over HISTORY_DAYS
  → fallback: (item, daypart) mean
  → fallback: dish_mix_weight × base_orders_per_day × daypart_weight  [Track A impl]
```
Historical lines use time-of-day to determine daypart, and `sim_time / 86400 % 7` for day-of-week.

### 7.2 Demand Forecast (§18.2)
```
forecast(item, window) = baseline(item, daypart, dow) × Π multipliers
```
Multipliers (all default 1.0 if no signal present):

| Multiplier | Source | Value |
|-----------|--------|-------|
| `event` | `USER_FACT(add_event)` | `EVENT_MULT` (1.35) × stated magnitude |
| `competitor` | `COMPETITOR_INTEL`, `COMPETITOR_UPDATE` | Spec: closed-nearby ×1.10; aggressive combo ×0.92 |
| `review` | `REVIEW_INSIGHT` | Spec: positive ×1.08; strong-negative ×0.90 |
| `staff_coverage` | `STAFF_COVERAGE(covered=false)` | Cap to `STAFF_CAP_FACTOR` (0.5) |
| `weather` | Latest `WeatherLog` + item `weather_tags` | Per §18.5 lookup (see below) |
| `recent_velocity` | Formatter's `item_velocity` | `actual_rate / expected_rate`, clamped to `VELOCITY_CLAMP` (0.6, 1.6) |

Output: `{qty (rounded), baseline, multipliers dict, confidence = 1/(1+spread)}`. Written to `forecasts` + emitted as `DEMAND_FORECAST`.

### 7.3 Weather Effect (§18.5)
Deterministic lookup by `condition` × item `category`/`weather_tags`:

| Condition | Effect |
|-----------|--------|
| `rain` / `storm` | Comfort/hot dishes ×1.10; cold drinks/salads ×0.80; **channel**: dine-in ×0.85, delivery ×1.20 |
| `snow` | Dine-in ×0.60, delivery ×1.10; soups/hot ×1.20; overall ×0.90 |
| `hot` (temp_c ≥ 30) | Cold drinks/salads/ice cream ×1.30; soups ×0.70 |
| `cold` (temp_c ≤ 5) | Soups/hot ×1.20; cold items ×0.80 |
| `clear` / `clouds`, mild | ×1.00 |

### 7.4 Batch Decision (§18.3)
At each `batch_definition.decide_by = serve_start − prep_lead − BATCH_BUFFER`:
```
Cook if:
  forecast ≥ batch_size_min
  AND no live MENU_TOGGLE(disable) for this item
  AND no live STOCKOUT_RISK covering its ingredients
  AND no live STAFF_COVERAGE(covered=false) for its station

qty = clamp(round_to_step(forecast, step), min, max)
```
Emits `BATCH_DECISION`; writes `batches` row with a human-readable reason.

### 7.5 FIFO Inventory Depletion (§18.4)
```
For each sold order_line or cooked batch:
  For each recipe_line of the dish:
    used = line_qty × recipe_qty / yield_factor
    Deplete inventory_lots ordered by expiry_date ASC:
      take = min(lot.qty_on_hand, remaining)
      lot.qty_on_hand -= take
      if lot.qty_on_hand ≤ 0: lot.status = 'depleted'
      Append InventoryLedger(delta=-take, balance_after=running_balance)
    If remaining > 0 after all lots: write shortfall ledger row (lot_id=None)
    Update inventory_levels.on_hand_cached = balance_after
```
`on_hand_cached` is always equal to the ledger sum (`Σ delta_qty` for that ingredient).

### 7.6 Reorder Logic (§18.8)
```
When on_hand ≤ reorder_point AND no PO in flight for this ingredient:
  qty = ceil((par_level − on_hand) / pack_size) × pack_size
  supplier = argmax(score = availability_weight − price_norm − lead_norm)
    where availability_weight = {in_stock: 1.0, limited: 0.5}
  If total > APPROVAL_PO_THRESHOLD: create approval_request; wait
  Else: place PO immediately
  On delivery (deadline trigger): receive → create lot + ledger(receipt)
```

### 7.7 Menu Toggle (§18.8)
```
When ingredient.projected_runout < now + min_supplier_lead_time
AND ≥2 active menu items share this ingredient:
  Disable the dish with lowest (margin × velocity)
  margin = dine_in_price − Σ(recipe_line.qty × supplier_catalog.current_price)
  velocity = count of sold order_lines for this dish
  Set menu_items.active = 0; write menu_toggles; emit MENU_TOGGLE(disable)

Re-enable when on_hand > reorder_point (periodic sweep):
  Set menu_items.active = 1; emit MENU_TOGGLE(enable)
```

### 7.8 Expiry → Promo (§18.8)
```
EXPIRY_SCAN_SIM_S interval:
  For each active lot where expiry − now ≤ EXPIRY_WINDOW_SIM_S (2 sim-days):
    If projected_usage_before_expiry < lot.qty_on_hand:
      Emit EXPIRY_RISK
      Create promotions(type=combo|discount, discount_pct=PROMO_DISCOUNT_PCT=20%, status=proposed)
      Create approval_request(type=promo)
      Emit PROMO_PROPOSAL

On APPROVAL_RESOLVED(approved, promo):
  Set promotions.status = 'active'
```

### 7.9 Competitor Intel Usage (§18.6)
```
COMPETITOR_INTEL.popular_dishes → name/category match against our menu_items
  → nudge that item's forecast multiplier × 1.05
  → surface a Forecaster suggestion to promote/add it
```

### 7.10 Forecaster Suggestions (§18.7)
Every `SUGGESTION_INTERVAL_SIM_S` (54000, ≈1 sim-day): send recent sell-through / waste / sellout stats to LLM; LLM returns `{add:[], remove:[], retime:[], resize:[]}`. Non-blocking suggestion cards on the dashboard. Canned fallback = empty ("no change").

---

## 8. Track A — Demand & Sensing

**Branch:** `track_a`. **Wiring:** `track_a/agents/__init__.py:bootstrap_track_a()` instantiates all agents and registers them + their triggers with the orchestrator. `MockInventory` is only created when `DEMO_MODE=track_a`.

### 8.1 Demand Forecaster (`track_a/agents/forecaster.py`)

**Subscribe groups:** `["forecasting"]` (forecaster.py line 55).

**Triggers:** Two interval triggers registered in `register()`:
- `track_a_forecast_interval` every `FORECAST_INTERVAL_SIM_S` (1800 sim-s = 30 sim-min)
- `track_a_forecast_suggestions` every `SUGGESTION_INTERVAL_SIM_S` (54000 sim-s ≈ 1 sim-day)

`on_signal` re-runs `run_forecast` on: `WASTE_EVENT`, `STAFF_COVERAGE`, `COMPETITOR_UPDATE`, `COMPETITOR_INTEL`, `REVIEW_INSIGHT`, `WEATHER_UPDATE`, `USER_FACT`, `MENU_TOGGLE`, `STOCKOUT_RISK`.

**Forecast pipeline** (`run_forecast`):
1. Determine current daypart + window from sim_time.
2. Query all `active=1` menu items.
3. For each item: check for live `MENU_TOGGLE(disable)` → skip if disabled.
4. Compute `baseline_qty` (§7.1 fallback chain).
5. Compute all 6 multipliers via `_multipliers()`.
6. `qty = baseline × Π(multipliers)`, rounded, clipped to ≥ 0.
7. `confidence = 1 / (1 + spread)` where spread = max(multipliers) − min(multipliers).
8. Write `forecasts` row + emit `DEMAND_FORECAST` with `dedup_key=forecast:{item}:{window_start}` + broadcast `forecast_updated`.
9. Log a human-readable `event_log` line with all multiplier values.
10. Call `decide_batches()`.

**Signals emitted:** `DEMAND_FORECAST`, `BATCH_DECISION`.

**Multiplier implementation detail** (`_multipliers`, forecaster.py):
- `_event_multiplier`: matches `USER_FACT(add_event)` with overlapping `effective_window`; multiplies by `payload["value"]` or `EVENT_MULT` (1.35).
- `_competitor_multiplier`: `COMPETITOR_INTEL` popular-dish name match → ×1.05; `COMPETITOR_UPDATE` with `offers_changed` + category in summary → ×0.97.
- `_review_multiplier`: `REVIEW_INSIGHT` with dish name match; severity-based: positive summary → ×1.05; high severity → ×0.85; medium → ×0.92; else → ×0.98.
- `_staff_multiplier`: `STAFF_COVERAGE(covered=false)` matching item or station → return `STAFF_CAP_FACTOR` (0.5).
- `_weather_multiplier`: reads latest `WeatherLog`; applies partial weather-tag matrix (rain/storm/snow ×1.1 for "comfort" tags; rain ×0.9 for "salad"/"cold"; clear ×1.05 for "salad"/"cold").
- `_velocity_multiplier`: `formatter.item_velocity(id)` → ratio vs expected, clamped to `VELOCITY_CLAMP` (0.6, 1.6).

**Batch decision pipeline** (`decide_batches`): for each `BatchDefinition` in the current daypart, get the latest forecast, check `_is_blocked_for_batch` (live `MENU_TOGGLE` disable, live `STOCKOUT_RISK`, live `STAFF_COVERAGE` uncovered), choose cook/skip, compute `qty = _round_batch_qty(forecast, definition)`. Writes `batches` row + emits `BATCH_DECISION` + logs a reason string ("cook 24 Pasta: lunch forecast 22, station staffed, ingredients OK" / "skip: station unstaffed").

**Suggestions (`generate_suggestions`):** Currently **fully stubbed** — returns `{"suggestions": [], "summary": "no_change"}` with no LLM call. The forecaster has no `llm` handle in its constructor.

### 8.2 Competitor Intelligence (`track_a/agents/competitor.py`)

**Subscribe groups:** `["sensing", "forecasting"]`.

**Passive monitoring** (`passive_monitor`, every 10800 sim-s): queries all competitors from the DB, emits `COMPETITOR_UPDATE` for each (dedup_key `competitor:{id}`).

**Discovery** (`discover_targets`): filters competitors within `COMPETITOR_RADIUS_KM` (3 km), shared cuisine, ranked by `rating × proximity`. Top `COMPETITOR_CALL_TARGETS` (2) become call targets. No live web-search — seed data only.

**Undercover call flow:**
1. `request_research(competitor_id)` → `calls.request(agent="competitor_intel", counterparty_type="competitor", ...)`.
2. Core runs the call lifecycle (freeze → roleplay → turn loop with §8.3 customer persona prompt).
3. On `CALL_OUTCOME`: `handle_call_outcome` writes `CompetitorIntel` + emits `COMPETITOR_INTEL`. Fallback to seeded `competitor_offers` if outcome empty.
4. `map_popular_to_menu_item` cross-references popular dishes to our menu items for the ×1.05 forecast nudge.

**Signals emitted:** `COMPETITOR_UPDATE`, `COMPETITOR_INTEL`.

### 8.3 Review Analysis (`track_a/agents/review.py`)

**Subscribe groups:** `["sensing"]`.

**Trigger:** interval every 900 sim-s; also on `USER_FACT(add_review)`.

**Logic** (`process_unprocessed`): for each unprocessed `reviews` row, calls `_analyze` (keyword + rating heuristic — see §13 gap note), writes `ReviewInsight`, marks `processed=1`, emits `REVIEW_INSIGHT` (dedup_key `review:{dish_name|general}`).

**Trend severity** (`_trend_severity`): counts negative dish-mentions across all reviews; ≥3 → severity bumped to `high`.

**Signals emitted:** `REVIEW_INSIGHT`.

### 8.4 Staff (`track_a/agents/staff.py`)

**Subscribe groups:** `["forecasting"]`.

**Trigger:** interval every 1800 sim-s; re-runs on `USER_FACT(set_leave|set_attendance)`.

**Logic** (`recompute`): for each station with active menu items:
1. Get all staff linked to the station via `staff_stations`.
2. For each staffer, check `attendance` table: if a row exists with `status ∈ {leave, sick}` for the current `date_sim_day` (null `daypart` = whole day; set `daypart` = scoped), mark unavailable.
3. `covered = any(available)`.
4. If uncovered: emit `STAFF_COVERAGE(covered=false, affected_items=[...], shortfall)`.
5. If restored: emit `STAFF_COVERAGE(covered=true)`.

`call_in_sick(station_id?)`: writes an `Attendance(sick)` exception; re-runs `recompute`.

**Signals emitted:** `STAFF_COVERAGE`.

### 8.5 MockInventory (`track_a/mocks/mock_inventory.py`)

Active only in `DEMO_MODE=track_a`. Interval trigger every 3600 sim-s.

Emits (per tick on the first active item):
- `MENU_TOGGLE(disable, "mock low stock")` → 1 sim-hour later: `MENU_TOGGLE(enable)`.
- `STOCKOUT_RISK` on the item's first ingredient.
- `SUPPLIER_PRICE_UPDATE` (new price = old × 1.08).

This proves the Forecaster can stop/resume forecasting and batching a disabled item.

### 8.6 Track A React Panels (`frontend/src/track_a/`)

All panels share a `useTrackAData` hook that calls `GET /api/track-a/snapshot` and re-fetches on WS events. Panels use relative `/api` paths; no business logic in UI.

| Panel | What it shows | Key WS events |
|-------|--------------|---------------|
| **ForecastDashboard** | Bar chart baseline vs forecast; "Why" breakdown (multiplier chips e.g. `event ×1.35`); batch decisions with cook/skip + qty + reason | `forecast_updated`, `batch_decided` |
| **CompetitorPanel** | Competitor cards (open/closed, distance, offers); "Research" button per competitor → `POST /api/track-a/competitors/{id}/research`; intel results | `competitor_update`, `call_ended` |
| **ReviewPanel** | Review stream with star/sentiment pills, dish mentions, insight + suggested action | `review_insight` |
| **StaffPanel** | Roster by station, green/red coverage pill; "Call in Sick" / "Restore" buttons | `signal_emitted(STAFF_COVERAGE)` |
| **SignalFeed** | Live signals: type, source, priority chip, expiry countdown (sim-min), click-to-expand payload + group chips | `signal_emitted` |

### 8.7 Track A Tests (`track_a/tests/`)

| File | Coverage |
|------|---------|
| `test_forecaster.py` | Event + weather multiplier applied; baseline=10; confidence > 0; batch skip when STOCKOUT_RISK or STAFF_COVERAGE uncovered |
| `test_competitor.py` | Discovery radius filter; CALL_OUTCOME → CompetitorIntel write; `map_popular_to_menu_item` |
| `test_review.py` | 3 identical negative reviews → trend severity `high`; dedup collapses to single REVIEW_INSIGHT |
| `test_staff.py` | Coverage covered → call_in_sick → uncovered + affected_items; restore → covered |
| `test_contract_a.py` | Subscribed groups ⊆ {forecasting, sensing}; emitted payloads validate against §15; AST check: no `track_b` imports in `track_a/` |

---

## 9. Track B — Inventory & Procurement

**Branch:** `track_b`. **Wiring:** `track_b/agents/__init__.py:register()` — constructs agents/services in dependency order, registers interval triggers, wires the order-line callback, mounts MockForecaster only when `DEMO_MODE=track_b`.

### 9.1 Inventory Ledger (`track_b/agents/ledger.py`)

**The source of truth for stock.** Only writer of `inventory_ledger`, `inventory_lots`, `inventory_levels`.

**Subscribe groups:** `["inventory"]`.

**Triggers:**
- `bus.register_order_line_handler(ledger.handle_order_line)` — per-sold-line callback (fast path, no bus overhead).
- `scan_expiry` interval every `EXPIRY_SCAN_SIM_S` (3600 sim-s).
- `on_signal`: `BATCH_DECISION(cook)` → batch depletion; `USER_FACT(record_receipt|add_inventory_count)` → surface to dashboard.

**Depletion** (`_deplete_for_recipe`, `_deplete_fifo`): exact §18.4 implementation. Per recipe line: `used = qty × recipe_qty / max(yield_factor, 1e-9)`. FIFO by `expiry_date ASC`. Appends `InventoryLedger` row per lot depleted + one shortfall row if stock runs out. Always keeps `inventory_levels.on_hand_cached` in lockstep with `balance_after`. Broadcasting `inventory_updated` WS event.

**Invariant:** `inventory_levels.on_hand_cached` = running `balance_after` from last ledger row. **Never recomputed** — maintained in lockstep at every write (depletion, receipt, waste, reconciliation).

**Threshold signals** (`_check_thresholds`): after each depletion:
- `on_hand ≤ 0` → `STOCKOUT_RISK` (with `affected_items`: active menu items using this ingredient via recipe).
- `on_hand ≤ safety_stock` → `LOW_STOCK`.
- Both deduped by `dedup_key`.

**Expiry scan** (`scan_expiry`): per active lot:
- `expiry − now ≤ EXPIRY_WINDOW_SIM_S` → compute `projected_usage_before_expiry` (from usage buffer); if usage won't consume the lot → emit `EXPIRY_RISK`.
- `expiry ≤ now` → `_expire_lot`: marks lot `expired`, writes waste ledger row, creates `WasteEvent(waste_type="expiry")`, emits `WASTE_EVENT`.

**Receipts** (`receive(po_id)`): called directly by Procurement (not via bus) on delivery. Creates `inventory_lot` (expiry = now + `shelf_life_days × 86400`, default 5 days) + `inventory_ledger(receipt, +qty)`.

**Reconciliation**: on `USER_FACT(add_inventory_count)`, voice.py already wrote the DB. Ledger broadcasts `inventory_updated` and logs the drift (counted − ledger_on_hand) as a `reconciliation` ledger entry for the dashboard.

**Signals emitted:** `LOW_STOCK`, `STOCKOUT_RISK`, `EXPIRY_RISK`, `WASTE_EVENT`.

### 9.2 Inventory Optimizer (`track_b/agents/optimizer.py`)

**Subscribe groups:** `["inventory", "procurement"]`.

**Triggers:**
- `reorder_check` interval every `FORECAST_INTERVAL_SIM_S` (1800 sim-s).
- `on_signal`: `LOW_STOCK`/`STOCKOUT_RISK` → `_maybe_toggle` + `_maybe_reorder`; `EXPIRY_RISK` → `_propose_promo`.

**Reorder** (`_maybe_reorder`): gates on `on_hand ≤ reorder_point` AND no in-flight PO for this ingredient. `qty = ceil(needed / pack_size) × pack_size`. Chooses supplier via `_choose_supplier` (exact §18.8 score formula). Calls `procurement.create_po(...)`.

**Supplier scoring** (`_choose_supplier`): `score = avail_weight − price_norm − lead_norm` where `avail_weight = {in_stock: 1.0, limited: 0.5}`, norms = value/max over usable suppliers. Returns `None` if all suppliers are `out`.

**Menu toggle** (`_maybe_toggle`): requires ≥2 active dishes share the ingredient AND `projected_runout − now < min_supplier_lead_days × 86400`. Disables the dish with lowest `_margin_x_velocity` (`margin = price − Σ(recipe_line.qty × supplier_catalog.current_price)`; `velocity = count(sold order_lines)`). Sets `menu_items.active=0`, writes `menu_toggles`, emits `MENU_TOGGLE(disable)`. Re-enable (`_reenable`, called by periodic `reorder_check`) when `on_hand > reorder_point`.

**Expiry → promo** (`_propose_promo`): builds `Promotion(status=proposed, type=combo|discount, discount_pct=20%)` covering active items using the expiring ingredient. Calls `approvals.create(type=promo)`. Emits `PROMO_PROPOSAL`. `activate_promo(id)` sets `status=active` on approval.

**Signals emitted:** `MENU_TOGGLE`, `PROMO_PROPOSAL`.

### 9.3 Market Spectator (`track_b/agents/market_spectator.py`)

**Subscribe groups:** `["procurement", "inventory"]`.

**Triggers:**
- `review_prices` interval every `WEATHER_FETCH_SIM_S` (10800 sim-s).
- `on_signal`: `CALL_OUTCOME` → process negotiation result; `WASTE_EVENT(spoilage)` → spoilage reaction.

**Price monitoring** (`review_prices`): for each `SupplierCatalog` row, if `current_price > median(supplier_price_history) × 1.15` and not already negotiating → `_start_negotiation`.

**Negotiation call** (`_start_negotiation`): `calls.request(agent="market_spectator", counterparty_type="supplier", ...)`. Core runs the full §8 lifecycle. Presenter-triggered via `negotiate(supplier_id, ingredient_id)` → `POST /api/market/negotiate`.

**Call outcome** (`_on_call_outcome`): on `CALL_OUTCOME` with `agreed=True`:
1. Updates `supplier_catalog.current_price + updated_at`.
2. Appends `SupplierPriceHistory` row.
3. Writes `Negotiation(savings=old_price − new_price)`.
4. Emits `SUPPLIER_PRICE_UPDATE(via="call")`.
5. Broadcasts `supplier_price_updated`.

**Spoilage reaction** (`_on_spoilage`): counts `WASTE_EVENT(spoilage)` events per ingredient. At threshold (2 events): reduces `par_level` by 10% and logs `spoilage_pattern`.

**Signals emitted:** `SUPPLIER_PRICE_UPDATE`.

### 9.4 Procurement Service (`track_b/procurement/procurement.py`)

Not a bus subscriber — a service called directly by the Optimizer and Approval handlers.

**`create_po(supplier_id, lines, created_by)`**:
- Creates `PurchaseOrder(status=proposed) + PurchaseOrderLine` rows.
- If `total > APPROVAL_PO_THRESHOLD` (200): calls `approvals.create(type=purchase_order)`; PO stays `proposed` until approved.
- Else: calls `_place(po)` immediately.

**`place(po_id)` / `_place(po)`**: sets `expected_delivery = now + lead_days × 86400`, status = `placed`; registers a **deadline trigger** at `expected_delivery` → `_deliver`. Emits `REORDER_PLACED`.

**`_deliver(po_id)`**: status = `delivered`; calls `ledger.receive(po_id)` (the only inventory writer). Logs `po_delivered` to `event_log`.

**Signals emitted:** `REORDER_PLACED`.

### 9.5 Approval Handlers (`track_b/approval/handlers.py`)

Subscribes directly: `bus.subscribe(APPROVAL_RESOLVED, self.on_resolved)`.

On `decision="approved"` and `type in {"purchase_order", "promo"}`:
- `purchase_order` → `procurement.place(ref_id)`
- `promo` → `optimizer.activate_promo(ref_id)`

Rejected → no-op. Other types (e.g. `outbound_call`) → ignored.

### 9.6 MockForecaster (`track_b/mocks/mock_forecaster.py`)

Active only in `DEMO_MODE=track_b`. **Drives the entire track.** First tick fires immediately (`due_at=now`).

**`_emit_demand_forecasts`** (every `FORECAST_INTERVAL_SIM_S`): for each active menu item in the current daypart → `DEMAND_FORECAST(qty=baseline, multipliers={}, confidence=0.8)`. Baselines from seeded order history (§18.1 fallback chain on negative-sim_time historical rows).

**`_emit_due_batch_decisions`**: for each `BatchDefinition`, computes `decide_by = serve_start − decide_by_offset_min × 60` (or fallback to `prep_lead + BATCH_BUFFER`). Fires once per `(batch_def, daypart, sim-day)` (guarded by `_fired_batches` set). Emits `BATCH_DECISION(decision="cook", qty=round_to_step(baseline))`.

### 9.7 Track B React Panels (`frontend/src/track_b/`)

| Panel | What it shows | Key WS events |
|-------|--------------|---------------|
| **InventoryDashboard** | Per-ingredient `on_hand` vs `par/reorder_point/safety_stock`; **drift** (last_counted_qty − on_hand); live depletion as orders flow; disabled menu items list | `inventory_updated`, `menu_toggled`, `order_created` |
| **ExpiryView** | Active lots with expiry countdown; at-risk highlights; active/proposed promotions | `signal_emitted(EXPIRY_RISK)`, `signal_emitted(PROMO_PROPOSAL)`, `approval_resolved` |
| **SupplierEditor** | Editable supplier catalog (price via `PATCH /api/supplier-catalog/{id}`, availability); negotiation history; "Negotiate" button per row → `POST /api/market/negotiate` (disabled during active call) | `signal_emitted(SUPPLIER_PRICE_UPDATE)`, `call_ended` |
| **ActivityLog** | `event_log` stream (reorders, toggles, promos, waste, negotiations); cap 200 | `event_logged` |

### 9.8 Track B Tests (`track_b/tests/`)

| File | Coverage |
|------|---------|
| `test_ledger.py` | FIFO across 2 lots; ledger==on_hand invariant; BATCH_DECISION depletion; LOW_STOCK → STOCKOUT_RISK; receipt; expiry → EXPIRY_RISK + WASTE_EVENT |
| `test_optimizer.py` | Reorder qty + supplier score; pack-size rounding; skip above reorder_point; all suppliers out → log; toggle lowest margin×velocity; skip single-dish; EXPIRY_RISK → promo + approval + PROMO_PROPOSAL; activate_promo |
| `test_market.py` | Negotiate when above median; skip in-line; agreed → price update + savings + SUPPLIER_PRICE_UPDATE; no-deal leaves price; 2× spoilage → par_level −10% |
| `test_procurement.py` | Auto-place below threshold + REORDER_PLACED; over threshold → approval + wait; delivery deadline fires → ledger.receive |
| `test_approval_handlers.py` | Approved PO → place; approved promo → activate; rejected → no-op; other types ignored |
| `test_contract_b.py` | All B-emitted signals validate against §15; agent groups exact; AST check: no `track_a` imports in `track_b/` |
| Frontend `__tests__/` | Each panel renders from sample REST payload + verifies WS-driven update + relative paths guard |

### 9.9 Core Tests (`tests/`)

| File | Coverage |
|------|---------|
| `test_bus.py` | Dedup single-row refresh; sweep expiry; subscriber fire/isolation; dedup-refresh does NOT re-fire subscribers; identical-emit no-op; payload validation |
| `test_calls.py` | Full call flow (request → approve → freeze → turn → end → CALL_OUTCOME); queuing; reject; auto_resolve; expire_pending TTL |
| `test_llm.py` | Canned without keys; cache single HTTP request; generation never cached; JSON round-trip; fenced response; malformed → re-ask → canned |
| `test_orchestrator.py` | Closed-hours auto-jump 82795 → next-day 08:00 |
| `test_pos_formatter.py` | Orders/lines/velocity; voided → cancelled_order waste; infinite interval when closed; daypart weights; weather channel-shift; anomaly bounds; velocity_mult |
| `test_scenarios.py` | Daypart helpers; velocity_mult writes window not velocity; surges don't compound |
| `test_seeding.py` | Load preset; list presets; validator flags + repairs missing supplier; generate offline |
| `test_voice.py` | set_leave → 7 attendance rows; sick status; record_receipt → lot + ledger; unrecognized → store-only |
| `test_weather.py` | map_weather_code; fetch + emit; override row; fallback offline; error reuses last row |

---

## 10. What a Runtime Looks Like

### 10.1 Boot Sequence

1. `docker compose up` (or `uvicorn core.api:app`).
2. FastAPI lifespan starts → `create_all()` creates DB tables.
3. Core services constructed: `SimClock`, `SignalBus`, `Orchestrator`, `DataFormatter`, `WeatherProvider`, `LLMProvider`, `VoiceProcessor`, `CallsSubsystem`, `ApprovalsHub`, `ScenarioEngine`.
4. `_register_tracks(demo_mode)` imports and registers `track_a.agents` and/or `track_b.agents`. This wires the order-line callback and all interval/deadline triggers.
5. WS sinks wired (formatter, weather, approvals, calls, BaseAgent all receive a `ws_broadcast` fn).
6. `orchestrator.run_loop(broadcast)` starts as an async background task.
7. Frontend opens at `localhost:5173`; Vite proxies `/api/` and `/ws` to the backend. React connects WebSocket; global store initialized.

### 10.2 Per-Tick Flow (every 250ms real while RUNNING)

```
Orchestrator.tick()
  ↓
  [1] advance sim_time by Δsim = 60 × speed × 0.25
      → if candidate ≥ 23:00: jump to next-day 08:00
      → write sim_state.sim_time + day_number + day_of_week
      → bus.sim_time = now
  ↓
  [2] fire due INTERVAL triggers (catch up across jumps):
      • POS simulator → generate orders → formatter.on_order → bus.notify_order_line
        → Ledger.handle_order_line → FIFO depletion → inventory_updated WS
        → threshold check → LOW_STOCK / STOCKOUT_RISK → Optimizer.on_signal
      • Forecaster interval → run_forecast + decide_batches
        → DEMAND_FORECAST + BATCH_DECISION signals → Ledger depletion (batches)
      • Staff interval → reorder_check → STAFF_COVERAGE
      • Weather interval → Open-Meteo fetch → WEATHER_UPDATE → Forecaster
      • MockForecaster (track_b only) → DEMAND_FORECAST + BATCH_DECISION
      • MockInventory (track_a only) → MENU_TOGGLE + STOCKOUT_RISK
      • Market Spectator review_prices → if price > median×1.15 → negotiation call
      • Optimizer reorder_check → re-enable toggles when restocked
      • Ledger scan_expiry → EXPIRY_RISK → Optimizer → PROMO_PROPOSAL
  ↓
  [3] fire due DEADLINE triggers:
      • Procurement delivery → ledger.receive → lot + receipt ledger → inventory_updated
  ↓
  [4] bus.sweep(now) → expire TTL-elapsed live signals
  ↓
  [5] fire due SCENARIO events (fired=0, at_sim_time ≤ now)
  ↓
  [6] drain bus.pending_broadcasts → WS events: sim_tick + signal_emitted × N
      → frontend updates dashboards
```

### 10.3 A Signal's Journey (example: LOW_STOCK → Menu Toggle)

```
1. Order arrives for "Pasta Arrabiata" (order_line created, status=sold).
2. Formatter.on_order → bus.notify_order_line(line).
3. Ledger.handle_order_line → _deplete_for_recipe(pasta, qty=1):
     recipe_line: tomato_sauce qty=0.15kg
     _deplete_fifo(tomato_sauce, 0.15kg):
       Lot-2 (400g, expiry day 5): take 0.15kg → balance_after=2.30kg
       Append InventoryLedger(delta=-0.15, balance_after=2.30)
       on_hand_cached = 2.30
       → broadcast inventory_updated(ingredient=tomato_sauce, on_hand=2.30)
     _check_thresholds(tomato_sauce, 2.30):
       safety_stock = 2.50kg → 2.30 ≤ 2.50 → emit LOW_STOCK
         (dedup_key="low_stock:tomato_sauce", groups=[procurement,inventory,human])
4. Bus dedup: if existing LOW_STOCK live → refresh payload in place (no duplicate).
5. Orchestrator.on_signal(LOW_STOCK) → fans to Optimizer (groups [inventory,procurement] ∩ [procurement,inventory] ≠ ∅).
6. Optimizer._on_signal(LOW_STOCK):
   → _maybe_reorder(tomato_sauce):
       on_hand 2.30 ≤ reorder_point 5.00 → build PO
       par_level=10.0, qty=ceil((10-2.3)/1.0)×1.0=8.0kg
       score: GreenFarm(in_stock, 0.80€/kg, 1 day lead) > SpiceHouse(limited, 1.20€/kg, 2 days)
       total = 8×0.80 = 6.40 ≤ APPROVAL_PO_THRESHOLD 200 → auto-place
       procurement.create_po → PO status=placed
       deadline trigger at now + 1×86400 (tomorrow's delivery)
       emit REORDER_PLACED
   → _maybe_toggle(tomato_sauce, projected_runout):
       projected_runout = now + 2.30/daily_usage × 86400
       if projected_runout < min_lead_days×86400 → check dishes
       items using tomato_sauce: [pasta_arrabiata (margin 8.20×15 vel), bruschetta (margin 5.10×8 vel)]
       lowest margin×velocity = bruschetta (40.80) vs pasta (123.00)
       disable bruschetta: menu_items.active=0, write MenuToggle, emit MENU_TOGGLE(disable, "low tomato stock")
7. MENU_TOGGLE(disable) → Forecaster.on_signal → run_forecast:
     bruschetta: _is_disabled_by_signal=True → skip forecasting + batching.
     Log: "Skipped bruschetta: menu disabled by inventory signal".
8. Frontend receives:
   - inventory_updated → InventoryDashboard updates tomato row (red highlight).
   - signal_emitted(LOW_STOCK) → Approval Inbox + SignalFeed.
   - signal_emitted(REORDER_PLACED) → ActivityLog.
   - signal_emitted(MENU_TOGGLE) → InventoryDashboard disabled list; ForecastDashboard item disappears.
   - forecast_updated → ForecastDashboard removes bruschetta forecasts.
```

### 10.4 A Call's Journey (Supplier Negotiation)

```
1. Market Spectator.review_prices: current_price=1.20€ > median(1.00€)×1.15=1.15 → negotiate.
2. calls.request(agent=market_spectator, counterparty=supplier, purpose="negotiate tomato price"):
   → Call row (status=requested)
   → ApprovalsHub.create(type=outbound_call) → ApprovalRequest(pending)
   → emit CALL_REQUEST → WS approval_created → Approval Inbox card appears.
3. Presenter clicks "Approve" → POST /api/approvals/{id}/approve:
   → ApprovalRequest status=approved; emit APPROVAL_RESOLVED(outbound_call)
   → calls._on_approval_resolved:
       no active call → _start_call:
           clock.freeze_for_call() → sim status=CALL_FROZEN (time stops)
           Call.status=active; emit CALL_STARTED
           WS: call_started → Frontend: Voice Console opens ("You are playing: GreenFarm Supplies")
4. Turn loop (turn-based, real time):
   Agent turn: LLM generates: "Hello, this is Bella's Kitchen. I see we're at 1.20€/kg for
               tomatoes — we've been a customer for 3 years. Could you do 0.95€ for a 20kg order?"
   TTS speaks the line. Frontend shows transcript.
   Presenter speaks: "Sure, for a regular order like that we can do 1.00€/kg."
   STT captures → POST /api/calls/{id}/turn {role: counterparty, text: "Sure, for a regular..."}
   Agent turn 2: "Excellent, 1.00€/kg works for us — confirming 20kg at 1.00€. Thank you!"
5. Presenter clicks "End Call" → POST /api/calls/{id}/end:
   → _finalize: LLM parses transcript → outcome={agreed:true, agreed_price:1.00, ingredient_id:3}
   → calls.outcome = outcome; emit CALL_OUTCOME(market_spectator, supplier)
   → clock.unfreeze_from_call() → restore prior status+speed
   WS: call_ended → Frontend: Voice Console closes.
6. Market Spectator._on_call_outcome(CALL_OUTCOME):
   agreed=true → update supplier_catalog.current_price=1.00 (was 1.20)
   → append SupplierPriceHistory(price=1.00)
   → write Negotiation(savings=0.20/kg)
   → emit SUPPLIER_PRICE_UPDATE(via="call", old=1.20, new=1.00)
   WS: supplier_price_updated → SupplierEditor refreshes price display.
```

### 10.5 The "Friday Rush" Scenario (End-to-End)

The flagship `friday_rush` scenario (in `scenarios.py`) exercises every agent and cross-track cascade:

| Sim-time | Event | Track A reaction | Track B reaction |
|----------|-------|-----------------|-----------------|
| **11:30** | Velocity ×1.6 for 45min | Forecaster fires on POS-velocity anomaly; `recent_velocity` multiplier climbs to ×1.4; batch quantities surge | Ledger depletion accelerates; on_hand drops faster; threshold signals arrive earlier |
| **12:15** | Grill cook calls in sick | Staff.recompute → `STAFF_COVERAGE(covered=false, station=grill)` → Forecaster caps grill items to ×0.5 of baseline; skips all grill batches | Optimizer may re-enable previously disabled grill items if stock returns |
| **13:00** | Tomato supplier `availability=out` | MENU_TOGGLE(disable) for pasta → Forecaster stops forecasting/batching pasta | Optimizer: no available supplier → reorder fails; LOW_STOCK → STOCKOUT_RISK; MENU_TOGGLE emitted |
| **15:00** | Weather set to rain | WEATHER_UPDATE → Forecaster: comfort food ×1.1; delivery demand up; ForecastDashboard shows channel shift | POS sim applies delivery weight increase → more orders → faster depletion |
| **18:00** | Dinner velocity ×1.4 | Forecaster dinner forecasts surge; batch sizes increase | Higher depletion; mozzarella lot approaches expiry faster |
| **21:30** | Inject EXPIRY_RISK (mozzarella) | No direct Track A reaction (Track A only reacts to its own signals) | Optimizer: EXPIRY_RISK → PROMO_PROPOSAL(combo, pizza+bruschetta, 20% off) → Approval Inbox card; approving activates the promo |

**Key cross-track moment:** 13:00 → tomato `availability=out` → Track B Optimizer emits `MENU_TOGGLE(disable, pasta, "stockout risk")` → Track A Forecaster.on_signal(MENU_TOGGLE) → Forecaster stops forecasting pasta, skips its batches, logs "Skipped: menu disabled by inventory signal". When tomato restocks (if a delivery arrives from a secondary supplier), Track B emits `MENU_TOGGLE(enable)` → Track A resumes pasta.

---

## 11. Frontend

### 11.1 Shell (`frontend/src/shell/`)

- **`ws.ts`**: single WebSocket client with auto-reconnect; dispatches typed events to the global Zustand store.
- **`store.ts`**: global Zustand store holding `simState`, latest signals, approvals, orders, weather, call state. Updated by WS events.
- **`App.tsx`**: tabbed layout — Track A tabs (Forecast, Competitors, Reviews, Staff, Signal Feed) and Track B tabs (Inventory, Expiry, Suppliers, Activity Log), with Approval Inbox always visible in a sidebar.
- **`ControlBar.tsx`**: Demo Control Bar — play/pause/stop/restart/step/speed buttons; POS velocity & dish-mix sliders; weather override dropdowns; scenario picker; seed/generate buttons; voice console (mic icon → speech-to-text → `POST /api/voice/transcript`).
- **`ApprovalInbox.tsx`**: lists pending `approval_requests`; "Approve" / "Reject" buttons → `POST /api/approvals/{id}/approve|reject`; card shows type, title, summary, urgency. During a call, the voice console switches to **ROLEPLAY mode**: banner "You are playing: {Supplier X}" + live transcript display + mic/text input.
- **`SettingsDrawer.tsx`**: (track_b branch) additional sim settings panel.

### 11.2 Vite Proxy & Relative Paths

All API calls use **relative paths** (`/api/...`, `/ws`) — never hardcoded host/port. Vite proxies to `BACKEND_ORIGIN` (env var, default `http://localhost:8000`):
```ts
proxy: {
  "/api": { target: process.env.BACKEND_ORIGIN ?? "http://localhost:8000", changeOrigin: true },
  "/ws":  { target: process.env.BACKEND_ORIGIN ?? "http://localhost:8000", ws: true }
}
```
In Docker, `BACKEND_ORIGIN=http://backend:8000`; in local dev it falls back to localhost. The frontend tests include a guard asserting no hardcoded host/port strings.

### 11.3 Voice Console

Normal mode: mic button activates `SpeechRecognition`; recognized text → `POST /api/voice/transcript` → backend runs the voice pipeline → `USER_FACT` returned + display confirmation.

ROLEPLAY mode (during a call): banner changes to "You are playing: {party}"; mic input auto-sends as `POST /api/calls/{id}/turn {role: "counterparty"}`; agent turns stream back via `call_turn` WS event and are TTS-spoken via `speechSynthesis`.

---

## 12. How to Run It

### 12.1 Docker (recommended for demos)

```bash
cp .env.example .env      # fill GEMINI_API_KEY (+ optional GROQ_API_KEY, OPENROUTER_API_KEY)
docker compose up --build  # or: make up
# open http://localhost:5173
```

The backend runs at `:8000`; the frontend at `:5173`. SQLite lives in a Docker volume (`dbdata`).

```bash
docker compose down          # stop; DB persists
docker compose down -v       # stop + wipe DB
make reset                   # = down -v then up (full re-seed)
```

### 12.2 Local Dev (no Docker)

```bash
pip install -r requirements.txt
uvicorn core.api:app --reload   # backend at :8000

cd frontend && npm install
npm run dev                     # frontend at :5173 (proxies to :8000)
```

### 12.3 DEMO_MODE

| Command | Effect |
|---------|--------|
| `DEMO_MODE=track_a make up` | Track A real + MockInventory; only A's panels have live data |
| `DEMO_MODE=track_b make up` | Track B real + MockForecaster; only B's panels have live data |
| `DEMO_MODE=combined make up` | Both tracks real; both panel sets active |

Default is `combined`.

### 12.4 Seeding

```bash
POST /api/seed/preset/bellas_kitchen    # load Bella's Kitchen (Italian)
POST /api/seed/preset/burger_joint      # load Burger Joint
POST /api/seed/generate {cuisine, size_params}  # LLM-generate a new restaurant
```

Then `POST /api/sim/play` to start the simulation.

### 12.5 Tests

```bash
make test          # pytest (backend) + vitest (frontend)
pytest tests/      # core tests only
pytest track_a/tests/   # Track A tests only (run from track_a branch)
pytest track_b/tests/   # Track B tests only
cd frontend && npx vitest run  # frontend component tests
```

---

## 13. Spec-vs-Implementation Gap Analysis

This section documents every **deviation from the spec** (`docs/00_ARCHITECTURE.md` + track docs) and **stubbed/incomplete paths** found in the committed code. Items are organized by severity.

### 13.1 Status Summary

| Area | Status | Notes |
|------|--------|-------|
| Core: clock, bus, DB, models | ✅ Complete | All 38 tables; all §14/§22 rules implemented |
| Core: POS sim, weather, seeding, LLM, voice, calls | ✅ Complete | All §10–§13 features implemented; canned fallbacks present |
| Core: REST/WS API | ✅ Complete | All §20/§21 routes present |
| Core: orchestrator, formatter, approvals, scenarios | ✅ Complete | "Friday Rush" seeded; all §16/§17/§18.8/§18.9 logic |
| Track B: Ledger (depletion, invariant, signals) | ✅ Complete | Exact §18.4 implementation |
| Track B: Optimizer (reorder, toggle, promo) | ✅ Complete | §18.8 implemented; minor LOW_STOCK threshold note |
| Track B: Market Spectator (negotiation calls) | ✅ Complete | Full §8 lifecycle |
| Track B: Procurement, Approval handlers | ✅ Complete | Full PO lifecycle |
| Track B: MockForecaster | ✅ Complete | Drives the full track |
| Track A: Forecaster (baseline, multipliers, batches) | ✅ Mostly complete | 6 multipliers implemented; some values differ from spec |
| Track A: Staff, Review | ✅ Mostly complete | Staff deterministic; Review uses heuristic not LLM |
| Track A: Competitor Intelligence | ⚠️ Partially complete | call flow works; monitoring has gaps |
| Track A: Forecaster suggestions | ❌ Stubbed | No LLM call; returns "no_change" |
| `call_mode="slow"` (0.1×) | ❌ Not implemented | Always freezes |
| `signal_driven`/`threshold` auto-dispatch | ⚠️ Registry-only | No tick-loop auto-dispatch; wake-path is via subscribers/REST |
| Phase 2 (combined) | ⚠️ Wired but untested | Mocking off wiring exists; cross-track integration run not done |
| `docs/03_BUILD_PLAN_A.md` | ❌ Missing | Referenced in §0 but never created on any branch |

### 13.2 Core Gaps

**1. `call_mode="slow"` not implemented** (`clock.py:226`):
`freeze_for_call()` always sets `CALL_FROZEN` regardless of `sim_state.call_mode`. The spec (§6.3) allows `call_mode="slow"` to clamp speed to 0.1× instead of freezing. The `call_mode` field is stored in the DB and stamped on `Call.clock_action` but has no behavioral effect.
*Effort to fix:* small — check `state.call_mode` in `freeze_for_call` and set `state.speed = 0.1` instead of `CALL_FROZEN`.

**2. `signal_driven`/`threshold`/`manual` triggers not auto-dispatched** (`orchestrator.py:43,276,290`):
These three trigger kinds are accepted by `register()` and stored in `self.triggers`, but the tick loop's `_fire_interval_triggers` and `_fire_deadline_triggers` only act on their own kinds. `signal_driven` triggers are in practice wired via `bus.subscribe()` directly (e.g. calls, approvals), and `manual` via REST endpoints. This is a reasonable design choice but differs from a strict reading of §17 ("five trigger kinds, registered by core and by agents").

**3. `db.reset_db` vs `api._wipe_for_seed`** (`db.py:reset_db`, `api.py:_wipe_for_seed`):
Two separate implementations for clearing transactional data. `reset_db` (per §19.4) drops/recreates table groups. The API's restart/reseed path uses its own `_wipe_for_seed` function. Functionally equivalent but creates two code paths to maintain.

**4. Cooldown semantics** (`bus.py:157–173`):
The spec (§14.5) describes cooldown as a strict time-gate. The implementation folds it into the identical-payload check: a changed payload refreshes the signal even within the cooldown window. Only an identical payload within cooldown is suppressed. This is a looser interpretation — a rapidly-fluctuating value could update the live signal faster than intended.

### 13.3 Track A Gaps

**5. Forecaster suggestions fully stubbed** (`track_a/agents/forecaster.py:generate_suggestions`):
```python
def generate_suggestions(self) -> Dict[str, Any]:
    result = {"suggestions": [], "summary": "no_change"}
    self.log_event("forecast", "Batch suggestion scan: no change", result)
    return result
```
No sell-through/waste/sellout stats are gathered. No LLM call is made. The forecaster receives no `llm` handle in its constructor — the `bootstrap_track_a` function does not pass one. This means the §18.7 periodic LLM suggestions feature **does not exist** in the current code.
*Effort to fix:* medium — gather recent `batches` (sold_qty/wasted_qty), pass to `llm.complete`, surface results as suggestion cards on `ForecastDashboard`.

**6. Review analysis uses keyword heuristic, never the LLM** (`track_a/agents/review.py:_analyze`):
Despite receiving an `llm` handle in the constructor, `_analyze` uses hardcoded positive/negative word sets to determine sentiment and severity. No `llm.complete` call is made.
*Effort to fix:* small — call `llm.complete(use_site="review")` with the review text; fall back to the existing keyword logic (which becomes the canned path).

**7. Competitor multiplier values differ from spec** (`track_a/agents/forecaster.py:_competitor_multiplier`):
- Spec §18.2: competitor-closed-nearby → ×1.10; aggressive combo on similar dish → ×0.92.
- Implemented: name-match in `COMPETITOR_INTEL.popular_dishes` → ×1.05; `COMPETITOR_UPDATE.offers_changed` + category in summary → ×0.97.
- The `is_open=False` check (closed-nearby uplift) is absent.

**8. Review multiplier values differ from spec** (`_review_multiplier`):
- Spec §18.2: strong positive → ×1.08; strong negative → ×0.90.
- Implemented: positive summary → ×1.05; high severity → ×0.85; medium → ×0.92; else → ×0.98.

**9. Weather multiplier incomplete** (`_weather_multiplier`):
- Spec §18.5: full matrix including `temp_c ≥ 30` (hot), `temp_c ≤ 5` (cold), ice cream, soups, channel shift.
- Implemented: partial — only 3 conditions checked (rain/storm/snow "comfort" ×1.1; rain "salad"/"cold" ×0.9; clear "salad"/"cold" ×1.05). `temp_c` is never read. Channel shift is handled in the POS sim but not in the Forecaster multiplier.

**10. Missing triggers** (`track_a/agents/forecaster.py:register`):
- Spec §A4.1 / §17: per-`batch_definition.decide_by` deadline trigger (so each batch is decided at exactly the right moment).
- Spec §17: POS-velocity-anomaly threshold trigger (fire Forecaster when `|actual_velocity − forecast| > VELOCITY_ANOMALY_PCT`).
- Neither is registered. Batches are re-decided as a side effect of `run_forecast` (which fires on the interval trigger), not at the individual `decide_by` moment. `VELOCITY_ANOMALY_PCT` is defined in `config.py` but never used anywhere.

**11. Method name mismatch** (`track_a/agents/competitor.py`):
`calls.request(...)` is called; the spec and core's `calls.py` expose `calls.request(...)` (the method is actually named `request` in `calls.py`, so this works — the spec's "calls.request_call" is a doc-only alias). No functional bug.

**12. Competitor monitoring: `offers_changed` hard-coded False** (`competitor.py:passive_monitor`):
The agent emits `COMPETITOR_UPDATE` for every competitor every tick but always sets `offers_changed=False`. No change detection vs prior state is implemented.

**13. Hard-coded magic numbers** (`competitor.py:10800`, `review.py:900`, `staff.py:1800`):
Interval values are inline literals rather than named constants from `config.py`. Cosmetic/maintenance issue, no functional gap.

**14. Baseline third fallback** (`forecaster.py:baseline_qty`):
Spec §18.1: third fallback = "item mean" (mean across all dayparts and days). Implemented: `dish_mix_weight × base_orders_per_day × daypart_weight`. Both are reasonable approximations but differ from the spec's literal description.

**15. MockInventory: deterministic first item** (`mock_inventory.py`):
The spec says "pick a random low-priority active item." The implementation always picks the first active item. Also, `STOCKOUT_RISK` and `SUPPLIER_PRICE_UPDATE` fire on the same tick as the disable (not "occasionally" as specified).

**16. Frontend: coarse snapshot refresh** (`useTrackAData.ts`):
All five panels share one `GET /api/track-a/snapshot` re-fetch hook, triggered on a fixed list of WS events. This list omits `order_created` and `event_logged`. No suggestion cards are rendered on `ForecastDashboard` (consistent with §13.3 #5 above — suggestions are stubbed).

### 13.4 Track B Notes (minor — not bugs, but worth knowing)

**17. `LOW_STOCK` fires at `safety_stock`, not `reorder_point`** (`ledger.py:_check_thresholds`):
The Ledger emits `LOW_STOCK` when `on_hand ≤ safety_stock`. The Optimizer's reorder decision correctly uses `reorder_point`. The spec (§18.8) says "stock crosses `reorder_point`" as the reorder trigger — but the Optimizer's periodic `reorder_check` always re-checks reorder_point independently, so this does not cause missed reorders. `LOW_STOCK` being at safety_stock is slightly earlier than the spec implies but causes no incorrect behavior.

**18. `SUPPLIER_PRICE_UPDATE` always `via="call"`** (`market_spectator.py:_on_call_outcome`):
The `via` field in §15 accepts `"market"` or `"call"`. The market-monitoring path (`review_prices`) that detects a price change vs median does not emit `SUPPLIER_PRICE_UPDATE` autonomously — it only triggers a negotiation *request*. The signal is only emitted on a successful call. Direct catalog edits via the `SupplierEditor` REST `PATCH` also don't emit this signal. The `"market"` variant is therefore never emitted.

**19. No `WASTE_EVENT(overproduction)`** (`ledger.py`, `optimizer.py`):
The spec (§16) specifies that overproduction at serve-window end should emit `WASTE_EVENT(overproduction)` → Ledger writes off leftover ingredients → Forecaster corrects its batch multiplier downward. Neither the Ledger nor the Optimizer implements this. Track B only emits `WASTE_EVENT(expiry)`. This means Market Spectator's spoilage reaction only fires on externally-injected `spoilage` events (via scenario `inject_signal` or voice), not on routine expiry waste.

**20. `_toggle_cause` in-process only** (`optimizer.py:53`):
The mapping of `{menu_item_id → ingredient_id}` that caused a disable is held in an instance variable. A process restart loses this mapping, and re-enable relies entirely on the periodic `reorder_check` sweep (which re-evaluates `on_hand > reorder_point` for each item in `_toggle_cause`). If the process restarts between disable and re-enable, the dish stays disabled until the Optimizer is next triggered by a `LOW_STOCK`/`STOCKOUT_RISK` signal for that ingredient (which keeps re-disabling it). The `menu_toggles` table has no `ingredient_id` column that would allow recovery.
*Effort to fix:* small — add `ingredient_id` to `menu_toggles` and rebuild `_toggle_cause` from active disable rows on startup.

### 13.5 Phase 2 Readiness

The `DEMO_MODE=combined` wiring exists and is correct: both tracks' `register()` functions are called, mocks are off. However, Phase 2 has not been run and the following gaps will reduce the richness of cross-track interactions:
- Track A's **weather multiplier** (§13.3 #9) means rain/storm won't shift demand forecasts as specified, reducing the 15:00 rain event's visible effect on forecasts.
- Track A's **suggestions** (§13.3 #5) being stubbed means one of the headline demo moments (LLM batch-sizing recommendations) won't fire.
- Track A's **review LLM** (§13.3 #6) means `REVIEW_INSIGHT` signals will carry keyword-heuristic sentiment rather than genuine LLM analysis.
- All of Track B's core math (depletion, reorder, toggle, promo) is complete and will work correctly against real Track A `DEMAND_FORECAST` signals.

---

## 14. Glossary

| Term | Meaning |
|------|---------|
| **Signal** | A typed, grouped, expiring message stored as a row in the `signals` table and routed to agents via the `SignalBus`. The *only* inter-agent communication channel. |
| **Batch** | A pre-cooked quantity of a sellable dish decided *before* orders arrive, based on a forecast for a specific serve window. |
| **Ledger / Theoretical Inventory** | Stock level derived from POS sales × recipe depletion, maintained as an append-only `inventory_ledger` table. The cash register's receipt book for inventory. `on_hand = Σ(delta_qty)`. |
| **Daypart** | A named time block within the operating day: `breakfast` (08–11), `lunch` (11–15), `afternoon` (15–17), `dinner` (17–22), `late` (22–23). Each has a demand weight that sums to 1.00. |
| **Sim-time** | A float representing seconds since sim-epoch (= 00:00 of day 0). All timestamps in the DB are sim-seconds. Display helpers derive `HH:MM` and day-of-week. |
| **Tick** | One 250ms real-time loop iteration. At 1× speed, each tick advances sim-time by 15 sim-seconds. |
| **Track** | One of the two parallel build verticals. Track A = Demand & Sensing (Forecaster, Competitors, Reviews, Staff). Track B = Inventory & Procurement (Ledger, Optimizer, Market Spectator). |
| **Call mode** | Clock behavior during a voice call. `freeze` (default): sim time stops completely. `slow` (spec only, not implemented): speed clamps to 0.1×. |
| **DEMO_MODE** | Env var controlling which track's signals are mocked: `track_a` (MockInventory on), `track_b` (MockForecaster on), `combined` (all mocks off; both tracks real). |
| **Dedup key** | A string uniquely identifying a *logical event*. On `bus.emit`, if a live signal with the same `dedup_key` exists, the bus refreshes it in place rather than creating a duplicate row. |
| **Cascade depth** | Counter embedded in `correlation_id` (suffix `:N`). The bus drops any emit where N > `MAX_CASCADE_DEPTH` (5), preventing A→B→A→... signal storms. |
| **Par level** | Target stock quantity = `PAR_DAYS × daily_usage`. The quantity the Optimizer reorders up to. |
| **Reorder point** | Stock level at which reordering is triggered = `supplier_lead_days × daily_usage + safety_stock`. |
| **Safety stock** | Buffer stock = `SAFETY_DAYS × daily_usage`. The `LOW_STOCK` signal fires when on_hand drops to this level. |
| **Yield factor** | Conversion efficiency in `inventory_levels` (e.g. 0.9 for 10% prep waste). `used = qty × recipe_qty / yield_factor`. |
| **Velocity** | Items-per-sim-second sales rate for a menu item, computed from a rolling ring buffer over `VELOCITY_WINDOW_SIM_S` (30 sim-min). |
| **Seed graph** | The complete FK-consistent dataset inserted by a preset: ingredients → menu → recipes → batches → staff → suppliers → inventory → historical orders → competitors → reviews. |

---

*End of 05_CODEBASE_ANALYSIS.md. This document was generated by analysis of all three branches (`main`, `track_a`, `track_b`) on 2026-06-18. For the authoritative spec see `docs/00_ARCHITECTURE.md` and track docs.*
