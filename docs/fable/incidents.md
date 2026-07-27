# Incident Management

Status: **partial** — every category has a detector; incidents are not yet
first-class objects (no ack / assign / resolve / history).

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

**Incidents as first-class objects.** Today an incident disappears when the
underlying signal expires — there is no acknowledge / assign / resolve, and no
history. When that is needed:

1. Keep detection derived (signals stay the source of truth); add a small
   manager-side store (SQLite next to the registry, see manager-dashboard.md)
   holding `{incident_id, instance_id, category, opened_at, status,
   acked_by, resolved_at, source_signal_id}`.
2. A reconcile pass in the manager (on each `/admin/api/incidents` call is
   fine at demo scale) opens rows for new derived incidents and auto-resolves
   rows whose source disappeared.
3. This store is also what the catch-up summarizer should read for "major
   incidents since you left" ([catchup.md](catchup.md)).
