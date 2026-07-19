# Approvals — combined across restaurants

Status: **implemented**.

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
