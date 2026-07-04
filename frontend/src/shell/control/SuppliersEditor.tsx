/**
 * SuppliersEditor (Phase 8) — full supplier management:
 *   - Edit supplier name, lead time, reliability, min-order, phone, delivery charge, volume discounts
 *   - Edit catalog price, availability (with re-run sourcing on default-supplier edits)
 *   - Defaults indicator: per ingredient shows which supplier is is_default + latest SourcingRun rationale
 *   - Manual override of default supplier → ManagerChange card
 *   - Add-supplier → onboarding call → /call tab
 *   - Trigger negotiation via /call tab (same flow as console Suppliers panel)
 */
import { useEffect, useState } from "react";
import { ExternalLink, PhoneCall, Plus, RefreshCw } from "lucide-react";
import { apiGet, apiPatch, apiPost } from "../../api";
import type { Ingredient, SupplierCatalogRow, SupplierRow } from "../../types";
import { SectionHeading } from "./shared";

// ---------------------------------------------------------------------------
// Local types (extending SupplierRow with new fields)
// ---------------------------------------------------------------------------

interface SupplierFull extends SupplierRow {
  phone?: string | null;
  delivery_charge?: number | null;
  volume_discount?: VolumeDiscountTier[] | null;
}

interface VolumeDiscountTier {
  min_value: number;
  discount_pct: number;
}

interface SourcingRunLatest {
  id?: number;
  method?: string;
  total_cost?: number;
  prev_cost?: number;
  savings?: number;
  rationale?: string;
  assignments?: Array<{
    ingredient_id: number;
    supplier_id: number;
    qty: number;
    unit_price: number;
    reason?: string;
  }>;
  created_at?: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtCost(v?: number | null) {
  if (v == null) return "—";
  return `$${Number(v).toFixed(2)}`;
}

function fmtTs(ts?: number | null) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString();
}

// ---------------------------------------------------------------------------
// SuppliersTable — edit supplier metadata incl. delivery charge / phone
// ---------------------------------------------------------------------------

function SuppliersTable({
  suppliers,
  onSaved,
}: {
  suppliers: SupplierFull[];
  onSaved: () => void;
}) {
  const [edits, setEdits] = useState<Record<number, Partial<SupplierFull>>>({});
  const [busyId, setBusyId] = useState<number | null>(null);
  const [discountOpen, setDiscountOpen] = useState<number | null>(null);

  function patch(id: number, p: Partial<SupplierFull>) {
    setEdits((prev) => ({ ...prev, [id]: { ...(prev[id] ?? {}), ...p } }));
  }

  async function save(s: SupplierFull) {
    const p = edits[s.id];
    if (!p || Object.keys(p).length === 0) return;
    setBusyId(s.id);
    try {
      await apiPatch(`/api/suppliers/${s.id}`, p);
      setEdits((prev) => { const n = { ...prev }; delete n[s.id]; return n; });
      onSaved();
    } catch { /* ignore */ } finally { setBusyId(null); }
  }

  function val<K extends keyof SupplierFull>(s: SupplierFull, key: K): string {
    return String((edits[s.id]?.[key] ?? s[key]) ?? "");
  }

  function NumCell({ s, k }: { s: SupplierFull; k: keyof SupplierFull }) {
    return (
      <input
        type="number"
        step="any"
        value={val(s, k)}
        onChange={(e) => patch(s.id, { [k]: Number(e.target.value) } as Partial<SupplierFull>)}
        className="w-20 bg-transparent outline-none focus:border-b focus:border-accent text-xs"
      />
    );
  }

  function updateTier(
    s: SupplierFull,
    tierIdx: number,
    field: keyof VolumeDiscountTier,
    value: number,
  ) {
    const existing: VolumeDiscountTier[] =
      (edits[s.id]?.volume_discount ?? s.volume_discount) ?? [];
    const updated = existing.map((t, i) =>
      i === tierIdx ? { ...t, [field]: value } : t,
    );
    patch(s.id, { volume_discount: updated });
  }

  function addTier(s: SupplierFull) {
    const existing: VolumeDiscountTier[] =
      (edits[s.id]?.volume_discount ?? s.volume_discount) ?? [];
    patch(s.id, { volume_discount: [...existing, { min_value: 100, discount_pct: 5 }] });
  }

  function removeTier(s: SupplierFull, idx: number) {
    const existing: VolumeDiscountTier[] =
      (edits[s.id]?.volume_discount ?? s.volume_discount) ?? [];
    patch(s.id, { volume_discount: existing.filter((_, i) => i !== idx) });
  }

  return (
    <div>
      <SectionHeading>Suppliers</SectionHeading>
      <div className="overflow-x-auto rounded-lg border border-muted">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-muted bg-surface/60">
              {["Name", "Phone", "Lead (days)", "Reliability", "Min order ($)", "Delivery ($)", ""].map(
                (h) => <th key={h} className="px-2 py-1.5 font-medium text-text/50">{h}</th>,
              )}
            </tr>
          </thead>
          <tbody>
            {suppliers.map((s) => {
              const hasPatch = Object.keys(edits[s.id] ?? {}).length > 0;
              return (
                <>
                  <tr
                    key={s.id}
                    className="border-b border-muted/40 last:border-0 hover:bg-surface/20"
                  >
                    <td className="px-2 py-1">
                      <input
                        value={val(s, "name")}
                        onChange={(e) => patch(s.id, { name: e.target.value })}
                        className="w-32 bg-transparent outline-none focus:border-b focus:border-accent"
                      />
                    </td>
                    <td className="px-2 py-1">
                      <input
                        value={val(s, "phone")}
                        onChange={(e) => patch(s.id, { phone: e.target.value })}
                        className="w-28 bg-transparent outline-none focus:border-b focus:border-accent text-xs"
                        placeholder="—"
                      />
                    </td>
                    <td className="px-2 py-1"><NumCell s={s} k="lead_time_days" /></td>
                    <td className="px-2 py-1"><NumCell s={s} k="reliability_score" /></td>
                    <td className="px-2 py-1"><NumCell s={s} k="min_order_value" /></td>
                    <td className="px-2 py-1"><NumCell s={s} k="delivery_charge" /></td>
                    <td className="px-2 py-1 flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => setDiscountOpen((prev) => prev === s.id ? null : s.id)}
                        className="text-[10px] text-text/40 hover:text-text/70"
                        title="Volume discounts"
                      >
                        Discounts ▾
                      </button>
                      {hasPatch && (
                        <button
                          type="button"
                          onClick={() => void save(s)}
                          disabled={busyId === s.id}
                          className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-white disabled:opacity-50"
                        >
                          {busyId === s.id ? "…" : "Save"}
                        </button>
                      )}
                    </td>
                  </tr>
                  {discountOpen === s.id && (
                    <tr key={`${s.id}-disc`} className="bg-surface/30">
                      <td colSpan={7} className="px-4 py-2">
                        <div className="flex flex-col gap-1.5">
                          <span className="text-[10px] font-semibold text-text/40 uppercase">
                            Volume discount tiers
                          </span>
                          {((edits[s.id]?.volume_discount ?? s.volume_discount) ?? []).map(
                            (tier, idx) => (
                              <div key={idx} className="flex items-center gap-2 text-xs">
                                <span className="text-text/40 w-16">Min order</span>
                                <input
                                  type="number"
                                  value={tier.min_value}
                                  onChange={(e) =>
                                    updateTier(s, idx, "min_value", Number(e.target.value))
                                  }
                                  className="w-16 rounded border border-muted bg-primary px-1 py-0.5 text-xs"
                                />
                                <span className="text-text/40">Discount %</span>
                                <input
                                  type="number"
                                  value={tier.discount_pct}
                                  onChange={(e) =>
                                    updateTier(s, idx, "discount_pct", Number(e.target.value))
                                  }
                                  className="w-12 rounded border border-muted bg-primary px-1 py-0.5 text-xs"
                                />
                                <button
                                  type="button"
                                  onClick={() => removeTier(s, idx)}
                                  className="text-danger/60 hover:text-danger"
                                >
                                  ✕
                                </button>
                              </div>
                            ),
                          )}
                          <button
                            type="button"
                            onClick={() => addTier(s)}
                            className="mt-1 flex w-fit items-center gap-1 text-[10px] text-accent hover:text-accent/80"
                          >
                            <Plus size={10} />
                            Add tier
                          </button>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
            {suppliers.length === 0 && (
              <tr>
                <td colSpan={7} className="py-4 text-center text-text/30">
                  No suppliers. Load a seed or add one below.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CatalogTable — with per-row default indicator + manual override button
// ---------------------------------------------------------------------------

function CatalogTable({
  catalog,
  suppliers,
  ingredients,
  sourcingRun,
  onSaved,
  onDefaultChanged,
}: {
  catalog: SupplierCatalogRow[];
  suppliers: SupplierFull[];
  ingredients: Ingredient[];
  sourcingRun: SourcingRunLatest | null;
  onSaved: () => void;
  onDefaultChanged: () => void;
}) {
  const [edits, setEdits] = useState<Record<number, Partial<SupplierCatalogRow>>>({});
  const [busyId, setBusyId] = useState<number | null>(null);
  const [overrideBusy, setOverrideBusy] = useState<string | null>(null);

  const supMap = new Map(suppliers.map((s) => [s.id, s.name]));
  const ingMap = new Map(ingredients.map((i) => [i.id, i.name]));

  function patch(id: number, p: Partial<SupplierCatalogRow>) {
    setEdits((prev) => ({ ...prev, [id]: { ...(prev[id] ?? {}), ...p } }));
  }

  async function save(row: SupplierCatalogRow) {
    const p = edits[row.id];
    if (!p || Object.keys(p).length === 0) return;
    setBusyId(row.id);
    try {
      await apiPatch(`/api/supplier-catalog/${row.id}`, p);
      setEdits((prev) => { const n = { ...prev }; delete n[row.id]; return n; });
      onSaved();
      // If we just edited a default row's price/availability/delivery, re-run sourcing
      if (row.is_default) {
        apiPost("/api/track-b/optimizer/sourcing/run").catch(() => undefined);
      }
    } catch { /* ignore */ } finally { setBusyId(null); }
  }

  async function overrideDefault(ingId: number, supplierId: number) {
    const key = `${ingId}:${supplierId}`;
    setOverrideBusy(key);
    try {
      await apiPost("/api/supplier-catalog/set-default", {
        ingredient_id: ingId,
        supplier_id: supplierId,
      });
      onDefaultChanged();
    } catch { /* ignore */ } finally { setOverrideBusy(null); }
  }

  function val(row: SupplierCatalogRow, key: keyof SupplierCatalogRow): string {
    return String((edits[row.id]?.[key] ?? row[key]) ?? "");
  }

  // Group by ingredient so we can show defaults indicator per-ingredient
  const byIngredient = new Map<number, SupplierCatalogRow[]>();
  for (const row of catalog) {
    const list = byIngredient.get(row.ingredient_id) ?? [];
    list.push(row);
    byIngredient.set(row.ingredient_id, list);
  }
  const sortedIngIds = [...byIngredient.keys()].sort((a, b) =>
    (ingMap.get(a) ?? "").localeCompare(ingMap.get(b) ?? ""),
  );

  // Get sourcing-run rationale for an ingredient
  function sourcingRationale(ingId: number): string | null {
    if (!sourcingRun?.assignments) return null;
    const asgn = sourcingRun.assignments.find((a) => a.ingredient_id === ingId);
    return asgn?.reason ?? sourcingRun.rationale ?? null;
  }

  return (
    <div>
      <SectionHeading>
        Supplier Catalog
        {sourcingRun && (
          <span className="ml-2 text-[10px] font-normal text-text/40">
            (last sourcing run: {fmtTs(sourcingRun.created_at)},
            method: {sourcingRun.method ?? "—"},
            savings: {fmtCost(sourcingRun.savings)})
          </span>
        )}
      </SectionHeading>
      <p className="mb-3 text-[10px] text-text/40">
        Editing price or availability for a default supplier automatically re-runs the sourcing optimizer.
        Use "Override default" to manually choose the default supplier for an ingredient.
      </p>

      <div className="flex flex-col gap-5">
        {sortedIngIds.map((ingId) => {
          const rows = byIngredient.get(ingId)!;
          const defaultRow = rows.find((r) => r.is_default === 1);
          const rationale = sourcingRationale(ingId);

          return (
            <div key={ingId}>
              <div className="mb-1.5 flex items-center gap-2">
                <span className="text-xs font-semibold text-text/60 uppercase tracking-wide">
                  {ingMap.get(ingId) ?? `#${ingId}`}
                </span>
                {defaultRow && (
                  <span className="rounded bg-success/15 px-1.5 py-0.5 text-[10px] text-success">
                    default: {supMap.get(defaultRow.supplier_id) ?? `#${defaultRow.supplier_id}`}
                  </span>
                )}
                {rationale && (
                  <span
                    className="max-w-xs truncate text-[10px] text-text/30 italic"
                    title={rationale}
                  >
                    {rationale}
                  </span>
                )}
              </div>

              <div className="overflow-x-auto rounded-lg border border-muted">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-muted bg-surface/60">
                      {["Supplier", "Price", "Unit", "Pack size", "Availability", "Default", ""].map(
                        (h) => <th key={h} className="px-2 py-1.5 font-medium text-text/50">{h}</th>,
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => {
                      const hasPatch = Object.keys(edits[row.id] ?? {}).length > 0;
                      const isDefault = row.is_default === 1;
                      const overrideKey = `${ingId}:${row.supplier_id}`;

                      return (
                        <tr
                          key={row.id}
                          className={[
                            "border-b border-muted/40 last:border-0",
                            isDefault ? "bg-success/5" : "hover:bg-surface/20",
                          ].join(" ")}
                        >
                          <td className="px-2 py-1 font-medium text-text/70">
                            {supMap.get(row.supplier_id) ?? `#${row.supplier_id}`}
                          </td>
                          <td className="px-2 py-1">
                            <input
                              type="number"
                              step="0.01"
                              value={val(row, "current_price")}
                              onChange={(e) =>
                                patch(row.id, { current_price: Number(e.target.value) })
                              }
                              className="w-20 bg-transparent outline-none focus:border-b focus:border-accent"
                            />
                          </td>
                          <td className="px-2 py-1 text-text/50">{row.unit}</td>
                          <td className="px-2 py-1">
                            <input
                              type="number"
                              step="any"
                              value={val(row, "pack_size")}
                              onChange={(e) =>
                                patch(row.id, { pack_size: Number(e.target.value) })
                              }
                              className="w-16 bg-transparent outline-none focus:border-b focus:border-accent"
                            />
                          </td>
                          <td className="px-2 py-1">
                            <select
                              value={val(row, "availability")}
                              onChange={(e) =>
                                patch(row.id, {
                                  availability:
                                    e.target.value as SupplierCatalogRow["availability"],
                                })
                              }
                              className="rounded border border-muted bg-primary px-1 py-0.5 text-xs text-text outline-none focus:border-accent"
                            >
                              <option value="in_stock">in_stock</option>
                              <option value="limited">limited</option>
                              <option value="out">out</option>
                            </select>
                          </td>
                          <td className="px-2 py-1">
                            {isDefault ? (
                              <span className="text-success text-xs">✓ Default</span>
                            ) : (
                              <button
                                type="button"
                                disabled={overrideBusy === overrideKey}
                                onClick={() => void overrideDefault(ingId, row.supplier_id)}
                                className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-text/50 hover:bg-muted/80 disabled:opacity-50"
                              >
                                {overrideBusy === overrideKey ? "…" : "Override"}
                              </button>
                            )}
                          </td>
                          <td className="px-2 py-1">
                            {hasPatch && (
                              <button
                                type="button"
                                onClick={() => void save(row)}
                                disabled={busyId === row.id}
                                className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-white disabled:opacity-50"
                              >
                                {busyId === row.id ? "…" : "Save"}
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
        {catalog.length === 0 && (
          <p className="py-4 text-center text-text/30 text-xs">
            No catalog entries. Load a seed.
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AddSupplierForm — name + phone → confirm → create → onboarding call tab
// ---------------------------------------------------------------------------

function AddSupplierForm({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [deliveryCharge, setDeliveryCharge] = useState("");
  const [stage, setStage] = useState<"form" | "confirm" | "busy" | "done">("form");
  const [callId, setCallId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleConfirm() {
    if (!name.trim()) return;
    setStage("busy");
    setError(null);
    try {
      // 1. Create the supplier row
      const newSup = await apiPost<{ id: number }>("/api/suppliers", {
        name: name.trim(),
        phone: phone.trim() || null,
        delivery_charge: deliveryCharge ? Number(deliveryCharge) : 0,
        lead_time_days: 2,
        reliability_score: 0.9,
        min_order_value: 0,
        contact: phone.trim() || null,
      });

      // 2. Start an onboarding call (inside the user gesture → no popup blocker)
      const callRes = await apiPost<{ call_id: number }>("/api/calls", {
        counterparty_type: "supplier",
        counterparty_id: newSup.id,
        purpose: `Onboarding call — new supplier ${name.trim()}`,
      });
      setCallId(callRes.call_id);

      // 3. Open the call tab immediately
      window.open(
        `/call?call_id=${callRes.call_id}&role=onboarding_call&party_name=${encodeURIComponent(name.trim())}`,
        "_blank",
      );

      setStage("done");
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
      setStage("form");
    }
  }

  if (stage === "done") {
    return (
      <div className="rounded-lg border border-success/30 bg-success/5 px-4 py-3 text-sm text-success">
        Supplier added. Onboarding call #{callId} opened in a new tab.{" "}
        <button
          type="button"
          onClick={() => { setName(""); setPhone(""); setDeliveryCharge(""); setStage("form"); }}
          className="ml-2 underline text-text/50 hover:text-text/70 text-xs"
        >
          Add another
        </button>
      </div>
    );
  }

  return (
    <div>
      <SectionHeading>Add Supplier</SectionHeading>
      <p className="mb-3 text-[10px] text-text/40">
        Adding a supplier will immediately open an onboarding call in a new tab.
        Roba will introduce the restaurant and gather the full catalog.
      </p>

      {stage === "confirm" ? (
        <div className="rounded-lg border border-warning/30 bg-warning/5 p-4 space-y-3">
          <p className="text-sm text-text">
            Confirm adding <span className="font-semibold">{name}</span>
            {phone && <> ({phone})</>}?
            <br />
            <span className="text-xs text-text/50">An onboarding call will open in a new tab.</span>
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void handleConfirm()}
              className="rounded-md bg-success/20 px-3 py-1.5 text-sm font-medium text-success hover:bg-success/30"
            >
              Confirm & open call
            </button>
            <button
              type="button"
              onClick={() => setStage("form")}
              className="rounded-md bg-muted px-3 py-1.5 text-sm text-text/50 hover:bg-muted/80"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-3 items-end">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase text-text/40">Supplier name *</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Fresh Farms Co."
              className="rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase text-text/40">Phone</span>
            <input
              type="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+1-555-0100"
              className="rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase text-text/40">Delivery charge ($)</span>
            <input
              type="number"
              step="0.01"
              value={deliveryCharge}
              onChange={(e) => setDeliveryCharge(e.target.value)}
              placeholder="0"
              className="w-24 rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
          </label>
          <button
            type="button"
            disabled={!name.trim() || stage === "busy"}
            onClick={() => setStage("confirm")}
            className="flex items-center gap-1.5 rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/80 disabled:opacity-50"
          >
            <Plus size={14} />
            Add supplier
          </button>
        </div>
      )}
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// NegotiateSection — trigger a /call-tab negotiation
// ---------------------------------------------------------------------------

function NegotiateSection({
  suppliers,
  ingredients,
}: {
  suppliers: SupplierFull[];
  ingredients: Ingredient[];
}) {
  const [supplierId, setSupplierId] = useState("");
  const [ingredientId, setIngredientId] = useState("");
  const [busy, setBusy] = useState(false);

  async function negotiate() {
    if (!supplierId || !ingredientId) return;
    setBusy(true);
    try {
      const res = await apiPost<{ call_id: number }>("/api/calls", {
        counterparty_type: "supplier",
        counterparty_id: Number(supplierId),
        purpose: `negotiate ${ingredients.find((i) => i.id === Number(ingredientId))?.name ?? ""} price`,
      });
      const supName = suppliers.find((s) => s.id === Number(supplierId))?.name ?? "";
      window.open(
        `/call?call_id=${res.call_id}&role=supplier_call&party_name=${encodeURIComponent(supName)}`,
        "_blank",
      );
    } catch { /* ignore */ } finally { setBusy(false); }
  }

  return (
    <div>
      <SectionHeading>Trigger Negotiation</SectionHeading>
      <p className="mb-3 text-[10px] text-text/40">
        Open a live negotiation call in a new tab.
        Roba drives the restaurant side; you role-play as the supplier.
      </p>
      <div className="flex flex-wrap gap-3 items-end">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] font-medium uppercase text-text/40">Supplier</span>
          <select
            value={supplierId}
            onChange={(e) => setSupplierId(e.target.value)}
            className="rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
          >
            <option value="">— select —</option>
            {suppliers.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] font-medium uppercase text-text/40">Ingredient</span>
          <select
            value={ingredientId}
            onChange={(e) => setIngredientId(e.target.value)}
            className="rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
          >
            <option value="">— select —</option>
            {ingredients.map((i) => (
              <option key={i.id} value={i.id}>{i.name}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => void negotiate()}
          disabled={busy || !supplierId || !ingredientId}
          className="flex items-center gap-1.5 rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/80 disabled:opacity-50"
        >
          {busy ? <RefreshCw size={13} className="animate-spin" /> : <PhoneCall size={13} />}
          {busy ? "Opening…" : "Negotiate (new tab)"}
          {!busy && <ExternalLink size={11} className="opacity-60" />}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

export function SuppliersEditor() {
  const [suppliers, setSuppliers] = useState<SupplierFull[]>([]);
  const [catalog, setCatalog] = useState<SupplierCatalogRow[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [sourcingRun, setSourcingRun] = useState<SourcingRunLatest | null>(null);
  const [loadingRun, setLoadingRun] = useState(false);

  function loadAll() {
    Promise.all([
      apiGet<SupplierFull[]>("/api/suppliers"),
      apiGet<SupplierCatalogRow[]>("/api/supplier-catalog"),
      apiGet<Ingredient[]>("/api/ingredients"),
    ]).then(([sup, cat, ing]) => {
      setSuppliers(sup);
      setCatalog(cat);
      setIngredients(ing);
    }).catch(() => undefined);
  }

  function loadSourcingRun() {
    setLoadingRun(true);
    apiGet<SourcingRunLatest>("/api/track-b/sourcing/latest")
      .then((r) => setSourcingRun(Object.keys(r).length ? r : null))
      .catch(() => undefined)
      .finally(() => setLoadingRun(false));
  }

  useEffect(() => {
    loadAll();
    loadSourcingRun();
  }, []);

  return (
    <div className="space-y-8">
      <SuppliersTable suppliers={suppliers} onSaved={loadAll} />
      <CatalogTable
        catalog={catalog}
        suppliers={suppliers}
        ingredients={ingredients}
        sourcingRun={sourcingRun}
        onSaved={loadAll}
        onDefaultChanged={() => { loadAll(); loadSourcingRun(); }}
      />

      {/* Run sourcing manually */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={loadingRun}
          onClick={() => {
            setLoadingRun(true);
            apiPost("/api/track-b/optimizer/sourcing/run")
              .then(() => { loadAll(); loadSourcingRun(); })
              .catch(() => undefined)
              .finally(() => setLoadingRun(false));
          }}
          className="flex items-center gap-1.5 rounded-md bg-muted px-3 py-2 text-xs font-medium text-text/60 hover:bg-muted/80 disabled:opacity-50"
        >
          <RefreshCw size={12} className={loadingRun ? "animate-spin" : ""} />
          Re-run sourcing optimizer
        </button>
        {sourcingRun?.savings != null && (
          <span className="text-xs text-text/40">
            Last savings: {fmtCost(sourcingRun.savings)} / {sourcingRun.method}
          </span>
        )}
      </div>

      <AddSupplierForm onAdded={loadAll} />
      <NegotiateSection suppliers={suppliers} ingredients={ingredients} />
    </div>
  );
}
