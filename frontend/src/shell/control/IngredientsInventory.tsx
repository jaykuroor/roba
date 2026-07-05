/** Read + edit inventory levels and view inventory lots. */
import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { apiGet, apiPatch } from "../../api";
import type { Ingredient, InventoryLevel, InventoryLot } from "../../types";
import { SectionHeading } from "./shared";
import { formatQty } from "../../utils/units";

interface LevelRow extends InventoryLevel {
  ingredient_name?: string;
  ingredient_unit?: "g" | "ml" | "each";
  patch?: Partial<InventoryLevel>;
}

type OosMode = "threshold" | "zero";

function OosModeToggle() {
  const [oosMode, setOosMode] = useState<OosMode>("threshold");

  useEffect(() => {
    apiGet<{ availability_oos_mode?: OosMode }>("/api/sim/pos")
      .then((data) => {
        if (data.availability_oos_mode === "zero" || data.availability_oos_mode === "threshold") {
          setOosMode(data.availability_oos_mode);
        }
      })
      .catch(() => undefined);
  }, []);

  async function handleOosModeChange(mode: OosMode) {
    setOosMode(mode);
    try {
      await apiPatch("/api/sim/pos", { availability_oos_mode: mode });
    } catch {
      // revert on error
      setOosMode((prev) => (prev === mode ? (mode === "threshold" ? "zero" : "threshold") : prev));
    }
  }

  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-text/60">Disable items when ingredient is</span>
      <label className="flex items-center gap-1 cursor-pointer">
        <input
          type="radio"
          name="oos_mode"
          value="threshold"
          checked={oosMode === "threshold"}
          onChange={() => void handleOosModeChange("threshold")}
        />
        <span className="text-text/80">at/below threshold</span>
      </label>
      <label className="flex items-center gap-1 cursor-pointer">
        <input
          type="radio"
          name="oos_mode"
          value="zero"
          checked={oosMode === "zero"}
          onChange={() => void handleOosModeChange("zero")}
        />
        <span className="text-text/80">at zero only</span>
      </label>
    </div>
  );
}

function IngredientsTable() {
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);

  useEffect(() => {
    apiGet<Ingredient[]>("/api/ingredients").then(setIngredients).catch(() => undefined);
  }, []);

  return (
    <div>
      <SectionHeading>Ingredients</SectionHeading>
      <div className="overflow-x-auto rounded-lg border border-muted">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-muted bg-surface/60">
              {["ID", "Name", "Category", "Unit", "Shelf life (days)", "Perishable"].map((h) => (
                <th key={h} className="px-2 py-1.5 font-medium text-text/50">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ingredients.map((ing) => (
              <tr key={ing.id} className="border-b border-muted/40 last:border-0 hover:bg-surface/30">
                <td className="px-2 py-1 text-text/40">{ing.id}</td>
                <td className="px-2 py-1 text-text">{ing.name}</td>
                <td className="px-2 py-1 text-text/60">{ing.category ?? "—"}</td>
                <td className="px-2 py-1 text-text/60">{ing.base_unit}</td>
                <td className="px-2 py-1 text-text/60">{ing.shelf_life_days ?? "—"}</td>
                <td className="px-2 py-1 text-text/60">{ing.perishable ? "yes" : "no"}</td>
              </tr>
            ))}
            {ingredients.length === 0 && (
              <tr><td colSpan={6} className="py-4 text-center text-text/30">No ingredients. Load a seed first.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function InventoryLevelsEditor() {
  const [rows, setRows] = useState<LevelRow[]>([]);
  const [busyId, setBusyId] = useState<number | null>(null);

  useEffect(() => {
    Promise.all([
      apiGet<InventoryLevel[]>("/api/inventory"),
      apiGet<Ingredient[]>("/api/ingredients"),
    ]).then(([levels, ings]) => {
      const ingMap = new Map(ings.map((i) => [i.id, i.name]));
      const unitMap = new Map(ings.map((i) => [i.id, i.base_unit]));
      setRows(levels.map((l) => ({
        ...l,
        ingredient_name: ingMap.get(l.ingredient_id) ?? `#${l.ingredient_id}`,
        ingredient_unit: unitMap.get(l.ingredient_id) ?? "g",
        patch: {},
      })));
    }).catch(() => undefined);
  }, []);

  function updatePatch(id: number, patch: Partial<InventoryLevel>) {
    setRows((prev) => prev.map((r) => r.id === id ? { ...r, patch: { ...(r.patch ?? {}), ...patch } } : r));
  }

  async function saveRow(row: LevelRow) {
    if (!row.patch || Object.keys(row.patch).length === 0) return;
    setBusyId(row.id);
    try {
      const updated = await apiPatch<InventoryLevel>(`/api/inventory/${row.id}`, row.patch);
      setRows((prev) => prev.map((r) => r.id === row.id ? { ...r, ...updated, patch: {} } : r));
    } catch { /* ignore */ } finally { setBusyId(null); }
  }

  function cell(row: LevelRow, key: keyof InventoryLevel) {
    const val = (row.patch?.[key] ?? row[key]) as string | number | null;
    return val == null ? "" : String(val);
  }

  function numField(row: LevelRow, key: keyof InventoryLevel, label: string) {
    return (
      <label key={key} className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text/40">{label}</span>
        <input
          type="number" step="any" value={cell(row, key)}
          onChange={(e) => updatePatch(row.id, { [key]: Number(e.target.value) } as Partial<InventoryLevel>)}
          className="w-24 rounded border border-muted bg-primary px-1 py-0.5 text-xs text-text outline-none focus:border-accent"
        />
      </label>
    );
  }

  return (
    <div>
      <SectionHeading>Inventory Levels</SectionHeading>
      <p className="mb-3 text-[10px] text-text/40">
        Edit par levels, reorder points, and safety stock. Changes take effect on the next reorder check.
      </p>
      <div className="space-y-3">
        {rows.map((row) => {
          const hasPatch = Object.keys(row.patch ?? {}).length > 0;
          return (
            <div key={row.id} className="rounded-lg border border-muted bg-surface p-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-sm font-medium text-text">{row.ingredient_name}</span>
                <span className="text-xs tabular-nums text-text/50">
                  on hand: <span className="text-success font-medium">{formatQty(row.on_hand_cached ?? 0, row.ingredient_unit ?? "g")}</span>
                </span>
              </div>
              <div className="flex flex-wrap gap-3">
                {numField(row, "par_level", "Par level")}
                {numField(row, "reorder_point", "Reorder point")}
                {numField(row, "safety_stock", "Safety stock")}
                {numField(row, "yield_factor", "Yield factor")}
              </div>
              {hasPatch && (
                <button type="button" onClick={() => void saveRow(row)} disabled={busyId === row.id}
                  className="mt-2 flex items-center gap-1 rounded bg-accent px-2 py-1 text-xs text-white disabled:opacity-50">
                  <Save size={10} /> {busyId === row.id ? "Saving…" : "Save changes"}
                </button>
              )}
            </div>
          );
        })}
        {rows.length === 0 && (
          <p className="text-sm text-text/40">No inventory data. Load a seed first.</p>
        )}
      </div>
    </div>
  );
}

function LotsView() {
  const [lots, setLots] = useState<InventoryLot[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);

  useEffect(() => {
    Promise.all([
      apiGet<InventoryLot[]>("/api/inventory/lots"),
      apiGet<Ingredient[]>("/api/ingredients"),
    ]).then(([l, i]) => { setLots(l); setIngredients(i); }).catch(() => undefined);
  }, []);

  const ingMap = new Map(ingredients.map((i) => [i.id, i.name]));
  const unitMap = new Map(ingredients.map((i) => [i.id, i.base_unit]));

  const activeLots = lots.filter((l) => l.status === "active");

  return (
    <div>
      <SectionHeading>Active Inventory Lots</SectionHeading>
      <div className="overflow-x-auto rounded-lg border border-muted">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-muted bg-surface/60">
              {["Ingredient", "Qty on hand", "Expiry (sim-s)", "Status"].map((h) => (
                <th key={h} className="px-2 py-1.5 font-medium text-text/50">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {activeLots.map((lot) => (
              <tr key={lot.id} className="border-b border-muted/40 last:border-0">
                <td className="px-2 py-1">{ingMap.get(lot.ingredient_id) ?? `#${lot.ingredient_id}`}</td>
                <td className="px-2 py-1 tabular-nums">
                  {formatQty(lot.qty_on_hand, unitMap.get(lot.ingredient_id) ?? lot.unit ?? "g")}
                </td>
                <td className="px-2 py-1 tabular-nums text-text/50">{lot.expiry_date?.toFixed(0) ?? "—"}</td>
                <td className="px-2 py-1">
                  <span className="rounded bg-success/20 px-1 py-0.5 text-[10px] text-success">{lot.status}</span>
                </td>
              </tr>
            ))}
            {activeLots.length === 0 && (
              <tr><td colSpan={5} className="py-4 text-center text-text/30">No active lots.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function IngredientsInventory() {
  return (
    <div className="space-y-8">
      <OosModeToggle />
      <IngredientsTable />
      <InventoryLevelsEditor />
      <LotsView />
    </div>
  );
}
