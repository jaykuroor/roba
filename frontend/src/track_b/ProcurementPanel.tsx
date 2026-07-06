// ProcurementPanel — planned → ordered → delivered history.
//
// Three stacked sections (planned on top):
//   Planned   — forward procurement plan from /api/track-b/procurement/plan
//   Ordered   — approved / placed POs awaiting delivery
//   Delivered — delivered POs, paginated
//
// Consumes `manager_change`, `signal_emitted(SUPPLIER_PRICE_UPDATE)`, and
// `procurement_plan_updated` WS events to refresh.

import { useEffect, useState, useCallback } from "react";
import { AlertTriangle, ChevronLeft, ChevronRight, Loader2, Package, RefreshCw } from "lucide-react";
import { apiGet, apiPost } from "../api";
import { useProcurementPlanVersion } from "../store";
import { wsClient } from "../ws";
import type { SignalEnvelope } from "../types";

// ─── Types ───────────────────────────────────────────────────────────────────

interface PoLine {
  id: number;
  ingredient_id: number;
  ingredient_name: string;
  qty: number;
  qty_display: string;
  unit: string;
  unit_price: number | null;
  line_total: number | null;
}

interface PurchaseOrder {
  id: number;
  supplier_id: number;
  supplier_name: string;
  status: string;
  created_at: number | null;
  expected_delivery: number | null;
  total_cost: number | null;
  lines: PoLine[];
}

interface PlanItem {
  id: number;
  ingredient_id: number;
  ingredient_name: string;
  supplier_id: number;
  supplier_name: string;
  qty: number;
  unit: string;
  unit_price: number | null;
  order_date: number;
  order_date_label: string;
  delivery_date: number;
  delivery_date_label: string;
  covers_from: number;
  covers_until: number;
  status: "planned" | "at_risk" | "placed" | "superseded";
  reason: string;
}

interface PlanResponse {
  plan_run_id: number | null;
  generated_at: number;
  items: PlanItem[];
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function simTimeLabel(t: number | null | undefined): string {
  if (t == null) return "—";
  const s = Math.round(t);
  if (s < 3600) return `${s}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function statusBadge(status: string) {
  const map: Record<string, string> = {
    proposed: "bg-muted/40 text-text/50",
    approved: "bg-success/20 text-success",
    placed: "bg-accent/20 text-accent",
    delivered: "bg-success/30 text-success",
    cancelled: "bg-danger/20 text-danger",
  };
  return (
    <span
      className={
        "inline-block rounded px-1.5 py-0.5 text-[10px] font-medium " +
        (map[status] ?? "bg-muted/30 text-text/50")
      }
    >
      {status}
    </span>
  );
}

// ─── PO card ─────────────────────────────────────────────────────────────────

function PoCard({ po }: { po: PurchaseOrder }) {
  return (
    <div className="rounded-lg border border-muted/50 bg-surface/60 overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2">
        <div className="flex items-center gap-2">
          <Package size={13} className="text-text/40 shrink-0" />
          <span className="text-sm font-medium text-text">{po.supplier_name}</span>
          {statusBadge(po.status)}
        </div>
        <div className="text-xs text-text/50 tabular-nums">
          {po.total_cost != null ? `€${po.total_cost.toFixed(2)}` : ""}
          {po.expected_delivery != null && (
            <span className="ml-2 text-text/40">
              ETA {simTimeLabel(po.expected_delivery)}
            </span>
          )}
        </div>
      </div>
      {po.lines && po.lines.length > 0 && (
        <table className="w-full text-xs border-t border-muted/30">
          <tbody>
            {po.lines.map((ln) => (
              <tr key={ln.id} className="border-t border-muted/20 first:border-0">
                <td className="px-3 py-1 text-text/70">{ln.ingredient_name}</td>
                <td className="px-3 py-1 text-right tabular-nums text-text/80 font-medium">
                  {ln.qty_display}
                </td>
                <td className="px-3 py-1 text-right text-text/40">
                  {ln.line_total != null ? `€${ln.line_total.toFixed(2)}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ─── Section component ────────────────────────────────────────────────────────

function PoSection({
  label,
  orders,
  emptyMsg,
  loading,
  footer,
}: {
  label: string;
  orders: PurchaseOrder[];
  emptyMsg?: string;
  loading?: boolean;
  footer?: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        {label}
        {loading && <Loader2 size={11} className="animate-spin" />}
      </h3>
      {orders.length === 0 && !loading ? (
        <p className="text-sm text-text/30">{emptyMsg ?? "None."}</p>
      ) : (
        orders.map((po) => <PoCard key={po.id} po={po} />)
      )}
      {footer}
    </div>
  );
}

// ─── Plan item row ────────────────────────────────────────────────────────────

function PlanItemRow({ item }: { item: PlanItem }) {
  return (
    <div className="rounded-lg border border-muted/50 bg-surface/60 overflow-hidden px-3 py-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-text">{item.ingredient_name}</span>
            <span className="text-xs text-text/60 tabular-nums">
              {item.qty} {item.unit}
            </span>
            {item.status === "at_risk" && (
              <span
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-warning/20 text-warning"
                title="Lead time too short to guarantee delivery before shortage"
              >
                <AlertTriangle size={10} />
                at-risk
              </span>
            )}
          </div>
          <div className="mt-0.5 text-xs text-text/50">{item.supplier_name}</div>
          <div className="mt-1 flex items-center gap-1 text-xs text-text/60">
            <span>Order: {item.order_date_label}</span>
            <span className="text-text/30">→</span>
            <span>Deliver: {item.delivery_date_label}</span>
          </div>
          {item.reason && (
            <div className="mt-0.5 truncate text-xs text-text/40">{item.reason}</div>
          )}
        </div>
        {item.unit_price != null && (
          <div className="shrink-0 text-xs tabular-nums text-text/50">
            €{(item.unit_price * item.qty).toFixed(2)}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Plan section ─────────────────────────────────────────────────────────────

function PlanSection({
  items,
  loading,
  emptyMsg,
  onReplan,
  replanning,
}: {
  items: PlanItem[];
  loading: boolean;
  emptyMsg?: string;
  onReplan: () => void;
  replanning: boolean;
}) {
  return (
    <div className="space-y-2">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        Planned ({items.length})
        {loading && <Loader2 size={11} className="animate-spin" />}
        <button
          type="button"
          onClick={onReplan}
          disabled={replanning}
          className="ml-auto flex items-center gap-1 rounded px-2 py-0.5 text-[10px] font-medium bg-muted text-text/60 hover:bg-muted/70 disabled:opacity-50"
          title="Generate a new procurement plan"
        >
          <RefreshCw size={10} className={replanning ? "animate-spin" : undefined} />
          Re-plan
        </button>
      </h3>
      {items.length === 0 && !loading ? (
        <p className="text-sm text-text/30">{emptyMsg ?? "None."}</p>
      ) : (
        <div className="space-y-1.5">
          {items.map((item) => (
            <PlanItemRow key={item.id} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

const PAGE_SIZE = 10;

export function ProcurementPanel() {
  const procurementPlanVersion = useProcurementPlanVersion();
  const [planItems, setPlanItems] = useState<PlanItem[]>([]);
  const [planLoading, setPlanLoading] = useState(false);
  const [replanning, setReplanning] = useState(false);
  const [ordered, setOrdered] = useState<PurchaseOrder[]>([]);
  const [delivered, setDelivered] = useState<PurchaseOrder[]>([]);
  const [deliveredPage, setDeliveredPage] = useState(0);
  const [deliveredHasMore, setDeliveredHasMore] = useState(false);
  const [loading, setLoading] = useState(true);

  const fetchPlan = useCallback(async () => {
    setPlanLoading(true);
    try {
      const res = await apiGet<PlanResponse>("/api/track-b/procurement/plan");
      setPlanItems(res.items);
    } catch {
      /* ignore */
    } finally {
      setPlanLoading(false);
    }
  }, []);

  const fetchOrders = useCallback(async (page: number) => {
    setLoading(true);
    try {
      const [orderedRes, deliveredRes] = await Promise.all([
        apiGet<PurchaseOrder[]>(
          "/api/purchase-orders?status=approved,placed&include_lines=true"
        ),
        apiGet<PurchaseOrder[]>(
          `/api/purchase-orders?status=delivered&include_lines=true&limit=${PAGE_SIZE + 1}&offset=${page * PAGE_SIZE}`
        ),
      ]);
      setOrdered(orderedRes);
      const hasMore = deliveredRes.length > PAGE_SIZE;
      setDelivered(deliveredRes.slice(0, PAGE_SIZE));
      setDeliveredHasMore(hasMore);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, []);

  // Re-fetch plan when procurementPlanVersion bumps (WS event) or on mount.
  useEffect(() => {
    void fetchPlan();
  }, [fetchPlan, procurementPlanVersion]);

  useEffect(() => {
    void fetchOrders(deliveredPage);
  }, [fetchOrders, deliveredPage]);

  useEffect(() => {
    const offSignal = wsClient.on("signal_emitted", (p) => {
      const signal = (p as { signal?: SignalEnvelope }).signal;
      if (signal?.type === "SUPPLIER_PRICE_UPDATE") {
        void fetchPlan();
        void fetchOrders(deliveredPage);
      }
    });
    const offChange = wsClient.on("manager_change", () => {
      void fetchPlan();
      void fetchOrders(deliveredPage);
    });
    const offPlan = wsClient.on("procurement_plan_updated", () => {
      void fetchPlan();
    });
    return () => {
      offSignal();
      offChange();
      offPlan();
    };
  }, [fetchPlan, fetchOrders, deliveredPage]);

  async function replan() {
    setReplanning(true);
    try {
      await apiPost("/api/track-b/procurement/plan/run");
      await fetchPlan();
    } catch {
      /* ignore */
    } finally {
      setReplanning(false);
    }
  }

  return (
    <div
      data-track="b"
      data-panel="Procurement"
      className="flex h-full flex-col gap-5 overflow-auto rounded-lg bg-surface/40 p-4"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-text">Procurement</h2>
        {loading && <Loader2 size={14} className="animate-spin text-text/40" />}
      </div>

      {/* Planned */}
      <PlanSection
        items={planItems}
        loading={planLoading}
        emptyMsg="No forward plan yet — click Re-plan to generate one."
        onReplan={() => void replan()}
        replanning={replanning}
      />

      {/* Ordered (approved/placed) */}
      <PoSection
        label={`Ordered (${ordered.length})`}
        orders={ordered}
        emptyMsg="No in-flight orders."
      />

      {/* Delivered — paginated */}
      <PoSection
        label="Delivered history"
        orders={delivered}
        emptyMsg="No delivery history yet."
        footer={
          (deliveredPage > 0 || deliveredHasMore) && (
            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                onClick={() => setDeliveredPage((p) => Math.max(0, p - 1))}
                disabled={deliveredPage === 0}
                className="rounded p-1 text-text/50 hover:text-text disabled:opacity-30"
              >
                <ChevronLeft size={14} />
              </button>
              <span className="text-xs text-text/40">Page {deliveredPage + 1}</span>
              <button
                onClick={() => setDeliveredPage((p) => p + 1)}
                disabled={!deliveredHasMore}
                className="rounded p-1 text-text/50 hover:text-text disabled:opacity-30"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          )
        }
      />
    </div>
  );
}
