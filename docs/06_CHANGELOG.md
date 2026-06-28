# 06 — Changelog

Reverse-chronological record of every significant commit. Entries include the short SHA, date, what changed functionally, and the files that matter. Read this before touching any system so you know what is actually wired versus stubbed.

---

## `pending` — 2026-06-27 — Voice-first interface, POS chart fix, Optimizer LLM, Cook feedback loop

This entry covers five simultaneous workstreams (A–E) delivered together. Read all sections before touching the voice pipeline, batch lifecycle, or inventory optimizer.

---

### Overview

Four major features were added:

1. **Signal→agent routing (Stream A)** — Signals can now name specific agents to target, breaking the group-only fan-out constraint.
2. **POS chart stabilisation (Stream D)** — The "Orders over time" X-axis no longer drifts; bucket widths are fixed per window.
3. **Bidirectional Gemini Live voice interface (Streams B + C)** — A dedicated `/voice` page with role-based UIs (Manager / Cook) that uses the Gemini Live API for real-time two-way speech. Roba both listens and talks back. The LLM turns spoken input into bus signals via a plan/confirm layer. Full text fallback when no key is set.
4. **Inventory Optimizer LLM pass (Stream E)** — The Track B optimizer now runs an LLM reasoning step that can disable dishes sharing a scarce ingredient, propose near-waste deals, and adjust procurement timing. Uses the same `DemandForecasterMemory`-style durable memory.
5. **Cook feedback loop (Stream B3)** — Batches advance through a full lifecycle (decided → approved → cooked). Cook waste reporting feeds back into the demand forecaster's learning memory. Auto-approve is the default; a gated mode exists.

---

### Stream A — Signal→agent routing

**Problem:** The orchestrator fanned signals out by *group-tag intersection only*. There was no way for a voice planner (or any agent) to say "send this signal specifically to the forecaster and no one else."

**Solution:** Added an optional `target_agents: List[str]` field to signals. Routing is now a union: an agent receives a signal if its subscribed groups intersect the signal's groups **OR** its name appears in `target_agents`. Both conditions are checked; unset `target_agents` keeps behaviour identical to before.

**What changed:**

- `core/models.py` — `Signal` table: new `target_agents = mapped_column(JSON, nullable=True)` column (after `correlation_id`). Existing rows unaffected (nullable).
- `core/bus.py` — `SignalBus.emit()` accepts `target_agents: Optional[List[str]] = None`. Persists it on the new column; on dedup-refresh, refreshes the field if provided.
- `core/orchestrator.py` — `on_signal()` union routing:
  ```python
  named = set(signal.target_agents or [])
  by_group = bool(sig_groups & set(agent.subscribed_groups))
  by_name  = agent.name in named
  if not (by_group or by_name): continue
  ```
- `core/signals.py` — `_signal_to_dict()` carries `target_agents` in the WS broadcast payload.

**Usage pattern:**
```python
bus.emit(
    SignalType.DEMAND_EVENT,
    payload,
    target_agents=["forecaster"],   # only wakes the forecaster
)
```

---

### Stream D — POS "Orders over time" chart fix

**Problem:** The chart X-axis rescaled constantly while the simulation ran. Root cause: `width = span / 24` where `span = now - since` grows every poll tick, so bucket boundaries (`floor(t/width)*width`) shift with every 3-second refresh.

**Solution:** Fixed bucket widths per window, anchored to absolute clock boundaries.

**Backend (`core/api.py` — `read_pos_stats`):**
```python
_BUCKET_WIDTHS = {"1h": 300, "6h": 1800, "day": 3600, "week": 86400}
```
- Added `window: str = Query("day")` parameter.
- Bucket start is `floor(since / width) * width`. Boundaries are always round multiples (08:00, 09:00, …) regardless of `now`.
- No more adaptive width: the same 24 buckets always align to the same clock positions.

**Frontend:**
- `frontend/src/pos/usePosStats.ts` — appends `&window=${windowKey}` to the fetch URL.
- `frontend/src/pos/PosMonitor.tsx` — added `simTimeToLabel(t, windowKey)` helper that formats as `HH:MM` for intraday windows and `Mon`, `Tue`, … for `week`.

**Invariant:** Switching the window selector or stopping/restarting the simulation no longer shifts or rescales the chart axis.

---

### Stream B — Voice planner backend

#### B1 — Plan/confirm layer (`core/voice.py`)

The existing `VoiceProcessor` was a one-shot transcribe→emit pipeline. A plan/confirm layer sits on top without replacing it.

**New public methods:**

| Method | What it does |
|--------|-------------|
| `plan(text, role, mode)` | Runs extraction + routing but does NOT emit signals. Persists a `VoicePlan` row with `status="pending"`. In `mode="auto"` immediately calls `confirm()` and returns the applied result. |
| `confirm(plan_id)` | Loads the plan, emits each route via `bus.emit(..., target_agents=route["target_agents"])`, marks it `applied`. |
| `cancel(plan_id)` | Marks it `cancelled`. Supersedes any existing `pending` plan for the same role first. |
| `clarify(plan_id, answer)` | Appends the answer to the raw text and re-plans. Returns a new plan (with a new `plan_id`; original is marked `superseded`). |

**Route target_agents derivation:** Each route's `target_modules` list (e.g. `["track_a.forecaster"]`) is mapped to agent names via `_MODULE_TO_AGENT`:
```python
_MODULE_TO_AGENT = {
    "track_a.forecaster":  "forecaster",
    "track_b.optimizer":   "optimizer",
    "track_b.ledger":      "ledger",
    "track_b.procurement": "procurement",
    ...
}
```
This gives the voice planner named targeting without hard-coding agent names at call sites.

**Cook intents** (gated by `role in {"cook", "kitchen"}`):

- *Mark batch cooked*: resolves the next `approved`/`decided` batch for the named item → records `actual_made_qty`, advances `Batch.status = "cooked"`, emits `BATCH_PROGRESS`.
- *Report waste*: if cause is ambiguous, returns a `clarification` block instead of emitting. The cook's follow-up answer re-plans with full cause → writes a `WasteEvent` and emits `WASTE_EVENT(source="cook")`.

**Manager human-in-the-loop** (competitor/supplier call intents): calls `ctx.calls.request(...)` which creates an `outbound_call` approval via the existing approval flow. Plan is marked `requires_approval=True`. The manager sees it in the approvals inbox.

#### B2 — New signal type and models

**`core/signals.py`:**
- `SignalType.BATCH_PROGRESS = "BATCH_PROGRESS"` — groups `["kitchen","forecasting","inventory","human","frontend"]`, priority 3, TTL 4 hours.
- `BatchProgressPayload`: `batch_id`, `menu_item_id`, `actual_made_qty`, `planned_qty?`, `sold_qty?`, `wasted_qty?`, `status`, `source`.

**`core/models.py` additions:**
- `VoicePlan` table — `plan_id` (PK text/UUID), `role`, `mode`, `raw_text`, `plan` (JSON route list), `status` (pending|applied|cancelled|superseded), `created_at`, `applied_at`.
- `InventoryOptimizerMemory` table — mirrors `DemandForecasterMemory`: `key` (unique), `insight`, `confidence`, `last_seen_at`, `times_observed`.
- `Batch.approval_id` — FK to `approval_requests` (nullable); set when gated mode creates an approval.
- `Batch.cooked_at` — sim-time when cook marked it done (nullable).

**`core/config.py` additions:**
```python
VOICE_DEFAULT_MODE      = os.getenv("VOICE_DEFAULT_MODE", "confirm")
BATCH_APPROVAL_GATED    = os.getenv("BATCH_APPROVAL_GATED", "0") in {"1","true","yes","on"}
OPTIMIZER_LLM_AUTO_MODE = os.getenv("OPTIMIZER_LLM_AUTO_MODE", "0") in {"1","true","yes","on"}
PROMO_SLOW_MOVER_PCT    = int(os.getenv("PROMO_SLOW_MOVER_PCT", "15"))
GEMINI_LIVE_MODEL       = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.0-flash-live-001")
```

#### B4 — Voice REST endpoints (`core/api.py`)

All new endpoints under `/api/voice/` and `/api/kitchen/`:

| Method | Path | Body | Returns |
|--------|------|------|---------|
| `POST` | `/api/voice/plan` | `{text, role, mode}` | plan object: `{plan_id, role, mode, summary, human_readable, routes, clarification?, requires_approval}` |
| `POST` | `/api/voice/plan/{id}/confirm` | — | `{status:"applied", signal_ids:[...]}` |
| `POST` | `/api/voice/plan/{id}/cancel` | — | `{status:"cancelled"}` |
| `POST` | `/api/voice/clarify` | `{plan_id, answer}` | re-planned result (same shape as `/plan`) |
| `GET` | `/api/settings/voice` | — | `{default_mode}` |
| `POST` | `/api/settings/voice` | `{default_mode}` | `{ok:true}` |
| `GET` | `/api/kitchen/batches` | `?status=approved,decided` | list of kitchen batches with `item_name`, `cook_by` |
| `POST` | `/api/kitchen/batches/{id}/cooked` | `{actual_made_qty}` | advances batch status, emits `BATCH_PROGRESS` |
| `POST` | `/api/kitchen/waste` | `{menu_item_id?, ingredient_id?, qty, waste_type, from_batch_id?}` | writes `WasteEvent`, emits `WASTE_EVENT` |
| `GET` | `/api/track-b/optimizer/insights` | — | recent `InventoryOptimizerMemory` rows |
| `POST` | `/api/track-b/optimizer/auto-mode` | `{enabled}` | toggles `OPTIMIZER_LLM_AUTO_MODE` at runtime |
| `POST` | `/api/track-b/optimizer/run-llm` | — | triggers `optimizer.llm_optimize()` immediately |
| `WS` | `/ws/voice/live` | `?role=&mode=` | Gemini Live bridge (binary PCM both ways + JSON control frames) |

#### B5 — Gemini Live bridge (`core/voice_live.py`)

This is the server-side half of the real-time voice path. It bridges the browser WebSocket to a Gemini Live session and executes tool calls server-side.

**Topology:**
```
Browser ←──WS /ws/voice/live──→ live_bridge() ←──Gemini Live WS──→ Gemini API
```

**Why server-side bridge?** The `GEMINI_API_KEY` never leaves the server. The browser only sends raw PCM audio and receives raw PCM audio + JSON control frames.

**Audio format contract (mandated by Gemini Live):**
- Browser → server: 16 kHz, mono, PCM16 LE (little-endian signed 16-bit)
- Server → browser: 24 kHz, mono, PCM16 LE

**SDK methods used (google-genai v2.8+):**

| Operation | Method |
|-----------|--------|
| Send audio chunk | `session.send_realtime_input(media=types.Blob(data=bytes, mime_type="audio/pcm;rate=16000"))` |
| End of audio stream | `session.send_realtime_input(audio_stream_end=True)` |
| Send text turn | `session.send_client_content(turns={"parts":[{"text":"..."}]}, turn_complete=True)` |
| Return tool result | `session.send_tool_response(function_responses=types.FunctionResponse(id=..., name=..., response=...))` |
| Receive audio | `chunk.data` property (concatenates all `inline_data` parts) |
| Receive text | `chunk.text` property |
| Input transcription | `chunk.server_content.input_transcription.text` |
| Output transcription | `chunk.server_content.output_transcription.text` |
| Tool calls | `chunk.tool_call.function_calls` (list of `FunctionCall` objects with `.id`, `.name`, `.args`) |

**Important: `session.receive()` is per-turn.** It yields chunks until `server_content.turn_complete = True`, then raises `StopAsyncIteration`. The bridge wraps it in `while True:` to handle multi-turn conversations.

**Timeouts (failsafe layer):**
- Connection timeout: `asyncio.timeout(10s)` around the initial `connect()` call. If the API key is invalid or the network is down, the WS immediately receives `{"type":"unavailable", "reason":"..."}` and the bridge exits cleanly.
- Response timeout: `asyncio.timeout(20s)` per turn inside the receive loop. If Gemini goes silent (API outage, very slow response), the browser gets `{"type":"error","message":"response_timeout"}` and the state resets to "ready".

**Tool declarations exposed to Gemini:**

| Tool | Gemini calls it when… | Server-side handler |
|------|-----------------------|---------------------|
| `process_note(text)` | The model wants to understand what the user said | `VoiceProcessor.plan(text, role, mode)` |
| `confirm_plan(plan_id)` | The model (or user voice) confirmed a pending plan | `VoiceProcessor.confirm(plan_id)` |
| `cancel_plan(plan_id)` | The model (or user voice) rejected a plan | `VoiceProcessor.cancel(plan_id)` |
| `mark_batch_cooked(item_name, actual_qty)` | Cook says batch is done | Synthesises voice note → `plan(text, role="cook", mode="auto")` |
| `report_waste(item_name, qty, cause)` | Cook reports thrown-away food | Synthesises voice note → `plan(text, role="cook", mode="auto")` |
| `request_competitor_call(target, counterparty_type, purpose)` | Manager wants to call a competitor/supplier | `plan(text, role="manager", mode="confirm")` → creates an outbound_call approval |

**Fallback:** If `GEMINI_API_KEY` is unset, the WS immediately sends `{"type":"unavailable","reason":"no_api_key"}` and returns. The frontend shows the text input fallback — no hang, no crash.

---

### Stream B3 — Batch lifecycle and cook feedback loop

#### Batch auto-approve

Previously batches were stuck at `status="decided"` forever — no cook ever saw them.

**Now:** When `decide_batches()` creates a batch with `decision="cook"`:
- If `BATCH_APPROVAL_GATED=False` (default): batch immediately gets `status="approved"`. Cooks see it via `GET /api/kitchen/batches?status=approved,decided`.
- If `BATCH_APPROVAL_GATED=True`: batch stays at `status="decided"` and an approval request (`type="batch"`) is created in the approval inbox. The manager approves → `ApprovalHandlers.on_resolved()` advances it to `"approved"`.

**Files changed:**
- `track_a/agents/forecaster.py` — `decide_batches()` conditionally sets `initial_status`, creates approval requests via `self.approvals.create(type="batch", ...)`.
- `track_a/__init__.py` / `core/api.py` — `approvals` passed through `bootstrap_track_a()` to the forecaster.
- `track_b/approval/handlers.py` — `OWN_TYPES` now includes `"batch"`; `on_resolved()` handles it by calling `_approve_batch(batch_id)` which writes `status="approved"` to the DB.

#### Forecaster learning from cook feedback

Two new signals feed back into `DemandForecasterMemory`:

**`BATCH_PROGRESS` (cook marks batch cooked):**
- `_learn_from_batch_progress(payload)`: if `actual_made_qty / planned_qty` deviates by more than 15%, writes a `DemandForecasterMemory` row scoped to `(menu_item_id, daypart)` nudging future batch quantities in the observed direction.
- Example: cook makes 8 instead of 15 margheritas at lunch → memory says "reduce lunch margherita batch by ~47%".

**`WASTE_EVENT(source="cook")` (cook reports throw-away):**
- `_learn_from_cook_waste(payload)`: if `waste_type="overproduction"`, writes a memory reducing the future forecasted quantity for that item in that daypart.
- Example: "I threw away 6 tiramisus, overproduction" → memory says "cut tiramisu dinner batch".

Both learning paths reuse the existing `_remember()` method which upserts by key and increments `times_observed`, so repeated signals converge rather than thrash.

---

### Stream E — Inventory Optimizer LLM pass

The `InventoryOptimizer` gained an LLM reasoning layer that runs alongside (not instead of) its deterministic rules.

#### When it runs

- On `LOW_STOCK`, `STOCKOUT_RISK`, `EXPIRY_RISK`, or `EXPIRY_USE_PRIORITY` signals (when `OPTIMIZER_LLM_AUTO_MODE=True`).
- On a timer: every `FORECAST_INTERVAL_SIM_S * 2` ticks.
- On demand: `POST /api/track-b/optimizer/run-llm`.

#### Context fed to the LLM (`_build_llm_context`)

```json
{
  "inventory": [{"ingredient_id":…, "name":"…", "on_hand":…, "par":…, "unit":"…"}],
  "near_expiry": [{"lot_id":…, "ingredient_id":…, "qty":…, "expiry_date":…}],
  "menu_items": [{"id":…, "name":…, "active":…, "margin_x_velocity":…, "ingredients":[…]}],
  "supplier_catalog": [{"ingredient_id":…, "supplier":…, "price":…, "lead_days":…}],
  "optimizer_memory": [{"key":"…", "insight":"…", "confidence":…, "times_observed":…}],
  "live_demand_forecasts": [{"menu_item_id":…, "forecast_qty":…, "confidence":…}]
}
```

The `margin_x_velocity` score = `(dine_in_price × historical_attach_rate)` — a proxy for contribution per service slot. Used by the LLM to prefer disabling the lower-value dish when an ingredient is shared.

#### Actions the LLM can return

```json
[
  {"action": "toggle_item",    "menu_item_id": 3, "enable": false, "confidence": 0.8, "rationale": "…"},
  {"action": "create_deal",    "menu_item_ids": [5], "discount_pct": 20, "trigger": "expiry", "confidence": 0.75, "rationale": "…"},
  {"action": "reorder",        "ingredient_id": 2, "qty": 50, "rationale": "…"},
  {"action": "defer_reorder",  "ingredient_id": 7, "rationale": "…"}
]
```

Confidence threshold for application: **0.55** (actions below this are logged but not executed). Actions map to existing deterministic writers:

| LLM action | Deterministic executor |
|------------|----------------------|
| `toggle_item` | `optimizer._disable()` / `_manual_toggle()` |
| `create_deal` | `optimizer._propose_promo_llm()` (wraps `_propose_promo`) |
| `reorder` | `procurement.create_po()` (still honours `APPROVAL_PO_THRESHOLD`) |
| `defer_reorder` | logs only; skips the reorder that would have fired deterministically |

**Fallback:** If `self.llm is None` or the LLM call raises any exception, the method returns silently and the normal deterministic reorder/promo paths run as before. No key = no LLM, no crash.

#### Learning memory (`InventoryOptimizerMemory`)

After each successful `llm_optimize()` run, outcomes are persisted via `_remember(key, insight, confidence)`. On the next run, these rows are included in the LLM context so it builds on prior observations (e.g. "tomatoes spoil every Sunday → reduce PO size by 30%").

---

### Stream C — Voice frontend

A dedicated `/voice` route mounted **outside** `OperatorLayout` (so it never opens the operator WS firehose). Accessible from the top nav ("Voice" link) and directly at `/voice`.

#### Architecture

```
VoicePage          ← role chooser (Manager / Kitchen)
  ├── ManagerVoice ← uses useVoiceLive("manager")
  └── CookVoice    ← uses useVoiceLive("cook")

useVoiceLive(role)
  └── RobaLiveClient    ← WS to /ws/voice/live
        ├── AudioWorklet (mic-processor.js)  ← mic capture
        └── Web Audio API                    ← playback
```

#### `mic-processor.js` (AudioWorklet, `frontend/public/`)

Runs in a dedicated audio thread (not the main thread — no jank):
- Receives float32 audio from the mic at the browser's native rate (48kHz typical).
- Downsamples to 16kHz using nearest-neighbour (correct for speech).
- Converts to signed PCM16 little-endian.
- Posts 20ms chunks (320 samples at 16kHz) as `ArrayBuffer` to the main thread.

#### `RobaLiveClient.ts`

Manages the entire WS + audio lifecycle:
- Connects to `ws[s]://<host>/ws/voice/live?role=<role>&mode=<mode>`.
- On `startListening()`: requests mic permission → creates `AudioContext` at 48kHz → adds the worklet → pipes mic → worklet output → WS binary frames.
- On `stopListening()`: tears down mic, sends `{"type":"end_of_turn"}` to signal the server to tell Gemini the audio turn is complete.
- Barge-in: calling `startListening()` while Roba is speaking closes the playback `AudioContext` immediately (interrupts Roba mid-sentence).
- Playback: incoming binary frames (24kHz PCM16 from Gemini) are decoded to float32, placed in an `AudioBuffer`, and scheduled sequentially on a second `AudioContext` for gap-free playback.
- Emits typed events: `connected`, `unavailable`, `transcript`, `plan_preview`, `tool_result`, `applied`, `error`, `disconnected`.

#### `useVoiceLive.ts`

React hook providing:
- State machine: `idle → connecting → ready → listening → thinking → speaking → unavailable`
- **Connection timeout (8s)**: if the WS doesn't receive `"connected"` or `"unavailable"` within 8 seconds, state transitions to `"unavailable"` with an error message. Protects against silent WS failures.
- **Thinking timeout (20s)**: if state stays `"thinking"` for 20 seconds after `stopListening()`, it resets to `"ready"` with the message "No response — Roba may be unavailable." Protects against Gemini API hangs.
- Both timers are cleared immediately if a real response arrives.
- `startListening()` / `stopListening()` / `sendText()` / `confirmPlan()` / `cancelPlan()` / `clearTranscript()`.

#### `MicButton.tsx`

Shared component with two simultaneous interaction modes:
- **Click to toggle**: short tap (< 300ms) starts listening; next short tap stops.
- **Hold to talk**: long press (≥ 300ms) listens while held; release sends.

Implementation uses `onPointerDown` / `onPointerUp` + `setPointerCapture` for reliable cross-device behaviour. A `pressTimeRef` records the press start; duration on release determines mode. Visual states: idle (red button), listening (pulsing ring + mic icon), thinking (spinner), speaking (volume icon), unavailable (greyed out + MicOff).

#### `PlanConfirmCard.tsx`

Rendered when Roba has processed a note and is in confirm-first mode:
- Shows `human_readable` summary (plain English — e.g. "Roba will boost the dinner forecast by 30% and flag it to the forecaster agent").
- Shows target agent pills (e.g. `forecaster`, `optimizer`).
- Shows a route breakdown (each signal type and its purpose).
- Confirm button → `confirmPlan(plan_id)` → signals are emitted to named agents.
- Cancel button → `cancelPlan(plan_id)` → plan discarded.
- When the plan has a `clarification` (Roba needs more info), renders option buttons instead of Confirm/Cancel. Selecting an option calls `POST /api/voice/clarify` and surfaces the re-planned result.

#### `ModeToggle.tsx`

Toggle between `confirm` (default) and `auto` modes. In auto mode, plans are applied without the confirmation step — Gemini calls `confirm_plan()` server-side immediately after `process_note()` returns. In confirm mode, Roba reads back the plan and waits.

#### `ManagerVoice.tsx`

Full Roba management console:
- `MicButton` (large, centred).
- Error strip with refresh button — shows any `lastError` from the hook.
- Approvals inbox: polls `GET /api/approvals?status=pending` every 5s; shows pending approvals with Approve/Reject buttons. This is the human-in-the-loop surface for competitor call requests, batch approvals (gated mode), PO approvals, etc.
- `PlanConfirmCard` shown when a plan is pending.
- Transcript (last 12 turns, scrollable, user messages right-aligned, Roba left-aligned).
- Hidden "Type instead" text input (always available as fallback, even when Live works).

#### `CookVoice.tsx`

Kitchen-focused:
- "Next batch" card: shows the queued `approved` or `decided` batch with dish name, planned qty, and a "cook by" time if set. Has an actual-qty input and a "Mark cooked" button (calls `POST /api/kitchen/batches/{id}/cooked` directly, bypassing voice for speed).
- `MicButton` (medium).
- Quick action buttons: "Batch done" (pre-fills the voice note) and "Report waste" (kicks off the clarification flow via voice).
- `PlanConfirmCard` for waste cause clarification.
- Transcript (last 8 turns).
- "Type instead" fallback.
- Polls `GET /api/kitchen/batches?status=approved,decided` every 5s and removes a batch from the UI immediately when marked cooked.

#### `VoicePage.tsx`

Role chooser entry point. Two large cards (Manager / Kitchen) with icon, label, and description. Selecting one renders the appropriate sub-component. A "Switch role" button in the header returns to the chooser without a page reload (no WS reconnect needed — `useVoiceLive` reconnects on role change via its `useEffect([role])` dependency).

---

### Runtime: how a voice turn flows end-to-end

**Happy path (Gemini Live available, Manager role, confirm-first mode):**

```
1. User opens /voice → selects Manager
   → useVoiceLive("manager") mounts
   → RobaLiveClient connects WS to /ws/voice/live?role=manager&mode=confirm
   → live_bridge() checks GEMINI_API_KEY → connects to Gemini Live
   → server sends {"type":"connected"} → state: "ready"

2. User holds the mic button (> 300ms hold)
   → startListening(): getUserMedia() → AudioContext → AudioWorklet loads
   → state: "listening"
   → mic-processor.js downsamples 48kHz → 16kHz PCM16, posts 20ms chunks
   → RobaLiveClient sends binary frames over WS
   → _client_to_gemini() → session.send_realtime_input(media=Blob(...))
   → [user speaks: "there is a parade tonight, expect a big crowd"]

3. User releases button
   → stopListening(): mic torn down
   → WS sends {"type":"end_of_turn"}
   → _client_to_gemini() → session.send_realtime_input(audio_stream_end=True)
   → state: "thinking" + 20s timeout armed

4. Gemini transcribes the audio, decides to call process_note
   → tool_call arrives in _gemini_to_client()
   → _execute_tool("process_note", {"text": "there is a parade tonight..."})
   → VoiceProcessor.plan("there is a parade...", role="manager", mode="confirm")
     → extracts: DEMAND_EVENT intent, +30% multiplier, evening window
     → builds routes: [{signal_type: DEMAND_EVENT, target_agents: ["forecaster"], summary: "..."}]
     → saves VoicePlan(status="pending")
     → returns {plan_id, human_readable: "Roba will boost evening demand by 30%..."}
   → session.send_tool_response(FunctionResponse(result={...}))

5. Gemini reads the plan back aloud in natural language
   → audio chunks flow through _gemini_to_client() → websocket.send_bytes()
   → RobaLiveClient.playPcm() schedules the 24kHz audio on the playback AudioContext
   → state: "speaking" (set when output_transcription.text arrives)
   → transcript line added: {role:"roba", text:"I'll boost the dinner forecast by about 30%..."}
   → PlanConfirmCard appears in UI

6. User says "yes, do it" (or taps Confirm button)
   → Gemini calls confirm_plan({plan_id})
   → _execute_tool() → VoiceProcessor.confirm(plan_id)
     → loads VoicePlan, calls bus.emit(DEMAND_EVENT, payload, target_agents=["forecaster"])
     → forecaster receives DEMAND_EVENT → adjusts forecast
     → VoicePlan.status = "applied"
   → tool result → {"status":"applied", "signal_ids":["abc..."]}
   → Gemini confirms aloud: "Done, I've updated the forecast."
   → frontend receives {"type":"applied"} → PlanConfirmCard dismissed
   → state: "ready"
```

**No-API-key path:**
```
WS connects → live_bridge() checks GEMINI_API_KEY="" 
→ sends {"type":"unavailable","reason":"no_api_key"} → returns
→ state: "unavailable"
→ lastError: "No GEMINI_API_KEY set — use the text input below."
→ TextFallback rendered; typing a note calls POST /api/voice/plan (REST, no Live)
→ PlanConfirmCard appears with REST-derived plan
```

**Thinking timeout:**
```
User stops speaking → state: "thinking" → 20s timer starts
→ 20s elapses with no response
→ state: "ready" + lastError: "No response — Roba may be unavailable."
→ Refresh button reloads the page (reconnects WS)
```

---

### Environment variables added

| Variable | Default | Effect |
|----------|---------|--------|
| `GEMINI_API_KEY` | — | Required for Gemini Live voice. Without it the voice page degrades to text. |
| `GEMINI_LIVE_MODEL` | `gemini-2.0-flash-live-001` | Which Live model to connect to. |
| `VOICE_DEFAULT_MODE` | `confirm` | `confirm` or `auto`. Initial mode for all voice sessions. |
| `BATCH_APPROVAL_GATED` | `0` | Set to `1` to require manager approval before cooks see batches. |
| `OPTIMIZER_LLM_AUTO_MODE` | `0` | Set to `1` to enable automatic LLM reasoning pass in the optimizer. |
| `PROMO_SLOW_MOVER_PCT` | `15` | Minimum velocity shortfall % to trigger a slow-mover promo. |

---

### Key files

```
# Backend — new files
core/voice_live.py                      Gemini Live WS bridge
frontend/public/mic-processor.js        AudioWorklet: mic capture → 16kHz PCM16

# Backend — modified files
core/models.py                          Signal.target_agents, VoicePlan, InventoryOptimizerMemory,
                                        Batch.approval_id, Batch.cooked_at
core/bus.py                             emit() target_agents param
core/orchestrator.py                    union routing (group OR named)
core/signals.py                         BATCH_PROGRESS type + BatchProgressPayload
core/config.py                          VOICE_DEFAULT_MODE, BATCH_APPROVAL_GATED,
                                        OPTIMIZER_LLM_AUTO_MODE, PROMO_SLOW_MOVER_PCT,
                                        GEMINI_LIVE_MODEL
core/voice.py                           plan/confirm/cancel/clarify, cook + manager intents
core/api.py                             voice/kitchen/optimizer endpoints + /ws/voice/live
track_a/agents/forecaster.py            batch auto-approve, approvals param,
                                        _learn_from_batch_progress, _learn_from_cook_waste
track_a/__init__.py                     approvals passed to bootstrap
track_b/agents/optimizer.py             llm_optimize, _build_llm_context, _apply_llm_actions,
                                        InventoryOptimizerMemory upserts
track_b/agents/__init__.py              llm + db_session_factory passed to optimizer/handlers
track_b/approval/handlers.py            batch type + _approve_batch
pos/usePosStats.ts                      &window= param
pos/PosMonitor.tsx                      simTimeToLabel helper

# Frontend — new files
frontend/src/voice/RobaLiveClient.ts    WS + audio client class
frontend/src/voice/useVoiceLive.ts      React hook (state machine + timeouts)
frontend/src/voice/MicButton.tsx        click-to-toggle + hold-to-talk button
frontend/src/voice/ModeToggle.tsx       confirm/auto toggle
frontend/src/voice/PlanConfirmCard.tsx  plan preview + confirm/cancel + clarification
frontend/src/voice/ManagerVoice.tsx     Manager role UI
frontend/src/voice/CookVoice.tsx        Cook role UI
frontend/src/voice/VoicePage.tsx        role chooser entry point

# Frontend — modified files
frontend/src/App.tsx                    /voice route (lazy, outside OperatorLayout)
frontend/src/routes/OperatorLayout.tsx  "Voice" added to NAV
```

---

### What is NOT implemented / known gaps

- **`VoiceProcessor.plan()` / `confirm()` implementation detail**: the route extraction in `core/voice.py` was extended with method stubs and intent handlers. If you find a cook/manager intent that isn't being extracted correctly, check `_try_cook_intent()` and `_try_call_intent()` in `core/voice.py` — those are the most likely places where pattern matching may need tuning for new phrases.
- **Batch status after "cooked"**: batches advance to `"cooked"` via the cook voice flow, but there is no automatic transition to `"served"`. That step (linking a cooked batch to fulfilled order lines) would require matching POS orders to batch inventory depletion — not yet implemented.
- **`OPTIMIZER_LLM_AUTO_MODE` defaults off**: the LLM optimizer pass is disabled by default so CI and demo environments without a key don't fail silently. Enable it with `OPTIMIZER_LLM_AUTO_MODE=1`.
- **No end-to-end tests for the voice frontend**: the voice components are not covered by the existing pytest suite (which is backend-only). Manual verification is required.
- **Gemini Live model availability**: `gemini-2.0-flash-live-001` requires the Live API to be enabled on the project. If the connection fails with a 403, check API key permissions and `GEMINI_LIVE_MODEL`.

---

## `4f9c49b` — 2026-06-24 — Frontend overhaul

**What changed:**
- Replaced `PanelsView.tsx` with `DashboardView.tsx`. The live dashboard is now a flat domain-grouped tab strip (Operations, Forecast, Staff, Inventory, Expiry, Competitors, Reviews, Suppliers, Activity, Signals). No "Track A / Track B" wording anywhere in the UI.
- Created `ControlDashboard.tsx` — a full-width 12-section settings page replacing the old empty `/control` placeholder. Sections: Simulation config, Seed & Restaurant, POS Generation, Anomalies, Weather, Menu & Recipes, Ingredients & Inventory, Suppliers, Staff & Stations, Competitors & Reviews, Forecast, Advanced.
- Retired `SettingsDrawer.tsx`. Its panels (POS mix, anomalies, scenarios, entities) were lifted into `shell/control/` and rewritten as purpose-built editors.
- `ControlBar.tsx` trimmed to live knobs only — transport, speed, velocity, voice, approvals. Weather/scenario/seed pickers moved to the Control page.
- `MenuPage.tsx` gained a `← Operator console` back-link.
- Backend: 4 new CRUD resources added to `_register_crud` — `stations`, `batch-definitions`, `recipe-lines`, `promotions`. New `PATCH /api/sim/state` endpoint writes `operating_window`, `skip_closed_hours`, `call_mode` to the `SimState` singleton and broadcasts `sim_state_changed`.
- `types.ts`: added `operating_window`, `skip_closed_hours` to `SimState`; added `Station`, `Recipe`, `RecipeLine`, `BatchDefinition` interfaces.

**Key files:**
```
frontend/src/shell/DashboardView.tsx           new — unified dashboard
frontend/src/shell/ControlDashboard.tsx        new — settings dashboard compositor
frontend/src/shell/control/                    new — 14 editor components
  AdvancedEntities.tsx  AnomaliesPanel.tsx  CompetitorsReviews.tsx
  ForecastControls.tsx  IngredientsInventory.tsx  MenuRecipeEditor.tsx
  PosMixPanel.tsx  ScenariosPanel.tsx  SeedManager.tsx  SimConfig.tsx
  StaffStations.tsx  SuppliersEditor.tsx  WeatherControl.tsx  shared.tsx
frontend/src/shell/SettingsDrawer.tsx          deleted
frontend/src/shell/ControlBar.tsx              modified — live-only
frontend/src/shell/ControlShell.tsx            modified — drawer removed
frontend/src/routes/ConsolePage.tsx            uses DashboardView
frontend/src/routes/ControlPage.tsx            uses ControlDashboard
frontend/src/routes/PanelsPage.tsx             uses DashboardView readOnly
frontend/src/menu/MenuPage.tsx                 back link added
frontend/src/types.ts                          SimState + new entity types
core/api.py                                    4 CRUD resources + PATCH /api/sim/state
```

**New REST endpoints:**
- `GET/POST/PATCH/DELETE /api/stations`
- `GET/POST/PATCH/DELETE /api/batch-definitions`
- `GET/POST/PATCH/DELETE /api/recipe-lines`
- `GET/POST/PATCH/DELETE /api/promotions`
- `PATCH /api/sim/state` — body: `{ operating_window?, skip_closed_hours?, call_mode? }`

---

## `3e438c4` — 2026-06-24 — Competitor market intelligence

**What changed:**
- Competitor agent gained automated polling: probes a competitor's pricing and hours on a configurable schedule, emits `COMPETITOR_SIGNAL` events with price delta, hours delta, and a threat level.
- New `signal_engine.py` converts raw competitor observations into typed signals that feed the forecaster's demand adjustments.
- `probe.py` drives the synthetic competitor phone-research call (uses the voice pipeline). `web_scraper.py` does a simulated web price check.
- `schemas.py` formalises `CompetitorSnapshot`, `CompetitorSignal`, `PricePoint`.
- Forecaster tests expanded to cover competitor-signal-driven demand adjustment paths.
- Contract tests for Track A updated (`test_contract_a.py`).

**Key files:**
```
track_a/competitors/providers/probe.py         new — voice-based research call
track_a/competitors/providers/web_scraper.py   new — simulated web check
track_a/competitors/schemas.py                 new — snapshot / signal types
track_a/competitors/signal_engine.py           new — observation → signal
track_a/agents/competitor.py                   polling loop + signal emit
track_a/agents/forecaster.py                   competitor-signal demand adjustment
track_a/tests/test_competitor.py               expanded
track_a/tests/test_contract_a.py               new assertions
track_a/tests/test_forecaster.py               competitor-path coverage
```

---

## `b1970f8` — 2026-06-23 — Track B merge into main

**What changed:**
- `track_b/` merged wholesale onto `main`. Track B is the Inventory & Procurement system.
- Three agents wired: `InventoryLedger` (FIFO depletion, receipts, expiry/waste signals), `InventoryOptimizer` (reorders, menu toggles, expiry-to-promo), `MarketSpectator` (supplier price monitoring, negotiation calls).
- `Procurement` service manages PO lifecycle and delivery scheduling.
- `ApprovalHandlers` acts on `APPROVAL_RESOLVED` for PO and promo approval types.
- `MockForecaster` drives Track B standalone (`DEMO_MODE=track_b`).
- `AppContext` now carries `ctx.tracks["track_b"]` in addition to `ctx.track_a`. Both are bootstrapped in `_bootstrap()` with independent try/except guards.
- New REST endpoint: `POST /api/market/negotiate { supplier_id, ingredient_id }`.
- All signal types fan out through the orchestrator via a new subscription loop in `_bootstrap()`.
- `BaseAgent.log_event()` now returns the `EventLog` row (non-breaking).
- Four new frontend panels: `InventoryDashboard`, `ExpiryView`, `SupplierEditor`, `ActivityLog` — mounted under Track B tabs in the dashboard.
- Full pytest suite: ledger, optimizer, market, procurement, approval, contract.

**`DEMO_MODE` values:**

| Value | Track A | Track B | MockForecaster |
|-------|---------|---------|----------------|
| `combined` (default) | real | real | off |
| `track_a` | real | off | off |
| `track_b` | off | real | on |

**Key files:**
```
track_b/agents/ledger.py           InventoryLedger
track_b/agents/optimizer.py        InventoryOptimizer
track_b/agents/market_spectator.py MarketSpectator
track_b/procurement/procurement.py PO lifecycle
track_b/approval/handlers.py       approval resolution
track_b/mocks/mock_forecaster.py   standalone driver
track_b/tests/                     full pytest suite
frontend/src/track_b/InventoryDashboard.tsx
frontend/src/track_b/ExpiryView.tsx
frontend/src/track_b/SupplierEditor.tsx
frontend/src/track_b/ActivityLog.tsx
frontend/src/track_b/index.ts      TRACK_B_PANELS registry
core/api.py                        ctx.tracks bootstrap + negotiate endpoint
core/agent_base.py                 log_event returns row
pytest.ini                         testpaths += track_b/tests
```

**New REST endpoint:**
- `POST /api/market/negotiate` — body: `{ supplier_id, ingredient_id }`; returns 503 if Track B not wired.

---

## `dbc2a9f` — 2026-06-23 — Multi-page routing + POS Monitor + customer menu

**What changed:**
- App split from a single page into four addressable routes. `react-router-dom` added. `main.tsx` wraps in `<BrowserRouter>`.
- `OperatorLayout.tsx` owns the single WS lifecycle (`wsClient.connect()`) and sim/weather hydration. Navigating between operator routes does not reconnect the socket.
- `/menu` (`MenuPage`) lazy-loaded outside `OperatorLayout` — never opens the WS.
- `PosMonitor.tsx` added as the "Operations" tab. Two data sources: windowed backend stats (`usePosStats` → `GET /api/pos/stats`) and a live ring-buffer order ticker (`usePosStream` → `order_created` WS events). Buffer cap 120, flushed to React state every 500ms.
- Window selector: Today / Last hour / Last 6 hours / This week. Stats computed server-side.
- `MenuPage` displays active and inactive items grouped by category. Polls `GET /api/menu` every 10s, no WS.

**Key files:**
```
frontend/src/App.tsx                     route table
frontend/src/routes/OperatorLayout.tsx   WS lifecycle + nav
frontend/src/routes/ConsolePage.tsx      / — ControlShell + panels
frontend/src/routes/ControlPage.tsx      /control — controls only
frontend/src/routes/PanelsPage.tsx       /panels — panels only
frontend/src/shell/ControlShell.tsx      shared ControlBar + drawers
frontend/src/pos/usePosStream.ts         live order ring buffer
frontend/src/pos/usePosStats.ts          windowed backend stats
frontend/src/pos/PosMonitor.tsx          the monitor view
frontend/src/menu/MenuPage.tsx           public customer menu (lazy)
```

---

## `7ccea81` — 2026-06-23 — POS read APIs + clock reset handling

**What changed:**
- `GET /api/orders?limit=N&since=<sim_time>` — newest-first order + lines backfill. Lines fetched in one `IN` query. Shared serializers with the WS `order_created` payload via `core/formatter.py`.
- `GET /api/pos/stats?since=<sim_time>` — returns `{orders, revenue, lines, voided_lines, channel_split, top_items, buckets}`. `since` clamped `>= 0` so seeded negative-`sim_time` history is excluded.
- `pos_reset` WS event emitted on: stop→play transition, restart, reseed. Frontend buffer clears on this event.
- `SimClock.stop()`/`restart()` call `Orchestrator.reset_schedules()`, which re-anchors interval trigger `next_due` after a clock rewind — without this, no orders generate after pressing play following a stop.
- `POSSimulator.tick()` detects a backward `sim_time` jump and resets the arrival schedule.

**Key files:**
```
core/api.py           GET /api/orders, GET /api/pos/stats, pos_reset broadcasts
core/formatter.py     module-level order_to_dict / line_to_dict
core/clock.py         stop()/restart() → reset_schedules(); active_seed_id in current_state()
core/orchestrator.py  reset_schedules() — re-anchors interval triggers
core/pos_simulator.py backward sim_time guard
```

---

## `dbac28c` — 2026-06-22 — Voice context + schema validation + POS robustness

**What changed:**
- Voice extraction given richer context: current menu items, on-hand inventory levels, and staff roster injected into the system prompt.
- Pydantic schema validation expanded across voice extraction response shapes — malformed LLM output is caught and surfaced as a structured error rather than crashing.
- POS simulator made robust to missing or zero dish-mix weights (falls back to equal-weight uniform sampling).
- `ForecastDashboard.tsx` gained a batch-decision breakdown panel.
- New tests: `test_api_session_lifecycle.py`, `test_pos_formatter.py`, expanded `test_voice.py` and `test_forecaster.py`.

**Key files:**
```
core/voice.py                              richer extraction context
track_a/agents/forecaster.py              schema validation + batch breakdown
track_a/forecast_jobs.py                  validation guards
frontend/src/track_a/ForecastDashboard.tsx batch breakdown panel
tests/test_api_session_lifecycle.py        new
tests/test_pos_formatter.py               new
```

---

## `54e0256` — 2026-06-20 — Async ForecastJobRunner

**What changed:**
- `ForecastJobRunner` in `track_a/forecast_jobs.py` decouples forecast runs from the request thread. Jobs enqueue and run in a background thread pool; the HTTP response returns a job ID immediately.
- Job states: `queued → running → done | failed`. WS events: `forecast_job_queued`, `forecast_job_done`, `forecast_job_failed`.
- `POST /api/track-a/forecast/run` now enqueues rather than blocking.
- Auto-mode: `POST /api/track-a/forecast/auto { enabled: bool }` — self-schedules runs on the orchestrator tick when on.
- Forecaster substantially reworked: full deterministic algorithm with weather, competitor-signal, staff-coverage, and batch-decision logic.

**Key files:**
```
track_a/forecast_jobs.py               ForecastJobRunner + job model
track_a/agents/forecaster.py           full deterministic algorithm
core/api.py                            async forecast endpoints
frontend/src/track_a/useTrackAData.ts  job polling
frontend/src/types.ts                  ForecastJob type
```

---

## `7663319` — 2026-06-20 — Forecast trace + LLM sampling params

**What changed:**
- Each forecast run now records a `ForecastTrace` row (full decision log: inputs, adjustments, per-dish outputs, batch decisions, reasoning). Exposed via `GET /api/track-a/forecast/trace/<job_id>`.
- `ForecastAdjustment` rows written per-dish per-run with factor, source, and confidence.
- LLM sampling params (`temperature`, `top_p`, `max_tokens`) exposed in `core/config.py` and threaded through all LLM call sites.
- Competitor agent wired to use the LLM path for research call summarisation.
- `frontend/src/track_a/types.ts` formalised with `ForecastTrace`, `ForecastAdjustment`, `ForecastOverride`, `CompetitorSignal`.

**Key files:**
```
track_a/agents/forecaster.py    trace + adjustment recording
track_a/agents/competitor.py    LLM summarisation wired
core/api.py                     GET /api/track-a/forecast/trace/<id>
core/config.py                  LLM sampling params
core/models.py                  ForecastTrace, ForecastAdjustment, ForecastOverride
frontend/src/track_a/types.ts   formalised Track A types
```

---

## `305de9d` — 2026-06-20 — Track A merge into main

**What changed:**
- `track_a/` merged wholesale onto `main`. Track A is the Demand & Sensing system.
- Four agents: `Forecaster`, `CompetitorIntelligence`, `ReviewAgent`, `StaffAgent`.
- `MockInventory` provides a lightweight inventory stub for Track A tests.
- pytest suite: competitor, contract, forecaster, review, staff.
- Frontend panels: `ForecastDashboard`, `CompetitorPanel`, `ReviewPanel`, `StaffPanel`.
- `AppContext.track_a` dict populated by `bootstrap_track_a()`.
- `BaseAgent` in `core/agent_base.py` provides shared signal subscription, event logging, approval request, and deferred action helpers.

**Track A REST surface (all under `/api/track-a/`):**
- `POST /forecast/run`, `/finalize`, `/optimize`; `GET /forecast/jobs/<id>`, `/trace/<id>`
- `GET /forecast/auto`; `POST /forecast/auto`
- `POST /competitors/research`, `/probe`; `GET /competitors`
- `POST /reviews/process`; `GET /reviews`
- `POST /staff/call-in-sick`; `GET /staff`

**Key files:**
```
track_a/agents/forecaster.py      Forecaster
track_a/agents/competitor.py      CompetitorIntelligence
track_a/agents/review.py          ReviewAgent
track_a/agents/staff.py           StaffAgent
track_a/mocks/mock_inventory.py   test stub
track_a/tests/                    full pytest suite
frontend/src/track_a/ForecastDashboard.tsx
frontend/src/track_a/CompetitorPanel.tsx
frontend/src/track_a/ReviewPanel.tsx
frontend/src/track_a/StaffPanel.tsx
core/agent_base.py                BaseAgent
```

---

## `dcfa1a9` — 2026-06-20 — Deferred agent action refactor

**What changed:**
- Agent LLM calls were blocking the signal bus dispatch loop. `BaseAgent` gained `defer(fn)` to submit slow work to a thread-pool executor, keeping signal handlers synchronous.
- Competitor, Forecaster, Review, and Staff agents updated to use `defer()` for all LLM paths.

**Key files:**
```
core/agent_base.py           defer() added
track_a/agents/competitor.py LLM path deferred
track_a/agents/forecaster.py LLM path deferred
track_a/agents/review.py     LLM path deferred
track_a/agents/staff.py      LLM path deferred
```

---

## `3608b27` — 2026-06-18 — Voice pipeline + forecasting engine + LLM integration

**What changed:**
- Full deterministic forecasting engine: baseline demand from historical attach rates, weather multiplier, event multiplier, competitor-price delta, staff coverage ratio, promotion effect, and time-of-day curve.
- LLM integration for: voice fact injection into forecast context, natural-language explanation generation, and LLM-driven batch decision override.
- `core/voice.py` pipeline finalised: transcribe → extract structured facts → validate schema → inject into agent context.
- `ForecastDashboard.tsx` shows deterministic output and LLM explanation side by side.

**Key files:**
```
track_a/agents/forecaster.py          deterministic algorithm + LLM overlay
core/voice.py                         extract → validate → inject pipeline
frontend/src/track_a/ForecastDashboard.tsx
frontend/src/track_a/types.ts         forecast + voice types
scripts/llm_smoke.py                  connectivity smoke test
```

---

## `cd8caae` — 2026-06-17 — Major agent revamp

**What changed:**
- All four Track A agents substantially rewritten from stub/heuristic to real decision logic.
- `Forecaster`: multi-factor demand model, per-dish confidence scores, batch-size calculation.
- `CompetitorIntelligence`: pricing and hours gap detection, threat-level classification, signal emission.
- `ReviewAgent`: sentiment scoring, per-dish demand modifier, time-decay weighting.
- `StaffAgent`: coverage gap detection, service-quality impact, shift-recommendation generation.

**Key files:**
```
track_a/agents/competitor.py     pricing/hours gap logic
track_a/agents/forecaster.py     multi-factor model
track_a/agents/review.py         sentiment + time-decay
track_a/agents/staff.py          coverage gap logic
track_a/mocks/mock_inventory.py  full interface coverage
```

---

## Before `cd8caae` — Initial scaffold

The scaffold (`core/`) was built before Track A work began. It is fully implemented and stable. See `docs/00_ARCHITECTURE.md` for the authoritative spec.

| Module | What it does |
|--------|-------------|
| `core/clock.py` | `SimClock` state machine: stopped / running / paused / call_frozen. Drives orchestrator ticks at configurable real-time speed. |
| `core/orchestrator.py` | Tick loop, interval trigger scheduling, signal fan-out. |
| `core/signal_bus.py` | Typed pub/sub bus. All inter-agent communication goes through here — never direct calls. |
| `core/models.py` | 38 SQLAlchemy tables covering every entity in the simulation. |
| `core/pos_simulator.py` | Poisson-arrival order generator. Reads channel mix, daypart curve, and dish-mix weights from `SimPosConfig`; emits `order_created` WS events. |
| `core/voice.py` | Voice call pipeline: LLM transcription → fact extraction → structured payload. |
| `core/weather.py` | Weather state with override support; emits `weather_updated`. |
| `core/seeder.py` | Loads restaurant presets from `seeds/`; seeds all 38 tables in a transaction. |
| `core/llm.py` | Thin Anthropic SDK wrapper; all LLM calls go through here. |
| `core/api.py` | FastAPI app: REST endpoints + WebSocket hub (`/ws`). Generic CRUD factory `_register_crud` covers most entity tables with one dict entry each. |
| `core/db.py` | SQLite + SQLAlchemy session management; `DB_LOCK` for write serialisation. |
