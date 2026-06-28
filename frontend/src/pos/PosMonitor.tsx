import { useEffect, useState, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiGet } from "../api";
import { useSimState } from "../store";
import type { MenuItem } from "../types";
import { EmptyState, Pill, TrackAShell } from "../track_a/ui";
import { usePosStream } from "./usePosStream";
import { POS_WINDOWS, usePosStats, type PosWindowKey } from "./usePosStats";

// Live POS monitor. Windowed totals (cards, channel split, top items, chart)
// come from the backend aggregate (usePosStats) so they're accurate for any
// selected window; the live order ticker and per-item velocity come from the
// streamed buffer (usePosStream).

function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
}) {
  return (
    <div className="rounded-md border border-muted/70 bg-primary/40 p-3">
      <div className="text-xs font-semibold uppercase tracking-wide text-text/40">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold text-text">{value}</div>
      {sub ? <div className="mt-0.5 text-xs text-text/50">{sub}</div> : null}
    </div>
  );
}

function fmtMoney(n: number): string {
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function simTimeToClock(simTime: number): string {
  const secOfDay = ((simTime % 86400) + 86400) % 86400;
  const h = Math.floor(secOfDay / 3600);
  const m = Math.floor((secOfDay % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function simTimeToLabel(simTime: number, windowKey: PosWindowKey): string {
  if (windowKey === "week") {
    // Each bucket is one day; label by day-of-week (sim day 0 = Monday).
    const dayIndex = Math.floor(simTime / 86400) % 7;
    return DAY_NAMES[dayIndex] ?? `D${dayIndex}`;
  }
  return simTimeToClock(simTime);
}

function WindowSelect({
  value,
  onChange,
}: {
  value: PosWindowKey;
  onChange: (key: PosWindowKey) => void;
}) {
  return (
    <div className="flex items-center gap-1 rounded-md bg-primary/40 p-0.5">
      {POS_WINDOWS.map((w) => (
        <button
          key={w.key}
          type="button"
          onClick={() => onChange(w.key)}
          className={
            value === w.key
              ? "rounded px-2.5 py-1 text-xs font-medium bg-accent text-white"
              : "rounded px-2.5 py-1 text-xs font-medium text-text/60 hover:bg-muted/50 hover:text-text"
          }
        >
          {w.label}
        </button>
      ))}
    </div>
  );
}

export function PosMonitor() {
  const { orders, velocity, ready } = usePosStream();
  const simState = useSimState();
  const running = simState?.status === "running";
  const [windowKey, setWindowKey] = useState<PosWindowKey>("day");
  const stats = usePosStats(windowKey, running);
  const [menuNames, setMenuNames] = useState<Record<number, string>>({});

  // Fetch the menu for id → name resolution, re-fetching when a new restaurant
  // is seeded (active_seed_id changes) so orders resolve to names not "#<id>".
  const activeSeedId = simState?.active_seed_id ?? null;
  useEffect(() => {
    apiGet<MenuItem[]>("/api/menu")
      .then((items) => {
        const map: Record<number, string> = {};
        for (const item of items) map[item.id] = item.name;
        setMenuNames(map);
      })
      .catch(() => undefined);
  }, [activeSeedId]);

  const nameOf = (id: number) => menuNames[id] ?? `#${id}`;

  // An order can carry several lines for the same dish (each qty 1); collapse
  // them into one "N× Name" entry instead of repeating "1× Name".
  const summarizeLines = (lines: { menu_item_id: number; qty: number }[]) => {
    const byItem = new Map<number, number>();
    for (const line of lines) {
      byItem.set(line.menu_item_id, (byItem.get(line.menu_item_id) ?? 0) + line.qty);
    }
    return [...byItem.entries()]
      .map(([id, qty]) => `${qty}× ${nameOf(id)}`)
      .join(", ");
  };

  const elapsedMin = stats ? (stats.now - stats.since) / 60 : 0;
  const ordersPerMin =
    stats && elapsedMin > 0 ? stats.orders / elapsedMin : null;
  const voidRate =
    stats && stats.lines > 0 ? stats.voidedLines / stats.lines : 0;

  const chartData = (stats?.buckets ?? []).map((b) => ({
    label: simTimeToLabel(b.t, windowKey),
    orders: b.orders,
  }));
  const channelData = Object.entries(stats?.channelSplit ?? {})
    .sort((a, b) => b[1] - a[1])
    .map(([channel, count]) => ({ channel, count }));

  const hasData = (stats?.orders ?? 0) > 0 || orders.length > 0;

  if (!hasData) {
    return (
      <TrackAShell
        eyebrow="POS"
        title="POS Monitor"
        action={<WindowSelect value={windowKey} onChange={setWindowKey} />}
      >
        <EmptyState
          label={
            ready
              ? running
                ? "Waiting for orders in this window…"
                : "Press play to start the POS simulator."
              : "Loading recent orders…"
          }
        />
      </TrackAShell>
    );
  }

  return (
    <TrackAShell
      eyebrow="POS"
      title="POS Monitor"
      action={
        <div className="flex items-center gap-2">
          <WindowSelect value={windowKey} onChange={setWindowKey} />
          <Pill tone={running ? "good" : "neutral"}>
            {running ? "Live" : simState?.status ?? "idle"}
          </Pill>
        </div>
      }
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Orders"
          value={stats?.orders ?? "—"}
          sub={`${stats?.lines ?? 0} lines`}
        />
        <StatCard label="Revenue" value={fmtMoney(stats?.revenue ?? 0)} />
        <StatCard
          label="Orders / min"
          value={ordersPerMin === null ? "—" : ordersPerMin.toFixed(1)}
          sub={`over ${elapsedMin.toFixed(0)} min`}
        />
        <StatCard
          label="Void rate"
          value={`${(voidRate * 100).toFixed(1)}%`}
          sub={`${stats?.voidedLines ?? 0} voided`}
        />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-md border border-muted/70 bg-primary/30 p-3 lg:col-span-2">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text/40">
            Orders over time
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#0f3460" />
              <XAxis dataKey="label" tick={{ fill: "#eaeaea", fontSize: 11 }} />
              <YAxis allowDecimals={false} tick={{ fill: "#eaeaea", fontSize: 11 }} />
              <Tooltip
                contentStyle={{ background: "#16213e", border: "1px solid #0f3460" }}
              />
              <Bar dataKey="orders" fill="#e94560" radius={[2, 2, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="rounded-md border border-muted/70 bg-primary/30 p-3">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text/40">
            Channel split
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={channelData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="#0f3460" />
              <XAxis type="number" allowDecimals={false} tick={{ fill: "#eaeaea", fontSize: 11 }} />
              <YAxis
                type="category"
                dataKey="channel"
                width={70}
                tick={{ fill: "#eaeaea", fontSize: 11 }}
              />
              <Tooltip
                contentStyle={{ background: "#16213e", border: "1px solid #0f3460" }}
              />
              <Bar dataKey="count" fill="#1a73e8" radius={[0, 2, 2, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-md border border-muted/70 bg-primary/30 p-3">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text/40">
            Top items
          </div>
          {(stats?.topItems.length ?? 0) === 0 ? (
            <EmptyState label="No sold items in this window yet." />
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {stats?.topItems.map((item) => (
                  <tr key={item.menuItemId} className="border-b border-muted/40">
                    <td className="py-1.5 text-text/80">{nameOf(item.menuItemId)}</td>
                    <td className="py-1.5 text-right font-medium text-text">
                      {item.qty.toFixed(0)}
                    </td>
                    <td className="py-1.5 pl-3 text-right text-xs text-text/40">
                      {velocity[String(item.menuItemId)] !== undefined
                        ? `${(velocity[String(item.menuItemId)] * 60).toFixed(1)}/min`
                        : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="rounded-md border border-muted/70 bg-primary/30 p-3">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text/40">
            Live order feed
          </div>
          <div className="max-h-64 space-y-1 overflow-y-auto">
            {orders.slice(0, 25).map(({ order, lines }) => {
              const voided = lines.some((l) => l.status === "voided");
              return (
                <div
                  key={order.id}
                  className="flex items-center justify-between rounded border border-muted/40 bg-surface/60 px-2 py-1 text-xs"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-text/40">{simTimeToClock(order.sim_time)}</span>
                    <span className="text-text/80">{summarizeLines(lines)}</span>
                    {voided ? <Pill tone="bad">void</Pill> : null}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-text/40">{order.channel}</span>
                    <span className="font-medium text-text">
                      {fmtMoney(order.total ?? 0)}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </TrackAShell>
  );
}
