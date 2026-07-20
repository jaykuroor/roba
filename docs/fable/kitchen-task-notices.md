# Kitchen task tracking — panel + escalating manager notices

Status: **implemented**.

## Tasks panel

`shell/TasksPanel.tsx` — the "Tasks" tab of the operator dashboard (both
`/​<id>` Console and `/​<id>/panels`, wired once in `DashboardView`). Shows
today's checklist from `GET /api/kitchen/tasks/board`, polled every 5s:
count chips (done / late / overdue / not done / pending) and a per-task table
with due time, station, status chip (`done`, `done late +Nm`, `overdue Nm`,
`not done` with the cook's reason, `pending`). Outcomes are recorded by the
cook desk; this panel is the manager's read-only view.

Board rows carry the derivations the panel and future features need:
`late` / `late_min` (done after due) and `overdue_min` (pending past due).

## Escalating notice logic (core/kitchen_tasks.py)

One task ⇒ **one** manager notice (`ApprovalRequest`, type `kitchen_task`,
`kind=notice`, `ref_id=task_id`) that evolves — created late, updated in place
via `ApprovalsHub.update()` + the `approval_updated` WS event:

- Tiers: `config.TASK_OVERDUE_NOTICE_TIERS_MIN` = `[5, 10, 15]` sim-minutes
  past due (env-overridable, e.g. `TASK_OVERDUE_NOTICE_TIERS_MIN=5,10,15`).
- A pending task crossing tier N gets its notice created/updated with
  "escalation N/3" and urgency `normal → high → critical`; **temp/safety
  tasks run one tier hotter** (start at high). `KitchenTask.notified_manager`
  stores the last-notified tier, so the sweep (`reconcile`, run by the
  orchestrator's kitchen-engine tick and on every board read) is idempotent
  per tier.
- Task **done late** past the first tier → the same notice flips to
  "Task done late: … completed N min after its due time" at the
  lateness-matched urgency, and stays pending until acknowledged.
  Done within the 5-min grace → no notice at all.
- Task reported **not done** (with the cook's reason) → the notice becomes
  "Task not done: …" at the cook-graded severity; further escalation stops.

Covered end to end by `core/kitchen_tasks._demo` (runnable:
`python -m core.kitchen_tasks`) and `tests/test_kitchen_task_notices.py`.

## Future work

- Per-restaurant task compliance in the manager overview (done-late /
  not-done rates are one `counts` read away — add them to `/api/ops/snapshot`
  per the fan-out guideline in [manager-dashboard.md](manager-dashboard.md)).
- Failed temp/safety outcomes should also emit a signal so they surface as
  `food_safety` incidents ([incidents.md](incidents.md)).
