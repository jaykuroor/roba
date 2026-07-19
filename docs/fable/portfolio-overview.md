# Portfolio Overview — all restaurants on one screen

Status: **implemented** except two fields (below).

## What exists

`GET /admin/api/overview` returns one card per instance plus the ranked action
queue. Per card, sourced by fan-out to the child:

| Field | Source (child endpoint) | Notes |
|---|---|---|
| `status` | derived | `manager.derive_status` — see rules below |
| `sales_today` / `orders_today` | `/api/pos/stats?since=<day start>` | day start = `floor(sim_time / 86400) * 86400` |
| `forecast_today` | `/api/ops/snapshot` | Σ `revenue_estimate` over active dishes |
| `staff_present` / `staff_total` / `absent` | `/api/ops/snapshot` | attendance-derived, `status != "present"` = absent |
| `stock_risks` | `/api/ops/snapshot` (low stock) + `/api/track-b/procurement/warnings` (uncoverable) | |
| `pending_approvals` | `/api/approvals?status=pending` | count; details in the queue/approvals sections |
| `orders_waiting`, `ticket_time_min` | — | **not implemented**, returned as `null` |
| `safety_issues` | — | **not implemented**, returned as `null` |

### Status rules (`manager.derive_status`, tested in `tests/test_manager.py`)

- **offline** — child `/api/health` unreachable.
- **critical** — any of: uncoverable ingredient warning; depleted ingredient;
  unstaffed station; pending approval with critical-class urgency
  (`critical` / `uncoverable`).
- **warning** — any of: below-safety-stock ingredient; any pending approval;
  any absent staff (still covered).
- **normal** — otherwise.

## Not implemented — guidance

### Orders waiting + ticket time

Roba has no kitchen ticket lifecycle: `Order`/`OrderLine` rows are created by
the POS simulator already "done" — nothing models *waiting → cooking → served*.
To implement:

1. Add a ticket state to orders (e.g. `Order.kitchen_status:
   queued|cooking|served` + `served_at` sim-time), advanced by a kitchen agent
   or a simple service-rate model in the POS simulator (staffing-dependent:
   fewer present staff → slower drain → backlog grows).
2. Expose `queued_count` and rolling `avg_ticket_minutes` — cheapest is to add
   them to `/api/ops/snapshot` (see the "add a child endpoint" guideline in
   [manager-dashboard.md](manager-dashboard.md)).
3. In `manager._instance_overview`, replace the hardcoded
   `"orders_waiting": None, "ticket_time_min": None` with the snapshot fields,
   and extend `derive_status` (backlog above a threshold → warning/critical).
   The UI (`frontend/src/admin/AdminPage.tsx`, "Tickets / safety" metric)
   already has the slot.

### Safety / equipment issues

Closest existing machinery: `core/kitchen_tasks.py` already generates
temperature/safety checklist tasks and escalates failures to approvals with
`urgency="high"`. To surface them:

1. Add equipment as a first-class object only if scenarios need it (a
   `ScenarioEvent` breaking an "oven" that disables its station's dishes is the
   natural fit — see `core/scenarios.py`).
2. Expose open safety tasks / failed outcomes (`/api/kitchen/tasks/board`
   filtered to `category in ("temp","safety")`, overdue or failed) as a count
   in `/api/ops/snapshot`.
3. Wire into the card + `derive_status` (failed food-safety check should be
   **critical**) and into incidents (see [incidents.md](incidents.md)).
