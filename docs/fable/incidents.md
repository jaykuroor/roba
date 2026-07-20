# Incident Management

Status: **partial** — derived incidents implemented; three categories have no
detector yet; incidents are not yet first-class objects.

## What exists

`GET /admin/api/incidents` derives open incidents from each child's live state:

| Category | Derived from |
|---|---|
| `staff_no_show` | live signals `STAFF_AVAILABILITY`, `STAFF_COVERAGE` |
| `stockout` | live signals `STOCKOUT_RISK`, `LOW_STOCK`, `INGREDIENT_UNCOVERABLE` |
| `food_safety` | live signal `EXPIRY_RISK` (expiring stock ≈ food-safety exposure) |
| `supplier_delay` | procurement plan items with status `at_risk` / `uncoverable` (`/api/track-b/procurement/plan`) |

The mapping is `manager.SIGNAL_TO_INCIDENT`. The response also lists
`unavailable_categories` (`equipment_failure`, `order_backlog`,
`food_safety_checks`) so the UI can label the gap honestly instead of showing
a silent empty list.

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
- Staff signals stay per-person/station: *"Marco is sick."*, *"Station Grill
  has no qualified cover — its dishes are blocked."* Routine
  covered-station broadcasts are dropped.

Each merged row carries `count` and `names`, so the UI can show a ×N badge.
New phrasings belong in `_GROUP_PHRASES` / `_DELAY_PHRASES` — never in the
frontend.

## Not implemented — guidance

**Missing detectors** (each is: create the child-side signal, then add one line
to `SIGNAL_TO_INCIDENT`):

- `equipment_failure` — no equipment model exists. Recommended: a
  `ScenarioEvent` kind that disables a station for a sim-window (reuses the
  station→dishes machinery that staffing already uses), emitting a new
  `SignalType.EQUIPMENT_FAILURE`. See portfolio-overview.md §safety.
- `order_backlog` — needs the kitchen ticket lifecycle
  (portfolio-overview.md §orders waiting). Emit a signal when queue depth or
  ticket time crosses a threshold; the POS already computes velocity anomalies
  (`VELOCITY_ANOMALY_PCT` in `core/config.py`) which could serve as a v0
  "unusual order volume" incident with zero new modeling.
- `food_safety_checks` — failed/overdue temp+safety kitchen tasks
  (`core/kitchen_tasks.py`, category `temp`/`safety`) are the real detector;
  today they only surface as approvals. Emit a signal on failure.

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
