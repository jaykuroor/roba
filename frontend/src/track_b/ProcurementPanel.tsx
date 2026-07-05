// ProcurementPanel — planned → ordered → delivered history.
//
// Three stacked sections (planned on top):
//   Planned   — proposed PurchaseOrders + latest SourcingRun assignments
//   Ordered   — approved / placed POs awaiting delivery
//   Delivered — delivered POs, paginated
//
// Consumes `manager_change` and `signal_emitted(SUPPLIER_PRICE_UPDATE)` WS
// events to refresh.

import { useEffect, useState, useCallback } from "react";
import { ChevronLeft, ChevronRight, Loader2, Package } from "lucide-react";
import { apiGet } from "../api";
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

// ─── Main component ───────────────────────────────────────────────────────────

const PAGE_SIZE = 10;

export function ProcurementPanel() {
  const [planned, setPlanned] = useState<PurchaseOrder[]>([]);
  const [ordered, setOrdered] = useState<PurchaseOrder[]>([]);
  const [delivered, setDelivered] = useState<PurchaseOrder[]>([]);
  const [deliveredPage, setDeliveredPage] = useState(0);
  const [deliveredHasMore, setDeliveredHasMore] = useState(false);
  const [loading, setLoading] = useState(true);

  const fetchOrders = useCallback(
    async (page: number) => {
      setLoading(true);
      try {
        const [plannedRes, orderedRes, deliveredRes] = await Promise.all([
          apiGet<PurchaseOrder[]>(
            "/api/purchase-orders?status=proposed&include_lines=true"
          ),
          apiGet<PurchaseOrder[]>(
            "/api/purchase-orders?status=approved,placed&include_lines=true"
          ),
          apiGet<PurchaseOrder[]>(
            `/api/purchase-orders?status=delivered&include_lines=true&limit=${PAGE_SIZE + 1}&offset=${page * PAGE_SIZE}`
          ),
        ]);
        setPlanned(plannedRes);
        setOrdered(orderedRes);
        const hasMore = deliveredRes.length > PAGE_SIZE;
        setDelivered(deliveredRes.slice(0, PAGE_SIZE));
        setDeliveredHasMore(hasMore);
      } catch {
        /* ignore */
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    void fetchOrders(deliveredPage);
  }, [fetchOrders, deliveredPage]);

  useEffect(() => {
    const offSignal = wsClient.on("signal_emitted", (p) => {
      const signal = (p as { signal?: SignalEnvelope }).signal;
      if (signal?.type === "SUPPLIER_PRICE_UPDATE") void fetchOrders(deliveredPage);
    });
    const offChange = wsClient.on("manager_change", () =>
      void fetchOrders(deliveredPage)
    );
    return () => {
      offSignal();
      offChange();
    };
  }, [fetchOrders, deliveredPage]);

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
      <PoSection
        label={`Planned (${planned.length})`}
        orders={planned}
        emptyMsg="No planned orders — run the sourcing optimizer to generate a plan."
      />

      {/* Ordered (approved/placed) */}
      <PoSection
        label={`Ordered (${ordered.length})`}
        orders={ordered}
        emptyMsg="No in-flight orders."
      />

      {/* Delivered — paginated */}
      <PoSection
        label={`Delivered history`}
        orders={delivered}
        emptyMsg="No delivery history yet."
        footer={
          (deliveredPage > 0 || deliveredHasMore) && (
            <div className="flex items-center gap-2 justify-end pt-1">
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
