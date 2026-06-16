// SupplierEditor — suppliers + supplier_catalog (price/availability/lead) edited
// live, negotiation history, and a per-supplier "Negotiate" button (02 §B6).
// Consumes `signal_emitted(SUPPLIER_PRICE_UPDATE)` + `call_*`; edits via
// PATCH /api/suppliers/{id} and PATCH /api/supplier-catalog/{id}.

import { useEffect, useMemo, useState } from "react";
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
    return () => {
      offSignal();
      offEnded();
    };
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
    setCatalog((prev) => prev.map((c) => (c.id === row.id ? { ...c, availability: availability as SupplierCatalogRow["availability"] } : c)));
  }

  async function negotiate(row: SupplierCatalogRow) {
    const key = `${row.supplier_id}:${row.ingredient_id}`;
    setBusyKey(key);
    try {
      await apiPost("/api/market/negotiate", {
        supplier_id: row.supplier_id,
        ingredient_id: row.ingredient_id,
      });
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <div data-track="b" data-panel="Suppliers" className="flex h-full flex-col gap-4 overflow-auto rounded-lg bg-surface/40 p-3">
      <div>
        <h2 className="mb-2 text-sm font-semibold text-text">Supplier catalog</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-text/40">
              <th className="py-1">Supplier</th>
              <th className="py-1">Ingredient</th>
              <th className="py-1">Price</th>
              <th className="py-1">Availability</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {catalog.map((row) => {
              const key = `${row.supplier_id}:${row.ingredient_id}`;
              return (
                <tr key={row.id} className="border-t border-muted">
                  <td className="py-1 font-medium text-text">{supplierName(row.supplier_id)}</td>
                  <td className="py-1 text-text/70">{ingredientName(row.ingredient_id)}</td>
                  <td className="py-1">
                    <input
                      type="number"
                      step="0.01"
                      defaultValue={row.current_price}
                      onBlur={(e) => {
                        const value = parseFloat(e.target.value);
                        if (!Number.isNaN(value) && value !== row.current_price) updatePrice(row, value);
                      }}
                      className="w-20 rounded border border-muted bg-primary px-1 py-0.5 text-text"
                    />
                  </td>
                  <td className="py-1">
                    <select
                      value={row.availability}
                      onChange={(e) => updateAvailability(row, e.target.value)}
                      className="rounded border border-muted bg-primary px-1 py-0.5 text-text"
                    >
                      <option value="in_stock">in_stock</option>
                      <option value="limited">limited</option>
                      <option value="out">out</option>
                    </select>
                  </td>
                  <td className="py-1">
                    <button
                      type="button"
                      disabled={busyKey === key || activeCall != null}
                      onClick={() => negotiate(row)}
                      className="rounded bg-accent px-2 py-0.5 text-xs font-medium text-white disabled:opacity-50"
                    >
                      Negotiate
                    </button>
                  </td>
                </tr>
              );
            })}
            {catalog.length === 0 && (
              <tr>
                <td colSpan={5} className="py-4 text-center text-text/40">
                  No supplier catalog rows yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

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
                    (saved {n.savings.toFixed(2)})
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
