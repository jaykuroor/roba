import { useState, useEffect } from "react";
import { RefreshCw, Zap, TrendingUp, Clock, ChevronRight } from "lucide-react";
import { apiGet, apiPatch, apiPost } from "../../api";
import { SectionHeading } from "./shared";
import { ForecastCard } from "../../voice/ForecastCard";
import type {
  IntervalForecastResult,
  HorizonForecast,
  HorizonForecastLine,
  HorizonForecastItem,
  HorizonDay,
} from "../../track_a/types";

function ActionButton({
  label, description, icon, onClick, busy,
}: {
  label: string;
  description: string;
  icon: React.ReactNode;
  onClick: () => void;
  busy: boolean;
}) {
  return (
    <div className="rounded-lg border border-muted bg-surface p-4">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-accent">{icon}</span>
        <span className="text-sm font-medium text-text">{label}</span>
      </div>
      <p className="mb-3 text-xs text-text/50">{description}</p>
      <button
        type="button" onClick={onClick} disabled={busy}
        className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent/80 disabled:opacity-50"
      >
        <RefreshCw size={14} className={busy ? "animate-spin" : undefined} />
        {busy ? "Running…" : label}
      </button>
    </div>
  );
}

type RangePreset = "week" | "day" | "daypart" | "custom";

// ─── horizon detail adapter ───────────────────────────────────────────────────
// Converts a HorizonForecast header + HorizonForecastLines → IntervalForecastResult
// so ForecastCard can render both complex (daypart) and simple (multi-day) forecasts.

function horizonDetailToResult(
  horizon: HorizonForecast,
  lines: HorizonForecastLine[]
): IntervalForecastResult {
  // Per-item totals
  const itemTotals = new Map<number, { name: string; qty: number; baseline: number; confidence: number; count: number }>();
  for (const ln of lines) {
    const prev = itemTotals.get(ln.menu_item_id) ?? { name: ln.item_name, qty: 0, baseline: 0, confidence: 0, count: 0 };
    itemTotals.set(ln.menu_item_id, {
      name: ln.item_name,
      qty: prev.qty + ln.qty,
      baseline: prev.baseline + ln.baseline,
      confidence: prev.confidence + (ln.confidence ?? 0.8),
      count: prev.count + 1,
    });
  }
  const items: HorizonForecastItem[] = Array.from(itemTotals.entries()).map(([id, v]) => ({
    menu_item_id: id,
    name: v.name,
    qty: Math.round(v.qty),
    baseline: Math.round(v.baseline),
    confidence: v.count > 0 ? v.confidence / v.count : undefined,
  }));

  // Per-day totals for by_day
  const dayMap = new Map<number, { qty: number; baseline: number; start: number; end: number; itemMap: Map<number, HorizonForecastItem> }>();
  for (const ln of lines) {
    const prev = dayMap.get(ln.day_index) ?? {
      qty: 0, baseline: 0,
      start: ln.window?.start ?? 0,
      end: ln.window?.end ?? 0,
      itemMap: new Map(),
    };
    prev.qty += ln.qty;
    prev.baseline += ln.baseline;
    if (ln.window) {
      prev.start = Math.min(prev.start, ln.window.start);
      prev.end = Math.max(prev.end, ln.window.end);
    }
    const prevItem = prev.itemMap.get(ln.menu_item_id);
    prev.itemMap.set(ln.menu_item_id, {
      menu_item_id: ln.menu_item_id,
      name: ln.item_name,
      qty: (prevItem?.qty ?? 0) + ln.qty,
      baseline: (prevItem?.baseline ?? 0) + ln.baseline,
    });
    dayMap.set(ln.day_index, prev);
  }
  const by_day: HorizonDay[] = Array.from(dayMap.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([dayIdx, v]) => ({
      day_index: dayIdx,
      start: v.start,
      end: v.end,
      qty: Math.round(v.qty),
      baseline: Math.round(v.baseline),
      items: Array.from(v.itemMap.values()).map((i) => ({
        ...i,
        qty: Math.round(i.qty),
        baseline: Math.round(i.baseline),
      })),
    }));

  // Per-daypart for by_daypart
  const dpMap = new Map<string, { qty: number; baseline: number }>();
  for (const ln of lines) {
    const prev = dpMap.get(ln.daypart) ?? { qty: 0, baseline: 0 };
    dpMap.set(ln.daypart, { qty: prev.qty + ln.qty, baseline: prev.baseline + ln.baseline });
  }
  const by_daypart: Record<string, { qty: number; baseline: number }> = {};
  for (const [dp, v] of dpMap.entries()) {
    by_daypart[dp] = { qty: Math.round(v.qty), baseline: Math.round(v.baseline) };
  }

  return {
    status: "ok",
    horizon_id: horizon.id,
    granularity: horizon.granularity,
    start: horizon.start,
    end: horizon.end,
    total_qty: Math.round(horizon.total_qty ?? 0),
    items,
    by_day,
    by_daypart,
    generated_at: horizon.generated_at,
    trigger_reason: horizon.trigger_reason,
  };
}

// ─── Clickable history row ────────────────────────────────────────────────────

function HorizonHistoryRow({
  row,
  onSelect,
  selected,
}: {
  row: HorizonForecast;
  onSelect: (h: HorizonForecast) => void;
  selected: boolean;
}) {
  const label = row.label ?? row.granularity ?? "forecast";
  const total = row.total_qty ?? 0;
  const dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const startDay = Math.floor(row.start / 86400);

  return (
    <button
      type="button"
      onClick={() => onSelect(row)}
      className={
        "w-full flex items-center justify-between text-xs py-1.5 px-2 rounded-md border-b border-muted/30 last:border-0 text-left transition-colors " +
        (selected
          ? "bg-accent/10 text-accent"
          : "hover:bg-muted/30 text-text/60")
      }
    >
      <div className="flex items-center gap-2 min-w-0">
        <Clock size={10} className="shrink-0 text-text/30" />
        <span className="truncate flex-1">{label}</span>
        <span className="text-text/30 text-[10px] whitespace-nowrap">
          Day {startDay} ({dow[startDay % 7]})
        </span>
      </div>
      <div className="flex items-center gap-1.5 shrink-0 ml-2">
        <span className="font-medium tabular-nums">{Math.round(total).toLocaleString()}</span>
        <ChevronRight size={10} className="text-text/30" />
      </div>
    </button>
  );
}

// ─── IntervalForecastPanel ────────────────────────────────────────────────────

export function IntervalForecastPanel() {
  const [range, setRange] = useState<RangePreset>("week");
  const [dayOffset, setDayOffset] = useState(0);
  const [daypart, setDaypart] = useState("dinner");
  const [startTime, setStartTime] = useState("11:00");
  const [endTime, setEndTime] = useState("15:00");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<IntervalForecastResult | null>(null);
  const [horizons, setHorizons] = useState<HorizonForecast[]>([]);
  const [selectedHorizonId, setSelectedHorizonId] = useState<number | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Fetch saved horizon headers on mount
  useEffect(() => {
    apiGet<{ horizons: HorizonForecast[] }>("/api/track-a/forecast/horizons")
      .then((d) => setHorizons(d.horizons ?? []))
      .catch(() => {});
  }, []);

  async function generate() {
    setBusy(true);
    setSelectedHorizonId(null);
    try {
      const body: Record<string, unknown> = { range, day_offset: dayOffset };
      if (range === "daypart") body.daypart = daypart;
      if (range === "custom") {
        body.start_time = startTime;
        body.end_time = endTime;
      }
      const data = await apiPost("/api/track-a/forecast/horizon", body);
      const r = data as IntervalForecastResult;
      setResult(r);
      // Refresh history list
      apiGet<{ horizons: HorizonForecast[] }>("/api/track-a/forecast/horizons")
        .then((d) => setHorizons(d.horizons ?? []))
        .catch(() => {});
    } catch (e) {
      console.error(e);
    } finally {
      setBusy(false);
    }
  }

  async function handleSelectHorizon(h: HorizonForecast) {
    if (!h.id) return;
    if (selectedHorizonId === h.id) {
      // Toggle off
      setSelectedHorizonId(null);
      setResult(null);
      return;
    }
    setSelectedHorizonId(h.id);
    setLoadingDetail(true);
    try {
      const res = await apiGet<{ horizon: HorizonForecast; lines: HorizonForecastLine[] }>(
        `/api/track-a/forecast/horizon/${h.id}`
      );
      const adapted = horizonDetailToResult(res.horizon ?? h, res.lines ?? []);
      setResult(adapted);
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingDetail(false);
    }
  }

  return (
    <div className="space-y-4">
      <SectionHeading>On-Demand Interval Forecast</SectionHeading>
      <p className="text-[10px] text-text/40">
        Generate a demand forecast for any future interval. Results are saved and can be used by the inventory optimizer.
      </p>

      {/* Range picker */}
      <div className="grid grid-cols-4 gap-1.5">
        {(["week", "day", "daypart", "custom"] as RangePreset[]).map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => setRange(r)}
            className={
              "rounded-md px-2 py-1.5 text-xs font-medium transition-colors " +
              (range === r
                ? "bg-accent text-white"
                : "bg-muted text-text/70 hover:bg-muted/70")
            }
          >
            {r === "week" ? "7-Day" : r === "day" ? "Day" : r === "daypart" ? "Daypart" : "Custom"}
          </button>
        ))}
      </div>

      {/* Contextual inputs */}
      {range !== "week" && (
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <div className="flex-1">
              <label className="text-xs text-text/40 block mb-1">Day offset</label>
              <select
                value={dayOffset}
                onChange={(e) => setDayOffset(Number(e.target.value))}
                className="w-full rounded-md bg-muted/50 border border-muted px-2 py-1.5 text-sm text-text focus:outline-none focus:ring-1 focus:ring-accent"
              >
                <option value={0}>Today</option>
                <option value={1}>Tomorrow</option>
                <option value={2}>Day +2</option>
                <option value={3}>Day +3</option>
                <option value={6}>Day +6</option>
              </select>
            </div>
            {range === "daypart" && (
              <div className="flex-1">
                <label className="text-xs text-text/40 block mb-1">Daypart</label>
                <select
                  value={daypart}
                  onChange={(e) => setDaypart(e.target.value)}
                  className="w-full rounded-md bg-muted/50 border border-muted px-2 py-1.5 text-sm text-text focus:outline-none focus:ring-1 focus:ring-accent"
                >
                  {["breakfast", "lunch", "afternoon", "dinner", "late"].map((dp) => (
                    <option key={dp} value={dp}>{dp.charAt(0).toUpperCase() + dp.slice(1)}</option>
                  ))}
                </select>
              </div>
            )}
          </div>

          {/* Custom: start + end time pickers */}
          {range === "custom" && (
            <>
              <div className="flex items-center gap-3">
                <div className="flex-1">
                  <label className="text-xs text-text/40 block mb-1">Start time</label>
                  <input
                    type="time"
                    value={startTime}
                    onChange={(e) => setStartTime(e.target.value)}
                    className="w-full rounded-md bg-muted/50 border border-muted px-2 py-1.5 text-sm text-text focus:outline-none focus:ring-1 focus:ring-accent"
                  />
                </div>
                <div className="flex-1">
                  <label className="text-xs text-text/40 block mb-1">End time</label>
                  <input
                    type="time"
                    value={endTime}
                    onChange={(e) => setEndTime(e.target.value)}
                    className="w-full rounded-md bg-muted/50 border border-muted px-2 py-1.5 text-sm text-text focus:outline-none focus:ring-1 focus:ring-accent"
                  />
                </div>
              </div>
              <p className="text-[10px] text-text/30">
                Outside operating hours (08:00–23:00) will show "no demand expected."
              </p>
            </>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => void generate()}
        disabled={busy}
        className="flex items-center gap-1.5 rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/80 disabled:opacity-50 w-full justify-center"
      >
        <TrendingUp size={15} className={busy ? "animate-pulse" : undefined} />
        {busy ? "Generating…" : "Generate Forecast"}
      </button>

      {/* Result card */}
      {(result || loadingDetail) && (
        <>
          {loadingDetail && (
            <div className="text-xs text-text/40 text-center py-2">Loading forecast detail…</div>
          )}
          {result && !loadingDetail && (
            <ForecastCard
              forecast={result}
              onDismiss={() => { setResult(null); setSelectedHorizonId(null); }}
            />
          )}
        </>
      )}

      {/* Saved history — click to view full detail */}
      {horizons.length > 0 && (
        <div className="space-y-1">
          <p className="text-[10px] font-semibold uppercase tracking-wide text-text/30">
            Recent forecasts — click to view
          </p>
          <div className="rounded-lg border border-muted bg-surface/50 px-2 py-1">
            {horizons.slice(0, 8).map((h, i) => (
              <HorizonHistoryRow
                key={h.id ?? i}
                row={h}
                onSelect={handleSelectHorizon}
                selected={selectedHorizonId === h.id}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function ForecastControls() {
  const [runBusy, setRunBusy] = useState(false);
  const [finBusy, setFinBusy] = useState(false);
  const [autoMode, setAutoMode] = useState<boolean | null>(null);
  const [autoMuteBusy, setAutoModeBusy] = useState(false);
  const [batchAutoQty, setBatchAutoQty] = useState<boolean>(false);
  const [batchQtyBusy, setBatchQtyBusy] = useState(false);
  const [seedBusy, setSeedBusy] = useState(false);

  // Load current batch_auto_qty setting on mount
  useEffect(() => {
    apiGet<{ batch_auto_qty?: number | boolean }>("/api/sim/pos")
      .then((d) => {
        if (d?.batch_auto_qty != null) setBatchAutoQty(Boolean(d.batch_auto_qty));
      })
      .catch(() => {});
  }, []);

  async function runForecast() {
    setRunBusy(true);
    try { await apiPost("/api/track-a/forecast/run"); }
    catch { /* ignore */ } finally { setRunBusy(false); }
  }

  async function finalizeForecast() {
    setFinBusy(true);
    try { await apiPost("/api/track-a/forecast/finalize"); }
    catch { /* ignore */ } finally { setFinBusy(false); }
  }

  async function toggleAutoMode() {
    const next = autoMode === null ? true : !autoMode;
    setAutoModeBusy(true);
    try {
      await apiPost("/api/track-a/forecast/auto-mode", { enabled: next });
      setAutoMode(next);
    } catch { /* ignore */ } finally { setAutoModeBusy(false); }
  }

  async function toggleBatchAutoQty() {
    const next = !batchAutoQty;
    setBatchQtyBusy(true);
    try {
      await apiPatch("/api/sim/pos", { batch_auto_qty: next });
      setBatchAutoQty(next);
    } catch { /* ignore */ } finally { setBatchQtyBusy(false); }
  }

  async function seedBatches() {
    setSeedBusy(true);
    try { await apiPost("/api/dev/seed-batches"); }
    catch { /* ignore */ } finally { setSeedBusy(false); }
  }

  return (
    <div className="space-y-8">
      <div className="space-y-4">
      <SectionHeading>Forecast Controls</SectionHeading>
      <p className="text-[10px] text-text/40">
        Manually trigger forecast runs or configure auto-mode. The forecaster runs every 30 sim-minutes automatically when the sim is running.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <ActionButton
          label="Run Deterministic Forecast"
          description="Re-runs the baseline × multiplier forecast immediately for all active menu items and triggers batch decisions."
          icon={<RefreshCw size={16} />}
          onClick={() => void runForecast()}
          busy={runBusy}
        />
        <ActionButton
          label="LLM Finalize Forecast"
          description="Sends the current forecast to the LLM for narrative suggestions and priority adjustments."
          icon={<Zap size={16} />}
          onClick={() => void finalizeForecast()}
          busy={finBusy}
        />
      </div>

      <div className="rounded-lg border border-muted bg-surface p-4">
        <p className="mb-1 text-sm font-medium text-text">Auto-mode</p>
        <p className="mb-3 text-xs text-text/50">
          When enabled, the forecaster runs on every signal (weather, competitor intel, reviews, etc.) in addition to the interval timer. The start-of-day batch advisor also activates.
        </p>
        <button
          type="button" onClick={() => void toggleAutoMode()} disabled={autoMuteBusy}
          className={"rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 " +
            (autoMode ? "bg-success/20 text-success hover:bg-success/30" : "bg-muted text-text hover:bg-muted/70")}
        >
          {autoMuteBusy ? "…" : autoMode ? "Auto-mode ON — click to disable" : "Auto-mode OFF — click to enable"}
        </button>
      </div>

      <div className="rounded-lg border border-muted bg-surface p-4">
        <p className="mb-1 text-sm font-medium text-text">Batch quantities</p>
        <p className="mb-3 text-xs text-text/50">
          When enabled, the forecaster's start-of-day advisor can adjust batch quantities automatically without requiring manager approval. Structural changes (add / retime) always require approval.
        </p>
        <button
          type="button" onClick={() => void toggleBatchAutoQty()} disabled={batchQtyBusy}
          className={"rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 " +
            (batchAutoQty ? "bg-success/20 text-success hover:bg-success/30" : "bg-muted text-text hover:bg-muted/70")}
        >
          {batchQtyBusy ? "…" : batchAutoQty ? "Auto qty ON — click to require approval" : "Auto qty OFF — click to enable"}
        </button>
      </div>

      <div className="rounded-lg border border-muted bg-surface p-4">
        <p className="mb-1 text-sm font-medium text-text">Seed batch schedule</p>
        <p className="mb-3 text-xs text-text/50">
          Regenerate today's full batch schedule from the loaded batch definitions. Useful after a preset change or to reset the cook panel.
        </p>
        <button
          type="button" onClick={() => void seedBatches()} disabled={seedBusy}
          className="flex items-center gap-1 rounded-md bg-muted px-3 py-1.5 text-sm font-medium text-text hover:bg-muted/70 disabled:opacity-50"
        >
          <RefreshCw size={14} className={seedBusy ? "animate-spin" : undefined} />
          {seedBusy ? "Seeding…" : "Seed batches"}
        </button>
      </div>
      </div>
      <IntervalForecastPanel />
    </div>
  );
}
