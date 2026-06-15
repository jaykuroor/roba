import { RefreshCw, Sparkles } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiPost } from "../api";
import { formatQty, formatSimTime, itemName, latestForecasts } from "./helpers";
import { EmptyState, Pill, TrackAShell } from "./ui";
import { useTrackAData } from "./useTrackAData";

export function ForecastDashboard() {
  const { data, loading, error, refresh } = useTrackAData();

  async function runForecast() {
    await apiPost("/api/track-a/forecast/run");
    await refresh();
  }

  if (loading) return <TrackAShell eyebrow="Track A" title="Forecast"><EmptyState label="Loading forecasts" /></TrackAShell>;
  if (error || !data) return <TrackAShell eyebrow="Track A" title="Forecast"><EmptyState label={error ?? "Forecast data unavailable"} /></TrackAShell>;

  const forecasts = latestForecasts(data);
  const chartData = forecasts.map((forecast) => ({
    name: itemName(data, forecast.menu_item_id),
    forecast: forecast.forecast_qty,
    baseline: forecast.baseline_qty,
  }));

  return (
    <TrackAShell
      eyebrow={`${data.demo_mode} demand loop`}
      title="Forecast dashboard"
      action={
        <button
          type="button"
          onClick={runForecast}
          className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent/90"
        >
          <RefreshCw size={16} />
          Run forecast
        </button>
      }
    >
      {forecasts.length === 0 ? (
        <EmptyState label="No forecasts yet. Start the sim or run a manual forecast." />
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="min-h-80 rounded-md border border-muted bg-primary/25 p-3">
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={chartData}>
                <CartesianGrid stroke="#0f3460" vertical={false} />
                <XAxis dataKey="name" stroke="#eaeaea" tick={{ fontSize: 11 }} />
                <YAxis stroke="#eaeaea" tick={{ fontSize: 11 }} />
                <Tooltip cursor={{ fill: "rgba(233,69,96,0.12)" }} />
                <Bar dataKey="baseline" fill="#0f3460" radius={[4, 4, 0, 0]} />
                <Bar dataKey="forecast" fill="#e94560" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="space-y-3">
            {data.batches.slice(0, 5).map((batch) => (
              <div key={batch.id} className="rounded-md border border-muted bg-primary/30 p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="font-medium">{itemName(data, batch.menu_item_id)}</div>
                  <Pill tone={batch.decision === "cook" ? "good" : "bad"}>
                    {batch.decision}
                  </Pill>
                </div>
                <div className="mt-2 text-sm text-text/65">
                  {formatQty(batch.planned_qty)} planned for {formatSimTime(batch.serve_window?.start)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="mt-4 overflow-hidden rounded-md border border-muted">
        <table className="w-full min-w-[760px] border-collapse text-left text-sm">
          <thead className="bg-primary/70 text-xs uppercase tracking-wide text-text/50">
            <tr>
              <th className="px-3 py-2">Item</th>
              <th className="px-3 py-2">Window</th>
              <th className="px-3 py-2">Baseline</th>
              <th className="px-3 py-2">Forecast</th>
              <th className="px-3 py-2">Why</th>
            </tr>
          </thead>
          <tbody>
            {forecasts.map((forecast) => (
              <tr key={forecast.id} className="border-t border-muted/70">
                <td className="px-3 py-3 font-medium">{itemName(data, forecast.menu_item_id)}</td>
                <td className="px-3 py-3 text-text/65">{forecast.daypart} · {formatSimTime(forecast.window.start)}</td>
                <td className="px-3 py-3">{formatQty(forecast.baseline_qty)}</td>
                <td className="px-3 py-3 font-semibold text-accent">{formatQty(forecast.forecast_qty)}</td>
                <td className="px-3 py-3">
                  <div className="flex flex-wrap gap-1.5">
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
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </TrackAShell>
  );
}
