# Portfolio Overview — all restaurants on one screen

Status: **implemented**.

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
| `safety_issues` | `/api/ops/snapshot` | count of open temp/safety check failures |
| `task_compliance` | `/api/ops/snapshot` | today's checklist counts + on-time `rate` |

### Status rules (`manager.derive_status`, tested in `tests/test_manager.py`)

- **offline** — child `/api/health` unreachable.
- **critical** — any of: uncoverable ingredient warning; a failed food-safety
  check; kitchen backlog at or above `BACKLOG_CRIT`; depleted ingredient;
  unstaffed station; pending approval with critical-class urgency
  (`critical` / `uncoverable`).
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

## Food-safety checks

A `temp`/`safety` checklist task reported not done — or left past its final
overdue tier — emits `SignalType.FOOD_SAFETY_CHECK` and lands on the card as
`safety_issues` (a red `N safety` chip in the Tickets metric), a `critical`
status, a `safety` row in the priority queue, and a `food_safety_checks`
incident. See [kitchen-task-notices.md](kitchen-task-notices.md).

Equipment failures are **not** part of this: no equipment model exists, and the
agreed design is a `ScenarioEvent` disabling a station for a sim-window rather
than an `Equipment` table (see `docs/fable/progress.md` Phase 3).
