# Priority Action Queue

Status: **implemented**.

## What exists

The `actions` array of `GET /admin/api/overview`: every instance's issues,
built by `manager.build_issues` and ranked by `manager.rank_issues`
(severity `critical > high > medium > low`, then earliest deadline; no deadline
sorts last). Each entry carries `restaurant`, `problem`, `severity`,
`deadline_sim`, `impact`, `recommended_action`, and (for approvals)
`approval_id` so the row can be resolved inline.

Issue sources:

| Kind | Source | Severity | Deadline |
|---|---|---|---|
| `approval` | pending `approval_requests` | mapped from `urgency` (`uncoverable`/`critical` → critical, `high`/`at_risk` → high, else medium) | `created_at + 6h TTL` (mirrors `core.approvals.APPROVAL_TTL_SIM_S`) |
| `stock` | uncoverable warnings | critical | — |
| `stock` | depleted / below-safety inventory | high / medium | — |
| `staff` | unstaffed station | high | — |
| `staff` | absent sole-cover staffer | high | — |

Approve/Reject posts `POST /admin/api/approvals/{instance}/{approval_id}/{decision}`
(a pass-through to the child's approvals hub, so all the child-side reactors —
PO placement, promo activation, outbound calls — fire exactly as if resolved
locally). "Open" is a full-page link to `/<instance_id>`.

## Guidance for extending

- New issue kinds belong in `build_issues` (pure, tested) — never in the UI.
  If the data needs a new child endpoint, follow the fan-out guideline in
  [manager-dashboard.md](manager-dashboard.md).
- `impact` today is descriptive text. A monetary impact estimate is available
  cheaply: `/api/ops/snapshot` dishes carry `revenue_estimate`, so "station
  unstaffed" can price its blocked dishes. Do that inside `build_issues` when
  the queue needs sorting *within* a severity band by € at stake.
- Deadlines for non-approval issues (e.g. "order by 14:00 or delivery slips a
  day") can come from the procurement plan's `latest_safe_arrival` /
  `order_date` fields (`/api/track-b/procurement/plan`).
