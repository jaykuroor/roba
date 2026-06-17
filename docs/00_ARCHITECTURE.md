# 00 — Overall Architecture & Core Implementation Spec
### Restaurant Multi-Agent System — Demand Forecasting & Inventory Optimization (Proof-of-Concept Demo)

---

## 0. How to read this document (READ FIRST — for the implementing agent)

- **This document is authoritative.** Every framework-level decision has already been made and is written down here. **Do not invent or change framework behavior.** If you think something is undecided, re-read — the binding default is stated. If it is genuinely absent, it is a bug in this doc; flag it, do not guess.
- **Your job (for whoever builds `core`): implement exactly what is in §4–§24.** The two track docs (`01_TRACK_A.md`, `02_TRACK_B.md`) depend on the contracts here being implemented verbatim.
- **What is left to you:** only *how to fetch/parse* external HTTP APIs (weather, web discovery) — see §7. Everything about *how that data is used* is decided here.
- **Document set (5 files):** `00_ARCHITECTURE.md` (this — core + all contracts), `01_TRACK_A.md` (Demand & Sensing track), `02_TRACK_B.md` (Inventory & Procurement track), `03_BUILD_PLAN_A.md` + `04_BUILD_PLAN_B.md` (execution/testing playbooks).
- **Golden rule of independence:** a track may read another track's **signals** (§14–§15) and the shared **`core` tables**, but **never another track's private tables directly.** This is what lets the two tracks be built in parallel.

---

## 1. What we are building

A demo that shows a layered set of software agents automating two restaurant decisions:

1. **Demand forecasting** — rolling, event-driven per-dish forecasts; decides whether to pre-cook "batches."
2. **Inventory optimization** — derives stock from sales, reorders automatically, toggles menu items, manages expiry/waste, routes approvals to a human.

Because real POS/supplier/aggregator feeds are unavailable, **the demo simulates a full restaurant day in compressed time** and lets a presenter perturb the world (sales velocity, weather, staff, supplier prices, competitor data, reviews, ad-hoc voice facts) and **watch the agents react and explain themselves**. Two showcase moments: the presenter **plays a supplier** while the Market Spectator agent negotiates by voice, and **plays a competitor** while the Competitor Intelligence agent does undercover phone research.

**Thesis to prove:** the agents make *correct, explainable* decisions in response to live signals, end-to-end, with a human in the loop only where it matters.

---

## 2. System architecture (layers)

```
Layer 0  Raw inputs ........ POS sim, inventory, menu, recipe, staff, attendance,
                             supplier/market, weather, voice  (mostly seeded; live during sim)
Layer 1  Data formatter .... routes + enriches raw data into typed signals; runs the wastage relay
Layer 2  Signal bus ........ one typed, grouped, expiring, deduped message bus (the nervous system)
Layer 3  Agents ............ Demand Forecaster, Competitor Intelligence, Review, Staff   (Track A)
                             Inventory Ledger, Inventory Optimizer, Market Spectator     (Track B)
Layer 4  Human-in-the-loop . approval queue + notifications + the live demo dashboards
Cross    Sim clock + orchestrator + interactive call subsystem + LLM provider  (core, drives all layers)
```

Agents never call each other directly. **All inter-component communication is signals on the bus** (Layer 2) plus the REST/WS API (Layer 4 ↔ frontend). This single rule is what makes the system testable and the two tracks independent.

---

## 3. Independence model: zones, phases, placeholders

### 3.1 Three zones

| Zone | Owner | Contents |
|---|---|---|
| **`core`** | Phase-0 (built first from this doc) | sim clock + orchestrator, signal bus, **all DB models + SQLite + reset**, data formatter + wastage relay, **POS simulator**, **voice intake**, **interactive call subsystem**, **weather provider**, **approval queue + inbox + approve/reject dispatch**, seeding/generation, LLM provider, FastAPI+WS app shell, React app shell + design tokens, demo control bar, scenario engine |
| **`track_a`** | Programmer A | agents: Demand Forecaster, Competitor Intelligence, Review, Staff; UI: forecast dashboard, competitor panel, review panel, staff panel, live signal feed; `mocks/` |
| **`track_b`** | Programmer B | agents: Inventory Ledger, Inventory Optimizer, Market Spectator; service: procurement (+ **approval handlers** for PO/promo via `APPROVAL_RESOLVED`); UI: inventory dashboard, expiry view, supplier editor, agent-activity log; `mocks/` |

**Why POS sim, voice, weather, and the call subsystem are in `core`, not a track:** they are *shared inputs/infra*. Forecasting needs POS velocity; inventory depletion needs POS lines; both domains receive voice facts and weather; both call-making agents use one call subsystem. Putting these in `core` means **neither track produces a runtime input the other strictly needs** — they only exchange signals, which can be mocked.

### 3.2 Phases

- **Phase 0 — Scaffold `core` (≈1 focused session).** Generate `core` from §4–§24. It is specified concretely enough that this is mechanical. Output: working clock, bus, DB+seed presets, POS sim emitting orders, voice pipeline, weather, call subsystem skeleton, LLM wrapper, API/WS shell, React shell, and **empty agent stubs**. Track B may stub `core` interfaces locally from this doc to avoid waiting, then swap to canonical `core`.
- **Phase 1 — Build the two tracks in parallel** against frozen `core` interfaces, each using `mocks/` for the *other* track's signals. Each runs standalone: `make demo-a` / `make demo-b`.
- **Phase 2 — Merge.** Set `DEMO_MODE=combined`; both real halves interoperate with **zero glue code** (they only ever spoke via the bus + API). Run the flagship scenario end-to-end.

### 3.3 The placeholder/mock contract (mandatory)

Every cross-track signal (marked → in §15) MUST have, in the **consuming** track's `mocks/`: (1) a mock emitter producing that signal on a believable schedule with payloads conforming to §15, and (2) selection via the `DEMO_MODE` env flag.

- **`DEMO_MODE=track_a`** — Track A real; a `MockInventory` in `track_a/mocks/` emits occasional `MENU_TOGGLE` and `SUPPLIER_PRICE_UPDATE` so A visibly reacts.
- **`DEMO_MODE=track_b`** — Track B real; a `MockForecaster` in `track_b/mocks/` emits `DEMAND_FORECAST` (per item, every forecast interval, numbers = seed historical mean × daypart curve, see §18.1) and `BATCH_DECISION`. **This mock is the single most important placeholder** — B's reorder/par/toggle logic is driven by it.
- **`DEMO_MODE=combined`** — all mocks off; both tracks real.

`core` itself always runs fully real (POS sim, voice, weather, calls) in every mode.

---

## 4. Tech stack (pinned — do not substitute)

| Concern | Choice | Notes |
|---|---|---|
| Repo | **Monorepo** | one repo; `track_b` imports `core` directly |
| Backend | **Python 3.14 / FastAPI / Uvicorn** | async; agents + orchestrator |
| ORM / DB | **SQLAlchemy 2.x / SQLite** | one file `demo.db`; trivial reset |
| Realtime | **WebSocket** (FastAPI native) | one hub broadcasts to frontend |
| Frontend | **React 18 + Vite + TypeScript + Tailwind** | dashboards, demo controls, voice |
| Charts | **Recharts** | forecast/inventory/velocity |
| Voice STT/TTS | **Browser Web Speech API** | `SpeechRecognition` + `speechSynthesis`; text fallback always present |
| LLM | **Gemini 3.1 Flash-Lite via google-genai (primary) -> Groq -> OpenRouter -> canned** | free tiers; see §13 |
| Weather | external HTTP (impl picks; suggest **Open-Meteo**, no key) | usage decided in §9/§18.5 |
| Scheduling | **in-process async loop bound to the sim clock** | no external scheduler |
| Python deps | fastapi, uvicorn, sqlalchemy, pydantic, httpx, google-genai, python-dotenv | |
| **Containerization** | **Docker + Docker Compose** | `docker compose up` runs backend+frontend on any machine — see §26 |

Env vars: `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.1-flash-lite`), `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `WEATHER_API_BASE` (optional), `DEMO_MODE`.

---

## 5. Repository layout

```
/Makefile                      # seed | demo-a | demo-b | demo | reset | test | up | down
/.env.example
/requirements.txt
/docker-compose.yml            # backend + frontend services (§26)
/Dockerfile.backend            # python app image (uvicorn core.api:app)
/frontend/Dockerfile           # node/vite image
/.dockerignore
/core/
  models.py                    # ALL SQLAlchemy tables (§19) — single source of truth
  db.py                        # engine, session, create_all, reset_db()
  clock.py                     # SimClock + tick loop (§6)
  orchestrator.py              # trigger registry + dispatch (§17)
  bus.py                       # SignalBus: emit/query/consume/expire/dedup (§14)
  signals.py                   # SignalType enum + payload pydantic models (§15)
  formatter.py                 # data formatter + wastage relay (§16)
  pos_simulator.py             # order generation (§10)
  voice.py                     # STT text -> LLM extract -> writes -> USER_FACT (§11)
  calls.py                     # interactive call subsystem (§8)
  weather.py                   # weather provider + override (§9)
  seeding.py                   # presets + LLM gen + validator + numeric layer (§12)
  llm.py                       # LLMProvider with fallback + cache + canned (§13)
  agent_base.py                # BaseAgent: subscribe(groups), on_signal, emit, log_event
  api.py                       # FastAPI app: all REST routes (§20) + /ws hub (§21)
  events.py                    # event_log helper (the narrative spine)
  scenarios.py                 # scenario engine (§18.8)
  config.py                    # ALL constants (§22)
/data/                         # preset bundles: bellas_kitchen.json, burger_joint.json, ...
/track_a/  agents/ mocks/      # Programmer A — Python only (Demand & Sensing)
/track_b/  agents/ procurement/ mocks/   # Programmer B — Python only (Inventory & Procurement)
/frontend/                     # ONE Vite app (no React inside track_* folders)
  src/shell/                   # core shell: WS client, store, Demo Control Bar, Approval Inbox, tokens
  src/track_a/                 # A's panels: Forecast, Competitors, Reviews, Staff, Signal Feed
  src/track_b/                 # B's panels: Inventory, Expiry, Suppliers, Activity Log
# Ownership cuts across runtimes: A owns /track_a + /frontend/src/track_a; B owns /track_b + /frontend/src/track_b; core + /frontend/src/shell are Phase-0 shared.
```

---

## 6. The simulation engine

### 6.1 Sim clock & tick math

- **Operating window:** `08:00–23:00` = **15 sim-hours = 54 000 sim-seconds** (configurable in `sim_settings`).
- **Default speed (1×): one operating day = 15 real minutes.** → rate = 54 000 / 900 = **60 sim-seconds per real-second** at 1×.
- **Tick cadence:** the loop ticks every **250 ms real**. Per tick at 1× it advances `60 × 0.25 = 15 sim-seconds`. General form: `Δsim_per_tick = 60 × speed × 0.25`.
- **Speeds:** `0.25× / 0.5× / 1× (default) / 2× / 4× / 8×`. Speed is a multiplier on the rate, never on the tick cadence.
- **Sim-time representation:** `sim_time` is a **float = seconds since sim-epoch** (epoch = 00:00 of day 0). Display helpers derive `day_number = floor(sim_time/86400)`, `time_of_day`, `day_of_week = day_number % 7` (0=Mon). All `*_at`, `expires_at`, etc. columns are sim-seconds (REAL). **Never use wall-clock for sim logic.**
- **Closed hours:** at `23:00` the clock auto-jumps to `08:00` next day (`skip_closed_hours=true` default), incrementing `day_number`. Each tick the orchestrator (1) advances `sim_time`, (2) runs due triggers (§17), (3) sweeps expired signals (§14), (4) broadcasts `sim_tick` (§21).

### 6.2 Clock state machine & controls

States: `STOPPED` → `RUNNING` ⇄ `PAUSED`; plus transient `CALL_FROZEN` (§6.3). Controls (REST §20):

| Control | Effect |
|---|---|
| **Play** | `→ RUNNING` (resume from current `sim_time`) |
| **Pause** | `→ PAUSED` (freeze; keep everything) |
| **Stop** | `→ STOPPED`; reset `sim_time` to start-of-current-day; **clear live signals + transient agent state; KEEP** seed data, ledger history, logs (for inspection) |
| **Restart** | full reset: re-run seed, `reset_db()` of transactional tables, `sim_time→0` |
| **Step** | advance to the next scheduled event OR by one daypart, then `PAUSED` |
| **Jump-to-next-event** | fast-forward to the next orchestrator trigger or scenario event |
| **Set speed** | change multiplier live; legal anytime |
| **Scrubber** | read-only daypart timeline of the current day |

POS velocity, dish-mix, channel-mix, weather, and all seed entities are **editable while `RUNNING`** (changes take effect on the next tick).

### 6.3 Dynamic slowdown / call mode

When an agent starts an interactive call (§8), the clock enters **`CALL_FROZEN`**: **sim time stops advancing** so the human↔agent voice conversation happens in real time. This is the robust default (you asked not to make it fragile). On call end it restores the prior state/speed.

- `sim_settings.call_mode ∈ {freeze (default), slow}`. `slow` = clamp speed to `0.1×` during a call instead of freezing (use only if continuity is wanted; freeze is recommended for the demo).
- Calls are **always preceded by a human approval** (§8.2), so the freeze never surprises the presenter.
- Hard rule: only **one active call at a time**. A second `CALL_REQUEST` while one is active is queued (its approval card shows "waiting for current call to end").

---

## 7. External-API principle (decided once, applies to all HTTP APIs)

For any external HTTP API (weather, web discovery for competitors):

- **You (implementer) own:** choosing the concrete endpoint/provider, the HTTP call, auth, and parsing the raw response.
- **This doc owns (already decided):** the **canonical internal struct** the API must be mapped into, and **exactly how the system uses that struct**. You MUST map the API response into the canonical struct and feed our deterministic logic.
- If the API is unavailable, fall back to the **demo override / canned value** (every external input has one). The demo must never hard-fail on a network error.

Canonical structs: weather → §9.1; competitor discovery → §8.4. Their deterministic usage → §18.5 (weather), §18.6 (competitor intel).

---

## 8. The interactive call subsystem (`core/calls.py`)

Two agents make outbound calls: **Market Spectator** (Track B, negotiates supplier prices/terms) and **Competitor Intelligence** (Track A, undercover research). Both use this one subsystem. In the demo, **the presenter plays the other party by voice.**

### 8.1 Why undercover competitor calls are in scope
The Competitor Intelligence agent phones a nearby competitor **posing as an ordinary customer** and asks what their most popular / customer-favourite dish is (and optionally prices) — the kind of mystery-shopping any customer can do, automated. It must **not** state it is an AI doing research (that would bias the answer). It only asks questions a normal caller could ask. **Every such call is gated behind explicit human approval** (§8.2); nothing is placed without the presenter clicking approve.

### 8.2 Call lifecycle (state machine)

```
agent decides to call
  → emit CALL_REQUEST (priority 4); core creates an approval_requests row (type=outbound_call)
  → human Approve / Reject (REST §20); core emits APPROVAL_RESOLVED {type:outbound_call, decision, ref_id=call_id}
      rejected -> call.status=rejected; agent may pick an alternative or skip
      approved -> the call subsystem (subscribed to APPROVAL_RESOLVED, type=outbound_call) starts the call:
               clock -> CALL_FROZEN (or 0.1× if call_mode=slow)
               -> emit CALL_STARTED; UI opens the Call console; voice console -> ROLEPLAY mode
                  (banner "You are playing: {Supplier X | Competitor Y}")
               -> conversation loop (8.3)
               -> on hangup: subsystem parses the outcome (8.5), stores calls.outcome, emits
                  CALL_OUTCOME (-> initiating agent's group); the INITIATING AGENT persists its own
                  domain record (8.5); subsystem then restores clock -> prior state/speed
```

Fallbacks: if the presenter declines to roleplay or STT fails, an **LLM-simulated counterpart** plays the other party so the call still completes (`call.status=auto_resolved`), using a seeded persona (supplier price band from `supplier_catalog`; competitor popular dishes sampled from `competitor_offers`). The demo always progresses.

### 8.3 Conversation loop
Turn-based, real time: agent utterance via TTS (`speechSynthesis`) ↔ human reply via STT (`SpeechRecognition`) → both appended to `calls.transcript` as `{role: agent|counterparty, text, sim_ts}` and streamed over WS (`call_turn`). The **agent turn** is generated by the LLM (§13) with a tight system prompt:
- **Supplier call (Market Spectator):** goal = lower unit price and/or better terms for a target ingredient; opens with current price context from `supplier_catalog`; stays polite, businesslike; closes by confirming the agreed number.
- **Competitor call (Competitor Intelligence):** goal = learn the favourite/most-popular dish (and prices if natural); **persona = a regular customer**; never reveals research intent; keeps it to 2–4 questions.

### 8.4 Competitor discovery (canonical struct)
The agent must "deem" which restaurants are competitors. Canonical struct per candidate: `{name, cuisine, distance_km, rating, is_open, source}`. Implementer may populate via a real web-search/places call **for the discovery moment only**; otherwise from the seeded `competitors` table. Selection rule (deterministic): candidates within `COMPETITOR_RADIUS_KM` (§22) sharing ≥1 cuisine tag, ranked by `rating × proximity_weight`; the top `N` become call targets, each requiring approval.

### 8.5 Outcome extraction (core parses & emits; the initiating agent persists)
After hangup the call subsystem (core) sends the transcript to the LLM with a JSON schema, stores the parsed result in `calls.outcome`, and emits `CALL_OUTCOME {call_id, counterparty_type, outcome}`. **Core writes no track tables** — it only fills `calls.outcome` and emits the signal. The initiating agent consumes `CALL_OUTCOME` and persists its own domain record (preserving the write-ownership of §19.4):
- Supplier → **Track B / Market Spectator**: outcome `{ingredient_id, agreed_price?, agreed_terms?, agreed}` → write `negotiations`; if `agreed`, update `supplier_catalog.current_price` + append `supplier_price_history` + emit `SUPPLIER_PRICE_UPDATE(via="call")`.
- Competitor → **Track A / Competitor Intelligence**: outcome `{popular_dishes, price_points}` → write `competitor_intel` + emit `COMPETITOR_INTEL(method="call")`.
If the parse yields nothing usable, `calls.outcome=null` and no domain write occurs (safe no-op).

---

## 9. Weather provider (`core/weather.py`)

### 9.1 Canonical struct & sources
`Weather = {sim_time, source: api|override, temp_c: float, condition: clear|clouds|rain|storm|snow, precip_mm: float, wind_kph: float}`. Current weather = latest `weather_log` row.
- **Source api:** implementer fetches (suggest Open-Meteo, free, no key) on a schedule (every 3 sim-hours) and maps the response into the canonical struct. Map provider weather-codes → our 5 `condition` buckets (you decide the exact mapping; document it inline).
- **Source override (demo):** the frontend Demo-Data panel can set any field directly (`POST /weather/override`), which writes a `weather_log` row with `source=override`. Overrides win until changed.
- On each new weather row, emit **`WEATHER_UPDATE`** (→ forecasting group). Forecaster usage is fully specified in §18.5.

---

## 10. POS simulator (`core/pos_simulator.py`)

Generates `orders` + `order_lines` during `RUNNING`, the live sales feed both tracks consume.

- **Order arrivals:** Poisson; rate at sim-time `t` = `base_orders_per_day × velocity × daypart_weight(t) / window_seconds`. Inter-arrival ~ Exponential. `base_orders_per_day` default 300; `velocity` is the slider (default 1.0); `daypart_weight` from the daypart curve (§22).
- **Per order:** `n_lines ~ {1:0.5, 2:0.3, 3:0.2}`; each line samples a `menu_item` from **`dish_mix_weights`** (editable; default = item popularity from seed history), restricted to `active` items. `channel` sampled from `channel_mix` (default dine-in .70 / delivery .20 / takeout .10). `unit_price` = dine-in or online price by channel.
- **On each created line:** persist, then **emit nothing directly** — instead append to bus via the formatter as an `ORDER_LINE` signal-equivalent? No: order lines are high-volume, so they are **not** individual signals. Instead the simulator writes rows and broadcasts `order_created` (WS) and notifies the formatter, which (a) updates rolling velocity and (b) triggers depletion in Track B via a single batched mechanism: Track B's Ledger subscribes to `order_created` WS-internal hook OR polls new `order_lines` since last cursor. **Decision:** the formatter calls `bus.notify_order_line(line)`; the Ledger registers a callback. (Order lines stay out of the `signals` table to avoid flooding it; they live in `order_lines` and drive depletion via this in-process callback. All *derived* events — LOW_STOCK, WASTE, etc. — are real signals.)
- **Anomaly injection:** `sim_settings.anomaly_injections` (and scenarios) can multiply velocity or skew `dish_mix_weights` for a window.
- **Cancellations:** with prob `CANCEL_RATE`, a line is marked `voided` shortly after creation → emits a `WASTE_EVENT(cancelled_order)` (§16).

Editable live: `base_orders_per_day`, `velocity`, `dish_mix_weights`, `channel_mix`, `daypart curve`.

---

## 11. Voice intake (`core/voice.py`)

Pipeline: **browser STT → text → LLM extraction (JSON) → validate → DB writes → emit `USER_FACT` (+ specific signals)**. The presenter can enter *any* operational fact by voice.

- **Extraction schema:** `{intent, entity_type, entity_ref, attribute, value, effective_window?:{start_sim,end_sim}, confidence}`. `intent ∈ {add_menu_item, edit_menu_item, set_recipe, add_inventory_count, record_receipt, set_attendance, set_leave, add_event, set_supplier_price, set_competitor, add_review, other}`.
- **Apply step (deterministic per intent):** e.g. `set_leave` → write structured `attendance` rows (one per affected sim-day in the window, `status='leave'|'sick'`) — the **queryable source of truth** for staff availability — plus a display-only `event_log` row for the narrative feed; the Staff agent (Track A) consumes the `USER_FACT` and recomputes `STAFF_COVERAGE` from the `attendance` table; `record_receipt` → create `inventory_lot` + `inventory_ledger(receipt)`; `add_event` (e.g., "parade Monday") → store as a `USER_FACT` with an `effective_window` and a demand multiplier tag the Forecaster reads (§18.4); `add_menu_item` → create `menu_items` row, then LLM drafts a `recipe` (validated, §12) ; `add_inventory_count` → `inventory_levels.last_counted_*` + reconciliation ledger entry.
- **Always** persist a `user_facts` row (raw_text, extracted JSON, resulting_writes) and emit `USER_FACT` (→ all groups). Voice never bypasses validation (§12.3).
- **Worked examples** (must work in the demo):
  - "Ansi is on leave the whole next week." → leave rows Mon–Sun next week; `USER_FACT(set_leave, staff=Ansi)`.
  - "We received 20 kg of tomatoes from GreenFarm at 2 dollars a kilo." → lot(+20kg, price 2.0, supplier=GreenFarm) + receipt ledger.
  - "Add a Margherita pizza for 12 dollars." → menu_item + LLM recipe + validation.
  - "There's a parade on our street this Monday." → USER_FACT(add_event) with Monday window + demand bump.

---

## 12. Seeding & one-click generation (`core/seeding.py`)

Two modes; both produce a **referentially consistent** dataset. **Key decision: the LLM generates only creative/qualitative content; all consistency-critical numbers are computed by code.** This makes one-click reliable on a flaky free LLM.

### 12.1 Mode A — Presets
2–3 curated, validated JSON bundles in `/data/` (e.g. `bellas_kitchen.json` Italian; `burger_joint.json`; `spice_house.json`). Loaded by `POST /seed/preset/{id}`. Each bundle is the full graph below, pre-wired and pre-validated. **Presets are the demo-safe path.**

### 12.2 Mode B — LLM generation (full bundle or per-entity)
`POST /seed/generate {cuisine, size_params}` and per-entity `POST /generate/{menu|recipes|staff|supplier}`.
Pipeline: **LLM generates qualitative JSON** (dish names/categories/stations/prices, recipe ingredient *lists*, supplier names, staff names/roles) → **validator** (§12.3) → **numeric layer (code)** computes everything that must be consistent:
- inventory levels per ingredient: `daily_usage = Σ over recipes(qty × seed_daily_item_sales)`; `safety_stock = SAFETY_DAYS × daily_usage`; `par_level = PAR_DAYS × daily_usage`; `reorder_point = supplier_lead_days × daily_usage + safety_stock`.
- initial `inventory_lots`: quantity ≈ `par_level × 0.8`, one or two lots with staggered `expiry_date` from `ingredient.shelf_life`.
- historical POS (`HISTORY_DAYS`, default 30): orders distributed across items × dayparts × day-of-week using the dish-mix weights and daypart curve (so the Forecaster has a baseline).
- `supplier_price_history`: small random-walk of past prices.

### 12.3 Validator (referential-integrity rules — hard gate)
Reject/repair until all hold: every `recipe_line.ingredient_id` exists; every `menu_item.station` exists in `stations`; every station has ≥1 staff in `staff_stations`; every `ingredient` is sold by ≥1 `supplier_catalog` row; all prices > 0; every `batch_definition.menu_item_id` exists and `is_batchable=true`; every `competitor_offer.competitor_id` exists. On failure: auto-repair trivial cases (add a missing supplier/station/staff with sensible defaults) or regenerate the offending sub-part (≤2 retries) before surfacing an error.

### 12.4 The seed graph (what a complete dataset contains)
ingredients → menu_items → recipes/recipe_lines → batch_definitions → stations → staff/staff_stations/staff_dish_skills → shifts/attendance → suppliers/supplier_catalog → inventory_lots/levels → historical orders/order_lines → competitors/competitor_offers → reviews → supplier_price_history → initial weather.

---

## 13. LLM provider layer (`core/llm.py`)

`LLMProvider.complete(messages, json_schema=None, max_tokens=...) -> str | dict`.

- **Fallback chain:** Gemini 3.1 Flash-Lite via the `google-genai` SDK -> Groq (llama-3.3-70b) -> OpenRouter free model -> **canned response**. Try next on 429/5xx/timeout.
- **Caching:** `cache_key = sha256(model + messages + schema)`; in-process dict (and optional on-disk) so repeated demo runs don't burn quota. TTL = process lifetime.
- **Backoff:** exponential, 3 retries per provider, base 1.5 s; **`sleep(2s)` between successive agent LLM calls** (free-tier RPM protection).
- **Prompt hygiene:** keep system prompts short; **never** concatenate full conversation history needlessly (Groq 429s come from tokens-per-minute on long prompts).
- **JSON mode:** when `json_schema` is given, request structured output; validate with pydantic; one re-ask on parse failure, else canned.
- **Canned fallbacks (demo-safe):** every LLM use-site ships a deterministic stub output so the demo never hard-stops: voice→best-effort regex/keyword parse; review→neutral insight; competitor/supplier turn→scripted persona line; generation→returns a small preset slice; forecaster suggestions→"no change".
- **Where the LLM is used (and ONLY here):** voice extraction (§11), dataset generation (§12), review sentiment/insights (Track A), competitor-combo interpretation + call turns + intel extraction (Track A/§8), supplier call turns + negotiation extraction (Track B/§8), Forecaster periodic batch *suggestions* (§18.7). **Everything else is deterministic.**

---

## 14. The signal bus (`core/bus.py`)

### 14.1 Envelope (every signal)
```json
{
  "signal_id": "uuid",
  "type": "LOW_STOCK",
  "source": "inventory_ledger",
  "groups": ["procurement", "human"],
  "priority": 4,                 // 1 lowest … 5 critical
  "payload": { ... },            // typed per §15
  "created_at": 51230.0,         // sim-seconds
  "expires_at": 65630.0,         // sim-seconds
  "dedup_key": "low_stock:tomato",
  "status": "live",              // live | consumed | expired
  "correlation_id": "uuid"       // chain tracing
}
```

### 14.2 API
- `emit(type, payload, source, groups=None, priority=None, ttl=None, dedup_key=None, correlation_id=None)` — groups/priority/ttl default per type from a **registry** (`signals.py`); writes one `signals` row.
- `live(groups=None, type=None)` → current `status='live'` signals (optionally filtered).
- `consume(signal_id)` → `status='consumed'` (agent acted on it).
- `sweep(now)` → flip `status='expired'` where `expires_at <= now` (called every tick).

### 14.3 Dedup of logical duplicates (hard requirement)
On `emit`, if a **live** signal with the same `dedup_key` exists: **do not create a duplicate**; instead **refresh** its `expires_at` and **replace** its `payload` (latest wins), unless the new payload is materially identical (then no-op). This collapses "tomato low" repeats into one live signal.

### 14.4 Groups (visibility — no LLM compute to decide relevance)
`forecasting, inventory, procurement, kitchen, sensing, human, frontend`. Agents subscribe to groups in `BaseAgent.subscribe([...])` and only receive signals whose `groups` intersect their subscription.

### 14.5 Cascade safety
- **Cooldown:** the same `dedup_key` may not re-emit within `SIGNAL_COOLDOWN_SIM_S` (§22) unless payload materially changes.
- **Max depth:** `correlation_id` carries a depth counter; reject emits beyond `MAX_CASCADE_DEPTH` (§22) and log a warning. Prevents A→B→A storms.

---

## 15. Signal taxonomy (every type + payload + defaults)

`(→ = crosses the track boundary; both tracks must mock these for standalone runs.)`

| Type | Default groups | Prio | Default TTL (sim-s) | Payload |
|---|---|---|---|---|
| `DEMAND_FORECAST` → | forecasting,inventory,human | 2 | until window end | `{menu_item_id, window:{start,end}, daypart, qty, baseline, multipliers:{}, confidence}` |
| `BATCH_DECISION` → | kitchen,inventory,human | 3 | 4h | `{batch_definition_id, menu_item_id, serve_window, decision:"cook"|"skip", qty, by:"agent"|"human"}` |
| `WASTE_EVENT` | inventory,forecasting,procurement,human | 3 | 6h | `{waste_type, ingredient_id?, menu_item_id?, lot_id?, qty, unit, cost, reason}` |
| `LOW_STOCK` → | procurement,inventory,human | 3 | 4h | `{ingredient_id, on_hand, threshold, projected_runout, unit}` |
| `STOCKOUT_RISK` → | procurement,inventory,human,frontend | 4 | 4h | `{ingredient_id, on_hand, projected_runout, affected_items:[]}` |
| `EXPIRY_RISK` | inventory,procurement,human | 3 | until expiry | `{ingredient_id, lot_id, qty, expiry, projected_usage_before_expiry}` |
| `MENU_TOGGLE` → | forecasting,kitchen,human,frontend | 3 | 24h | `{menu_item_id, action:"disable"|"enable", reason}` |
| `REORDER_PLACED` | procurement,human | 2 | 24h | `{po_id, supplier_id, lines:[{ingredient_id,qty}], total, eta}` |
| `SUPPLIER_PRICE_UPDATE` → | inventory,procurement,forecasting | 2 | 24h | `{supplier_id, ingredient_id, old_price, new_price, availability, via:"market"|"call"}` |
| `COMPETITOR_UPDATE` → | forecasting,human | 1 | 12h | `{competitor_id, is_open, offers_changed:bool, summary}` |
| `COMPETITOR_INTEL` → | forecasting,human | 2 | 24h | `{competitor_id, popular_dishes:[], price_points:{}, method:"call"|"aggregator", call_id?}` |
| `REVIEW_INSIGHT` → | forecasting,human | 2 | 12h | `{review_id?, severity, summary, suggested_action, dish_mentions:[]}` |
| `STAFF_COVERAGE` → | forecasting,human | 3 | until shift end | `{station_id, covered:bool, affected_items:[], shortfall}` |
| `PROMO_PROPOSAL` | human | 3 | until expiry | `{promo_id, type:"combo"|"discount", menu_items:[], discount_pct, channel, trigger}` |
| `APPROVAL_REQUEST` | human | 4 | 6h | `{approval_id, type, title, summary, payload, urgency}` |
| `APPROVAL_RESOLVED` → | human,procurement,inventory,kitchen | 3 | 2h | `{approval_id, type, decision:"approved"|"rejected", ref_id, payload}` |
| `WEATHER_UPDATE` | forecasting | 1 | 3h | `{temp_c, condition, precip_mm, wind_kph, source}` |
| `CALL_REQUEST` | human | 4 | 1h | `{call_id, agent, counterparty_type, counterparty_id, purpose}` |
| `CALL_STARTED` | human,frontend | 2 | call len | `{call_id}` |
| `CALL_OUTCOME` → | forecasting,procurement,inventory,human | 2 | 12h | `{call_id, counterparty_type, outcome:{}}` |
| `USER_FACT` | forecasting,inventory,procurement,human | 2 | per fact | `{intent, entity_type, entity_ref, attribute, value, effective_window?, raw_text}` |

`source` ∈ {`pos_sim, formatter, voice, weather, calls, forecaster, competitor_intel, review, staff, ledger, optimizer, market_spectator, procurement, human, scenario`}. Defaults live in a registry in `signals.py`; `emit` looks them up unless overridden.

---

## 16. Data formatter & wastage relay (`core/formatter.py`)

The formatter turns raw inputs into typed signals and **routes derived events**. Mostly deterministic rules.

- **Velocity enrichment:** maintains a rolling per-item sales rate from `order_lines` (last `VELOCITY_WINDOW_SIM_S`), exposed to the Forecaster (§18.2) and broadcast as part of `order_created` WS payload.
- **Wastage relay (decided routing):** every `waste_events` row emits one `WASTE_EVENT` signal that fans out:
  - **overproduction** (batch made > sold by serve-window end): → Ledger (decrement leftover ingredients already consumed; record cost), → Forecaster (signal that the batch forecast was high → it lowers that item's multiplier, §18.4).
  - **spoilage / expiry** (lot expired unused): → Ledger (write-off), → Market Spectator (pattern → order less/fresher), → human dashboard (cost).
  - **cancelled_order** (voided line): → Ledger (return depleted ingredients if pre-prepped, else none), → human dashboard.
  - **prep_error** (manual/voice or scenario): → Ledger (decrement), → human dashboard.
  - All waste rows also feed the dashboard's running **waste-cost** counter.
- **USER_FACT routing:** voice facts are emitted as `USER_FACT` to all groups; each agent filters by `intent`.

---

## 17. The orchestrator (`core/orchestrator.py`)

Per tick: advance clock → run due triggers → sweep signals → broadcast. Five trigger kinds, registered by `core` and by agents:

1. **Scheduled/interval** — e.g. Forecaster every `FORECAST_INTERVAL_SIM_S` (30 sim-min); weather fetch every 3 sim-h.
2. **Lead-time/deadline** — batch `decide_by = serve_start − prep_lead − BATCH_BUFFER`; reorder checks; expiry scans.
3. **Signal-driven** — a high-priority signal lands in an agent's group → wake that agent.
4. **Threshold/anomaly** — POS velocity deviates from live forecast by > `VELOCITY_ANOMALY_PCT`; stock crosses `reorder_point`.
5. **Manual/scenario** — demo buttons; scenario events at their `at_sim_time`.

**Full Demand-Forecaster trigger set (binding):** fixed interval; each batch `decide_by`; `WASTE_EVENT` (overproduction → correct down); `STAFF_COVERAGE`, `COMPETITOR_UPDATE`, `COMPETITOR_INTEL`, `REVIEW_INSIGHT`, `WEATHER_UPDATE`, `USER_FACT` (event) in its group; POS-velocity anomaly; manual/scenario.

Cascade controls per §14.5 apply to all emits.

---

## 18. Core algorithms (the deterministic logic — decided)

These are framework decisions; the track docs reference them. Constants in §22.

### 18.1 Baseline demand
`baseline(item, daypart, dow) = mean of historical order_line qty for that (item, daypart, dow)` over `HISTORY_DAYS`. If sparse, fall back to (item, daypart) mean, then item mean.

### 18.2 Forecast
`forecast(item, window) = baseline(item, daypart, dow) × Π multipliers`, multipliers ∈:
- **event** (from `USER_FACT` add_event with overlapping window): `EVENT_MULT` (default +0.35 → 1.35) scaled by stated magnitude if present.
- **competitor**: competitor closed nearby → ×1.10; competitor running an aggressive combo on a similar dish → ×0.92; from `COMPETITOR_UPDATE`/`COMPETITOR_INTEL`.
- **review**: strong negative trend on a dish → ×0.90; strong positive → ×1.08 (from `REVIEW_INSIGHT`).
- **staff_coverage**: station for this item not covered → cap item to `STAFF_CAP_FACTOR` (0.5) of baseline (you can't cook what you can't staff).
- **weather**: §18.5.
- **recent_velocity** (intraday correction): `actual_so_far / max(expected_so_far, ε)`, clamped to `[0.6, 1.6]`.
Output: `qty` (rounded), `baseline`, the `multipliers` dict (for explainability), `confidence` = inverse of multiplier spread. Emit `DEMAND_FORECAST`; write `forecasts` row; append `event_log`.

### 18.3 Batch decision
For each `batch_definition` at its `decide_by`: `f = forecast(item, serve_window)`. **Cook** if `f ≥ batch_size_min` AND ingredients available (**determined from live `STOCKOUT_RISK`/`MENU_TOGGLE` signals — an agent never reads another track's tables**; in `DEMO_MODE=track_a` the MockInventory supplies these) AND station staffed; choose `qty = clamp(round_to_step(f), min, max)`. Else **skip**. Emit `BATCH_DECISION`; create `batches` row; if cooking, the Ledger depletes ingredients (§18.4 reverse — batch depletion).

### 18.4 Inventory depletion (ledger)
On each **sold** `order_line` and each **made** `batch`, for each `recipe_line`: `used = qty_line × recipe_qty / yield_factor`; deplete FIFO across `inventory_lots` (oldest `expiry` first); append `inventory_ledger(reason=sale_depletion|batch_depletion, delta=−used, ref_id, balance_after)`; update lot `qty_on_hand` and cached `inventory_levels.on_hand`. **on_hand is always the ledger sum** (cache is a convenience). Overproduction at serve-window end → `WASTE_EVENT(overproduction)`.

### 18.5 Weather effect (decided lookup)
Apply a per-item multiplier from item `category`/tags × current `condition`/`temp_c`:
- **rain/storm:** comfort/hot dishes ×1.10; cold drinks/ice cream ×0.80; **channel shift** dine-in ×0.85, delivery ×1.20 (apply to channel mix, not item).
- **snow:** dine-in ×0.60, delivery ×1.10; soups/hot ×1.20; overall items ×0.90.
- **hot (temp_c ≥ 30):** cold drinks/salads/ice cream ×1.30; hot soups ×0.70.
- **cold (temp_c ≤ 5):** soups/hot dishes ×1.20; cold items ×0.80.
- **clear/clouds, mild:** ×1.00.
Item→category tags come from `menu_items.category` + an optional `weather_tags` JSON. The Forecaster multiplies; the POS simulator applies the channel shift. Demo override drives this directly.

### 18.6 Competitor intel usage
`COMPETITOR_INTEL.popular_dishes` → if a competitor's popular dish maps (by name/category) to one of ours, nudge that item's forecast ×1.05 and surface a Forecaster *suggestion* (§18.7) to consider promoting/adding it; price_points feed margin context. Deterministic mapping by category; LLM only did the transcript parse.

### 18.7 Forecaster suggestions (LLM, periodic, low-frequency)
Every `SUGGESTION_INTERVAL_SIM_S` (e.g. once per sim-day) the Forecaster sends recent POS + batch waste/sellout stats to the LLM to propose **add/remove/retime/resize batches** in a fixed JSON schema; results surface as non-blocking suggestions on the dashboard (no auto-apply). Canned fallback = "no change."

### 18.8 Reorder / toggle / expiry / promo (Track B logic, decided here for the contract)
- **Reorder:** when `on_hand ≤ reorder_point`, build PO `qty = par_level − on_hand` rounded to `pack_size`; pick supplier maximizing `score = availability_weight − price_norm − lead_norm`; if `PO_total > APPROVAL_PO_THRESHOLD` → `APPROVAL_REQUEST(purchase_order)` else auto-place; emit `REORDER_PLACED`; on `eta`, write receipt lot + ledger.
- **Menu toggle:** if an ingredient's `projected_runout < resupply_eta` and it's shared with a faster mover, disable the dish with lowest `margin × velocity` using it; emit `MENU_TOGGLE(disable)`; re-enable (`MENU_TOGGLE(enable)`) when `on_hand > reorder_point`.
- **Expiry:** scan lots each `EXPIRY_SCAN_SIM_S`; if `expiry − now ≤ EXPIRY_WINDOW_SIM_S` and `projected_usage_before_expiry < qty` → `EXPIRY_RISK` → propose `PROMO_PROPOSAL` (combo/discount `PROMO_DISCOUNT_PCT`) → `APPROVAL_REQUEST(promo)`. On expiry → `WASTE_EVENT(expiry)`.
- **Reconciliation:** a voice/UI count writes `inventory_levels.last_counted_*` + a `reconciliation` ledger delta = `counted − ledger_on_hand`; the drift is shown on the dashboard.

### 18.9 Scenario engine
A scenario = ordered `scenario_events` `{at_sim_time, event_type, payload}`, `event_type ∈ {inject_signal, change_setting, inject_review, set_competitor, call_in_sick, supplier_change, weather_set, velocity_mult}`. Ship a flagship **"Friday Rush"**: 11:30 velocity×1.6; 12:15 grill cook sick (`STAFF_COVERAGE` grill uncovered); 13:00 tomato delivery delayed (`supplier_catalog` tomato availability=out → reorder fails → `LOW_STOCK` → pasta `MENU_TOGGLE`); 15:00 rain set (channel shift); 18:00 dinner velocity×1.4; 21:30 surplus mozzarella near expiry → `EXPIRY_RISK` → `PROMO_PROPOSAL`. This single run exercises every agent and the full cascade.

---

## 19. Complete data model (DDL-level — implement in `core/models.py`)

**Conventions:** every table has `id INTEGER PRIMARY KEY AUTOINCREMENT` unless noted. All `*_at`/`*_time`/`expiry`/`expires_at` are **REAL sim-seconds**. JSON columns are TEXT holding JSON (SQLAlchemy `JSON`). FKs named `<entity>_id`. Booleans as INTEGER 0/1.

### 19.1 Reference / config
- **ingredients**(name, category, base_unit ∈ g|ml|each, perishable, shelf_life_days, allergen_flags JSON, weather_tags JSON, notes)
- **stations**(name)
- **menu_items**(name, category, station_id→stations, dine_in_price, online_price, prep_time_min, is_batchable, active=1, weather_tags JSON, description)
- **recipes**(menu_item_id→menu_items)
- **recipe_lines**(recipe_id→recipes, ingredient_id→ingredients, qty, unit, optional=0)
- **batch_definitions**(menu_item_id→menu_items, applicable_menus JSON, dayparts JSON, prep_lead_time_min, batch_size_min, batch_size_step, batch_size_max, decide_by_offset_min, prepared_shelf_life_min, station_id→stations, required_skill, default_cadence_min, historical_attach_rate)
- **staff**(name, role, skill_level, hourly_cost, active=1)
- **staff_stations**(staff_id→staff, station_id→stations)  — M:N coverage
- **staff_dish_skills**(staff_id→staff, menu_item_id→menu_items)  — dish-level exceptions
- **suppliers**(name, lead_time_days, reliability_score, min_order_value, contact)
- **supplier_catalog**(supplier_id→suppliers, ingredient_id→ingredients, current_price, unit, pack_size, availability ∈ in_stock|limited|out, updated_at)

### 19.2 State / transactional
- **inventory_lots**(ingredient_id→ingredients, qty_on_hand, unit, purchase_price, purchase_date, received_date, expiry_date, supplier_id→suppliers, storage_location, status ∈ active|depleted|expired)
- **inventory_ledger**(ingredient_id→ingredients, lot_id→inventory_lots, delta_qty, reason ∈ receipt|sale_depletion|batch_depletion|waste|reconciliation, ref_id, sim_time, balance_after)  — **append-only; source of truth**
- **inventory_levels**(ingredient_id→ingredients UNIQUE, par_level, reorder_point, safety_stock, yield_factor=1.0, on_hand_cached, last_counted_at, last_counted_qty)
- **orders**(sim_time, service_mode ∈ dine_in|delivery|takeout, table_no, staff_id→staff, guest_count, status ∈ open|closed|cancelled, channel, total)
- **order_lines**(order_id→orders, menu_item_id→menu_items, qty, unit_price, modifiers JSON, discount, line_total, status ∈ sold|voided|comped, sim_time)
- **batches**(batch_definition_id→batch_definitions, menu_item_id→menu_items, decided_at, serve_window JSON, decision ∈ cook|skip, planned_qty, actual_made_qty, sold_qty, wasted_qty, status ∈ decided|prepping|ready|served|expired, by ∈ agent|human)
- **waste_events**(waste_type ∈ overproduction|spoilage|cancelled_order|prep_error|expiry, ingredient_id?→ingredients, menu_item_id?→menu_items, lot_id?→inventory_lots, qty, unit, cost, reason, sim_time, source)
- **purchase_orders**(supplier_id→suppliers, status ∈ proposed|approved|placed|delivered|cancelled, created_at, expected_delivery, total_cost, created_by, approval_id?→approval_requests)
- **purchase_order_lines**(po_id→purchase_orders, ingredient_id→ingredients, qty, unit, unit_price, line_total)
- **menu_toggles**(menu_item_id→menu_items, action ∈ disable|enable, reason, triggered_by, sim_time, active)
- **attendance**(staff_id→staff, date_sim_day INTEGER, status ∈ present|leave|sick, daypart? (null = whole day), reason?, sim_time)  — queryable staff availability over time; voice `set_leave` writes it, Track A's Staff agent reads it (joined with `staff_stations`) for coverage

### 19.3 Intelligence / agent I/O
- **forecasts**(menu_item_id→menu_items, window JSON, daypart, forecast_qty, baseline_qty, multipliers JSON, confidence, generated_at, trigger_reason)
- **signals**(signal_id TEXT PK, type, source, groups JSON, priority, payload JSON, created_at, expires_at, dedup_key, status ∈ live|consumed|expired, correlation_id)  — indexes on (status), (dedup_key,status)
- **competitors**(name, platform, cuisine JSON, distance_km, rating, is_open, price_tier, updated_at)
- **competitor_offers**(competitor_id→competitors, dish_or_combo, price, description, updated_at)
- **competitor_intel**(competitor_id→competitors, method ∈ call|aggregator|discovery, popular_dishes JSON, price_points JSON, notes, call_id?→calls, sim_time)
- **reviews**(source, rating, text, dish_mentions JSON, sentiment, sim_time, processed=0)
- **review_insights**(review_id?→reviews, insight_type, summary, suggested_action, severity, sim_time)
- **supplier_price_history**(supplier_id→suppliers, ingredient_id→ingredients, price, sim_time)
- **negotiations**(supplier_id→suppliers, ingredient_id→ingredients, call_id?→calls, transcript JSON, outcome JSON, savings, sim_time)
- **approval_requests**(type ∈ purchase_order|menu_change|promo|outbound_call|other, title, summary, payload JSON, urgency, status ∈ pending|approved|rejected|expired, created_at, resolved_at, resolved_by, ref_id)
- **promotions**(type ∈ combo|discount, menu_items JSON, trigger ∈ expiry|slow_mover|intel, discount_pct, channel ∈ menu|aggregator|both, status ∈ proposed|approved|active|expired, approval_id?→approval_requests, sim_time)
- **user_facts**(raw_text, source ∈ voice|text, extracted JSON, applied, resulting_writes JSON, sim_time)
- **weather_log**(sim_time, source ∈ api|override, temp_c, condition ∈ clear|clouds|rain|storm|snow, precip_mm, wind_kph, applied)
- **calls**(agent ∈ market_spectator|competitor_intel, counterparty_type ∈ supplier|competitor, counterparty_id, purpose, status ∈ requested|approved|rejected|active|completed|failed|auto_resolved, approval_id?→approval_requests, transcript JSON, outcome JSON, started_at, ended_at, clock_action ∈ freeze|slow)

### 19.4 Simulation / control
- **sim_state**(id=1 singleton, sim_time, day_number, day_of_week, speed, status ∈ stopped|running|paused|call_frozen, operating_window JSON, skip_closed_hours=1, call_mode ∈ freeze|slow, active_seed_id)
- **sim_settings**(id=1 singleton, base_orders_per_day=300, velocity=1.0, dish_mix_weights JSON, daypart_curve JSON, channel_mix JSON, anomaly_injections JSON)
- **scenarios**(name, description, is_active)
- **scenario_events**(scenario_id→scenarios, at_sim_time, event_type, payload JSON, fired=0)
- **event_log**(sim_time, category, actor, summary, detail JSON)  — **the activity-log/narrative feed**

**Track write-ownership** (independence): `core` writes all reference tables, `orders`/`order_lines`, `user_facts`, `weather_log`, `calls`, `approval_requests`, `signals`, `event_log`, `attendance` (via voice `set_leave`; Track A reads it for coverage), `sim_*`, `scenario*`. **Track A** writes `forecasts`, `batches`, `competitor_offers`, `competitor_intel`, `review_insights`. **Track B** writes `inventory_ledger/lots/levels`, `waste_events`, `purchase_orders(_lines)`, `menu_toggles` (+`menu_items.active`), `promotions`, `negotiations`, `supplier_price_history`, and the **dynamic fields of `supplier_catalog`** (`current_price` / `availability` / `updated_at`; `core` only seeds that table — Track A never writes supplier tables). **Approvals are human-in-the-loop infra owned by `core`** (the queue + inbox + `/approvals/*` endpoints): any component creates an `APPROVAL_REQUEST`; on approve/reject `core` updates the row and emits `APPROVAL_RESOLVED`; the owning side acts on its own types (PO/promo → Track B handlers; `outbound_call` → `core` calls subsystem). Shared writes (`signals`, `event_log`, `approval_requests`) are append-only / status-updates → no contention.

---

## 20. REST API contract (`core/api.py`)

All JSON. Prefix `/api`.

**Sim control:** `POST /sim/play|pause|stop|restart|step`, `POST /sim/speed {speed}`, `POST /sim/jump-next`, `GET /sim/state`, `PATCH /sim/pos {base_orders_per_day?, velocity?, dish_mix_weights?, channel_mix?, daypart_curve?}`.
**Seeding/generation:** `POST /seed/preset/{id}`, `GET /seed/presets`, `POST /seed/generate {cuisine, size_params}`, `POST /generate/{menu|recipes|staff|supplier} {...}`.
**Demo editing (CRUD):** `/menu`, `/recipes`, `/staff`, `/suppliers`, `/inventory`, `/competitors`, `/reviews` — `GET` list, `POST` create, `PATCH /{id}`, `DELETE /{id}`.
**Weather:** `GET /weather`, `POST /weather/override {temp_c, condition, precip_mm, wind_kph}`.
**Reads:** `GET /forecasts`, `/inventory`, `/inventory/ledger`, `/signals?status=live&group=`, `/approvals?status=pending`, `/events?since=`, `/batches`, `/waste`, `/purchase-orders`, `/competitors`, `/competitor-intel`, `/calls`.
**Actions:** `POST /approvals/{id}/approve|reject`, `POST /voice/transcript {text}` (returns extracted + writes), `POST /calls/{id}/turn {role, text}` (append a roleplay turn), `POST /calls/{id}/end`.
**Scenarios:** `GET /scenarios`, `POST /scenarios/{id}/activate`, `POST /scenarios/{id}/deactivate`.

---

## 21. WebSocket contract (`/ws`)

One connection; server pushes `{event, payload}`. Events: `sim_tick {sim_time, day_number, time_of_day, speed, status}`, `order_created {order, lines, velocity}`, `signal_emitted {signal}`, `forecast_updated {forecast}`, `batch_decided {batch}`, `inventory_updated {ingredient_id, on_hand}`, `menu_toggled {menu_item_id, action}`, `approval_created {approval}`, `approval_resolved {approval}`, `event_logged {event}`, `weather_updated {weather}`, `call_request {call}`, `call_started {call}`, `call_turn {call_id, role, text}`, `call_ended {call, outcome}`. The frontend is a **pure consumer**; each track UI subscribes to the subset it needs.

---

## 22. Constants & thresholds (`core/config.py` — no magic numbers left to implementers)

```
# clock
OPERATING_WINDOW            = ("08:00","23:00")     # 54000 sim-s
REAL_MINUTES_PER_DAY_1X     = 15                    # default
TICK_REAL_MS                = 250
SPEEDS                      = [0.25,0.5,1,2,4,8]
SKIP_CLOSED_HOURS           = True
CALL_MODE                   = "freeze"              # or "slow" (0.1x)
# dayparts (start,end, weight) — weights sum ~1.0
DAYPARTS = {breakfast:("08:00","11:00",0.18), lunch:("11:00","15:00",0.34),
            afternoon:("15:00","17:00",0.10), dinner:("17:00","22:00",0.33),
            late:("22:00","23:00",0.05)}
# pos
BASE_ORDERS_PER_DAY=300; LINES_PER_ORDER={1:.5,2:.3,3:.2}
CHANNEL_MIX={dine_in:.70,delivery:.20,takeout:.10}; CANCEL_RATE=0.03
VELOCITY_WINDOW_SIM_S=1800; VELOCITY_ANOMALY_PCT=0.30
# forecasting
FORECAST_INTERVAL_SIM_S=1800; HISTORY_DAYS=30
EVENT_MULT=1.35; STAFF_CAP_FACTOR=0.5
VELOCITY_CLAMP=(0.6,1.6); SUGGESTION_INTERVAL_SIM_S=54000   # ~1 sim-day
# batches
BATCH_BUFFER_SIM_S=900
# inventory
SAFETY_DAYS=0.5; PAR_DAYS=3
EXPIRY_SCAN_SIM_S=3600; EXPIRY_WINDOW_SIM_S=172800   # 2 sim-days
PROMO_DISCOUNT_PCT=20
APPROVAL_PO_THRESHOLD=200          # currency units; above -> needs approval
# signals
SIGNAL_COOLDOWN_SIM_S=1800; MAX_CASCADE_DEPTH=5
# competitors / calls
COMPETITOR_RADIUS_KM=3; COMPETITOR_CALL_TARGETS=2
# llm
LLM_FALLBACK=["gemini","groq","openrouter","canned"]
LLM_RETRIES=3; LLM_BACKOFF_BASE_S=1.5; LLM_INTER_CALL_SLEEP_S=2
# weather
WEATHER_FETCH_SIM_S=10800   # every 3 sim-h
```
(These are the binding defaults; expose the sim/pos/weather ones in the UI as adjustable.)

---

## 23. Frontend shell & design tokens (`/frontend`)

`core` provides: the app shell (router, single WS client + reconnect, a global store of the latest WS state), the **Demo Control Bar** (play/pause/stop/restart/step/speed + scrubber + scenario picker + seed/generate buttons + POS velocity & dish-mix sliders + weather override + voice console), the **Approval Inbox** (shared shell panel, shown in every mode; lists `approval_requests` and posts approve/reject), and **design tokens** (Tailwind config: spacing, a restrained palette, type scale; light/dark). Track panels mount into a tabbed layout: **Track A tabs** = Forecast, Competitors, Reviews, Staff, Signal Feed; **Track B tabs** = Inventory, Expiry, Suppliers, Activity Log. The voice console has a normal mode and a **ROLEPLAY mode** (during calls) showing "You are playing: {party}" with mic + text input and the live transcript.

---

## 24. Core acceptance criteria (definition of done for `core`)

1. `make reset && make seed` loads a preset; `GET /sim/state` shows `stopped`, day 0.
2. `POST /sim/play` advances `sim_time`; `order_created` events stream; one sim-day ≈ 15 real min at 1×; speeds work; pause/stop/restart/step behave per §6.2.
3. Editing velocity/dish-mix/channel-mix/weather live changes the order stream and weather signal.
4. The signal bus enforces dedup (same `dedup_key` collapses), expiry sweep, group visibility, cooldown, and max-depth; `signal_emitted` streams.
5. Voice: a spoken/typed fact is extracted, written, and emits `USER_FACT` (all 4 worked examples in §11 pass).
6. Seeding Mode B produces a dataset that passes the validator (§12.3) with consistent numeric layer.
7. LLM wrapper falls back across providers and to canned output without crashing; cache prevents duplicate calls.
8. Call subsystem: a `CALL_REQUEST` creates an approval; approving freezes the clock, opens ROLEPLAY console, runs a turn loop, extracts an outcome, emits `CALL_OUTCOME`, and restores the clock; declining auto-resolves.
9. `reset_db()` clears transactional tables but can re-seed cleanly; `DEMO_MODE` switches mocks on/off.
10. Agent stubs subscribe to groups and receive only in-group signals.
11. `docker compose up --build` on a fresh machine brings up backend (`:8000`) + frontend (`:5173`); opening `http://localhost:5173` shows the dashboard and the WS connects (§26).

---

## 25. Glossary
- **Signal** — a typed, grouped, expiring message on the bus; the only inter-agent comms.
- **Batch** — a pre-cooked quantity of a sellable dish decided before orders arrive.
- **Ledger / theoretical inventory** — on-hand derived from POS×recipe depletions, reconciled by occasional real counts.
- **Daypart** — a named time block of the operating day (breakfast/lunch/…).
- **Sim-time** — float seconds since sim-epoch; the only clock for logic.
- **Track** — one of the two parallel build verticals (A demand/sensing, B inventory/procurement).
- **Call mode** — clock freeze/slow during a live agent↔human voice call.
- **DEMO_MODE** — `track_a | track_b | combined`; selects which side's signals are mocked.

---

## 26. Containerization & local setup (Docker Compose)

The whole demo runs with one command on any machine via Docker Compose: a **backend** service (FastAPI/uvicorn, embedded SQLite) and a **frontend** service (Vite). No external DB container — SQLite lives in a mounted volume. `requirements.txt` and `frontend/package.json` are created in Phase 0.

### 26.1 docker-compose.yml
```yaml
services:
  backend:
    build: { context: ., dockerfile: Dockerfile.backend }
    ports: ["8000:8000"]
    env_file: [.env]
    environment: { DB_PATH: /app/dbdata/demo.db, DEMO_MODE: "${DEMO_MODE:-combined}" }
    volumes: ["dbdata:/app/dbdata"]            # SQLite persists here
  frontend:
    build: { context: ./frontend }
    ports: ["5173:5173"]
    depends_on: [backend]
    environment: { BACKEND_ORIGIN: http://backend:8000 }
volumes: { dbdata: {} }
```

### 26.2 Dockerfile.backend
```dockerfile
FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core ./core
COPY track_a ./track_a
COPY track_b ./track_b
COPY data ./data
RUN mkdir -p /app/dbdata
EXPOSE 8000
CMD ["uvicorn", "core.api:app", "--host", "0.0.0.0", "--port", "8000"]
```
The backend imports both tracks; `DEMO_MODE` selects which mocks run. `DB_PATH` points SQLite at the mounted volume (`db.py` reads it); `docker compose down -v` wipes it, `make reset` re-seeds.

### 26.3 frontend/Dockerfile
```dockerfile
FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
EXPOSE 5173
CMD ["npm","run","dev","--","--host","0.0.0.0","--port","5173"]
```

### 26.4 Vite proxy (frontend/vite.config.ts)
The browser hits the frontend origin; Vite proxies API + WebSocket to the backend service (no CORS/origin issues):
```ts
server: { host: true, port: 5173, proxy: {
  "/api": { target: process.env.BACKEND_ORIGIN ?? "http://localhost:8000", changeOrigin: true },
  "/ws":  { target: process.env.BACKEND_ORIGIN ?? "http://localhost:8000", ws: true }
}}
```
All frontend calls use **relative paths** (`/api/...`, `/ws`) — never hardcode host/port.

### 26.5 Run
```
cp .env.example .env          # fill GEMINI_API_KEY (+ GROQ/OPENROUTER optional)
docker compose up --build     # or: make up
# open http://localhost:5173
docker compose down           # make down   (down -v also wipes the DB)
```
For non-Docker dev: `uvicorn core.api:app --reload` + `npm run dev` in `/frontend` (the proxy falls back to `localhost:8000`). The two paths are interchangeable; demos use Docker.

### 26.6 Makefile targets
`up` = `docker compose up --build`; `down` = `docker compose down`; `reset` = `down -v` then `up` (re-seeds); `seed` = POST default preset; `demo-a|demo-b|demo` = set `DEMO_MODE` and `up`; `test` = pytest (backend) + vitest (frontend).

---

*End of 00_ARCHITECTURE.md. See 01_TRACK_A.md / 02_TRACK_B.md (implementation briefs) and 03_BUILD_PLAN_A.md / 04_BUILD_PLAN_B.md (execution + testing playbooks).*
