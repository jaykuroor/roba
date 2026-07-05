// SupplierEditor — per-supplier cards with current / alternate sections.
//
// "Current" = suppliers with ≥1 is_default catalog row (MILP-chosen).
// "Alternate" = available suppliers not currently default for any ingredient.
//
// Each card shows:
//   • Supplier name, phone, lead time, reliability
//   • Items from next planned order (latest SourcingRun) with qty + price
//   • Negotiate button (opens inline note form → triggers approval-gated call)
//
// "Add supplier" (+) card: enter name + phone → stages onboarding call
// confirmation on the card AND on the manager desk feed.

import { useEffect, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronUp,
  Loader2,
  Phone,
  PhoneCall,
  Plus,
  X,
} from "lucide-react";
import { apiGet, apiPost } from "../api";
import { wsClient } from "../ws";
import { useActiveCall } from "../store";
import { SpectateOverlay } from "../shell/SpectateOverlay";
import type { SignalEnvelope } from "../types";

// ─── Types ───────────────────────────────────────────────────────────────────

interface SupplierItem {
  catalog_id: number;
  ingredient_id: number;
  ingredient_name: string;
  is_default: boolean;
  current_price: number;
  price_display: string;
  unit: string;
  base_unit: string;
  availability: string | null;
  planned_qty: number | null;
  planned_qty_display: string | null;
}

interface ActivePoItem {
  ingredient_id: number;
  ingredient_name: string;
  qty: number;
  qty_display: string;
  unit_price: number;
  line_total: number;
  po_status: string;
}

interface SupplierEntry {
  id: number;
  name: string;
  phone: string | null;
  lead_time_days: number | null;
  reliability_score: number | null;
  min_order_value: number | null;
  delivery_charge: number | null;
  is_current: boolean;
  items: SupplierItem[];
  active_po_items: ActivePoItem[];
}

interface OverviewData {
  current: SupplierEntry[];
  alternate: SupplierEntry[];
}

// ─── Negotiate inline form ────────────────────────────────────────────────────

function NegotiateForm({
  supplier,
  onClose,
  onDone,
}: {
  supplier: SupplierEntry;
  onClose: () => void;
  onDone: () => void;
}) {
  const [note, setNote] = useState("");
  const [ingredientId, setIngredientId] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const defaultItems = supplier.items.filter((i) => i.is_default);

  async function handleSubmit() {
    setBusy(true);
    try {
      await apiPost("/api/market/negotiate", {
        supplier_id: supplier.id,
        ingredient_id: ingredientId !== "" ? ingredientId : null,
        note: note || undefined,
      });
      setDone(true);
      setTimeout(onDone, 1200);
    } catch {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="mt-3 flex items-center gap-2 rounded-lg border border-success/40 bg-success/10 px-3 py-2 text-sm text-success">
        <Check size={14} />
        Call request staged — awaiting approval on the manager desk.
      </div>
    );
  }

  return (
    <div className="mt-3 space-y-2 rounded-lg border border-accent/30 bg-accent/5 p-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-accent">Negotiate with {supplier.name}</span>
        <button onClick={onClose} className="text-text/40 hover:text-text/70">
          <X size={13} />
        </button>
      </div>

      {defaultItems.length > 0 && (
        <div>
          <label className="text-[10px] text-text/50">Focus on ingredient (optional)</label>
          <select
            value={ingredientId}
            onChange={(e) =>
              setIngredientId(e.target.value === "" ? "" : Number(e.target.value))
            }
            className="mt-0.5 w-full rounded border border-muted bg-primary px-2 py-1 text-xs text-text outline-none focus:border-accent"
          >
            <option value="">All / general terms</option>
            {defaultItems.map((it) => (
              <option key={it.ingredient_id} value={it.ingredient_id}>
                {it.ingredient_name}
              </option>
            ))}
          </select>
        </div>
      )}

      <div>
        <label className="text-[10px] text-text/50">Note for AI caller (optional)</label>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. 'push hard on price', 'ask about bulk discount above 50kg'"
          rows={2}
          className="mt-0.5 w-full resize-none rounded border border-muted bg-primary px-2 py-1 text-xs text-text placeholder-text/30 outline-none focus:border-accent"
        />
      </div>

      <button
        onClick={() => void handleSubmit()}
        disabled={busy}
        className="flex items-center gap-1.5 rounded bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
      >
        {busy ? <Loader2 size={11} className="animate-spin" /> : <PhoneCall size={11} />}
        {busy ? "Requesting…" : "Request call"}
      </button>
    </div>
  );
}

// ─── Single supplier card ─────────────────────────────────────────────────────

function SupplierCard({ supplier }: { supplier: SupplierEntry }) {
  const hasPlanned = supplier.items.some(
    (i) => i.planned_qty != null && i.planned_qty > 0
  );
  const [expanded, setExpanded] = useState(hasPlanned);
  const [showNegotiate, setShowNegotiate] = useState(false);
  const [negotiateDone, setNegotiateDone] = useState(false);

  const plannedItems = supplier.items.filter(
    (i) => i.planned_qty != null && i.planned_qty > 0
  );
  const catalogOnlyItems = supplier.items.filter(
    (i) => i.planned_qty == null || i.planned_qty === 0
  );
  const inFlightItems = supplier.active_po_items;
  const hasItems = supplier.items.length > 0 || inFlightItems.length > 0;

  return (
    <div className="rounded-xl border border-muted/50 bg-surface/70 overflow-hidden">
      {/* Card header */}
      <div className="flex items-center gap-3 px-4 py-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm text-text truncate">{supplier.name}</span>
            {supplier.is_current && (
              <span className="rounded bg-success/20 px-1.5 py-0.5 text-[10px] font-medium text-success">
                current
              </span>
            )}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-text/50">
            {supplier.phone && (
              <span className="flex items-center gap-1">
                <Phone size={9} />
                {supplier.phone}
              </span>
            )}
            {supplier.lead_time_days != null && (
              <span>Lead {supplier.lead_time_days}d</span>
            )}
            {supplier.reliability_score != null && (
              <span>Rel {(supplier.reliability_score * 100).toFixed(0)}%</span>
            )}
            {supplier.min_order_value != null && supplier.min_order_value > 0 && (
              <span>Min €{supplier.min_order_value.toFixed(0)}</span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {!negotiateDone && (
            <button
              onClick={() => setShowNegotiate((p) => !p)}
              className={
                "flex items-center gap-1 rounded-lg border px-2 py-1 text-xs font-medium transition-colors " +
                (showNegotiate
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-muted/60 text-text/60 hover:border-accent/40 hover:text-accent")
              }
            >
              <PhoneCall size={11} />
              Negotiate
            </button>
          )}
          {hasItems && (
            <button
              onClick={() => setExpanded((p) => !p)}
              className="rounded p-1 text-text/40 hover:text-text/70"
            >
              {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>
          )}
        </div>
      </div>

      {/* Negotiate form */}
      {showNegotiate && (
        <div className="px-4 pb-3">
          <NegotiateForm
            supplier={supplier}
            onClose={() => setShowNegotiate(false)}
            onDone={() => {
              setShowNegotiate(false);
              setNegotiateDone(true);
              setTimeout(() => setNegotiateDone(false), 5000);
            }}
          />
        </div>
      )}
      {negotiateDone && (
        <div className="mx-4 mb-3 flex items-center gap-2 rounded-lg border border-success/40 bg-success/10 px-3 py-1.5 text-xs text-success">
          <Check size={11} />
          Call approval card sent to manager desk
        </div>
      )}

      {/* Items table */}
      {expanded && hasItems && (
        <div className="border-t border-muted/30 bg-surface/40">
          {plannedItems.length > 0 && (
            <div>
              <div className="px-4 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-text/40">
                Next planned order
              </div>
              <table className="w-full text-xs">
                <tbody>
                  {plannedItems.map((item) => (
                    <tr key={item.ingredient_id} className="border-t border-muted/20">
                      <td className="px-4 py-1.5 text-text/80">{item.ingredient_name}</td>
                      <td className="px-4 py-1.5 text-right tabular-nums font-medium text-text">
                        {item.planned_qty_display}
                      </td>
                      <td className="px-4 py-1.5 text-right text-text/50">
                        {item.price_display}
                      </td>
                      <td className="px-4 py-1.5 w-20">
                        {item.availability && item.availability !== "in_stock" && (
                          <span className="rounded bg-warning/20 px-1 py-0.5 text-[10px] text-warning">
                            {item.availability}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {inFlightItems.length > 0 && (
            <div>
              <div className="px-4 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-text/40">
                In-flight orders
              </div>
              <table className="w-full text-xs">
                <tbody>
                  {inFlightItems.map((item, idx) => (
                    <tr key={idx} className="border-t border-muted/20">
                      <td className="px-4 py-1.5 text-text/80">{item.ingredient_name}</td>
                      <td className="px-4 py-1.5 text-right tabular-nums font-medium text-text">
                        {item.qty_display}
                      </td>
                      <td className="px-4 py-1.5 text-right text-text/50">
                        {item.line_total != null ? `€${item.line_total.toFixed(2)}` : ""}
                      </td>
                      <td className="px-4 py-1.5 w-20">
                        <span
                          className={
                            "rounded px-1 py-0.5 text-[10px] " +
                            (item.po_status === "placed"
                              ? "bg-accent/20 text-accent"
                              : item.po_status === "approved"
                              ? "bg-success/20 text-success"
                              : "bg-muted/30 text-text/50")
                          }
                        >
                          {item.po_status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {catalogOnlyItems.length > 0 && (
            <div>
              <div className="px-4 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-text/40">
                Catalog — not in next order
              </div>
              <table className="w-full text-xs">
                <tbody>
                  {catalogOnlyItems.map((item) => (
                    <tr key={item.ingredient_id} className="border-t border-muted/20 opacity-60">
                      <td className="px-4 py-1.5 text-text/60">{item.ingredient_name}</td>
                      <td className="px-4 py-1.5 text-right text-text/40">—</td>
                      <td className="px-4 py-1.5 text-right text-text/40">
                        {item.price_display}
                      </td>
                      <td className="px-4 py-1.5 w-20" />
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="h-2" />
        </div>
      )}
    </div>
  );
}

// ─── Add supplier card ────────────────────────────────────────────────────────

function AddSupplierCard({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [addedName, setAddedName] = useState("");

  async function handleSubmit() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      await apiPost("/api/suppliers/onboard", {
        name: name.trim(),
        phone: phone.trim() || null,
      });
      setAddedName(name.trim());
      setDone(true);
      setTimeout(() => {
        setDone(false);
        setOpen(false);
        setName("");
        setPhone("");
        onAdded();
      }, 2000);
    } catch {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-success/40 bg-success/10 px-4 py-4 text-sm text-success">
        <Check size={14} />
        {addedName} added — onboarding call staged for manager approval.
      </div>
    );
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-muted/60 bg-surface/30 py-4 text-sm text-text/50 transition-colors hover:border-accent/40 hover:text-accent"
      >
        <Plus size={16} />
        Add supplier
      </button>
    );
  }

  return (
    <div className="rounded-xl border border-muted/50 bg-surface/70 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-text">New supplier</span>
        <button onClick={() => setOpen(false)} className="text-text/40 hover:text-text/70">
          <X size={14} />
        </button>
      </div>

      <div className="space-y-2">
        <div>
          <label className="text-[10px] text-text/50">Supplier name *</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void handleSubmit()}
            placeholder="e.g. Fresh Valley Co."
            className="mt-0.5 w-full rounded border border-muted bg-primary px-2 py-1.5 text-sm text-text placeholder-text/30 outline-none focus:border-accent"
          />
        </div>
        <div>
          <label className="text-[10px] text-text/50">Phone number (optional)</label>
          <input
            type="tel"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+1-555-0000"
            className="mt-0.5 w-full rounded border border-muted bg-primary px-2 py-1.5 text-sm text-text placeholder-text/30 outline-none focus:border-accent"
          />
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => void handleSubmit()}
          disabled={busy || !name.trim()}
          className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <PhoneCall size={12} />}
          {busy ? "Adding…" : "Add & call to onboard"}
        </button>
        <button
          onClick={() => setOpen(false)}
          className="rounded-lg border border-muted px-3 py-1.5 text-sm text-text/60 hover:text-text"
        >
          Cancel
        </button>
      </div>

      <p className="text-[10px] text-text/40">
        Creates the supplier and requests an onboarding call — visible on the manager desk for
        approval.
      </p>
    </div>
  );
}

// ─── Suppliers section ────────────────────────────────────────────────────────

function SuppliersSection({
  label,
  suppliers,
  emptyMsg,
}: {
  label: string;
  suppliers: SupplierEntry[];
  emptyMsg?: string;
}) {
  return (
    <div className="space-y-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-text/40">{label}</h3>
      {suppliers.length === 0 ? (
        <p className="text-sm text-text/30">{emptyMsg ?? "None."}</p>
      ) : (
        suppliers.map((s) => <SupplierCard key={s.id} supplier={s} />)
      )}
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export function SupplierEditor() {
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [spectating, setSpectating] = useState(false);
  const activeCall = useActiveCall();

  function reload() {
    apiGet<OverviewData>("/api/track-b/suppliers/overview")
      .then(setOverview)
      .catch(() => undefined);
  }

  useEffect(() => {
    reload();
  }, []);

  useEffect(() => {
    const offSignal = wsClient.on("signal_emitted", (p) => {
      const signal = (p as { signal?: SignalEnvelope }).signal;
      if (signal?.type === "SUPPLIER_PRICE_UPDATE") reload();
    });
    const offEnded = wsClient.on("call_ended", () => reload());
    const offChange = wsClient.on("manager_change", () => reload());
    return () => {
      offSignal();
      offEnded();
      offChange();
    };
  }, []);

  useEffect(() => {
    if (activeCall?.status === "active" && !spectating) setSpectating(true);
    if (!activeCall && spectating) setSpectating(false);
  }, [activeCall, spectating]);

  const totalCurrent = overview?.current.length ?? 0;
  const totalAlternate = overview?.alternate.length ?? 0;

  return (
    <div
      data-track="b"
      data-panel="Suppliers"
      className="flex h-full flex-col gap-4 overflow-auto rounded-lg bg-surface/40 p-4"
    >
      {spectating && activeCall && (
        <SpectateOverlay call={activeCall} onClose={() => setSpectating(false)} />
      )}

      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-text">Suppliers</h2>
        <div className="flex items-center gap-3 text-xs text-text/40">
          <span>{totalCurrent} current</span>
          <span>{totalAlternate} alternate</span>
        </div>
      </div>

      {overview === null ? (
        <div className="flex flex-1 items-center justify-center gap-2 text-sm text-text/30">
          <Loader2 size={16} className="animate-spin" />
          Loading suppliers…
        </div>
      ) : (
        <div className="space-y-6">
          <SuppliersSection
            label="Current suppliers"
            suppliers={overview.current}
            emptyMsg="No current suppliers — run the sourcing optimizer to assign defaults."
          />
          <SuppliersSection
            label="Alternate suppliers"
            suppliers={overview.alternate}
            emptyMsg="No alternates on record."
          />
          <AddSupplierCard onAdded={reload} />
        </div>
      )}
    </div>
  );
}
