import { useState, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Brain,
  Database,
  Eye,
  Gauge,
  RefreshCw,
  Sparkles,
  ToggleLeft,
  ToggleRight,
} from "lucide-react";
import { apiPost } from "../api";
import {
  formatBaseline,
  formatQty,
  formatSimTime,
  itemName,
  latestForecasts,
} from "./helpers";
import type { EventLog, Forecast, TrackASnapshot } from "./types";
import { EmptyState, Pill, TrackAShell } from "./ui";
import { useTrackAData } from "./useTrackAData";

type ReasonDetail = {
  forecast_id?: number;
  menu_item_id?: number;
  item_name?: string;
  forecast_qty?: number;
  raw_qty?: number;
  explanations?: Record<string, string>;
  multipliers?: Record<string, number>;
  optimized?: boolean;
  hard_override?: number | null;
};

export function ForecastDashboard() {
  const { data, loading, error, refresh } = useTrackAData();
  const [busyAction, setBusyAction] = useState<"run" | "optimize" | "auto" | null>(null);

  async function runForecast() {
    setBusyAction("run");
    try {
      await apiPost("/api/track-a/forecast/run");
      await refresh();
    } finally {
      setBusyAction(null);
    }
  }

  async function optimizeForecast() {
    setBusyAction("optimize");
    try {
      await apiPost("/api/track-a/forecast/optimize");
      await refresh();
    } finally {
      setBusyAction(null);
    }
  }

  async function toggleAutoMode() {
    if (!data) return;
    setBusyAction("auto");
    try {
      await apiPost("/api/track-a/forecast/auto-mode", {
        enabled: !Boolean(data.forecast_agent?.llm_auto_mode),
      });
      await refresh();
    } finally {
      setBusyAction(null);
    }
  }

  if (loading) {
    return (
      <TrackAShell eyebrow="Track A" title="Forecast">
        <EmptyState label="Loading forecasts" />
      </TrackAShell>
    );
  }

  if (error || !data) {
    return (
      <TrackAShell eyebrow="Track A" title="Forecast">
        <EmptyState label={error ?? "Forecast data unavailable"} />
      </TrackAShell>
    );
  }

  const forecasts = latestForecasts(data);
  const reasoningByForecast = buildReasoningMap(data);
  const chartData = forecasts.map((forecast) => ({
    name: itemName(data, forecast.menu_item_id),
    forecast: Math.round(forecast.forecast_qty),
    baseline: Number(forecast.baseline_qty.toFixed(1)),
  }));
  const totalForecast = forecasts.reduce((sum, forecast) => sum + Math.round(forecast.forecast_qty), 0);
  const constrained = forecasts.filter((forecast) => {
    const reason = reasoningByForecast.get(forecast.id);
    return reason?.hard_override === 0 || Object.values(forecast.multipliers ?? {}).some((value) => value === 0);
  }).length;
  return (
    <TrackAShell
      eyebrow={`${data.demo_mode} demand stack`}
      title="Forecast agent"
      action={
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={toggleAutoMode}
            disabled={busyAction !== null}
            className="inline-flex items-center gap-2 rounded-md border border-muted bg-primary/55 px-3 py-2 text-sm font-semibold text-text/80 hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-60"
          >
            {data.forecast_agent?.llm_auto_mode ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
            Auto LLM
          </button>
          <button
            type="button"
            onClick={runForecast}
            disabled={busyAction !== null}
            className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <RefreshCw size={16} className={busyAction === "run" ? "animate-spin" : ""} />
            Run forecast
          </button>
          <button
            type="button"
            onClick={optimizeForecast}
            disabled={busyAction !== null}
            className="inline-flex items-center gap-2 rounded-md bg-[#7c5cff] px-3 py-2 text-sm font-semibold text-white hover:bg-[#6c4df1] disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Sparkles size={16} className={busyAction === "optimize" ? "animate-pulse" : ""} />
            Optimize with LLM
          </button>
        </div>
      }
    >
      {forecasts.length === 0 ? (
        <EmptyState label="No forecasts yet. Start the sim or run a manual forecast." />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 md:grid-cols-3">
            <Metric label="Forecasted plates" value={formatQty(totalForecast)} icon={<Gauge size={17} />} />
            <Metric label="Constrained items" value={formatQty(constrained)} icon={<Eye size={17} />} />
            <Metric label="Memory entries" value={formatQty(data.demand_memory.length)} icon={<Brain size={17} />} />
          </div>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px] xl:items-start">
            <div className="h-[348px] rounded-md border border-[#16426e] bg-[#101d39] p-3">
              <ResponsiveContainer width="100%" height={320}>
                <BarChart data={chartData}>
                  <CartesianGrid stroke="#173760" vertical={false} />
                  <XAxis dataKey="name" stroke="#dce6f7" tick={{ fontSize: 11 }} />
                  <YAxis
                    allowDecimals={false}
                    stroke="#dce6f7"
                    tick={{ fontSize: 11 }}
                  />
                  <Tooltip
                    cursor={{ fill: "rgba(124,92,255,0.12)" }}
                    contentStyle={{
                      background: "#10182f",
                      border: "1px solid #244b7c",
                      borderRadius: 6,
                      color: "#f4f7fb",
                    }}
                  />
                  <Legend />
                  <Bar dataKey="baseline" fill="#1f6f9f" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="forecast" fill="#ef476f" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div className="grid h-[348px] min-h-0 grid-rows-[auto_minmax(0,1fr)_auto_minmax(0,1fr)] gap-3 overflow-hidden">
              <PanelTitle icon={<Sparkles size={15} />} label="Agent notifications" />
              <div className="min-h-0 space-y-2 overflow-y-auto pr-1">
                {latestAgentEvents(data).map((event) => (
                  <div key={event.id} className="rounded-md border border-muted bg-primary/35 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold uppercase text-text/45">
                        {formatSimTime(event.sim_time)}
                      </span>
                      <Pill tone={(event.detail as ReasonDetail | null)?.optimized ? "accent" : "neutral"}>
                        {(event.detail as ReasonDetail | null)?.optimized ? "llm" : event.category}
                      </Pill>
                    </div>
                    <div className="mt-2 text-sm font-medium text-text">{event.summary}</div>
                  </div>
                ))}
              </div>

              <PanelTitle icon={<Database size={15} />} label="Memory" />
              <div className="min-h-0 space-y-2 overflow-y-auto pr-1">
                {data.demand_memory.map((memory) => (
                  <div key={memory.id} className="rounded-md border border-muted bg-[#0f1a33] p-3">
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-sm font-medium text-text">
                        {memory.insight.title ?? `${memory.scope_type}: ${memory.scope_ref}`}
                      </div>
                      <Pill tone={memory.source === "llm" ? "accent" : "neutral"}>
                        {Math.round(memory.confidence * 100)}%
                      </Pill>
                    </div>
                    {memory.insight.summary ? (
                      <div className="mt-1 text-xs leading-5 text-text/60">{String(memory.insight.summary)}</div>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="mt-4 overflow-hidden rounded-md border border-muted">
        <table className="w-full min-w-[980px] border-collapse text-left text-sm">
          <thead className="bg-primary/70 text-xs uppercase tracking-wide text-text/50">
            <tr>
              <th className="px-3 py-2">Item</th>
              <th className="px-3 py-2">Window</th>
              <th className="px-3 py-2">Generated</th>
              <th className="px-3 py-2">Baseline</th>
              <th className="px-3 py-2">Forecast</th>
              <th className="px-3 py-2">Multipliers</th>
              <th className="px-3 py-2">Reason</th>
            </tr>
          </thead>
          <tbody>
            {forecasts.map((forecast) => {
              const reason = reasoningByForecast.get(forecast.id);
              return (
                <tr key={forecast.id} className="border-t border-muted/70 align-top">
                  <td className="px-3 py-3 font-medium">{itemName(data, forecast.menu_item_id)}</td>
                  <td className="px-3 py-3 text-text/65">
                    {forecast.daypart} - {formatSimTime(forecast.window.start)}
                  </td>
                  <td className="px-3 py-3 text-text/65">{formatSimTime(forecast.generated_at)}</td>
                  <td className="px-3 py-3">{formatBaseline(forecast.baseline_qty)}</td>
                  <td className="px-3 py-3 text-lg font-semibold text-accent">{formatQty(forecast.forecast_qty)}</td>
                  <td className="px-3 py-3">
                    <div className="flex max-w-xl flex-wrap gap-1.5">
                      {Object.entries(forecast.multipliers ?? {}).map(([key, value]) => (
                        <Pill key={key} tone={value > 1 ? "good" : value < 1 ? "warn" : "neutral"}>
                          {key.replace("_", " ")} x{Number(value).toFixed(2)}
                        </Pill>
                      ))}
                      <Pill tone="accent">
                        <Sparkles size={12} className="mr-1" />
                        {Math.round(forecast.confidence * 100)}%
                      </Pill>
                    </div>
                  </td>
                  <td className="max-w-md px-3 py-3 text-xs leading-5 text-text/65">
                    {reasonSummary(reason, forecast)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <RawPanel title="Latest forecast output" value={forecasts.slice(0, 8)} />
        <RawPanel title="Reasoning payloads" value={data.forecast_reasoning.slice(0, 8)} />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {data.batches.slice(0, 6).map((batch) => (
          <div key={batch.id} className="rounded-md border border-muted bg-primary/30 p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="font-medium">{itemName(data, batch.menu_item_id)}</div>
              <Pill tone={batch.decision === "cook" ? "good" : "bad"}>{batch.decision}</Pill>
            </div>
            <div className="mt-2 text-sm text-text/65">
              {formatQty(batch.planned_qty)} planned for {formatSimTime(batch.serve_window?.start)}
            </div>
          </div>
        ))}
      </div>
    </TrackAShell>
  );
}

function Metric({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon: ReactNode;
}) {
  return (
    <div className="rounded-md border border-[#1d4d7d] bg-[#0f1c35] p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs font-semibold uppercase text-text/45">{label}</div>
        <div className="text-[#7c5cff]">{icon}</div>
      </div>
      <div className="mt-2 text-2xl font-semibold text-text">{value}</div>
    </div>
  );
}

function PanelTitle({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="flex items-center gap-2 text-xs font-semibold uppercase text-text/45">
      <span className="text-[#7c5cff]">{icon}</span>
      {label}
    </div>
  );
}

function RawPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="rounded-md border border-muted bg-primary/30">
      <div className="border-b border-muted px-3 py-2 text-xs font-semibold uppercase text-text/45">
        {title}
      </div>
      <pre className="max-h-80 overflow-auto p-3 text-xs leading-5 text-text/65">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function buildReasoningMap(data: TrackASnapshot) {
  const map = new Map<number, ReasonDetail>();
  for (const event of data.forecast_reasoning ?? []) {
    if (event.category !== "forecast") continue;
    const detail = event.detail as ReasonDetail | null;
    if (!detail?.forecast_id) continue;
    if (!map.has(detail.forecast_id)) map.set(detail.forecast_id, detail);
  }
  return map;
}

function latestAgentEvents(data: TrackASnapshot): EventLog[] {
  return [...(data.forecast_reasoning ?? [])].sort((a, b) => b.sim_time - a.sim_time);
}

function reasonSummary(reason: ReasonDetail | undefined, forecast: Forecast) {
  if (!reason?.explanations) {
    return forecast.trigger_reason === "llm_manual"
      ? "Optimized forecast emitted through the LLM stack."
      : "Deterministic forecast emitted through the base stack.";
  }
  const multipliers = forecast.multipliers ?? {};
  const hardPriority = ["llm_override", "availability", "llm_target"];
  for (const key of hardPriority) {
    if (reason.explanations[key]) return reason.explanations[key];
  }
  if ((multipliers.staff_coverage ?? 1) < 0.99 && reason.explanations.staff_coverage) {
    return reason.explanations.staff_coverage;
  }
  const driverPriority = [
    "event",
    "settings_demand",
    "llm_overall",
    "weather",
    "recent_velocity",
    "competitor",
    "review",
  ];
  for (const key of driverPriority) {
    const value = Number(multipliers[key] ?? 1);
    if (reason.explanations[key] && Math.abs(value - 1) >= 0.03) {
      return reason.explanations[key];
    }
  }
  return forecast.trigger_reason === "llm_manual"
    ? "LLM optimization ran, with no major multiplier override for this item."
    : "Forecast generated with no major active demand driver.";
}
