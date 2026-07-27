import { useEffect, useState } from "react";
import { apiGet, apiPatch, apiPost } from "../../api";
import { useSimState } from "../../store";
import type { MenuItem, SimSettings } from "../../types";
import { Label, SectionHeading, ApplyButton } from "./shared";

const DAYPART_NAMES = ["breakfast", "lunch", "afternoon", "dinner", "late"] as const;
const DAYPART_DEFAULTS: Record<string, number> = {
  breakfast: 0.18,
  lunch: 0.34,
  afternoon: 0.10,
  dinner: 0.33,
  late: 0.05,
};
const CHANNEL_DEFAULTS = { dine_in: 0.70, delivery: 0.20, takeout: 0.10 };

function normalise(mix: Record<string, number>): Record<string, number> {
  const total = Object.values(mix).reduce((a, b) => a + b, 0);
  if (total === 0) return mix;
  return Object.fromEntries(Object.entries(mix).map(([k, v]) => [k, v / total]));
}

type TicketMode = "lifecycle" | "instant";

/** Kitchen ticket lifecycle switch (docs/fable/portfolio-overview.md).
 *  Applied immediately rather than on Apply — flipping to "instant" is how you
 *  clear a backlog, and waiting for a batched save hides that. */
function KitchenTicketModeToggle() {
  const [mode, setMode] = useState<TicketMode>("lifecycle");

  useEffect(() => {
    apiGet<SimSettings>("/api/sim/pos")
      .then((s) => {
        if (s.kitchen_ticket_mode === "instant" || s.kitchen_ticket_mode === "lifecycle") {
          setMode(s.kitchen_ticket_mode);
        }
      })
      .catch(() => undefined);
  }, []);

  async function change(next: TicketMode) {
    const previous = mode;
    setMode(next);
    try {
      await apiPatch("/api/sim/pos", { kitchen_ticket_mode: next });
    } catch {
      setMode(previous);
    }
  }

  return (
    <div>
      <SectionHeading>Kitchen Tickets</SectionHeading>
      <p className="mb-2 text-[10px] text-text/40">
        Lifecycle queues each order until a cook clears it, so short-staffing grows a
        visible backlog. Instant closes orders the moment they arrive.
      </p>
      <div className="flex items-center gap-4 text-sm">
        {(["lifecycle", "instant"] as const).map((value) => (
          <label key={value} className="flex cursor-pointer items-center gap-1">
            <input
              type="radio"
              name="kitchen_ticket_mode"
              value={value}
              checked={mode === value}
              onChange={() => void change(value)}
            />
            <span className="text-text/80">{value}</span>
          </label>
        ))}
      </div>
    </div>
  );
}

export function PosMixPanel() {
  const [settings, setSettings] = useState<SimSettings | null>(null);
  const [menuItems, setMenuItems] = useState<MenuItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [ordersPerDay, setOrdersPerDay] = useState(300);
  const [channelMix, setChannelMix] = useState({ ...CHANNEL_DEFAULTS });
  const [dishWeights, setDishWeights] = useState<Record<string, number>>({});
  const [daypartWeights, setDaypartWeights] = useState<Record<string, number>>({ ...DAYPART_DEFAULTS });

  const activeSeedId = useSimState()?.active_seed_id ?? null;

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiGet<SimSettings>("/api/sim/pos"),
      apiGet<MenuItem[]>("/api/menu"),
    ]).then(([s, items]) => {
      if (cancelled) return;
      setSettings(s);
      setOrdersPerDay(s.base_orders_per_day ?? 300);
      setChannelMix({ ...CHANNEL_DEFAULTS, ...(s.channel_mix ?? {}) });
      setMenuItems(items.filter((m) => m.active));
      const wts: Record<string, number> = {};
      for (const item of items.filter((m) => m.active)) {
        wts[String(item.id)] = s.dish_mix_weights?.[String(item.id)] ?? 1;
      }
      setDishWeights(wts);
      setDaypartWeights({ ...DAYPART_DEFAULTS, ...(s.daypart_curve ?? {}) });
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [activeSeedId]);

  async function apply() {
    setBusy(true);
    try {
      await apiPatch("/api/sim/pos", {
        base_orders_per_day: ordersPerDay,
        channel_mix: normalise(channelMix),
        dish_mix_weights: Object.keys(dishWeights).length > 0 ? dishWeights : undefined,
        daypart_curve: daypartWeights,
      });
      await apiPost("/api/track-a/forecast/run").catch(() => undefined);
      const updated = await apiGet<SimSettings>("/api/sim/pos");
      setSettings(updated);
    } catch { /* ignore */ } finally { setBusy(false); }
  }

  const channelTotal = Object.values(channelMix).reduce((a, b) => a + b, 0);
  const dishTotal = Object.values(dishWeights).reduce((a, b) => a + b, 0);
  const daypartTotal = Object.values(daypartWeights).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-6">
      <div>
        <SectionHeading>Volume</SectionHeading>
        <label className="flex flex-col gap-1">
          <Label>Base orders / day</Label>
          <input
            type="number" min={1} max={9999} value={ordersPerDay}
            onChange={(e) => setOrdersPerDay(Number(e.target.value))}
            className="w-28 rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
          />
        </label>
      </div>

      <KitchenTicketModeToggle />

      <div>
        <SectionHeading>Channel Mix</SectionHeading>
        <p className="mb-2 text-[10px] text-text/40">Values are normalised on apply. Sum: {channelTotal.toFixed(2)}</p>
        <div className="space-y-2">
          {(["dine_in", "delivery", "takeout"] as const).map((ch) => (
            <label key={ch} className="flex items-center gap-3">
              <span className="w-16 text-xs text-text/60">{ch}</span>
              <input
                type="range" min={0} max={1} step={0.01} value={channelMix[ch]}
                onChange={(e) => setChannelMix((prev) => ({ ...prev, [ch]: Number(e.target.value) }))}
                className="w-36 accent-accent"
              />
              <span className="w-10 text-right text-xs tabular-nums text-text">
                {(channelMix[ch] * 100).toFixed(0)}%
              </span>
            </label>
          ))}
        </div>
      </div>

      <div>
        <SectionHeading>Daypart Weights</SectionHeading>
        <p className="mb-2 text-[10px] text-text/40">Rate multipliers — not normalised. Sum: {daypartTotal.toFixed(2)} (default 1.00)</p>
        <div className="space-y-2">
          {DAYPART_NAMES.map((name) => (
            <label key={name} className="flex items-center gap-3">
              <span className="w-16 text-xs text-text/60">{name}</span>
              <input
                type="range" min={0} max={1} step={0.01}
                value={daypartWeights[name] ?? DAYPART_DEFAULTS[name]}
                onChange={(e) => setDaypartWeights((prev) => ({ ...prev, [name]: Number(e.target.value) }))}
                className="w-36 accent-accent"
              />
              <span className="w-10 text-right text-xs tabular-nums text-text">
                {((daypartWeights[name] ?? DAYPART_DEFAULTS[name]) * 100).toFixed(0)}%
              </span>
            </label>
          ))}
        </div>
      </div>

      {menuItems.length > 0 && (
        <div>
          <SectionHeading>Dish Mix Weights</SectionHeading>
          <p className="mb-2 text-[10px] text-text/40">
            Drag to set each dish's relative weight. The % is its share of orders (weight ÷ total).
          </p>
          <div className="space-y-2">
            {menuItems.map((item) => {
              const w = dishWeights[String(item.id)] ?? 1;
              const share = dishTotal > 0 ? (w / dishTotal) * 100 : 0;
              return (
                <label key={item.id} className="flex items-center gap-3">
                  <span className="w-36 truncate text-xs text-text/60" title={item.name}>{item.name}</span>
                  <input
                    type="range" min={0} max={10} step={0.1} value={w}
                    onChange={(e) => setDishWeights((prev) => ({ ...prev, [String(item.id)]: Number(e.target.value) }))}
                    className="w-32 accent-accent"
                  />
                  <span className="w-12 text-right text-xs tabular-nums text-text">{share.toFixed(1)}%</span>
                </label>
              );
            })}
          </div>
        </div>
      )}

      <ApplyButton onClick={() => void apply()} busy={busy} />
      {settings && (
        <p className="text-[10px] text-text/30">Last read: orders/day={settings.base_orders_per_day ?? "default"}</p>
      )}
    </div>
  );
}
