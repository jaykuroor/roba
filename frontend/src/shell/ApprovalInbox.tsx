import { useEffect, useState } from "react";
import { Check, X } from "lucide-react";
import { apiGet, apiPost } from "../api";
import { actions, useApprovals } from "../store";
import type { ApprovalRequest } from "../types";

// Shared shell panel (00 §23): lists pending approval_requests and posts
// approve/reject. It only renders server state and posts operator actions —
// the store + WS keep the list in sync (approval_created adds, approval_resolved
// removes); this component just does the initial load and the action POSTs.

function PayloadPreview({ payload }: { payload: unknown }) {
  if (payload == null) return null;
  let text: string;
  try {
    text = JSON.stringify(payload, null, 2);
  } catch {
    text = String(payload);
  }
  return (
    <pre className="mt-2 max-h-32 overflow-auto rounded bg-primary/60 p-2 text-xs text-text/70">
      {text}
    </pre>
  );
}

function PurchaseOrderPreview({ payload }: { payload: unknown }) {
  if (!payload || typeof payload !== "object") return <PayloadPreview payload={payload} />;
  const po = payload as Record<string, unknown>;
  const lines = Array.isArray(po.lines) ? (po.lines as Record<string, unknown>[]) : [];
  return (
    <div className="mt-2 rounded-md border border-muted bg-primary/50 p-2 text-xs text-text/70">
      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        <span className="text-text/45">PO #</span>
        <span className="font-medium text-text">{String(po.po_id ?? "—")}</span>
        <span className="text-text/45">Total</span>
        <span className="font-medium text-text">€{Number(po.total ?? 0).toFixed(2)}</span>
      </div>
      {lines.length > 0 && (
        <table className="mt-2 w-full text-[11px]">
          <thead>
            <tr className="border-b border-muted/60 text-text/45">
              <th className="py-0.5 text-left font-normal">Ingredient #</th>
              <th className="py-0.5 text-right font-normal">Qty</th>
              <th className="py-0.5 text-right font-normal">Unit price</th>
              <th className="py-0.5 text-right font-normal">Line total</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l, i) => {
              const qty = Number(l.qty ?? 0);
              const price = Number(l.unit_price ?? 0);
              return (
                <tr key={i} className="border-b border-muted/30 last:border-0">
                  <td className="py-0.5">{String(l.ingredient_id ?? "?")}</td>
                  <td className="py-0.5 text-right">{qty.toLocaleString()}</td>
                  <td className="py-0.5 text-right">€{price.toFixed(4)}</td>
                  <td className="py-0.5 text-right">€{(qty * price).toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ForecastProposalPreview({ payload }: { payload: unknown }) {
  if (!payload || typeof payload !== "object") return <PayloadPreview payload={payload} />;
  const proposal = payload as Record<string, unknown>;
  return (
    <div className="mt-2 rounded-md border border-muted bg-primary/50 p-2 text-xs text-text/70">
      <div className="grid grid-cols-2 gap-2">
        <span className="text-text/45">Item</span>
        <span className="font-medium text-text">{String(proposal.item_name ?? proposal.menu_item_id ?? "Unknown")}</span>
        <span className="text-text/45">Operation</span>
        <span>{String(proposal.operation ?? "set_target").replaceAll("_", " ")}</span>
        <span className="text-text/45">Final qty</span>
        <span>{String(proposal.qty ?? "0")}</span>
        <span className="text-text/45">Confidence</span>
        <span>{Math.round(Number(proposal.confidence ?? 0) * 100)}%</span>
      </div>
      {proposal.evidence ? (
        <pre className="mt-2 max-h-20 overflow-auto rounded bg-primary/60 p-2 text-[11px] text-text/60">
          {JSON.stringify(proposal.evidence, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

function ApprovalCard({ approval }: { approval: ApprovalRequest }) {
  const [busy, setBusy] = useState(false);

  async function resolve(decision: "approve" | "reject") {
    setBusy(true);
    try {
      await apiPost(`/api/approvals/${approval.id}/${decision}`);
      // The approval_resolved WS event removes it; remove eagerly for snappiness.
      actions.removeApproval(approval.id);
    } catch {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-muted bg-surface p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <span className="inline-block rounded bg-muted px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-text/70">
            {approval.type}
          </span>
          <h3 className="mt-1 text-sm font-semibold text-text">{approval.title}</h3>
        </div>
      </div>
      <p className="mt-1 text-sm text-text/70">{approval.summary}</p>
      {approval.type === "forecast_override_proposal" ? (
        <ForecastProposalPreview payload={approval.payload} />
      ) : approval.type === "purchase_order" ? (
        <PurchaseOrderPreview payload={approval.payload} />
      ) : (
        <PayloadPreview payload={approval.payload} />
      )}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => resolve("approve")}
          className="flex flex-1 items-center justify-center gap-1 rounded-md bg-success px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          <Check size={16} /> Approve
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => resolve("reject")}
          className="flex flex-1 items-center justify-center gap-1 rounded-md bg-danger px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          <X size={16} /> Reject
        </button>
      </div>
    </div>
  );
}

export function ApprovalInbox({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const approvals = useApprovals();

  useEffect(() => {
    let cancelled = false;
    apiGet<ApprovalRequest[]>("/api/approvals?status=pending")
      .then((rows) => {
        if (!cancelled) actions.setApprovals(rows);
      })
      .catch(() => {
        /* offline / not seeded yet — the WS keeps it current */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/40"
          onClick={onClose}
          aria-hidden
        />
      )}
      <aside
        className={
          "fixed right-0 top-0 z-50 flex h-full w-96 max-w-full flex-col bg-primary shadow-2xl transition-transform duration-200 " +
          (open ? "translate-x-0" : "translate-x-full")
        }
        aria-hidden={!open}
      >
        <header className="flex items-center justify-between border-b border-muted px-4 py-3">
          <h2 className="text-base font-semibold text-text">
            Approval Inbox
            <span className="ml-2 rounded-full bg-accent px-2 py-0.5 text-xs text-white">
              {approvals.length}
            </span>
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-text/60 hover:bg-muted hover:text-text"
            aria-label="Close approval inbox"
          >
            <X size={18} />
          </button>
        </header>
        <div className="flex flex-1 flex-col gap-3 overflow-auto p-4">
          {approvals.length === 0 ? (
            <p className="mt-8 text-center text-sm text-text/40">
              No pending approvals.
            </p>
          ) : (
            approvals.map((approval) => (
              <ApprovalCard key={approval.id} approval={approval} />
            ))
          )}
        </div>
      </aside>
    </>
  );
}
