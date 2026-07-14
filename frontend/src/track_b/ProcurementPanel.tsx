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
  order_overdue?: boolean;
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
  // FEFO-derived coverage (authoritative — drives badges and warnings)
  coverage_status: "covered" | "nominal_uncoverable" | "delay_exposed";
  short_nominal: number;   // per-ingredient on-time shortfall
  short_delayed: number;   // per-ingredient shortfall under +1-day delay
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
  // Honest cost: cash_optimal_cost = pass-1 baseline, reliability_premium = delta
  cash_optimal_cost: number;
  // Robustness
  robust_requested: boolean;
  robust_applied: boolean;
  robust_status: string;
  robust_premium: number;
  // Coverage counts derived from items (used for banner cross-check)
  nominal_uncoverable_count: number;
  delay_exposed_count: number;
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
  shortage_if_late?: number;       // units short if THIS line arrives one day late
  projected_stock_before?: number; // running stock before this order arrives
  // Fields consumed by deriveBadges() for per-line-accurate risk labels.
  status?: PlanItem["status"];
  coverage_status?: PlanItem["coverage_status"];
  short_nominal?: number;          // per-ingredient on-time shortfall
  short_delayed?: number;          // per-ingredient +1-day-delay shortfall
  order_overdue?: boolean;         // order-by window has passed
}

// ─── Per-line risk badges ─────────────────────────────────────────────────────
//
// A single line can carry SEVERAL coexisting badges. Rules are per-LINE where the
// underlying metric is per-line (shortage_if_late) and per-INGREDIENT otherwise
// (coverage_status, short_nominal, short_delayed). This is what keeps only the
// actually-tight line reading "Late if slips" while other lines of a delay-exposed
// ingredient show the subtle context badge instead.

type BadgeVariant = "danger" | "warning" | "caution" | "warning-subtle";

interface LineBadge {
  key: string;
  label: string;
  variant: BadgeVariant;
  title: string;
}

const BADGE_CLASSES: Record<BadgeVariant, string> = {
  danger: "bg-danger/20 text-danger",                 // Uncoverable (red)
  warning: "bg-warning/20 text-warning",               // Late (orange)
  caution: "bg-caution/20 text-caution",               // Delay Vulnerable (yellow)
  "warning-subtle": "bg-caution/10 text-caution/80",   // Delay-exposed context (faint)
};

// Per-line risk labels — each answers a distinct, non-overlapping question and
// several can coexist on one line.  Kept deliberately small:
//   Uncoverable      (red)    — genuinely NO feasible supply (qty=0 sentinel).
//                               NOT driven by coverage_status: an ordered-but-short
//                               ingredient is a coverage/forecast concern, not a
//                               sourcing failure, and must not read "Uncoverable".
//   Late             (orange) — the order-by window has already passed (timing).
//                               Driven ONLY by order_overdue, never by the per-line
//                               delay flag (status="at_risk" conflates the two).
//   Delay Vulnerable (yellow) — covered on time, but THIS line slipping 1 day
//                               causes a shortage (per-line shortage_if_late).
//   Delay-exposed    (faint)  — sibling context: the ingredient is delay-exposed
//                               but this specific line is not the tight one.
function deriveBadges(item: SupplierCardItem): LineBadge[] {
  const badges: LineBadge[] = [];
  const unit = item.unit;
  const shortageIfLate = item.shortage_if_late ?? 0;
  const lineIsTight = shortageIfLate > 0.5;

  // 1. Uncoverable — genuinely no feasible supply (qty=0 no-supply sentinel).
  if (item.status === "uncoverable") {
    badges.push({
      key: "uncoverable",
      label: "Uncoverable",
      variant: "danger",
      title: "Cannot be sourced — no lead-feasible / in-stock supplier for this ingredient.",
    });
  }

  // 2. Late — order-by window has already passed (pure timing; supply exists).
  //    For a planned line this means "place it now"; for an already-placed order
  //    it means "placed after its window — arriving deliver-ASAP, later than the
  //    plan wanted".
  if (item.order_overdue) {
    badges.push({
      key: "late",
      label: "Late",
      variant: "warning",
      title: item.status === "placed"
        ? `Placed after its order-by window — arriving as soon as the lead time allows (${item.delivery_date_label}), later than originally planned.`
        : `Order-by window passed (${item.order_date_label}) — place now for earliest feasible delivery.`,
    });
  }

  // 3. Delay Vulnerable — THIS line runs short if its delivery slips one day.
  if (lineIsTight) {
    badges.push({
      key: "delay-vulnerable",
      label: "Delay Vulnerable",
      variant: "caution",
      title: `If this delivery slips 1 day, ${formatQty(shortageIfLate, unit)} short on ${item.delivery_date_label}. Stock before arrival: ${formatQty(item.projected_stock_before ?? 0, unit)}.`,
    });
  }

  // 4. Delay-exposed — ingredient is covered on time but exposed to a slip on
  //    SOME line (faint context; suppressed on the line that is itself tight).
  if (item.coverage_status === "delay_exposed" && !lineIsTight) {
    badges.push({
      key: "delay-exposed",
      label: "Delay-exposed",
      variant: "warning-subtle",
      title: `${item.ingredient_name} is covered on time but ${formatQty(item.short_delayed ?? 0, unit)} short if any delivery slips 1 day (not this line).`,
    });
  }

  return badges;
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
                  const badges = deriveBadges(item);
                  return (
                    <div
                      key={item.id}
                      className="flex items-center justify-between"
                    >
                      <div className="flex items-center gap-2 min-w-0 flex-wrap">
                        <span className="text-sm text-text truncate">
                          {item.ingredient_name}
                        </span>
                        <span className="text-xs text-text/50 tabular-nums whitespace-nowrap">
                          {formatQty(item.qty, item.unit)}
                        </span>
                        {badges.map((badge) => (
                          <span
                            key={badge.key}
                            className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium whitespace-nowrap ${BADGE_CLASSES[badge.variant]}`}
                            title={badge.title}
                          >
                            {badge.variant === "danger" ? (
                              <AlertOctagon size={10} />
                            ) : (
                              <AlertTriangle size={10} />
                            )}
                            {badge.label}
                          </span>
                        ))}
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
              delivery_charge_share: i.delivery_charge_share,
              // Carry all fields deriveBadges() needs so per-line badges stay
              // consistent with the plan-level four-axis summary (both read the
              // same FEFO coverage + per-line delay metrics).
              status: i.status,
              coverage_status: i.coverage_status,
              short_nominal: i.short_nominal,
              short_delayed: i.short_delayed,
              order_overdue: i.order_overdue,
              // shortage_if_late is the PER-LINE delay shortfall (drives the
              // "Late if slips" badge on the actually-tight line only).
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
            // These are already-placed, in-flight orders.  There is no "Placed"
            // badge (the Ordered section header conveys that), but a placed order
            // still surfaces LATE when the optimizer flagged its group as at_risk
            // — i.e. it was placed after its order-by window and is arriving on a
            // deliver-ASAP (now + lead) schedule rather than the day the plan
            // wanted (see execute_due_planned_orders: group_late → urgency).
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
                status: "placed" as const,
                order_overdue: po.urgency === "at_risk",
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
  // Approval threshold (currency): planned orders auto-place when due unless the
  // PO total exceeds this, in which case it routes to the manager's desk.
  const [approvalThreshold, setApprovalThreshold] = useState(500);

  useEffect(() => {
    apiGet<{ approval_threshold: number }>("/api/runtime/approval-threshold")
      .then((t) => setApprovalThreshold(t.approval_threshold))
      .catch(() => {});
  }, []);

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

  async function replan(robust?: boolean) {
    setReplanning(true);
    try {
      const url =
        robust === undefined
          ? "/api/track-b/procurement/plan/run"
          : `/api/track-b/procurement/plan/run?robust=${robust}`;
      await apiPost(url);
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

      {/* Coverage status panel — four orthogonal labeled axes */}
      {planMeta && (
        <div className="rounded-lg border border-muted/30 bg-surface/60 px-3 py-2.5 space-y-1.5 text-xs">
          {/* COVERAGE axis — red ONLY for a genuine sourcing failure (no feasible
              supplier: items with status="uncoverable").  An ordered-but-short
              ingredient (covered by supply that exists but the plan can't fully
              stock, e.g. shelf-life limits) is a calmer coverage note, not red —
              it must never read "cannot be sourced".  Matches the per-line badges. */}
          <div className="flex items-baseline gap-2">
            <span className="font-semibold text-text/50 uppercase tracking-wide text-[10px] w-20 shrink-0">
              Coverage
            </span>
            {(() => {
              const uncoverableIngs = new Set(
                planItems
                  .filter((it) => it.status === "uncoverable")
                  .map((it) => it.ingredient_id)
              );
              const nUncoverable = uncoverableIngs.size;
              if (nUncoverable > 0) {
                return (
                  <span className="text-danger font-medium flex items-center gap-1">
                    <AlertOctagon size={11} />
                    {nUncoverable} ingredient{nUncoverable !== 1 ? "s" : ""} cannot be sourced (no supplier)
                  </span>
                );
              }
              const nShort = planMeta.nominal_uncoverable_count ?? 0;
              if (nShort > 0) {
                return (
                  <span className="text-warning font-medium flex items-center gap-1">
                    <AlertTriangle size={11} />
                    Covered, but {nShort} ingredient{nShort !== 1 ? "s" : ""} short vs forecast
                  </span>
                );
              }
              return <span className="text-success font-medium">Fully covered</span>;
            })()}
          </div>

          {/* SCHEDULED axis — planned orders auto-place on their order date; this
              is informational, not a manual to-do.  Only orders over the approval
              threshold route to the manager's desk. */}
          {planMeta.coverage_depends_on_planned_orders && (() => {
            const pendingCount = planItems.filter(
              (it) => it.status === "planned" || it.status === "at_risk"
            ).length;
            if (pendingCount === 0) return null;
            return (
              <div className="flex items-baseline gap-2">
                <span className="font-semibold text-text/50 uppercase tracking-wide text-[10px] w-20 shrink-0">
                  Scheduled
                </span>
                <span
                  className="text-text/60 flex items-center gap-1"
                  title={`These orders place themselves automatically on their order date. Only orders whose PO total exceeds €${approvalThreshold.toFixed(0)} are sent to the manager's desk for approval.`}
                >
                  {pendingCount} order{pendingCount !== 1 ? "s" : ""} auto-place on their order date
                  <span className="text-text/40">· approval only if over €{approvalThreshold.toFixed(0)}</span>
                </span>
              </div>
            );
          })()}

          {/* DELAY RISK axis */}
          <div className="flex items-baseline gap-2">
            <span className="font-semibold text-text/50 uppercase tracking-wide text-[10px] w-20 shrink-0">
              Delay risk
            </span>
            {planMeta.robust_applied ? (
              <span className="text-success font-medium">
                Guaranteed to survive a 1-day delay
                {(planMeta.robust_premium ?? 0) > 0.01 && (
                  <span className="font-normal text-text/50"> (+€{planMeta.robust_premium.toFixed(2)} vs on-time plan)</span>
                )}
              </span>
            ) : planMeta.late_delivery_coverage_ok ? (
              <span className="text-text/60 flex items-center gap-1">
                Survives a 1-day delivery delay
                {!planMeta.robust_applied && (
                  <button
                    type="button"
                    onClick={() => void replan(true)}
                    disabled={replanning}
                    className="ml-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-muted text-text/60 hover:bg-muted/70 disabled:opacity-50"
                    title="Re-plan with pass-3 hard robustness guarantee"
                  >
                    Make robust
                  </button>
                )}
              </span>
            ) : (() => {
              // Find the worst delay-exposed ingredient for a concrete headline
              const worst = planItems
                .filter((it) => (it.short_delayed ?? 0) > 0.5)
                .sort((a, b) => (b.short_delayed ?? 0) - (a.short_delayed ?? 0))[0];
              const reliabilityNote = planMeta.reliability_premium > 0.005 && planMeta.exposed_value_protected > 0
                ? ` · safer plan active (+€${planMeta.reliability_premium.toFixed(2)})`
                : "";
              return (
                <span className="text-warning font-medium flex items-center gap-2">
                  <AlertTriangle size={11} />
                  <span>
                    {worst
                      ? `${worst.ingredient_name} ${formatQty(worst.short_delayed ?? 0, worst.unit)} short if a delivery slips 1 day`
                      : "Not robust to a 1-day delivery delay"}
                    {reliabilityNote && (
                      <span className="font-normal text-text/50">{reliabilityNote}</span>
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={() => void replan(true)}
                    disabled={replanning}
                    className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-muted text-text/60 hover:bg-muted/70 disabled:opacity-50 shrink-0"
                    title="Re-plan with pass-3 hard robustness guarantee"
                  >
                    Make robust
                  </button>
                </span>
              );
            })()}
          </div>

          {/* COST axis — honest: cash-optimal vs chosen (risk-adjusted) */}
          {(planMeta.cash_optimal_cost ?? 0) > 0.01 && (
            <div className="flex items-baseline gap-2">
              <span className="font-semibold text-text/50 uppercase tracking-wide text-[10px] w-20 shrink-0">
                Cost
              </span>
              {(planMeta.reliability_premium ?? 0) < 0.01 ? (
                <span className="text-text/60">
                  Cash-optimal €{planMeta.cash_optimal_cost.toFixed(2)}
                </span>
              ) : (
                <span className="text-text/60">
                  Cash-optimal €{planMeta.cash_optimal_cost.toFixed(2)}
                  <span className="text-text/40"> · Chosen (reliability-adjusted) €{((planMeta.cash_optimal_cost ?? 0) + (planMeta.reliability_premium ?? 0)).toFixed(2)} (+€{planMeta.reliability_premium.toFixed(2)})</span>
                </span>
              )}
            </div>
          )}
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
