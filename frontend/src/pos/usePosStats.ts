import { useEffect, useState } from "react";
import { apiGet } from "../api";
import { store } from "../store";
import { wsClient } from "../ws";

// Windowed POS statistics fetched from the backend aggregate (GET /api/pos/stats).
// Server-side aggregation keeps totals accurate for any window (e.g. a full day)
// regardless of the client's bounded live buffer. The `since` boundary is
// recomputed from the latest sim_time at fetch time — NOT held as an effect
// dependency — so a rolling window slides without re-subscribing every tick.

export type PosWindowKey = "day" | "1h" | "6h" | "week";

export const POS_WINDOWS: { key: PosWindowKey; label: string }[] = [
  { key: "day", label: "Today" },
  { key: "1h", label: "Last hour" },
  { key: "6h", label: "Last 6 hours" },
  { key: "week", label: "This week" },
];

const ROLLING_SECONDS: Record<"1h" | "6h", number> = {
  "1h": 3600,
  "6h": 21600,
};

/** Operating-day open (08:00 = 28800 sim-s); the closed-hours jump lands here,
 *  so no orders occur before it. Anchoring "Today" here (not midnight) keeps the
 *  orders/min denominator honest by excluding the closed pre-open hours. */
const OPERATING_OPEN_SIM_S = 28800;

/** Refresh cadence (ms) while the sim is running. */
const POLL_INTERVAL_MS = 3000;

export interface PosWindowStats {
  since: number;
  now: number;
  orders: number;
  revenue: number;
  lines: number;
  voidedLines: number;
  channelSplit: Record<string, number>;
  topItems: { menuItemId: number; qty: number }[];
  buckets: { t: number; orders: number }[];
}

/** `since` (sim-seconds) for a window, from the current sim_time. "day" is the
 *  start of the current operating day; rolling windows look back from now. */
function sinceFor(windowKey: PosWindowKey): number {
  const sim = store.getState().simState;
  const simTime = sim?.sim_time ?? 0;
  const dayIndex = Math.floor(simTime / 86400);
  if (windowKey === "day") {
    return dayIndex * 86400 + OPERATING_OPEN_SIM_S;
  }
  if (windowKey === "week") {
    // Start of the current week: back up to this week's first day, at open.
    const dow = sim?.day_of_week ?? 0;
    return (dayIndex - dow) * 86400 + OPERATING_OPEN_SIM_S;
  }
  return Math.max(0, simTime - ROLLING_SECONDS[windowKey]);
}

interface RawStats {
  since: number;
  now: number;
  orders: number;
  revenue: number;
  lines: number;
  voided_lines: number;
  channel_split: Record<string, number>;
  top_items: { menu_item_id: number; qty: number }[];
  buckets: { t: number; orders: number }[];
}

export function usePosStats(
  windowKey: PosWindowKey,
  running: boolean,
): PosWindowStats | null {
  const [stats, setStats] = useState<PosWindowStats | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      apiGet<RawStats>(`/api/pos/stats?since=${sinceFor(windowKey)}&window=${windowKey}`)
        .then((r) => {
          if (cancelled) return;
          setStats({
            since: r.since,
            now: r.now,
            orders: r.orders,
            revenue: r.revenue,
            lines: r.lines,
            voidedLines: r.voided_lines,
            channelSplit: r.channel_split,
            topItems: r.top_items.map((t) => ({
              menuItemId: t.menu_item_id,
              qty: t.qty,
            })),
            buckets: r.buckets,
          });
        })
        .catch(() => undefined);
    };
    load();
    // Refetch immediately when the run is reset (restart / reseed / start after
    // stop all emit pos_reset and wipe orders) so the cards/chart reflect the
    // cleared data at once, even while stopped (when polling is off).
    const unsubscribe = wsClient.on("pos_reset", load);
    // Only poll while running — orders don't change while paused/stopped.
    const timer = running ? setInterval(load, POLL_INTERVAL_MS) : null;
    return () => {
      cancelled = true;
      unsubscribe();
      if (timer) clearInterval(timer);
    };
  }, [windowKey, running]);

  return stats;
}
