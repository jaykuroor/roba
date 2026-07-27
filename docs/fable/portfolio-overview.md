# Portfolio Overview — all restaurants on one screen

Status: **implemented** except `safety_issues` (below).

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
| `orders_waiting`, `ticket_time_min` | `/api/ops/snapshot` | `queued_count` and `avg_ticket_minutes`, from the kitchen ticket lifecycle |
| `safety_issues` | — | **not implemented**, returned as `null` |

### Status rules (`manager.derive_status`, tested in `tests/test_manager.py`)

- **offline** — child `/api/health` unreachable.
- **critical** — any of: uncoverable ingredient warning; kitchen backlog at or
  above `BACKLOG_CRIT`; depleted ingredient; unstaffed station; pending approval
  with critical-class urgency (`critical` / `uncoverable`).
- **warning** — any of: below-safety-stock ingredient; any pending approval;
  kitchen backlog at or above `BACKLOG_WARN`; any absent staff (still covered).
- **normal** — otherwise.

## Kitchen ticket lifecycle

Orders are no longer born done. `Order.kitchen_status` is
`queued | cooking | served` with a `served_at` sim-time, and the POS tick drains
the pass oldest-first at `cooks_present × config.KITCHEN_TICKETS_PER_COOK_PER_HOUR`
— so a sick cook visibly grows a backlog and a full brigade clears it. Presence
comes from the same `core/availability.py::_staff_available` rules that unstaff a
station.

`/api/ops/snapshot` exposes `queued_count`, `cooking_count` and
`avg_ticket_minutes` (rolling over the last sim-hour); the card reads the first
and last as `orders_waiting` / `ticket_time_min`.

`SimSettings.kitchen_ticket_mode` (Controls → POS) switches between `lifecycle`
(default) and `instant`, which restores the old born-served behaviour and
flushes any backlog left on the pass.

## Not implemented — guidance

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
