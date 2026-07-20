# Approvals — combined across restaurants

Status: **implemented**.

## Decisions vs notices

Every `approval_requests` row now carries a `kind` (`core.approvals.kind_for`):

- **decision** — a reactor acts on approve/reject, so the choice is real:
  `purchase_order` (place PO), `promo` (activate), `batch` (cook), `outbound_call`
  (dial), `forecast_override_proposal` (apply override). UI: Approve / Reject.
- **notice** — nothing subscribes to the resolution; approve/reject would be
  theater: `kitchen_task` (overdue / done late / not done) and `staff_shift`
  (late / absent). UI: a single **Acknowledge** button (which resolves the row
  as approved, `resolved_by=human` — same endpoint, honest label).

The child inbox (`shell/ApprovalInbox.tsx`) renders two sections ("Needs
decision" / "Notices"); the admin dashboard shows Acknowledge on notice rows in
the queue and approvals tab, plus a decisions/notices filter. **When adding a
new approval type, add it to `NOTICE_TYPES` iff no reactor consumes it** — the
default is decision.

Notices can also **escalate in place**: `ApprovalsHub.update()` edits a pending
row (title/summary/urgency/payload) and broadcasts `approval_updated` (upserted
by the store like `approval_created`). Kitchen-task notices use this — see
[kitchen-task-notices.md](kitchen-task-notices.md) — so one fact is one inbox
entry, never a pile of duplicates.

## What exists

- `GET /admin/api/approvals?status=pending` — fans out to each child's
  `/api/approvals`, tags every row with `instance_id` + `restaurant`
  (the clear per-restaurant indication), newest first. Any child `status`
  filter passes through (`pending|approved|rejected|expired`).
- `POST /admin/api/approvals/{instance_id}/{approval_id}/{approve|reject}` —
  forwards to the child. Resolution semantics are identical to the in-restaurant
  inbox: the child's `ApprovalsHub` emits `APPROVAL_RESOLVED` on its bus and
  its reactors act (see `core/approvals.py`).
- UI: the "Approvals — all restaurants" section of `/admin` with restaurant and
  type filters (`frontend/src/admin/AdminPage.tsx`).

## Guidance

- The admin list **polls** (5s). The children broadcast `approval_created` /
  `approval_resolved` on their `/ws` — if the dashboard ever needs to feel
  live, have the manager hold one WS per child and re-broadcast on a manager
  WS, rather than making the browser open N sockets.
- The per-restaurant inbox (`frontend/src/shell/ApprovalInbox.tsx`) renders
  rich previews via `voice/approvalPreviews.tsx` keyed on `approval.type` +
  `payload`. The admin cards show title/summary only; reuse `ApprovalPreview`
  in `AdminPage` if managers need the full PO/promo detail without opening the
  restaurant (it is pure over the approval row, so it works as-is).
- Expiry is child-owned (6h sim TTL swept by each child's orchestrator);
  the manager never mutates approval state except via approve/reject.
