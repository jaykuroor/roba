// ProcurementPanel — planned → ordered → delivered history.
//
// Three stacked sections:
//   Planned   — supplier cards grouped by (order_date, delivery_date) pair
//   Ordered   — supplier cards (same UI as Planned) grouped by (order→delivery) pair
//   Delivered — one row per supplier, expandable to per-item lines with delivery date
//
// Consumes `manager_change`, `signal_emitted(SUPPLIER_PRICE_UPDATE)`, and
// `procurement_plan_updated` WS events to refresh.

import { useEffect, useState, useCallback } from "react";
import {
  AlertTriangle,
  AlertOctagon,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  ChevronDown,
  Loader2,
  Package,
  RefreshCw,
  Truck,
} from "lucide-react";
import { apiGet, apiPost } from "../api";
import { useProcurementPlanVersion } from "../store";
import { wsClient } from "../ws";
import type { SignalEnvelope } from "../types";
import { formatQty } from "../utils/units";

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
  urgency: "at_risk" | "uncoverable" | null;
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
  status: "planned" | "at_risk" | "uncoverable" | "placed" | "superseded";
  reason: string;
  delivery_charge: number;
  delivery_charge_share: number;
  // Per-line risk detail
  projected_stock_before: number;
  qty_needed_before: number;
  shortage_if_late: number;
  latest_safe_arrival: number | null;
}

interface PlanResponse {
  plan_run_id: number | null;
  generated_at: number;
  coverage_ok: boolean;
  forecast_coverage_ok: boolean;
  total_short: number;
  coverage_depends_on_planned_orders: boolean;
  late_delivery_coverage_ok: boolean;
  reliability_premium: number;
  exposed_value_baseline: number;
  exposed_value_protected: number;
  items: PlanItem[];
}

interface ProcurementWarning {
  signal_id: string;
  ingredient_id: number | null;
  ingredient_name: string;
  short_qty: number;
  unit: string;
  reason: string;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Convert a sim-time seconds value to "Day N (Dow)" — mirrors server _day_label. */
function dayLabel(simS: number | null | undefined): string {
  if (simS == null) return "—";
  const day = Math.floor(simS / 86400);
  const dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day % 7];
  return `Day ${day} (${dow})`;
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

// ─── Shared supplier card ─────────────────────────────────────────────────────
//
// Used by both Planned and Ordered sections.  Items are sub-grouped by
// (order_date_label, delivery_date_label) pair so the date arrow appears once
// per group.  Delivery charge shown as a footer line when > 0.

interface SupplierCardItem {
  id: number;
  ingredient_name: string;
  qty: number;
  unit: string;
  unit_price: number | null;
  order_date_label: string;
  delivery_date_label: string;
  at_risk?: boolean;
  uncoverable?: boolean;
  cost?: number | null;             // pre-computed line_total; falls back to unit_price*qty
  delivery_charge_share?: number;  // this item's share of the shipment delivery fee
  shortage_if_late?: number;       // units short if this line arrives one day late
  projected_stock_before?: number; // running stock before this order arrives
}

function SupplierCard({
  supplierName,
  items,
  deliveryCharge = 0,
  defaultOpen = true,
}: {
  supplierName: string;
  items: SupplierCardItem[];
  deliveryCharge?: number;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  // Goods total (pre-computed cost or unit_price × qty)
  const goodsTotal = items.reduce(
    (sum, i) => sum + (i.cost != null ? i.cost : (i.unit_price ?? 0) * i.qty),
    0
  );
  // Per-shipment delivery: sum delivery_charge_share across all items.
  // Falls back to the legacy prop so the OrderedSection (which passes deliveryCharge
  // from stored PO totals) is unaffected.
  const totalDelivery = items.some((i) => (i.delivery_charge_share ?? 0) > 0)
    ? items.reduce((sum, i) => sum + (i.delivery_charge_share ?? 0), 0)
    : deliveryCharge;
  const total = goodsTotal + totalDelivery;
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

      {/* Card body — one sub-block per (order date → delivery date) shipment */}
      {open && (
        <div className="border-t border-muted/30 divide-y divide-muted/20">
          {Array.from(byDatePair.entries()).map(([dateKey, dateItems]) => {
            const first = dateItems[0];
            // Per-shipment delivery fee = sum of delivery_charge_share for this date-pair
            const pairDelivery = dateItems.some((i) => (i.delivery_charge_share ?? 0) > 0)
              ? dateItems.reduce((s, i) => s + (i.delivery_charge_share ?? 0), 0)
              : 0;
            return (
              <div key={dateKey} className="px-3 py-2 space-y-1">
                {/* Date header */}
                <div className="flex items-center gap-1 text-xs text-text/50">
                  <span>{first.order_date_label}</span>
                  <span className="text-text/30">→</span>
                  <span>{first.delivery_date_label}</span>
                </div>
                {/* Items */}
                {dateItems.map((item) => {
                  const itemCost =
                    item.cost != null
                      ? item.cost
                      : (item.unit_price ?? 0) * item.qty;
                  // At-risk tooltip: concrete numbers when available
                  const atRiskTitle = (item.shortage_if_late ?? 0) > 0.5
                    ? `Late-delivery risk: ${formatQty(item.shortage_if_late ?? 0, item.unit)} short on this day if shipment slips 1 day · stock before arrival: ${formatQty(item.projected_stock_before ?? 0, item.unit)}`
                    : "Order-by window passed — placed now for earliest feasible delivery";
                  return (
                    <div
                      key={item.id}
                      className="flex items-center justify-between"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-sm text-text truncate">
                          {item.ingredient_name}
                        </span>
                        <span className="text-xs text-text/50 tabular-nums whitespace-nowrap">
                          {formatQty(item.qty, item.unit)}
                        </span>
                        {item.uncoverable && (
                          <span
                            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-danger/20 text-danger whitespace-nowrap"
                            title="Cannot be sourced — no lead-feasible / in-stock supply"
                          >
                            <AlertOctagon size={10} />
                            uncoverable
                          </span>
                        )}
                        {item.at_risk && !item.uncoverable && (
                          <span
                            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-warning/20 text-warning whitespace-nowrap"
                            title={atRiskTitle}
                          >
                            <AlertTriangle size={10} />
                            at-risk
                          </span>
                        )}
                      </div>
                      <span className="shrink-0 text-xs text-text/50 tabular-nums ml-3">
                        €{itemCost.toFixed(2)}
                      </span>
                    </div>
                  );
                })}
                {/* Per-shipment delivery fee — one line per (order → delivery) pair */}
                {pairDelivery > 0.005 && (
                  <div className="flex items-center justify-between pt-0.5">
                    <div className="flex items-center gap-1.5 text-xs text-text/40">
                      <Truck size={11} />
                      <span>Delivery</span>
                    </div>
                    <span className="text-xs text-text/40 tabular-nums">
                      €{pairDelivery.toFixed(2)}
                    </span>
                  </div>
                )}
              </div>
            );
          })}
          {/* Legacy per-card delivery footer (OrderedSection path: deliveryCharge prop,
              no delivery_charge_share on items). Shown only when items carry no share. */}
          {deliveryCharge > 0 && !items.some((i) => (i.delivery_charge_share ?? 0) > 0) && (
            <div className="px-3 py-2 flex items-center justify-between">
              <div className="flex items-center gap-1.5 text-xs text-text/40">
                <Truck size={11} />
                <span>Delivery</span>
              </div>
              <span className="text-xs text-text/40 tabular-nums">
                €{deliveryCharge.toFixed(2)}
              </span>
            </div>
          )}
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
          {Array.from(bySupplier.entries()).map(([supplier, supplierItems]) => {
            const cardItems: SupplierCardItem[] = supplierItems.map((i) => ({
              id: i.id,
              ingredient_name: i.ingredient_name,
              qty: i.qty,
              unit: i.unit,
              unit_price: i.unit_price,
              order_date_label: i.order_date_label,
              delivery_date_label: i.delivery_date_label,
              at_risk: i.status === "at_risk",
              uncoverable: i.status === "uncoverable",
              delivery_charge_share: i.delivery_charge_share,
              shortage_if_late: i.shortage_if_late,
              projected_stock_before: i.projected_stock_before,
            }));
            return (
              <SupplierCard
                key={supplier}
                supplierName={supplier}
                items={cardItems}
                defaultOpen={true}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Ordered section ──────────────────────────────────────────────────────────
//
// Groups committed POs by supplier — identical card UI to Planned.

function OrderedSection({
  orders,
  loading,
}: {
  orders: PurchaseOrder[];
  loading: boolean;
}) {
  const bySupplier = groupBy(orders, (po) => po.supplier_name);
  const totalOrders = orders.length;

  return (
    <div className="space-y-2">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        Ordered ({totalOrders})
        {loading && <Loader2 size={11} className="animate-spin" />}
      </h3>
      {totalOrders === 0 && !loading ? (
        <p className="text-sm text-text/30">No in-flight orders.</p>
      ) : (
        <div className="space-y-2">
          {Array.from(bySupplier.entries()).map(([supplier, supplierPos]) => {
            // Flatten all lines from all POs for this supplier into card items.
            // Carry the PO-level urgency label onto each line so the badge shows
            // on ordered items that were at_risk or uncoverable when placed.
            const cardItems: SupplierCardItem[] = supplierPos.flatMap((po) =>
              (po.lines ?? []).map((ln) => ({
                id: ln.id,
                ingredient_name: ln.ingredient_name,
                qty: ln.qty,
                unit: ln.unit,
                unit_price: ln.unit_price,
                order_date_label: dayLabel(po.created_at),
                delivery_date_label: dayLabel(po.expected_delivery),
                cost: ln.line_total,
                at_risk: po.urgency === "at_risk",
                uncoverable: po.urgency === "uncoverable",
              }))
            );
            // Delivery charge = total_cost minus goods total (what create_po added)
            const goodsSum = supplierPos.reduce(
              (s, po) =>
                s +
                (po.lines ?? []).reduce(
                  (ls, ln) => ls + (ln.line_total ?? 0),
                  0
                ),
              0
            );
            const supplierTotal = supplierPos.reduce(
              (s, po) => s + (po.total_cost ?? 0),
              0
            );
            const impliedDelivery = Math.max(0, supplierTotal - goodsSum);
            return (
              <SupplierCard
                key={supplier}
                supplierName={supplier}
                items={cardItems}
                deliveryCharge={impliedDelivery}
                defaultOpen={true}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Delivered history section ────────────────────────────────────────────────
//
// One compact row per supplier across all delivered POs on the page.
// Expanding shows every line item with individual cost and delivery date.

interface SupplierDeliveredGroup {
  supplier_name: string;
  total: number;
  items: Array<{
    id: number;
    ingredient_name: string;
    qty: number;
    unit: string;
    qty_display: string;
    line_total: number | null;
    delivery_label: string;
  }>;
}

function aggregateBySupplier(orders: PurchaseOrder[]): SupplierDeliveredGroup[] {
  const map = new Map<string, SupplierDeliveredGroup>();
  for (const po of orders) {
    const name = po.supplier_name;
    if (!map.has(name)) {
      map.set(name, { supplier_name: name, total: 0, items: [] });
    }
    const grp = map.get(name)!;
    grp.total += po.total_cost ?? 0;
    const delivLabel = dayLabel(po.expected_delivery);
    for (const ln of po.lines ?? []) {
      grp.items.push({
        id: ln.id,
        ingredient_name: ln.ingredient_name,
        qty: ln.qty,
        unit: ln.unit,
        qty_display: ln.qty_display,
        line_total: ln.line_total,
        delivery_label: delivLabel,
      });
    }
  }
  return Array.from(map.values());
}

function SupplierDeliveredRow({ group }: { group: SupplierDeliveredGroup }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-lg border border-muted/40 bg-surface/50 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-1.5 hover:bg-muted/20 text-left"
      >
        <div className="flex items-center gap-2 min-w-0">
          <Package size={12} className="text-text/30 shrink-0" />
          <span className="text-xs font-medium text-text truncate">
            {group.supplier_name}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-xs text-text/50 tabular-nums">
            €{group.total.toFixed(2)}
          </span>
          {open ? (
            <ChevronUp size={12} className="text-text/30" />
          ) : (
            <ChevronDown size={12} className="text-text/30" />
          )}
        </div>
      </button>

      {open && group.items.length > 0 && (
        <table className="w-full text-xs border-t border-muted/30">
          <tbody>
            {group.items.map((item, idx) => (
              <tr
                key={`${item.id}-${idx}`}
                className="border-t border-muted/20 first:border-0"
              >
                <td className="px-3 py-1 text-text/70">{item.ingredient_name}</td>
                <td className="px-3 py-1 text-right tabular-nums text-text/60 whitespace-nowrap">
                  {formatQty(item.qty, item.unit)}
                </td>
                <td className="px-3 py-1 text-right text-text/40 whitespace-nowrap">
                  {item.line_total != null ? `€${item.line_total.toFixed(2)}` : ""}
                </td>
                <td className="px-3 py-1 text-right text-text/30 whitespace-nowrap text-[10px]">
                  {item.delivery_label}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

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
  const groups = aggregateBySupplier(orders);

  return (
    <div className="space-y-2">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        Delivered history
        {loading && <Loader2 size={11} className="animate-spin" />}
      </h3>
      {groups.length === 0 && !loading ? (
        <p className="text-sm text-text/30">No delivery history yet.</p>
      ) : (
        <div className="space-y-1">
          {groups.map((grp) => (
            <SupplierDeliveredRow key={grp.supplier_name} group={grp} />
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
  const [planMeta, setPlanMeta] = useState<Omit<PlanResponse, "items"> | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [replanning, setReplanning] = useState(false);
  const [ordered, setOrdered] = useState<PurchaseOrder[]>([]);
  const [delivered, setDelivered] = useState<PurchaseOrder[]>([]);
  const [deliveredPage, setDeliveredPage] = useState(0);
  const [deliveredHasMore, setDeliveredHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  // Procurement warnings — live INGREDIENT_UNCOVERABLE signals.
  // Future features query: GET /api/track-b/procurement/warnings
  // Live updates arrive via signal_emitted WS event (type === "INGREDIENT_UNCOVERABLE").
  const [warnings, setWarnings] = useState<ProcurementWarning[]>([]);

  const fetchWarnings = useCallback(async () => {
    try {
      const res = await apiGet<ProcurementWarning[]>(
        "/api/track-b/procurement/warnings"
      );
      setWarnings(res);
    } catch {
      /* ignore */
    }
  }, []);

  const fetchPlan = useCallback(async () => {
    setPlanLoading(true);
    try {
      const res = await apiGet<PlanResponse>("/api/track-b/procurement/plan");
      const { items, ...meta } = res;
      setPlanItems(items);
      setPlanMeta(meta);
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

  useEffect(() => {
    void fetchPlan();
    void fetchWarnings();
  }, [fetchPlan, fetchWarnings, procurementPlanVersion]);

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
      if (signal?.type === "INGREDIENT_UNCOVERABLE") {
        // A new uncoverable warning arrived — re-fetch the full list so the
        // banner is always consistent with the server state.
        void fetchWarnings();
      }
    });
    const offChange = wsClient.on("manager_change", () => {
      void fetchPlan();
      void fetchOrders(deliveredPage);
    });
    const offPlan = wsClient.on("procurement_plan_updated", () => {
      void fetchPlan();
      void fetchWarnings();
    });
    return () => {
      offSignal();
      offChange();
      offPlan();
    };
  }, [fetchPlan, fetchOrders, fetchWarnings, deliveredPage]);

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

      {/* Uncoverable-ingredient warnings — distinct from at-risk badges.
          One row per ingredient the optimizer cannot source at all.
          Live-updated via signal_emitted(INGREDIENT_UNCOVERABLE) WS event.
          Future features query: GET /api/track-b/procurement/warnings */}
      {warnings.length > 0 && (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 space-y-1">
          <div className="flex items-center gap-2 text-xs font-semibold text-danger uppercase tracking-wide">
            <AlertOctagon size={13} />
            Sourcing Alerts
          </div>
          {warnings.map((w) => (
            <div
              key={w.signal_id}
              className="flex items-start gap-2 text-xs text-danger/80"
            >
              <span className="font-medium shrink-0">{w.ingredient_name}</span>
              <span className="text-danger/60 min-w-0 truncate" title={w.reason}>
                — {w.reason}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Reliability trade-off summary */}
      {planMeta && planMeta.reliability_premium > 0.005 && planMeta.exposed_value_protected > 0 && (
        <div className="rounded-lg border border-muted/40 bg-surface/60 px-3 py-2 text-xs text-text/70">
          <span className="font-medium text-text">Selected safer plan</span>
          {" · "}Extra cash{" "}
          <span className="font-medium text-text">
            €{planMeta.reliability_premium.toFixed(2)}
          </span>
          {" · "}Forecast value protected under one-day delay{" "}
          <span className="font-medium text-text">
            €{Math.max(0, planMeta.exposed_value_baseline - planMeta.exposed_value_protected).toFixed(2)}
          </span>
        </div>
      )}

      {/* Coverage quality banner — only shown when coverage is plan-dependent or fragile */}
      {planMeta && planMeta.forecast_coverage_ok && planMeta.coverage_depends_on_planned_orders && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
          <AlertTriangle size={12} className="inline mr-1 shrink-0" />
          Forecast covered — conditional on planned orders being placed.
          {!planMeta.late_delivery_coverage_ok && " Not robust to a late delivery."}
        </div>
      )}
      {planMeta && !planMeta.forecast_coverage_ok && planMeta.total_short > 0 && (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          <AlertOctagon size={12} className="inline mr-1 shrink-0" />
          Demand not fully covered —{" "}
          <span className="font-medium">{planMeta.total_short.toFixed(0)} units short</span>
          {" "}even with planned orders.
        </div>
      )}

      {/* Planned — supplier cards */}
      <PlanSection
        items={planItems}
        loading={planLoading}
        emptyMsg="No forward plan yet — click Re-plan to generate one."
        onReplan={() => void replan()}
        replanning={replanning}
      />

      {/* Ordered (approved/placed) — same supplier card UI as Planned */}
      <OrderedSection orders={ordered} loading={loading} />

      {/* Delivered — compact supplier rows with per-item receipt dropdowns */}
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
