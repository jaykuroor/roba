import { useEffect, useState } from "react";
import { Plus, Trash2, X } from "lucide-react";
import { apiDelete, apiGet, apiPatch, apiPost } from "../api";
import type {
  AnomalyInjection,
  EntityRow,
  MenuItem,
  Scenario,
  ScenarioEvent,
  ScenarioEventType,
  SimSettings,
} from "../types";
import { SCENARIO_EVENT_TYPES } from "../types";

// ===========================================================================
// Shared helpers
// ===========================================================================

/** Ordered daypart names matching config.DAYPARTS. */
const DAYPART_NAMES = [
  "breakfast",
  "lunch",
  "afternoon",
  "dinner",
  "late",
] as const;

/** Config-default weights (§22); used when sim_settings.daypart_curve is absent. */
const DAYPART_DEFAULTS: Record<string, number> = {
  breakfast: 0.18,
  lunch: 0.34,
  afternoon: 0.10,
  dinner: 0.33,
  late: 0.05,
};

/** Config-default channel mix (§22). */
const CHANNEL_DEFAULTS = { dine_in: 0.70, delivery: 0.20, takeout: 0.10 };

const ENTITY_RESOURCES = [
  "menu",
  "recipes",
  "staff",
  "suppliers",
  "inventory",
  "competitors",
  "reviews",
] as const;

type EntityResource = (typeof ENTITY_RESOURCES)[number];

function Label({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[10px] font-medium uppercase tracking-wide text-text/40">
      {children}
    </span>
  );
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-text/50">
      {children}
    </h3>
  );
}

function ApplyButton({
  onClick,
  busy,
}: {
  onClick: () => void;
  busy?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="mt-4 w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/80 disabled:opacity-50"
    >
      {busy ? "Applying…" : "Apply"}
    </button>
  );
}

// ===========================================================================
// Tab: POS Mix
// ===========================================================================

function PosMixPanel() {
  const [settings, setSettings] = useState<SimSettings | null>(null);
  const [menuItems, setMenuItems] = useState<MenuItem[]>([]);
  const [busy, setBusy] = useState(false);

  // Local editable state — initialised from server once loaded.
  const [ordersPerDay, setOrdersPerDay] = useState(300);
  const [channelMix, setChannelMix] = useState({ ...CHANNEL_DEFAULTS });
  const [dishWeights, setDishWeights] = useState<Record<string, number>>({});
  const [daypartWeights, setDaypartWeights] = useState<Record<string, number>>(
    { ...DAYPART_DEFAULTS },
  );

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiGet<SimSettings>("/api/sim/pos"),
      apiGet<MenuItem[]>("/api/menu"),
    ])
      .then(([s, items]) => {
        if (cancelled) return;
        setSettings(s);
        setOrdersPerDay(s.base_orders_per_day ?? 300);
        setChannelMix({ ...CHANNEL_DEFAULTS, ...(s.channel_mix ?? {}) });
        setMenuItems(items.filter((m) => m.active));
        // Seed dish weights from settings; unknown ids default to 1.
        const wts: Record<string, number> = {};
        for (const item of items.filter((m) => m.active)) {
          wts[String(item.id)] = s.dish_mix_weights?.[String(item.id)] ?? 1;
        }
        setDishWeights(wts);
        setDaypartWeights({ ...DAYPART_DEFAULTS, ...(s.daypart_curve ?? {}) });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  function normalise(mix: Record<string, number>): Record<string, number> {
    const total = Object.values(mix).reduce((a, b) => a + b, 0);
    if (total === 0) return mix;
    return Object.fromEntries(Object.entries(mix).map(([k, v]) => [k, v / total]));
  }

  async function apply() {
    setBusy(true);
    try {
      await apiPatch("/api/sim/pos", {
        base_orders_per_day: ordersPerDay,
        channel_mix: normalise(channelMix),
        dish_mix_weights:
          Object.keys(dishWeights).length > 0 ? normalise(dishWeights) : undefined,
        daypart_curve: daypartWeights,
      });
      await apiPost("/api/track-a/forecast/run").catch(() => undefined);
      const updated = await apiGet<SimSettings>("/api/sim/pos");
      setSettings(updated);
    } catch {
      /* ignore; server state is source of truth */
    } finally {
      setBusy(false);
    }
  }

  const channelTotal = Object.values(channelMix).reduce((a, b) => a + b, 0);
  const dishTotal = Object.values(dishWeights).reduce((a, b) => a + b, 0);
  const daypartTotal = Object.values(daypartWeights).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-6">
      {/* Orders per day */}
      <div>
        <SectionHeading>Volume</SectionHeading>
        <label className="flex flex-col gap-1">
          <Label>Base orders / day</Label>
          <input
            type="number"
            min={1}
            max={9999}
            value={ordersPerDay}
            onChange={(e) => setOrdersPerDay(Number(e.target.value))}
            className="w-28 rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
          />
        </label>
      </div>

      {/* Channel mix */}
      <div>
        <SectionHeading>Channel Mix</SectionHeading>
        <p className="mb-2 text-[10px] text-text/40">
          Values are normalised on apply. Sum: {channelTotal.toFixed(2)}
        </p>
        <div className="space-y-2">
          {(["dine_in", "delivery", "takeout"] as const).map((ch) => (
            <label key={ch} className="flex items-center gap-3">
              <span className="w-16 text-xs text-text/60">{ch}</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={channelMix[ch]}
                onChange={(e) =>
                  setChannelMix((prev) => ({
                    ...prev,
                    [ch]: Number(e.target.value),
                  }))
                }
                className="w-36 accent-accent"
              />
              <span className="w-10 text-right text-xs tabular-nums text-text">
                {(channelMix[ch] * 100).toFixed(0)}%
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* Daypart curve */}
      <div>
        <SectionHeading>Daypart Weights</SectionHeading>
        <p className="mb-2 text-[10px] text-text/40">
          Rate multipliers — not normalised. Sum: {daypartTotal.toFixed(2)}{" "}
          (default 1.00)
        </p>
        <div className="space-y-2">
          {DAYPART_NAMES.map((name) => (
            <label key={name} className="flex items-center gap-3">
              <span className="w-16 text-xs text-text/60">{name}</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={daypartWeights[name] ?? DAYPART_DEFAULTS[name]}
                onChange={(e) =>
                  setDaypartWeights((prev) => ({
                    ...prev,
                    [name]: Number(e.target.value),
                  }))
                }
                className="w-36 accent-accent"
              />
              <span className="w-10 text-right text-xs tabular-nums text-text">
                {((daypartWeights[name] ?? DAYPART_DEFAULTS[name]) * 100).toFixed(0)}%
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* Dish mix weights */}
      {menuItems.length > 0 && (
        <div>
          <SectionHeading>Dish Mix Weights</SectionHeading>
          <p className="mb-2 text-[10px] text-text/40">
            Relative weights — normalised on apply. % shown is each item's
            share of orders.
          </p>
          <div className="space-y-2">
            {menuItems.map((item) => {
              const w = dishWeights[String(item.id)] ?? 1;
              const share = dishTotal > 0 ? (w / dishTotal) * 100 : 0;
              return (
                <label key={item.id} className="flex items-center gap-3">
                  <span
                    className="w-36 truncate text-xs text-text/60"
                    title={item.name}
                  >
                    {item.name}
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={10}
                    step={0.1}
                    value={w}
                    onChange={(e) =>
                      setDishWeights((prev) => ({
                        ...prev,
                        [String(item.id)]: Number(e.target.value),
                      }))
                    }
                    className="w-32 accent-accent"
                  />
                  <span className="w-12 text-right text-xs tabular-nums text-text">
                    {share.toFixed(1)}%
                  </span>
                </label>
              );
            })}
          </div>
        </div>
      )}

      <ApplyButton onClick={() => void apply()} busy={busy} />
      {settings && (
        <p className="text-[10px] text-text/30">
          Last read: orders/day={settings.base_orders_per_day ?? "default"}
        </p>
      )}
    </div>
  );
}

// ===========================================================================
// Tab: Anomalies
// ===========================================================================

function AnomalyRow({
  inj,
  onChange,
  onDelete,
}: {
  inj: AnomalyInjection;
  onChange: (updated: AnomalyInjection) => void;
  onDelete: () => void;
}) {
  function num(v: unknown): string {
    return v == null ? "" : String(v);
  }

  function parseNum(s: string): number | undefined {
    const n = parseFloat(s);
    return isNaN(n) ? undefined : n;
  }

  return (
    <div className="rounded-lg border border-muted bg-primary p-3">
      <div className="flex flex-wrap gap-2">
        {(
          [
            { key: "start", label: "Start (sim-s)" },
            { key: "end", label: "End (sim-s)" },
            { key: "velocity_mult", label: "Velocity ×" },
          ] as const
        ).map(({ key, label }) => (
          <label key={key} className="flex flex-col gap-0.5">
            <Label>{label}</Label>
            <input
              type="number"
              value={num(inj[key])}
              onChange={(e) =>
                onChange({ ...inj, [key]: parseNum(e.target.value) })
              }
              placeholder="any"
              className="w-28 rounded-md border border-muted bg-surface px-2 py-1 text-sm text-text outline-none focus:border-accent"
            />
          </label>
        ))}
      </div>
      <div className="mt-2">
        <Label>Dish mix skew (JSON)</Label>
        <textarea
          rows={2}
          value={
            inj.dish_mix_skew
              ? JSON.stringify(inj.dish_mix_skew, null, 2)
              : ""
          }
          onChange={(e) => {
            try {
              const parsed = e.target.value ? JSON.parse(e.target.value) : undefined;
              onChange({ ...inj, dish_mix_skew: parsed });
            } catch {
              /* ignore malformed JSON while typing */
            }
          }}
          placeholder='{"<item_id>": 2.0, ...}'
          className="mt-0.5 w-full rounded-md border border-muted bg-surface px-2 py-1 font-mono text-xs text-text outline-none focus:border-accent"
        />
      </div>
      <button
        type="button"
        onClick={onDelete}
        className="mt-2 flex items-center gap-1 text-xs text-danger hover:text-danger/80"
      >
        <Trash2 size={12} /> Remove
      </button>
    </div>
  );
}

function AnomaliesPanel() {
  const [injections, setInjections] = useState<AnomalyInjection[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiGet<SimSettings>("/api/sim/pos")
      .then((s) => setInjections(s.anomaly_injections ?? []))
      .catch(() => undefined);
  }, []);

  function add() {
    setInjections((prev) => [...prev, {}]);
  }

  function update(idx: number, updated: AnomalyInjection) {
    setInjections((prev) => prev.map((inj, i) => (i === idx ? updated : inj)));
  }

  function remove(idx: number) {
    setInjections((prev) => prev.filter((_, i) => i !== idx));
  }

  async function apply() {
    setBusy(true);
    try {
      await apiPatch("/api/sim/pos", { anomaly_injections: injections });
      await apiPost("/api/track-a/forecast/run").catch(() => undefined);
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <SectionHeading>Anomaly Injections</SectionHeading>
      <p className="text-[10px] text-text/40">
        Windowed velocity surges and dish-mix skews. Active when sim-time is
        within [start, end). Apply replaces the full list.
      </p>
      {injections.length === 0 && (
        <p className="text-sm text-text/40">No injections. Add one below.</p>
      )}
      {injections.map((inj, idx) => (
        <AnomalyRow
          key={idx}
          inj={inj}
          onChange={(updated) => update(idx, updated)}
          onDelete={() => remove(idx)}
        />
      ))}
      <button
        type="button"
        onClick={add}
        className="flex items-center gap-1 rounded-md border border-dashed border-muted px-3 py-2 text-sm text-text/60 hover:border-accent hover:text-text"
      >
        <Plus size={14} /> Add injection
      </button>
      <ApplyButton onClick={() => void apply()} busy={busy} />
    </div>
  );
}

// ===========================================================================
// Tab: Entities
// ===========================================================================

function EntityCell({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (v: string) => void;
}) {
  const display =
    value == null
      ? ""
      : typeof value === "object"
        ? JSON.stringify(value)
        : String(value);
  return (
    <input
      value={display}
      onChange={(e) => onChange(e.target.value)}
      className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-xs text-text outline-none focus:border-accent focus:bg-primary"
    />
  );
}

function EntitiesPanel() {
  const [resource, setResource] = useState<EntityResource>("menu");
  const [rows, setRows] = useState<EntityRow[]>([]);
  const [edits, setEdits] = useState<Record<number, EntityRow>>({});
  const [busyRow, setBusyRow] = useState<number | null>(null);
  const [busyCreate, setBusyCreate] = useState(false);

  useEffect(() => {
    apiGet<EntityRow[]>(`/api/${resource}`)
      .then((data) => {
        setRows(data);
        setEdits({});
      })
      .catch(() => undefined);
  }, [resource]);

  const columns =
    rows.length > 0
      ? Object.keys(rows[0]).filter((k) => k !== "id")
      : [];

  function cellValue(row: EntityRow, idx: number, col: string): unknown {
    return (edits[idx]?.[col] ?? row[col]);
  }

  function updateCell(idx: number, col: string, rawValue: string) {
    setEdits((prev) => {
      const row = rows[idx];
      // Try to coerce to original type.
      let value: unknown = rawValue;
      const original = row[col];
      if (typeof original === "number") {
        const n = Number(rawValue);
        if (!isNaN(n)) value = n;
      } else if (original != null && typeof original === "object") {
        try {
          value = JSON.parse(rawValue);
        } catch {
          value = rawValue;
        }
      }
      return {
        ...prev,
        [idx]: { ...(prev[idx] ?? {}), [col]: value },
      };
    });
  }

  async function saveRow(idx: number) {
    const row = rows[idx];
    const patch = edits[idx];
    if (!patch || Object.keys(patch).length === 0) return;
    setBusyRow(idx);
    try {
      const updated = await apiPatch<EntityRow>(
        `/api/${resource}/${row.id}`,
        patch,
      );
      setRows((prev) => prev.map((r, i) => (i === idx ? updated : r)));
      setEdits((prev) => {
        const next = { ...prev };
        delete next[idx];
        return next;
      });
    } catch {
      /* ignore */
    } finally {
      setBusyRow(null);
    }
  }

  async function deleteRow(idx: number) {
    const row = rows[idx];
    try {
      await apiDelete(`/api/${resource}/${row.id}`);
      setRows((prev) => prev.filter((_, i) => i !== idx));
      setEdits((prev) => {
        const next = { ...prev };
        delete next[idx];
        return next;
      });
    } catch {
      /* ignore */
    }
  }

  async function createRow() {
    setBusyCreate(true);
    try {
      // POST an empty-ish row — the server validates required fields.
      // We just reload the list so the user can fill in inline.
      const created = await apiPost<EntityRow>(`/api/${resource}`, {});
      setRows((prev) => [...prev, created]);
    } catch {
      /* server may reject empty creates for required fields; that's fine */
    } finally {
      setBusyCreate(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <SectionHeading>Entity Editor</SectionHeading>
        <select
          value={resource}
          onChange={(e) => setResource(e.target.value as EntityResource)}
          className="rounded-md border border-muted bg-primary px-2 py-1 text-sm text-text outline-none focus:border-accent"
        >
          {ENTITY_RESOURCES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </div>

      <div className="overflow-x-auto rounded-lg border border-muted">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-muted bg-surface/60">
              <th className="px-2 py-1.5 font-medium text-text/50">id</th>
              {columns.map((col) => (
                <th key={col} className="px-2 py-1.5 font-medium text-text/50">
                  {col}
                </th>
              ))}
              <th className="px-2 py-1.5" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr
                key={String(row.id)}
                className="border-b border-muted/40 last:border-0 hover:bg-surface/30"
              >
                <td className="px-2 py-1 text-text/40">{String(row.id)}</td>
                {columns.map((col) => (
                  <td key={col} className="px-1 py-0.5">
                    <EntityCell
                      value={cellValue(row, idx, col)}
                      onChange={(v) => updateCell(idx, col, v)}
                    />
                  </td>
                ))}
                <td className="flex items-center gap-1 px-2 py-1">
                  {edits[idx] && Object.keys(edits[idx]).length > 0 && (
                    <button
                      type="button"
                      onClick={() => void saveRow(idx)}
                      disabled={busyRow === idx}
                      className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-white disabled:opacity-50"
                    >
                      {busyRow === idx ? "…" : "Save"}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => void deleteRow(idx)}
                    className="text-danger hover:text-danger/70"
                  >
                    <Trash2 size={12} />
                  </button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={columns.length + 2}
                  className="py-6 text-center text-text/30"
                >
                  No rows
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <button
        type="button"
        onClick={() => void createRow()}
        disabled={busyCreate}
        className="flex items-center gap-1 rounded-md border border-dashed border-muted px-3 py-2 text-sm text-text/60 hover:border-accent hover:text-text disabled:opacity-50"
      >
        <Plus size={14} /> {busyCreate ? "Creating…" : "New row"}
      </button>
    </div>
  );
}

// ===========================================================================
// Tab: Scenarios
// ===========================================================================

function EventForm({
  scenarioId,
  event,
  onSaved,
  onCancel,
}: {
  scenarioId: number;
  event?: ScenarioEvent;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [atSimTime, setAtSimTime] = useState(event?.at_sim_time ?? 0);
  const [eventType, setEventType] = useState<ScenarioEventType>(
    (event?.event_type as ScenarioEventType) ?? "change_setting",
  );
  const [payloadStr, setPayloadStr] = useState(
    event?.payload ? JSON.stringify(event.payload, null, 2) : "{}",
  );
  const [busy, setBusy] = useState(false);

  async function save() {
    let payload: unknown;
    try {
      payload = JSON.parse(payloadStr);
    } catch {
      alert("Payload must be valid JSON.");
      return;
    }
    setBusy(true);
    try {
      if (event) {
        await apiPatch(`/api/scenario_events/${event.id}`, {
          at_sim_time: atSimTime,
          event_type: eventType,
          payload,
        });
      } else {
        await apiPost("/api/scenario_events", {
          scenario_id: scenarioId,
          at_sim_time: atSimTime,
          event_type: eventType,
          payload,
          fired: 0,
        });
      }
      onSaved();
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-accent/40 bg-surface p-3">
      <div className="flex flex-wrap gap-3">
        <label className="flex flex-col gap-0.5">
          <Label>Sim time (s)</Label>
          <input
            type="number"
            value={atSimTime}
            onChange={(e) => setAtSimTime(Number(e.target.value))}
            className="w-28 rounded-md border border-muted bg-primary px-2 py-1 text-sm text-text outline-none focus:border-accent"
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <Label>Event type</Label>
          <select
            value={eventType}
            onChange={(e) => setEventType(e.target.value as ScenarioEventType)}
            className="rounded-md border border-muted bg-primary px-2 py-1 text-sm text-text outline-none focus:border-accent"
          >
            {SCENARIO_EVENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="mt-2">
        <Label>Payload (JSON)</Label>
        <textarea
          rows={4}
          value={payloadStr}
          onChange={(e) => setPayloadStr(e.target.value)}
          className="mt-0.5 w-full rounded-md border border-muted bg-primary px-2 py-1 font-mono text-xs text-text outline-none focus:border-accent"
        />
      </div>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-text"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function ScenarioCard({
  scenario,
  onRefresh,
}: {
  scenario: Scenario;
  onRefresh: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [addingEvent, setAddingEvent] = useState(false);
  const [editingEvent, setEditingEvent] = useState<number | null>(null);
  const [editingName, setEditingName] = useState(false);
  const [name, setName] = useState(scenario.name);

  async function toggleActive() {
    const action = scenario.is_active ? "deactivate" : "activate";
    await apiPost(`/api/scenarios/${scenario.id}/${action}`).catch(() => undefined);
    onRefresh();
  }

  async function saveName() {
    if (name === scenario.name) {
      setEditingName(false);
      return;
    }
    await apiPatch(`/api/scenarios/${scenario.id}`, { name }).catch(() => undefined);
    setEditingName(false);
    onRefresh();
  }

  async function deleteScenario() {
    if (!confirm(`Delete scenario "${scenario.name}"?`)) return;
    await apiDelete(`/api/scenarios/${scenario.id}`).catch(() => undefined);
    onRefresh();
  }

  async function deleteEvent(eventId: number) {
    await apiDelete(`/api/scenario_events/${eventId}`).catch(() => undefined);
    onRefresh();
  }

  const events = scenario.events ?? [];

  return (
    <div className="rounded-lg border border-muted bg-surface">
      {/* Header */}
      <div className="flex items-center gap-2 p-3">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className={
            "flex-1 text-left text-sm font-medium text-text " +
            (expanded ? "text-accent" : "")
          }
        >
          {editingName ? (
            <input
              value={name}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onBlur={() => void saveName()}
              onKeyDown={(e) => {
                if (e.key === "Enter") void saveName();
                if (e.key === "Escape") {
                  setName(scenario.name);
                  setEditingName(false);
                }
              }}
              onClick={(e) => e.stopPropagation()}
              className="rounded border border-accent bg-primary px-1 py-0.5 text-sm outline-none"
            />
          ) : (
            <span onDoubleClick={() => setEditingName(true)}>{scenario.name}</span>
          )}
        </button>
        <span
          className={
            "rounded px-1.5 py-0.5 text-[10px] font-medium " +
            (scenario.is_active
              ? "bg-success/20 text-success"
              : "bg-muted text-text/40")
          }
        >
          {scenario.is_active ? "active" : "inactive"}
        </span>
        <button
          type="button"
          onClick={() => void toggleActive()}
          className="rounded-md bg-muted px-2 py-1 text-xs text-text hover:bg-muted/70"
        >
          {scenario.is_active ? "Deactivate" : "Activate"}
        </button>
        <button
          type="button"
          onClick={() => void deleteScenario()}
          className="text-danger hover:text-danger/70"
        >
          <Trash2 size={14} />
        </button>
      </div>

      {/* Events */}
      {expanded && (
        <div className="border-t border-muted/40 px-3 pb-3 pt-2">
          <p className="mb-2 text-[10px] uppercase text-text/40">Events</p>
          {events.length === 0 && (
            <p className="mb-2 text-xs text-text/30">No events yet.</p>
          )}
          <div className="space-y-2">
            {events.map((ev) =>
              editingEvent === ev.id ? (
                <EventForm
                  key={ev.id}
                  scenarioId={scenario.id}
                  event={ev}
                  onSaved={() => {
                    setEditingEvent(null);
                    onRefresh();
                  }}
                  onCancel={() => setEditingEvent(null)}
                />
              ) : (
                <div
                  key={ev.id}
                  className="flex items-start gap-2 rounded border border-muted/40 px-2 py-1.5"
                >
                  <div className="flex-1 text-xs">
                    <span className="font-mono text-text/50">
                      {ev.at_sim_time}s
                    </span>{" "}
                    <span className="rounded bg-muted px-1 py-0.5 text-text/70">
                      {ev.event_type}
                    </span>
                    {ev.fired ? (
                      <span className="ml-1 text-[10px] text-success">
                        fired
                      </span>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    onClick={() => setEditingEvent(ev.id)}
                    className="text-[10px] text-accent hover:underline"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={() => void deleteEvent(ev.id)}
                    className="text-danger hover:text-danger/70"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ),
            )}
          </div>
          {addingEvent ? (
            <div className="mt-2">
              <EventForm
                scenarioId={scenario.id}
                onSaved={() => {
                  setAddingEvent(false);
                  onRefresh();
                }}
                onCancel={() => setAddingEvent(false)}
              />
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setAddingEvent(true)}
              className="mt-2 flex items-center gap-1 text-xs text-text/50 hover:text-text"
            >
              <Plus size={12} /> Add event
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ScenariosPanel() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [creatingName, setCreatingName] = useState("");
  const [creating, setCreating] = useState(false);
  const [busyCreate, setBusyCreate] = useState(false);

  function load() {
    apiGet<Scenario[]>("/api/scenarios")
      .then(setScenarios)
      .catch(() => undefined);
  }

  useEffect(() => {
    load();
  }, []);

  async function createScenario() {
    if (!creatingName.trim()) return;
    setBusyCreate(true);
    try {
      await apiPost("/api/scenarios", {
        name: creatingName.trim(),
        is_active: 0,
      });
      setCreatingName("");
      setCreating(false);
      load();
    } catch {
      /* ignore */
    } finally {
      setBusyCreate(false);
    }
  }

  return (
    <div className="space-y-4">
      <SectionHeading>Scenarios</SectionHeading>
      <div className="space-y-3">
        {scenarios.map((s) => (
          <ScenarioCard key={s.id} scenario={s} onRefresh={load} />
        ))}
        {scenarios.length === 0 && (
          <p className="text-sm text-text/40">No scenarios. Create one below.</p>
        )}
      </div>
      {creating ? (
        <div className="flex items-center gap-2">
          <input
            autoFocus
            value={creatingName}
            onChange={(e) => setCreatingName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void createScenario();
              if (e.key === "Escape") {
                setCreating(false);
                setCreatingName("");
              }
            }}
            placeholder="Scenario name…"
            className="flex-1 rounded-md border border-accent bg-primary px-2 py-1.5 text-sm text-text outline-none"
          />
          <button
            type="button"
            onClick={() => void createScenario()}
            disabled={busyCreate || !creatingName.trim()}
            className="rounded-md bg-accent px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {busyCreate ? "…" : "Create"}
          </button>
          <button
            type="button"
            onClick={() => {
              setCreating(false);
              setCreatingName("");
            }}
            className="rounded-md bg-muted px-3 py-1.5 text-sm text-text"
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="flex items-center gap-1 rounded-md border border-dashed border-muted px-3 py-2 text-sm text-text/60 hover:border-accent hover:text-text"
        >
          <Plus size={14} /> New scenario
        </button>
      )}
    </div>
  );
}

// ===========================================================================
// Drawer shell
// ===========================================================================

type DrawerTab = "POS Mix" | "Anomalies" | "Entities" | "Scenarios";
const DRAWER_TABS: DrawerTab[] = ["POS Mix", "Anomalies", "Entities", "Scenarios"];

export function SettingsDrawer({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [activeTab, setActiveTab] = useState<DrawerTab>("POS Mix");

  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/40"
          onClick={onClose}
          aria-hidden
        />
      )}
      <aside
        className={
          "fixed right-0 top-0 z-50 flex h-full w-[480px] max-w-full flex-col bg-primary shadow-2xl transition-transform duration-200 " +
          (open ? "translate-x-0" : "translate-x-full")
        }
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-muted px-4 py-3">
          <h2 className="text-sm font-semibold text-text">Settings</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-text/40 hover:text-text"
          >
            <X size={18} />
          </button>
        </div>

        {/* Tab strip */}
        <div className="flex gap-1 border-b border-muted px-4 py-2">
          {DRAWER_TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={
                "rounded-md px-3 py-1.5 text-xs font-medium transition-colors " +
                (activeTab === tab
                  ? "bg-accent text-white"
                  : "text-text/60 hover:bg-muted/50 hover:text-text")
              }
            >
              {tab}
            </button>
          ))}
        </div>

        {/* Panel */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {activeTab === "POS Mix" && <PosMixPanel />}
          {activeTab === "Anomalies" && <AnomaliesPanel />}
          {activeTab === "Entities" && <EntitiesPanel />}
          {activeTab === "Scenarios" && <ScenariosPanel />}
        </div>
      </aside>
    </>
  );
}
