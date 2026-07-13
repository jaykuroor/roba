/** Generic entity table editor — escape hatch for any resource. */
import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { apiDelete, apiGet, apiPatch, apiPost } from "../../api";
import type { EntityRow } from "../../types";
import { SectionHeading } from "./shared";
import { CopyStateButton } from "./CopyState";

const ENTITY_RESOURCES = [
  "menu", "recipes", "recipe-lines", "staff", "suppliers", "supplier-catalog",
  "inventory", "competitors", "reviews", "ingredients", "stations",
  "batch-definitions", "promotions",
] as const;

type EntityResource = (typeof ENTITY_RESOURCES)[number];

function EntityCell({ value, onChange }: { value: unknown; onChange: (v: string) => void }) {
  const display =
    value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
  return (
    <input
      value={display} onChange={(e) => onChange(e.target.value)}
      className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-xs text-text outline-none focus:border-accent focus:bg-primary"
    />
  );
}

export function AdvancedEntities() {
  const [resource, setResource] = useState<EntityResource>("menu");
  const [rows, setRows] = useState<EntityRow[]>([]);
  const [edits, setEdits] = useState<Record<number, EntityRow>>({});
  const [busyRow, setBusyRow] = useState<number | null>(null);
  const [busyCreate, setBusyCreate] = useState(false);

  useEffect(() => {
    apiGet<EntityRow[]>(`/api/${resource}`)
      .then((data) => { setRows(data); setEdits({}); })
      .catch(() => undefined);
  }, [resource]);

  const columns = rows.length > 0 ? Object.keys(rows[0]).filter((k) => k !== "id") : [];

  function cellValue(row: EntityRow, idx: number, col: string): unknown {
    return edits[idx]?.[col] ?? row[col];
  }

  function updateCell(idx: number, col: string, rawValue: string) {
    setEdits((prev) => {
      const row = rows[idx];
      let value: unknown = rawValue;
      const original = row[col];
      if (typeof original === "number") {
        const n = Number(rawValue);
        if (!isNaN(n)) value = n;
      } else if (original != null && typeof original === "object") {
        try { value = JSON.parse(rawValue); } catch { value = rawValue; }
      }
      return { ...prev, [idx]: { ...(prev[idx] ?? {}), [col]: value } };
    });
  }

  async function saveRow(idx: number) {
    const row = rows[idx];
    const patch = edits[idx];
    if (!patch || Object.keys(patch).length === 0) return;
    setBusyRow(idx);
    try {
      const updated = await apiPatch<EntityRow>(`/api/${resource}/${row.id}`, patch);
      setRows((prev) => prev.map((r, i) => (i === idx ? updated : r)));
      setEdits((prev) => { const next = { ...prev }; delete next[idx]; return next; });
    } catch { /* ignore */ } finally { setBusyRow(null); }
  }

  async function deleteRow(idx: number) {
    const row = rows[idx];
    try {
      await apiDelete(`/api/${resource}/${row.id}`);
      setRows((prev) => prev.filter((_, i) => i !== idx));
      setEdits((prev) => { const next = { ...prev }; delete next[idx]; return next; });
    } catch { /* ignore */ }
  }

  async function createRow() {
    setBusyCreate(true);
    try {
      const created = await apiPost<EntityRow>(`/api/${resource}`, {});
      setRows((prev) => [...prev, created]);
    } catch { /* ignore */ } finally { setBusyCreate(false); }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <SectionHeading>Copy State</SectionHeading>
        <p className="text-[10px] text-text/40">
          Copies the current weekly forecast, inventory with expiry, and the procurement plan &amp; orders
          to the clipboard as a formatted text report.
        </p>
        <CopyStateButton />
      </div>

      <div className="flex items-center gap-3">
        <SectionHeading>Raw Entity Editor</SectionHeading>
        <select
          value={resource}
          onChange={(e) => setResource(e.target.value as EntityResource)}
          className="rounded-md border border-muted bg-primary px-2 py-1 text-sm text-text outline-none focus:border-accent"
        >
          {ENTITY_RESOURCES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>
      <p className="text-[10px] text-text/40">
        Direct table editor. Edits write immediately via PATCH. Use the purpose-built editors above for guided workflows.
      </p>

      <div className="overflow-x-auto rounded-lg border border-muted">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-muted bg-surface/60">
              <th className="px-2 py-1.5 font-medium text-text/50">id</th>
              {columns.map((col) => (
                <th key={col} className="px-2 py-1.5 font-medium text-text/50">{col}</th>
              ))}
              <th className="px-2 py-1.5" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={String(row.id)} className="border-b border-muted/40 last:border-0 hover:bg-surface/30">
                <td className="px-2 py-1 text-text/40">{String(row.id)}</td>
                {columns.map((col) => (
                  <td key={col} className="px-1 py-0.5">
                    <EntityCell value={cellValue(row, idx, col)} onChange={(v) => updateCell(idx, col, v)} />
                  </td>
                ))}
                <td className="flex items-center gap-1 px-2 py-1">
                  {edits[idx] && Object.keys(edits[idx]).length > 0 && (
                    <button type="button" onClick={() => void saveRow(idx)} disabled={busyRow === idx}
                      className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-white disabled:opacity-50">
                      {busyRow === idx ? "…" : "Save"}
                    </button>
                  )}
                  <button type="button" onClick={() => void deleteRow(idx)} className="text-danger hover:text-danger/70">
                    <Trash2 size={12} />
                  </button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={columns.length + 2} className="py-6 text-center text-text/30">No rows</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <button type="button" onClick={() => void createRow()} disabled={busyCreate}
        className="flex items-center gap-1 rounded-md border border-dashed border-muted px-3 py-2 text-sm text-text/60 hover:border-accent hover:text-text disabled:opacity-50">
        <Plus size={14} /> {busyCreate ? "Creating…" : "New row"}
      </button>
    </div>
  );
}
