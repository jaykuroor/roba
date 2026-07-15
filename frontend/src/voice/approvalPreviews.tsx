/**
 * Shared, human-readable previews for approval_requests — used by both the
 * Manager Desk ops board (ApprovalItem) and the shell ApprovalInbox panel so
 * the two surfaces stay consistent. No raw JSON: each type renders the few
 * fields a manager needs to glance and decide. Types without a structured
 * payload (promo, outbound_call, …) carry everything in title/summary, so
 * their preview is intentionally empty.
 */

import type { ReactNode } from "react";
import type { ApprovalRequest } from "../types";

function fmtSimClock(secs: number): string {
  const h = Math.floor(secs / 3600) % 24;
  const m = Math.floor((secs % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function Field({ label, value, accent }: { label: string; value: ReactNode; accent?: boolean }) {
  return (
    <div className="flex gap-2">
      <span className="w-28 shrink-0 text-text/45">{label}</span>
      <span className={accent ? "font-medium text-accent" : "text-text"}>{value}</span>
    </div>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="mt-2 space-y-1.5 rounded-md border border-muted/60 bg-primary/40 p-2 text-xs text-text/70">
      {children}
    </div>
  );
}

function PurchaseOrderPreview({ payload }: { payload: Record<string, unknown> }) {
  const lines = Array.isArray(payload.lines) ? (payload.lines as Record<string, unknown>[]) : [];
  return (
    <Shell>
      <div className="flex items-baseline justify-between">
        <span className="text-text/45">PO #{String(payload.po_id ?? "—")}</span>
        <span className="text-sm font-semibold text-text">€{Number(payload.total ?? 0).toFixed(2)}</span>
      </div>
      {lines.length > 0 && (
        <table className="w-full text-[11px]">
          <thead>
            <tr className="border-b border-muted/60 text-text/45">
              <th className="py-0.5 text-left font-normal">Item #</th>
              <th className="py-0.5 text-right font-normal">Qty</th>
              <th className="py-0.5 text-right font-normal">Unit</th>
              <th className="py-0.5 text-right font-normal">Line</th>
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
    </Shell>
  );
}

function isScalar(v: unknown): v is string | number | boolean {
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean";
}

function ForecastPreview({ payload }: { payload: Record<string, unknown> }) {
  const evidence = payload.evidence;
  // Render evidence as readable label/value lines (scalars only), never JSON.
  const evidenceRows =
    evidence && typeof evidence === "object" && !Array.isArray(evidence)
      ? Object.entries(evidence as Record<string, unknown>).filter(([, v]) => isScalar(v))
      : [];
  return (
    <Shell>
      <Field label="Item" value={String(payload.item_name ?? payload.menu_item_id ?? "Unknown")} />
      <Field label="Operation" value={String(payload.operation ?? "set_target").replaceAll("_", " ")} />
      <Field label="Final qty" value={String(payload.qty ?? "0")} />
      <Field label="Confidence" value={`${Math.round(Number(payload.confidence ?? 0) * 100)}%`} />
      {evidenceRows.length > 0 && (
        <div className="mt-1 border-t border-muted/40 pt-1.5">
          {evidenceRows.map(([k, v]) => (
            <Field key={k} label={k.replaceAll("_", " ")} value={String(v)} />
          ))}
        </div>
      )}
    </Shell>
  );
}

function BatchPreview({ payload }: { payload: Record<string, unknown> }) {
  const benefit = payload.projected_benefit;
  const reasoning = payload.reasoning;
  const demand = payload.forecast_demand;
  return (
    <Shell>
      {payload.dish_name != null && <Field label="Dish" value={String(payload.dish_name)} />}
      {payload.target_window_start != null && (
        <Field label="Target window" value={fmtSimClock(Number(payload.target_window_start))} />
      )}
      {payload.target_qty != null && <Field label="Suggested qty" value={`${payload.target_qty} portions`} />}
      {demand != null && <Field label="Forecast demand" value={`${Number(demand).toFixed(1)} portions`} />}
      {benefit != null && <Field label="Benefit" value={String(benefit)} accent />}
      {reasoning != null && (
        <div className="mt-1 rounded bg-muted/30 p-2 leading-relaxed text-text/70">{String(reasoning)}</div>
      )}
    </Shell>
  );
}

/**
 * Structured preview for an approval, dispatched by type. Returns null for
 * types whose payload is just IDs (promo, outbound_call, kitchen_task, …) —
 * their title/summary already say everything.
 */
export function ApprovalPreview({ approval }: { approval: ApprovalRequest }) {
  const payload = approval.payload;
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Record<string, unknown>;
  switch (approval.type) {
    case "purchase_order":
      return <PurchaseOrderPreview payload={p} />;
    case "forecast_override_proposal":
      return <ForecastPreview payload={p} />;
    case "batch":
      return <BatchPreview payload={p} />;
    default:
      return null;
  }
}
