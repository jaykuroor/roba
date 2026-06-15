import type { MenuItem, SignalRow, TrackASnapshot } from "./types";

export function itemName(data: TrackASnapshot, id: number) {
  return data.menu_items.find((item) => item.id === id)?.name ?? `Item ${id}`;
}

export function stationName(data: TrackASnapshot, id: number) {
  return data.stations.find((station) => station.id === id)?.name ?? `Station ${id}`;
}

export function latestForecasts(data: TrackASnapshot) {
  const byItem = new Map<number, (typeof data.forecasts)[number]>();
  for (const forecast of data.forecasts) {
    if (!byItem.has(forecast.menu_item_id)) byItem.set(forecast.menu_item_id, forecast);
  }
  return Array.from(byItem.values()).sort((a, b) => a.menu_item_id - b.menu_item_id);
}

export function menuByStation(data: TrackASnapshot, stationId: number): MenuItem[] {
  return data.menu_items.filter((item) => item.station_id === stationId && item.active);
}

export function latestCoverageSignals(data: TrackASnapshot): SignalRow[] {
  const byStation = new Map<number, SignalRow>();
  for (const signal of data.signals) {
    if (signal.type !== "STAFF_COVERAGE") continue;
    const stationId = Number(signal.payload.station_id);
    if (!byStation.has(stationId)) byStation.set(stationId, signal);
  }
  return Array.from(byStation.values());
}

export function formatSimTime(value: number | null | undefined) {
  if (value == null) return "n/a";
  const day = Math.floor(value / 86400);
  const seconds = Math.floor(value % 86400);
  const h = Math.floor(seconds / 3600)
    .toString()
    .padStart(2, "0");
  const m = Math.floor((seconds % 3600) / 60)
    .toString()
    .padStart(2, "0");
  return `D${day} ${h}:${m}`;
}

export function formatQty(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return "0";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 });
}
