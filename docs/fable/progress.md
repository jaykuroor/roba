# Fable features — implementation progress

The living checklist for the multi-restaurant manager features specified in this folder.
Every other doc in `docs/fable/` describes *what a feature is* and *how it should be built*;
this doc tracks *what is actually built* and *what a cold session needs to know before
touching it*.

Last full audit of the codebase against the docs: **2026-07-27**.

---

## 1. How to use this doc

**Checking things off**

- `- [ ]` → `- [x]` only when **backend + UI + tests are all in** and both suites pass
  (§6 Verification). A backend endpoint with no UI and no test is not done — leave it
  unchecked and add a sub-bullet saying what landed.
- Append the commit sha to the item you check off: `- [x] … — a1b2c3d`.
- A phase is done when every item under it is checked **and** its "Done when" line is
  demonstrably true in the running app.

**Changing things**

- **Never delete an item.** If it turns out to be unnecessary or wrong, strike it
  (`- [ ] ~~item~~ — dropped: reason`) so the next reader knows the decision was made
  deliberately rather than forgotten.
- Newly discovered work goes under the phase that found it, marked `(added <date>)`.
- If work moves between phases, say so on the item — phases are sized to fit one session,
  and silently growing one breaks that.

**Adding knowledge**

- A gotcha that would cost the next implementor an hour goes in **§4 Infrastructure notes**
  with a `file:line`, *not* buried in a phase item. §4 is the section a cold session reads
  first.
- A bug you found but did not fix goes in **§5 Known pre-existing issues**.
- If you discover a `docs/fable/*.md` statement that is no longer true, fix that doc **and**
  record the drift in §4 — the docs are the spec, so silent drift is how the spec rots.
- Keep the `Status:` line at the top of each feature doc in sync with §2 below.

---

## 2. Status at a glance

| Doc | Status | Pending work | Phase |
|---|---|---|---|
| [manager-dashboard.md](manager-dashboard.md) | ✅ implemented | — | — |
| [approvals.md](approvals.md) | ✅ implemented | — | — |
| [kitchen-task-notices.md](kitchen-task-notices.md) | 🟡 partial | task compliance in the manager overview; failed temp/safety → signal | 2 |
| [portfolio-overview.md](portfolio-overview.md) | 🟡 partial | `orders_waiting`, `ticket_time_min`, `safety_issues` — all hardcoded `None` (`manager.py:584,624`) | 1, 2 |
| [priority-action-queue.md](priority-action-queue.md) | 🟡 partial | € impact in `build_issues`; deadlines for non-approval issues | 3 |
| [incidents.md](incidents.md) | 🟠 partial | 3 missing detectors; incidents not first-class objects | 2, 3, 4 |
| [catchup.md](catchup.md) | 🟠 infra only | LLM summary, expand-for-detail, history drawer, merge, auto-capture | 5, 6 |
| [daily-summary.md](daily-summary.md) | 🟡 partial | LLM prose briefing, tomorrow-specific risk, end-of-day archive | 6 |

**Already implemented and verified** (do not rebuild): manager registry / child spawn /
HTTP+WS proxy, `/admin/api/overview` with the ranked `actions` queue, `/admin/api/approvals`
with pass-through resolve, `/admin/api/incidents` (derived, 4 of 7 categories),
`/admin/api/summary` (numeric briefing), catch-up **capture** infrastructure, kitchen-task
escalating notices, and the `/admin` UI with its 4 tabs.

---

## 3. Phases

Six phases, each sized for a single session to complete end to end. **Order matters**:
Phase 3 needs Phase 1's queue model, Phase 4 builds the manager DB that Phase 6 reuses, and
Phase 6 needs Phase 5's summarizer.

```
1 ──► 3 ──► 4 ──► 6
2 ──►       5 ──►┘
```

Phases 1 and 2 are independent of each other and can run in either order. Phase 5 needs only
the existing capture infrastructure, so it can run any time after Phase 1.

### Design decisions already made (do not relitigate)

1. **Kitchen tickets: full lifecycle**, `queued → cooking → served`, drained at a
   staffing-dependent rate so short-staffing visibly grows a backlog — **and toggleable from
   Controls** (lifecycle vs. today's instantly-closed orders), defaulting to lifecycle.
2. **Incidents become first-class** — manager-side SQLite with ack / resolve / history.
3. **Equipment: scenario event only** — no `Equipment` table. A `ScenarioEvent` kind
   disables a station for a sim-window, reusing the availability machinery staffing uses.
4. **UI ships in every phase** — each phase must be independently demoable.

---

### Phase 1 — Kitchen ticket lifecycle + the Tickets metric

Unblocks `orders_waiting` / `ticket_time_min` ([portfolio-overview.md](portfolio-overview.md))
and the `order_backlog` detector in Phase 3. Today `core/pos_simulator.py:354` creates every
order `status="closed"` — nothing models waiting → cooking → served.

**Child**

- [ ] `core/models.py` `Order`: add `kitchen_status` (`queued|cooking|served`, default
      `served`) and `served_at` (Float, nullable), plus two `_migrate_schema` lines.
- [ ] `core/models.py` `SimSettings`: add `kitchen_ticket_mode` (`lifecycle|instant`,
      default `lifecycle`) + migration + `PosBody` field/validator — follow
      `availability_oos_mode` end to end (see §4).
- [ ] `core/pos_simulator.py` `_persist()` (`:360`): stamp `queued` in lifecycle mode, or
      `served` + `served_at = sim_time` in instant mode (today's behaviour, `:348-357`).
- [ ] `core/pos_simulator.py` `tick()` (`:380`): drain step — capacity per tick =
      `cooks_present × KITCHEN_TICKETS_PER_COOK_PER_HOUR × Δsim/3600`, advancing
      oldest-first `queued → cooking → served`. Reuse
      `core/availability.py::_staff_available` (`:120-140`) for presence rather than
      re-reading `Attendance`.
- [ ] `core/config.py`: `KITCHEN_TICKETS_PER_COOK_PER_HOUR` (env-overridable, default ~12) —
      this is the calibration knob, leave it tunable; plus `BACKLOG_WARN` / `BACKLOG_CRIT`.
- [ ] `core/ops_snapshot.py`: add `queued_count`, `cooking_count`, `avg_ticket_minutes`
      (rolling over orders served in the last sim-hour) to the returned dict (`:222-234`).

**Manager + UI**

- [ ] `manager._instance_overview`: fill `orders_waiting` / `ticket_time_min` from the
      snapshot in **both** branches (`:584` offline/busy, `:624` online) — they must stay in
      step or an offline card reports stale numbers.
- [ ] `manager.derive_status` (`:98`): backlog above threshold → `warning`, well above →
      `critical`.
- [ ] `frontend/src/admin/AdminPage.tsx:193-203`: `grid-cols-3` → `grid-cols-4`, new
      `Metric label="Tickets"`. **The slot does not exist yet** despite what
      portfolio-overview.md claims (see §4).
- [ ] Controls toggle for `kitchen_ticket_mode` in `frontend/src/shell/control/` — POS
      settings live in `PosMixPanel.tsx`.

**Tests**

- [ ] `tests/test_pos_ticket_lifecycle.py`: backlog grows with cooks absent and drains when
      present; `instant` mode reproduces today's born-closed behaviour; `served_at` is set
      exactly once.
- [ ] `tests/test_manager.py`: backlog `derive_status` cases.
- [ ] `frontend/src/admin/__tests__/AdminPage.test.tsx` rendering a stubbed overview —
      **this is a new pattern**, no test renders `AdminPage` with data today (§4).

**Done when** a restaurant with a sick cook shows a rising ticket count on its admin card,
and flipping the Controls toggle to `instant` returns it to zero.

---

### Phase 2 — Food-safety detector, task compliance, `safety_issues`

Closes [kitchen-task-notices.md](kitchen-task-notices.md) §Future work and the
`food_safety_checks` incident category.

- [ ] New `SignalType.FOOD_SAFETY_CHECK` — **all four registration points** (§4), with
      `groups` an existing agent already subscribes to, or every emit dead-letters.
- [ ] `core/kitchen_tasks.py`: emit it when a `temp`/`safety` task is reported `not_done`
      (`set_outcome`, `:310-319`) or escalates past the final overdue tier (`reconcile`,
      `:331`). Note there is **no distinct "failed" status** — `not_done` + `severity` *is*
      the failure signal (`status ∈ pending|done|not_done|skipped`, and `skipped` is
      unreachable).
- [ ] `core/ops_snapshot.py`: add `safety_issues` (open temp/safety failures) and
      `task_compliance` — reuse `kitchen_tasks.task_board()["counts"]` (`:144-178`) rather
      than recounting.
- [ ] `manager.py`: `safety_issues` on the card (both branches); `derive_status` →
      `critical` on a failed food-safety check; `SIGNAL_TO_INCIDENT` (`:236`) entry; a
      phrase in `_GROUP_PHRASES` (`:247`); drop `food_safety_checks` from
      `unavailable_categories` (`:673`); a `safety` issue kind in `build_issues` (`:130`).
- [ ] UI: safety count in the Phase 1 metric slot; compliance rate on the card or briefing.
- [ ] Tests: emission on a not-done temp task; `merge_incidents` phrasing; `derive_status`.

**Done when** reporting a temperature check not-done turns that restaurant's card critical
and the incident appears under Incidents with human phrasing (not a raw status string).

---

### Phase 3 — `order_backlog` + `equipment_failure`, and priority-queue enrichment

Completes every incident category (`unavailable_categories` → `[]`) and finishes
[priority-action-queue.md](priority-action-queue.md). **Depends on Phase 1.**

- [ ] `SignalType.ORDER_BACKLOG` — emitted from the POS tick when queue depth or ticket time
      crosses the Phase 1 thresholds.
- [ ] **First, settle the `_fire_scenario_events` duplicate-firing bug** (§5) — otherwise
      the scenario event below is consumed before it ever dispatches.
- [ ] `SignalType.EQUIPMENT_FAILURE` + an `equipment_failure` handler in the
      `core/scenarios.py:200-214` dispatch table: payload `{station, until_sim, label}` → a
      blocking `MenuToggle` with a new `RC_EQUIPMENT_DOWN` reason code beside
      `RC_STATION_UNSTAFFED` (`core/availability.py:46`), cleared by
      `recompute_availability` once `until_sim` passes.
- [ ] `manager.SIGNAL_TO_INCIDENT` + phrases for both; `unavailable_categories` → `[]`.
- [ ] `manager.build_issues`: monetary `impact` from `revenue_estimate` on the snapshot's
      dishes (so "station unstaffed" prices its blocked dishes).
- [ ] `manager.build_issues`: `deadline_sim` for stock issues from the plan's `order_date` /
      `latest_safe_arrival` — both already on `/api/track-b/procurement/plan` items
      (`core/api.py:3158,3174`). Requires adding the plan to the `_instance_overview`
      fan-out.
- [ ] UI: € at stake on action rows. The two new incident categories render with no UI
      change.
- [ ] Tests: backlog signal thresholds; equipment event disables the station's dishes and
      re-enables at `until_sim`; `build_issues` impact/deadline.

**Done when** `/admin/api/incidents` returns `"unavailable_categories": []` and action rows
show € at stake.

---

### Phase 4 — Incidents as first-class objects

[incidents.md](incidents.md) §"Incidents as first-class objects". Today an incident vanishes
when its source signal expires — no ack, no assign, no history.

- [ ] `dbdata/manager.db` via **stdlib `sqlite3`** (manager has no ORM dependency and should
      not gain one): `incidents(incident_id, instance_id, category, summary, opened_at,
      status, acked_by, resolved_at, source_signal_id)`.
- [ ] A **pure** `reconcile_incidents(derived, stored)` in `manager.py`, unit-tested like
      `merge_incidents`, called from `GET /admin/api/incidents`: opens rows for new derived
      incidents, auto-resolves rows whose source disappeared. Stable key =
      `(instance_id, category, summary)` — `merge_incidents` already dedupes by summary.
- [ ] `POST /admin/api/incidents/{id}/ack`, `POST .../resolve`,
      `GET /admin/api/incidents/history?instance_id=&since=`.
- [ ] UI: Acknowledge / Resolve on incident rows, acknowledged styling, a history view.
- [ ] Tests: reconcile open / auto-resolve / re-open; ack survives a manager restart.

**Done when** acknowledging an incident, restarting the manager, and reloading `/admin`
still shows it acknowledged.

---

### Phase 5 — Catch-up summarizer + history and expand-for-detail

[catchup.md](catchup.md) §"Readable structured summary" and §"Viewing previous catch-ups".
Capture infrastructure already exists; `summary` is a `null` slot at `manager.py:802`.

- [ ] Manager gains an LLM path: `from core import llm, config`;
      `LLMProvider.complete(..., json_schema=..., model=config.GEMINI_REASONER_MODEL)`.
      Importing `core` as a *library* is fine — the "HTTP is the contract" rule is about
      child *data* (§4).
- [ ] Pure `bucket_events(events)` grouping a capture's rows by **`category`** — **not
      `event_type`, which does not exist** (§4) — into procurement / kitchen / staffing /
      reviews / promos. One prompt per bucket, never over the raw firehose.
- [ ] Each summary bullet references its source event ids, so "click to expand" resolves
      against `GET .../catchups/{n}`.
- [ ] `POST /admin/api/instances/{id}/catchups/{n}/summarize` — writes into the capture
      file's `summary` slot, idempotent overwrite (re-summarizing with a better prompt is
      expected).
- [ ] **Canned-fallback guard**: if `note == CANNED_NOTE`, store and render an *error*, not
      prose (§4 — this has bitten the project twice).
- [ ] UI: per-card catch-up history drawer (`GET .../catchups` is already served and unused
      by the frontend), bullets expanding to raw events.
- [ ] Tests: bucketing is deterministic; a canned response yields an error, never a summary.

**Done when** a catch-up produces a readable per-subsystem summary whose bullets expand to
the underlying events — and a deliberately bad `GEMINI_MODEL` shows an error instead of
canned text.

---

### Phase 6 — Merge, auto-capture, LLM briefing, EOD archive, next-day risk

Finishes [catchup.md](catchup.md) and [daily-summary.md](daily-summary.md).
**Depends on Phases 4 (manager DB) and 5 (summarizer).**

- [ ] `POST /admin/api/instances/{id}/catchups/merge {from,to}` — captures are contiguous
      (`since_sim(n+1) == until_sim(n)`), so merging is concatenating `events` and reusing
      the Phase 5 summarizer. **Never delete the originals** — they are the audit trail.
- [ ] Manager background task polling each child's `sim.day_number` (already returned by
      `/api/health`), last-seen day persisted in the Phase 4 SQLite. **There is no
      day-rollover hook in the sim** (§4) — the manager must detect it.
- [ ] On rollover: create a catch-up **and** snapshot `/admin/api/summary` to
      `dbdata/summaries/<id>/day-NNN.json` (copy the catch-up marker machinery).
- [ ] LLM prose briefing over the existing `daily_summary` JSON — on demand or once per
      sim-day, **never per poll** (the summary endpoint is polled every 30s). Provider
      errors surfaced in the UI.
- [ ] `next_day_risks` enriched by joining `/api/track-a/forecast/horizons` with plan
      `covers_until`, inside `manager.daily_summary` — not the frontend.
- [ ] UI: briefing prose panel + archive day picker + merge control in the history drawer.
- [ ] Tests: merge window contiguity; rollover fires exactly once per sim-day.

**Done when** a sim-day rollover creates a capture and archives that day's summary with
nobody pressing a button.

> This phase is the most divisible. If it runs long, ship merge + auto-capture first and
> leave the briefing/archive for a follow-up session — say so here when you do.

---

## 4. Infrastructure notes

The non-obvious facts that shape this work. **Read this section before starting any phase.**

### Doc drift (the spec is wrong here)

- **`EventLog` has no `event_type` column — it is `category`** (`core/models.py:1087-1099`).
  [catchup.md](catchup.md) §2 says `event_type`. Values in use include `optimizer`,
  `po_placed`, `po_delivered`, `waste`, `forecast`, `attendance`, `menu_toggle`, `call`,
  `promo_activated`, `receipt`, `scenario`.
- **The "Tickets / safety" UI slot does not exist.**
  [portfolio-overview.md](portfolio-overview.md) says the UI "already has the slot";
  `AdminPage.tsx:193-203` is a hardcoded `grid-cols-3` of Sales / Orders / Staff.
  `orders_waiting` / `ticket_time_min` / `safety_issues` are typed in
  `useAdminData.ts:26-28` and rendered nowhere.

### Simulator

- **There is no day-rollover hook anywhere.** `day_number` is a derived field recomputed on
  every write (`core/clock.py:187`); per-day rows are lazily materialized on first read of
  the new day (`kitchen_tasks.ensure_tasks_for_day`). Anything that must happen "once per
  sim-day" has to detect the boundary itself.
- **Sim toggles have an established pattern**: a column on the `SimSettings` singleton
  (`core/models.py:1040-1053`) → a field + validator on `PosBody` (`core/api.py:568-582`) →
  `GET/PATCH /api/sim/pos` (`:725,736`) → a control in `frontend/src/shell/control/`.
  Follow `availability_oos_mode` end to end as the template.
- **Additive schema changes need two edits**: the model column **and** an
  `ALTER TABLE … ADD COLUMN` line in `_migrate_schema` (`core/api.py:210-228`).
  `create_all()` cannot alter existing tables and `demo.db` is committed to the repo.
- **Adding a `SignalType` touches four places or it breaks**: the enum
  (`core/signals.py:26-68`), a `<Name>Payload` model (`:273-632`), `SIGNAL_PAYLOADS`
  (`:636`), and `SIGNAL_REGISTRY` (`:76`). Omitting the **registry** entry is a hard
  `KeyError` at `core/bus.py:168`; omitting the **payload** silently skips validation. Give
  the type `groups` an existing agent subscribes to, or every emit dead-letters
  (`core/orchestrator.py:240-247`).

### Manager

- **`manager.py` imports no `core.*` module, has no DB and no LLM.** Its rule — "do not
  reach into child DBs, the HTTP surface is the contract" — is about *child data*. Importing
  `core.llm` / `core.config` as a *library* is fine, and is the intended path for Phases 5-6.
- **Manager persistence should be stdlib `sqlite3`.** The manager has no ORM dependency;
  keep it that way. All state lives under `STATE_DIR` (`MANAGER_STATE_DIR`, default
  `./dbdata`), so tests can relocate the whole footprint.
- **Card fields are assembled in two places** — `_instance_overview` has an offline/busy
  branch (`:584`) and an online branch (`:624`). Every new field must be added to both.
- **Aggregation logic must stay pure and unit-tested** (`derive_status`, `build_issues`,
  `rank_issues`, `merge_incidents`); async endpoints only fetch and delegate.

### LLM

- **Silent canned fallback is a known trap** (`core/config.py:128-131`). A bad model id 404s
  and `LLMProvider` returns a canned response with `note == CANNED_NOTE`
  (`core/llm.py:42`) instead of raising. **Every new LLM call site must check that marker
  and surface an error — never render canned text as a summary.** A second cause is output
  starvation: a thinking model with too low a `max_tokens` truncates into the same canned
  path, so size the cap generously (`core/calls.py:735` uses 3000 for extraction).
- Only `gemini-2.5-*` models exist for this project. `GEMINI_REASONER_MODEL`
  (`config.py:262`, default `gemini-2.5-pro`) bypasses `LLMProvider` in existing call sites
  (`core/reasoner.py`) but works fine passed as `model=` to `complete()`.

### Tests

- **Run bare `.venv/bin/pytest`, not just `make test`** — `Makefile:32` runs
  `pytest tests/ track_b/tests/`, omitting `track_a/tests` even though `pytest.ini`
  includes it.
- **`tests/test_api_session_lifecycle.py:27,32` calls `db.reset_db()` against the real
  `config.DB_PATH`** — running the Python suite **wipes the committed `demo.db`**. Back it
  up first or point `DB_PATH` at a scratch file.
- **`tests/test_manager.py` has zero fan-out coverage** — pure functions only, no
  `TestClient`, no mocking of any kind. Phase 1 establishes the pattern (monkeypatch
  `manager._get` and `manager.registry.instances`); later phases follow it.
- **No frontend test renders `AdminPage` with data** — `src/admin/__tests__/` contains only
  `routing.test.tsx`. Phase 1 establishes that pattern too. Vitest + jsdom +
  `@testing-library/react`; shared helpers in `frontend/src/test/` (`wsMock.ts`,
  `relativePaths.ts`).
- Frontend typecheck is folded into `npm run build` (`tsc -b`) — there is no separate
  `typecheck` script, and `make test` does not run the build.
- Tailwind classes must be **statically named** (`frontend/tailwind.config.ts` comment) —
  hence the `Record<string,string>` lookup-map pattern at `AdminPage.tsx:34-39`. No
  dynamically generated class strings.

---

## 5. Known pre-existing issues

Found during the 2026-07-27 audit. None of these are caused by the fable work; they are
recorded because they will bite an implementor.

- **`Orchestrator._fire_scenario_events` fires nothing but consumes everything.**
  `core/orchestrator.py:383-401` queries every `ScenarioEvent` with `fired == 0 AND
  at_sim_time <= now` — with **no `is_active` filter and no dispatch** — and sets
  `fired = 1` (verified 2026-07-27). It runs every tick right after the `scenario_engine`
  interval trigger, so events belonging to an *inactive* scenario are burned by the
  orchestrator without `ScenarioEngine.tick` (`core/scenarios.py:146`) ever dispatching them.
  So the seeded-but-inactive Friday Rush burns its six events as sim time passes them, and
  activating the scenario afterwards fires nothing (`_set_active` does not reset `fired`).
  **Must be settled before Phase 3**, which drives equipment failure through a scenario
  event.
- **`make test` skips `track_a/tests`** (`Makefile:32`) while `pytest.ini` includes it.
- **The Python suite destroys `demo.db`** — see §4 Tests.
- `KitchenTask.notified_manager`'s model comment says "bool 0/1" (`core/models.py:414`) but
  the code uses it as a **tier counter** 0..3 (`kitchen_tasks.py:317,349,358-360`). The
  comment is stale.
- `KitchenTask.status` declares `skipped` (`core/models.py:409`) and the board counts it,
  but nothing can ever set it — `set_outcome` accepts only `done|not_done|pending`.
- `ApprovalsHub._resolve` (`core/approvals.py:185-187`) resolves a row in **any** status, so
  an already-approved or expired approval can be re-approved, re-emitting
  `APPROVAL_RESOLVED` and re-firing its reactors. `update()` guards on `pending`; `_resolve`
  does not.
- `/api/events` and `/api/waste` are unbounded full-table dumps with no pagination, and
  `?since=` on `/api/events` is **inclusive** (`>=`, `core/api.py:1269`) so a naive poller
  re-fetches boundary rows. The catch-up capture already compensates by filtering `> since`.
- `core/ops_snapshot.py:68-72` reads forecasts from only the newest 500 rows, silently
  yielding qty 0 for any menu item outside that window.
- `core/ops_snapshot.py:22` `_TODAY_SECONDS` is dead code; `core/availability.py:185-195`
  `changed_ingredient_ids` / `changed_station_ids` are vestigial parameters, accepted and
  ignored.

---

## 6. Verification

Per phase, before checking anything off:

```bash
cp demo.db /tmp/demo.db.bak          # the Python suite wipes it — see §4
.venv/bin/pytest -q                  # NOT `make test` — that skips track_a/tests
cd frontend && npm run test -- --run && npm run build   # build == typecheck (tsc -b)
cp /tmp/demo.db.bak demo.db
```

End-to-end, with two instances running:

```bash
make manager                 # :8100
cd frontend && npm run dev   # :5173
```

Open `https://localhost:5173/admin` and confirm the phase's "Done when" line:

| Phase | Visible outcome |
|---|---|
| 1 | A sick cook grows the Tickets count; the Controls toggle switches the lifecycle off |
| 2 | A not-done temp check turns the card critical and appears under Incidents |
| 3 | `unavailable_categories` is `[]`; action rows show € at stake |
| 4 | Acknowledge persists across a manager restart |
| 5 | Catch-up bullets expand to raw events; a bad `GEMINI_MODEL` shows an error, not prose |
| 6 | A sim-day rollover creates a capture and archives the summary unattended |
