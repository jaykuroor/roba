# Priority Action Queue

Status: **implemented**.

## What exists

The `actions` array of `GET /admin/api/overview`: every instance's issues,
built by `manager.build_issues` and ranked by `manager.rank_issues`
(severity `critical > high > medium > low`, then earliest deadline; no deadline
sorts last). Each entry carries `restaurant`, `problem`, `severity`,
`deadline_sim`, `impact`, `impact_eur`, `recommended_action`, and (for
approvals) `approval_id` so the row can be resolved inline.

Issue sources:

| Kind | Source | Severity | Deadline | € at stake |
|---|---|---|---|---|
| `approval` | pending `approval_requests` | mapped from `urgency` (`uncoverable`/`critical` → critical, `high`/`at_risk` → high, else medium) | `created_at + 6h TTL` (mirrors `core.approvals.APPROVAL_TTL_SIM_S`) | — |
| `stock` | uncoverable warnings | critical | plan `order_date` | — |
| `stock` | depleted / below-safety inventory | high / medium | plan `order_date` | — |
| `safety` | failed temp/safety check | critical | — | — |
| `staff` | unstaffed station | high | — | blocked dishes' `revenue_estimate` |
| `staff` | absent sole-cover staffer | high | — | at-risk dishes' `revenue_estimate` |

Approve/Reject posts `POST /admin/api/approvals/{instance}/{approval_id}/{decision}`
(a pass-through to the child's approvals hub, so all the child-side reactors —
PO placement, promo activation, outbound calls — fire exactly as if resolved
locally). "Open" is a full-page link to `/<instance_id>`.

## Guidance for extending

- New issue kinds belong in `build_issues` (pure, tested) — never in the UI.
  If the data needs a new child endpoint, follow the fan-out guideline in
  [manager-dashboard.md](manager-dashboard.md).
- `impact` is descriptive text; **`impact_eur`** is the numeric companion
  (`manager.revenue_at_stake`), summing `revenue_estimate` over the dishes a row
  actually blocks. It is `null` — never `0.0` — when nothing can be attributed,
  so the UI shows no chip rather than claiming the problem is free. Stock rows
  carry no `impact_eur`: the snapshot has no ingredient→dish mapping, and
  attributing every out-of-stock dish to every low ingredient would be a guess.
  Add that mapping to the snapshot before pricing them.
- Deadlines for stock rows come from `manager.stock_deadlines`: the earliest
  `order_date` per ingredient from `/api/track-b/procurement/plan`, falling back
  to `latest_safe_arrival`. The plan is fetched in the `_instance_overview`
  fan-out alongside the snapshot, so it costs no extra round trip.
- Ranking is still severity-then-deadline. Sorting *within* a severity band by
  `impact_eur` is now a one-line change to `rank_issues` if the queue ever wants
  it — deliberately not done, since severity already separates the urgent rows.
