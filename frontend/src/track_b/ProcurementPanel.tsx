// ProcurementPanel — planned → ordered → delivered history.
//
// Three stacked sections:
//   Planned   — supplier cards grouped by (order_date, delivery_date) pair
//   Ordered   — active POs (approved/placed), collapsible per-PO receipt
//   Delivered — log rows, each with a chevron receipt dropdown
//
// Consumes `manager_change`, `signal_emitted(SUPPLIER_PRICE_UPDATE)`, and
// `procurement_plan_updated` WS events to refresh.

import { useEffect, useState, useCallback } from "react";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  ChevronDown,
  Loader2,
  Package,
  RefreshCw,
} from "lucide-react";
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

/** Convert a sim-time seconds value to "Day N (Dow)" — mirrors server _day_label. */
function dayLabel(simS: number | null | undefined): string {
  if (simS == null) return "—";
  const day = Math.floor(simS / 86400);
  const dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day % 7];
  return `Day ${day} (${dow})`;
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

/** Group items by a string key, preserving insertion order. */
function groupBy<T>(items: T[], key: (item: T) => string): Map<string, T[]> {
  const map = new Map<string, T[]>();
  for (const item of items) {
    const k = key(item);
    if (!map.has(k)) map.set(k, []);
    map.get(k)!.push(item);
  }
  return map;
}

// ─── SupplierPlanCard ─────────────────────────────────────────────────────────
//
// One card per supplier in the forward plan.  Inside, items are sub-grouped by
// shared (order_date_label, delivery_date_label) pair so the date arrow appears
// once per group rather than repeating on every row.

function SupplierPlanCard({
  supplierName,
  items,
}: {
  supplierName: string;
  items: PlanItem[];
}) {
  const [open, setOpen] = useState(true);
  const total = items.reduce(
    (sum, i) => sum + (i.unit_price ?? 0) * i.qty,
    0
  );
  const byDatePair = groupBy(
    items,
    (i) => `${i.order_date_label}|${i.delivery_date_label}`
  );

  return (
    <div className="rounded-xl border border-muted/50 bg-surface/70 overflow-hidden">
      {/* Card header */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2.5 hover:bg-muted/20 text-left"
      >
        <div className="flex items-center gap-2">
          <Package size={13} className="text-text/40 shrink-0" />
          <span className="text-sm font-semibold text-text">{supplierName}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-text/50 tabular-nums">
            €{total.toFixed(2)}
          </span>
          {open ? (
            <ChevronUp size={14} className="text-text/40" />
          ) : (
            <ChevronDown size={14} className="text-text/40" />
          )}
        </div>
      </button>

      {/* Card body — one sub-block per date pair */}
      {open && (
        <div className="border-t border-muted/30 divide-y divide-muted/20">
          {Array.from(byDatePair.entries()).map(([dateKey, dateItems]) => {
            const first = dateItems[0];
            return (
              <div key={dateKey} className="px-3 py-2 space-y-1">
                {/* Date header */}
                <div className="flex items-center gap-1 text-xs text-text/50">
                  <span>{first.order_date_label}</span>
                  <span className="text-text/30">→</span>
                  <span>{first.delivery_date_label}</span>
                </div>
                {/* Items */}
                {dateItems.map((item) => (
                  <div
                    key={item.id}
                    className="flex items-center justify-between"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-sm text-text truncate">
                        {item.ingredient_name}
                      </span>
                      <span className="text-xs text-text/50 tabular-nums whitespace-nowrap">
                        {item.qty} {item.unit}
                      </span>
                      {item.status === "at_risk" && (
                        <span
                          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-warning/20 text-warning whitespace-nowrap"
                          title="Lead time too short to guarantee delivery before shortage"
                        >
                          <AlertTriangle size={10} />
                          at-risk
                        </span>
                      )}
                    </div>
                    {item.unit_price != null && (
                      <span className="shrink-0 text-xs text-text/50 tabular-nums ml-3">
                        €{(item.unit_price * item.qty).toFixed(2)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      )}
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
  const bySupplier = groupBy(items, (i) => i.supplier_name);

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
        <div className="space-y-2">
          {Array.from(bySupplier.entries()).map(([supplier, supplierItems]) => (
            <SupplierPlanCard
              key={supplier}
              supplierName={supplier}
              items={supplierItems}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Active PO card (Ordered section) ────────────────────────────────────────
//
// Collapsible receipt; collapsed by default since these are already placed.

function ActivePoCard({ po }: { po: PurchaseOrder }) {
  const [open, setOpen] = useState(false);
  const orderLabel = dayLabel(po.created_at);
  const deliveryLabel = dayLabel(po.expected_delivery);

  return (
    <div className="rounded-xl border border-muted/50 bg-surface/70 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2.5 hover:bg-muted/20 text-left"
      >
        <div className="flex items-center gap-2 min-w-0">
          <Package size={13} className="text-text/40 shrink-0" />
          <span className="text-sm font-semibold text-text truncate">
            {po.supplier_name}
          </span>
          {statusBadge(po.status)}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className="text-right">
            <div className="text-xs text-text/50 tabular-nums">
              {po.total_cost != null ? `€${po.total_cost.toFixed(2)}` : ""}
            </div>
            <div className="text-[10px] text-text/40">
              {orderLabel} → {deliveryLabel}
            </div>
          </div>
          {open ? (
            <ChevronUp size={14} className="text-text/40" />
          ) : (
            <ChevronDown size={14} className="text-text/40" />
          )}
        </div>
      </button>

      {open && po.lines && po.lines.length > 0 && (
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

// ─── Ordered section ──────────────────────────────────────────────────────────

function OrderedSection({
  orders,
  loading,
}: {
  orders: PurchaseOrder[];
  loading: boolean;
}) {
  return (
    <div className="space-y-2">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        Ordered ({orders.length})
        {loading && <Loader2 size={11} className="animate-spin" />}
      </h3>
      {orders.length === 0 && !loading ? (
        <p className="text-sm text-text/30">No in-flight orders.</p>
      ) : (
        <div className="space-y-2">
          {orders.map((po) => (
            <ActivePoCard key={po.id} po={po} />
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Delivered history row ────────────────────────────────────────────────────
//
// One compact line per delivered PO; chevron expands the full receipt.

function DeliveredPoRow({ po }: { po: PurchaseOrder }) {
  const [open, setOpen] = useState(false);
  const orderLabel = dayLabel(po.created_at);
  const deliveryLabel = dayLabel(po.expected_delivery);

  return (
    <div className="rounded-lg border border-muted/40 bg-surface/50 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-1.5 hover:bg-muted/20 text-left"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-medium text-text truncate">
            {po.supplier_name}
          </span>
          <span className="text-[10px] text-text/40 whitespace-nowrap">
            {orderLabel} → {deliveryLabel}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-xs text-text/50 tabular-nums">
            {po.total_cost != null ? `€${po.total_cost.toFixed(2)}` : ""}
          </span>
          {open ? (
            <ChevronUp size={12} className="text-text/30" />
          ) : (
            <ChevronDown size={12} className="text-text/30" />
          )}
        </div>
      </button>

      {open && po.lines && po.lines.length > 0 && (
        <table className="w-full text-xs border-t border-muted/30">
          <tbody>
            {po.lines.map((ln) => (
              <tr key={ln.id} className="border-t border-muted/20 first:border-0">
                <td className="px-3 py-1 text-text/70">{ln.ingredient_name}</td>
                <td className="px-3 py-1 text-right tabular-nums text-text/60">
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

// ─── Delivered section ────────────────────────────────────────────────────────

function DeliveredSection({
  orders,
  loading,
  page,
  hasMore,
  onPrev,
  onNext,
}: {
  orders: PurchaseOrder[];
  loading: boolean;
  page: number;
  hasMore: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <div className="space-y-2">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        Delivered history
        {loading && <Loader2 size={11} className="animate-spin" />}
      </h3>
      {orders.length === 0 && !loading ? (
        <p className="text-sm text-text/30">No delivery history yet.</p>
      ) : (
        <div className="space-y-1">
          {orders.map((po) => (
            <DeliveredPoRow key={po.id} po={po} />
          ))}
        </div>
      )}
      {(page > 0 || hasMore) && (
        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            onClick={onPrev}
            disabled={page === 0}
            className="rounded p-1 text-text/50 hover:text-text disabled:opacity-30"
          >
            <ChevronLeft size={14} />
          </button>
          <span className="text-xs text-text/40">Page {page + 1}</span>
          <button
            onClick={onNext}
            disabled={!hasMore}
            className="rounded p-1 text-text/50 hover:text-text disabled:opacity-30"
          >
            <ChevronRight size={14} />
          </button>
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

      {/* Planned — supplier cards */}
      <PlanSection
        items={planItems}
        loading={planLoading}
        emptyMsg="No forward plan yet — click Re-plan to generate one."
        onReplan={() => void replan()}
        replanning={replanning}
      />

      {/* Ordered (approved/placed) */}
      <OrderedSection orders={ordered} loading={loading} />

      {/* Delivered — compact log with receipt dropdowns */}
      <DeliveredSection
        orders={delivered}
        loading={loading}
        page={deliveredPage}
        hasMore={deliveredHasMore}
        onPrev={() => setDeliveredPage((p) => Math.max(0, p - 1))}
        onNext={() => setDeliveredPage((p) => p + 1)}
      />
    </div>
  );
}
