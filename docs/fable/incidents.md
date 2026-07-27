# Incident Management

Status: **implemented**.

## What exists

`GET /admin/api/incidents` derives open incidents from each child's live state:

| Category | Derived from |
|---|---|
| `staff_no_show` | live signals `STAFF_AVAILABILITY`, `STAFF_COVERAGE` |
| `stockout` | live signals `STOCKOUT_RISK`, `LOW_STOCK`, `INGREDIENT_UNCOVERABLE` |
| `food_safety` | live signal `EXPIRY_RISK` (expiring stock ≈ food-safety exposure) |
| `food_safety_checks` | live signal `FOOD_SAFETY_CHECK` — a `temp`/`safety` checklist task reported not done, or past its final overdue tier |
| `supplier_delay` | procurement plan items with status `at_risk` / `uncoverable` (`/api/track-b/procurement/plan`) |
| `order_backlog` | live signal `ORDER_BACKLOG` — the kitchen pass past `BACKLOG_WARN` / `BACKLOG_CRIT` tickets |
| `equipment_failure` | live signal `EQUIPMENT_FAILURE` — a station out of service for a scenario-driven window |

The mapping is `manager.SIGNAL_TO_INCIDENT`. `unavailable_categories` is now
`[]` — every category has a detector — but the field is kept in the response so
the contract is stable and a future gap can be declared honestly again. The UI
hides the label when the list is empty.

### Merging + human phrasing (`manager.merge_incidents`)

Raw statuses never reach the manager. Similar items are batched
deterministically and phrased as one sentence (tested in
`tests/test_manager.py`):

- Plan items group by **(supplier, status)**: *"Basil, Romaine Lettuce and
  Tomato from GreenFarm Produce may arrive late — a one-day delivery slip
  would leave the kitchen short."* (`at_risk`) / *"…cannot be delivered in
  time by any supplier — dishes will run short."* (`uncoverable`).
- Stock/expiry signals group by **type** with ingredient names resolved via
  the child's `/api/ingredients`: *"Running low on Garlic and Pasta (at or
  below safety stock)."*, *"Basil close to expiry — use first or discard."*
- Food-safety checks stay **one row per check** (`_SAFETY_PHRASES`): *"Record
  walk-in fridge & freezer temperatures — the kitchen reported this not done:
  walk-in reading 9C."* Batching them would discard the reason, which is the
  incident. Note `_GROUP_PHRASES` is ingredient-batching only — a key added
  there renders `{names}` as `"some items"` for a non-ingredient signal.
- The backlog reads in kitchen language, not counters: *"24 tickets are backed
  up on the pass with nobody cooking — guests are waiting and orders will start
  walking."* (`_BACKLOG_PHRASES`). Equipment names what broke and what it costs:
  *"Grill oven is out of service — the Grill station's dishes are off the menu
  until it is back."*
- Staff signals stay per-person/station: *"Marco is sick."*, *"Station Grill
  has no qualified cover — its dishes are blocked."* Routine
  covered-station broadcasts are dropped.

Each merged row carries `count` and `names`, so the UI can show a ×N badge.
New phrasings belong in `_GROUP_PHRASES` / `_DELAY_PHRASES` — never in the
frontend.

## Not implemented — guidance

**Detectors are complete.** Each was: create the child-side signal, then add one
line to `SIGNAL_TO_INCIDENT`. Two implementation notes worth keeping:

- `equipment_failure` has **no equipment model** — the live signal *is* the
  outage and its TTL *is* the window, so `recompute_availability` blocks the
  station's dishes with `RC_EQUIPMENT_DOWN` and re-enables them when the signal
  lapses. Authored as a scenario event (`equipment_failure`, payload
  `{station, until_sim | duration_sim_s, label}`).
- `order_backlog` re-emits under a constant `dedup_key` while the queue stays
  deep, so one live row tracks the current depth rather than one signal per
  tick, and the TTL retires it once the pass drains.

## Incidents as first-class objects

Detection stays derived — signals remain the source of truth — but each incident
is now a row in the manager's own store, so it survives its source signal and a
manager restart.

- **Store**: `dbdata/manager.db` via stdlib `sqlite3` (the manager has no ORM and
  should not gain one), table `incidents(incident_id, instance_id, category,
  summary, opened_at, status, acked_by, resolved_at, source_signal_id)`.
  `opened_at` is child sim-time (from the source signal); `resolved_at` is
  wall-clock, because resolving is an operator action rather than a sim event.
- **Reconcile**: `manager.reconcile_incidents(derived, stored)` is pure and unit
  tested. It runs on every `GET /admin/api/incidents` and returns
  `{"open": [...], "resolve": [incident_ids]}`. Key =
  `(instance_id, category, summary)` — `merge_incidents` phrases
  deterministically and dedupes by summary, which is what makes that stable.
  A resolved row is **never revived**: a recurrence opens a new row, so history
  reads as two episodes rather than one flapping row.
- **Actions**: `POST /admin/api/incidents/{id}/ack` (optional `acked_by`),
  `POST /admin/api/incidents/{id}/resolve`, and
  `GET /admin/api/incidents/history?instance_id=&since=` (`since` filters
  `opened_at`, i.e. sim-time).
- **Manual resolve exists because auto-resolve cannot always fire.** A
  `FOOD_SAFETY_CHECK` signal only expires on its 24h TTL — the bus has no
  retract. For that one case the derived set is filtered by the child snapshot's
  still-failing `safety_issues`, so a remediated check drops out and reconcile
  auto-resolves it. Every other category auto-resolves when its signal expires.
