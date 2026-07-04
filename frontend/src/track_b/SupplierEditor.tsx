// SupplierEditor — suppliers + supplier_catalog with grouped view.
//
// Per-ingredient groups:
//   "Ordering from" — is_default=1 rows (MILP-chosen default supplier)
//   "Alternates"    — other catalog rows for the same ingredient
//
// Negotiation opens a dedicated /call tab automatically.
// Consumes `signal_emitted(SUPPLIER_PRICE_UPDATE)` + `call_*` WS events.

import { useEffect, useMemo, useState } from "react";
import { ExternalLink, PhoneCall } from "lucide-react";
import { apiGet, apiPatch, apiPost } from "../api";
import { wsClient } from "../ws";
import { useActiveCall } from "../store";
import type {
  Ingredient,
  Negotiation,
  SignalEnvelope,
  SupplierCatalogRow,
  SupplierRow,
} from "../types";

export function SupplierEditor() {
  const [suppliers, setSuppliers] = useState<SupplierRow[]>([]);
  const [catalog, setCatalog] = useState<SupplierCatalogRow[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [negotiations, setNegotiations] = useState<Negotiation[]>([]);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const activeCall = useActiveCall();

  function reload() {
    apiGet<SupplierRow[]>("/api/suppliers").then(setSuppliers).catch(() => undefined);
    apiGet<SupplierCatalogRow[]>("/api/supplier-catalog").then(setCatalog).catch(() => undefined);
    apiGet<Negotiation[]>("/api/negotiations").then(setNegotiations).catch(() => undefined);
  }

  useEffect(() => {
    reload();
    apiGet<Ingredient[]>("/api/ingredients").then(setIngredients).catch(() => undefined);
  }, []);

  useEffect(() => {
    const offSignal = wsClient.on("signal_emitted", (p) => {
      const signal = (p as { signal?: SignalEnvelope }).signal;
      if (signal?.type === "SUPPLIER_PRICE_UPDATE") reload();
    });
    const offEnded = wsClient.on("call_ended", () => reload());
    const offChange = wsClient.on("manager_change", () => reload());
    return () => { offSignal(); offEnded(); offChange(); };
  }, []);

  const ingredientName = useMemo(() => {
    const map = new Map(ingredients.map((i) => [i.id, i.name]));
    return (id: number) => map.get(id) ?? `#${id}`;
  }, [ingredients]);

  const supplierName = useMemo(() => {
    const map = new Map(suppliers.map((s) => [s.id, s.name]));
    return (id: number) => map.get(id) ?? `#${id}`;
  }, [suppliers]);

  async function updatePrice(row: SupplierCatalogRow, current_price: number) {
    await apiPatch(`/api/supplier-catalog/${row.id}`, { current_price });
    setCatalog((prev) => prev.map((c) => (c.id === row.id ? { ...c, current_price } : c)));
  }

  async function updateAvailability(row: SupplierCatalogRow, availability: string) {
    await apiPatch(`/api/supplier-catalog/${row.id}`, { availability });
    setCatalog((prev) =>
      prev.map((c) =>
        c.id === row.id
          ? { ...c, availability: availability as SupplierCatalogRow["availability"] }
          : c,
      ),
    );
  }

  /** Start a negotiation call — opens /call in a new tab. */
  async function negotiate(row: SupplierCatalogRow) {
    const key = `${row.supplier_id}:${row.ingredient_id}`;
    setBusyKey(key);
    try {
      const res = await apiPost<{ call_id: number }>("/api/calls", {
        counterparty_type: "supplier",
        counterparty_id: row.supplier_id,
        purpose: `negotiate ${ingredientName(row.ingredient_id)} price`,
      });
      const supName = supplierName(row.supplier_id);
      window.open(
        `/call?call_id=${res.call_id}&role=supplier_call&party_name=${encodeURIComponent(supName)}`,
        "_blank",
      );
    } catch {
      // fallback: use old market spectator endpoint
      try {
        await apiPost("/api/market/negotiate", {
          supplier_id: row.supplier_id,
          ingredient_id: row.ingredient_id,
        });
      } catch { /* ignore */ }
    } finally {
      setBusyKey(null);
    }
  }

  // Group catalog by ingredient, then separate defaults from alternates
  const byIngredient = useMemo(() => {
    const map = new Map<number, { defaults: SupplierCatalogRow[]; alternates: SupplierCatalogRow[] }>();
    for (const row of catalog) {
      if (!map.has(row.ingredient_id)) {
        map.set(row.ingredient_id, { defaults: [], alternates: [] });
      }
      const group = map.get(row.ingredient_id)!;
      if (row.is_default === 1) {
        group.defaults.push(row);
      } else {
        group.alternates.push(row);
      }
    }
    return map;
  }, [catalog]);

  // Sort ingredient ids for stable display
  const sortedIngredientIds = useMemo(
    () => [...byIngredient.keys()].sort((a, b) => ingredientName(a).localeCompare(ingredientName(b))),
    [byIngredient, ingredientName],
  );

  return (
    <div
      data-track="b"
      data-panel="Suppliers"
      className="flex h-full flex-col gap-4 overflow-auto rounded-lg bg-surface/40 p-3"
    >
      {/* ── Grouped supplier catalog ───────────────────────────────────── */}
      <div>
        <h2 className="mb-3 text-sm font-semibold text-text">Supplier catalog</h2>

        {sortedIngredientIds.length === 0 && (
          <p className="text-xs text-text/40">No supplier catalog rows yet.</p>
        )}

        <div className="flex flex-col gap-4">
          {sortedIngredientIds.map((ingId) => {
            const group = byIngredient.get(ingId)!;
            return (
              <section key={ingId}>
                <h3 className="mb-1.5 text-xs font-semibold text-text/60 uppercase tracking-wide">
                  {ingredientName(ingId)}
                </h3>

                {/* Ordering from */}
                {group.defaults.length > 0 && (
                  <div className="mb-1.5">
                    <p className="text-xs text-success mb-1">✓ Ordering from</p>
                    {group.defaults.map((row) => (
                      <CatalogRow
                        key={row.id}
                        row={row}
                        supplierName={supplierName(row.supplier_id)}
                        busyKey={busyKey}
                        callActive={activeCall != null}
                        onPriceChange={updatePrice}
                        onAvailChange={updateAvailability}
                        onNegotiate={negotiate}
                        highlight
                      />
                    ))}
                  </div>
                )}

                {/* Alternates */}
                {group.alternates.length > 0 && (
                  <div>
                    <p className="text-xs text-text/40 mb-1">Alternates</p>
                    {group.alternates.map((row) => (
                      <CatalogRow
                        key={row.id}
                        row={row}
                        supplierName={supplierName(row.supplier_id)}
                        busyKey={busyKey}
                        callActive={activeCall != null}
                        onPriceChange={updatePrice}
                        onAvailChange={updateAvailability}
                        onNegotiate={negotiate}
                        highlight={false}
                      />
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      </div>

      {/* ── Negotiation history ────────────────────────────────────────── */}
      <div>
        <h2 className="mb-2 text-sm font-semibold text-text">Negotiation history</h2>
        {negotiations.length === 0 ? (
          <p className="text-xs text-text/40">No negotiations yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {negotiations.map((n) => (
              <li key={n.id} className="rounded border border-muted bg-surface p-2 text-sm">
                <span className="font-medium text-text">{supplierName(n.supplier_id)}</span>{" "}
                <span className="text-text/60">— {ingredientName(n.ingredient_id)}</span>{" "}
                {n.savings != null && (
                  <span className={n.savings > 0 ? "text-success" : "text-text/40"}>
                    (saved {n.savings.toFixed(4)})
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CatalogRow sub-component
// ---------------------------------------------------------------------------

function CatalogRow({
  row,
  supplierName,
  busyKey,
  callActive,
  onPriceChange,
  onAvailChange,
  onNegotiate,
  highlight,
}: {
  row: SupplierCatalogRow;
  supplierName: string;
  busyKey: string | null;
  callActive: boolean;
  onPriceChange: (row: SupplierCatalogRow, price: number) => void;
  onAvailChange: (row: SupplierCatalogRow, avail: string) => void;
  onNegotiate: (row: SupplierCatalogRow) => void;
  highlight: boolean;
}) {
  const key = `${row.supplier_id}:${row.ingredient_id}`;

  return (
    <div
      className={[
        "flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 mb-1 text-sm",
        highlight
          ? "border-success/30 bg-success/5"
          : "border-muted/40 bg-surface/30",
      ].join(" ")}
    >
      <span className={`font-medium ${highlight ? "text-text" : "text-text/70"} flex-1 min-w-[6rem]`}>
        {supplierName}
      </span>

      <input
        type="number"
        step="0.0001"
        defaultValue={row.current_price}
        onBlur={(e) => {
          const v = parseFloat(e.target.value);
          if (!Number.isNaN(v) && v !== row.current_price) onPriceChange(row, v);
        }}
        className="w-20 rounded border border-muted bg-primary px-1 py-0.5 text-text"
      />
      <span className="text-xs text-text/40">/{row.unit}</span>

      <select
        value={row.availability}
        onChange={(e) => onAvailChange(row, e.target.value)}
        className="rounded border border-muted bg-primary px-1 py-0.5 text-text"
      >
        <option value="in_stock">in stock</option>
        <option value="limited">limited</option>
        <option value="out">out</option>
      </select>

      <button
        type="button"
        disabled={busyKey === key || callActive}
        onClick={() => onNegotiate(row)}
        className="flex items-center gap-1 rounded bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent hover:bg-accent/25 disabled:opacity-50"
        title="Start a negotiation call in a new tab"
      >
        {busyKey === key ? (
          <span className="animate-spin">⟳</span>
        ) : (
          <>
            <PhoneCall size={10} />
            Negotiate
            <ExternalLink size={9} className="opacity-60" />
          </>
        )}
      </button>
    </div>
  );
}
