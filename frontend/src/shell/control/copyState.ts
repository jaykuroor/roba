/**
 * Copy State — fetches the current simulation state from existing endpoints and
 * assembles a plain-text report with titled sections for clipboard export.
 *
 * Sections:
 *   1. FORECAST — THIS WEEK   (day-by-day, falls back to per-daypart if no horizon)
 *   2. INVENTORY              (active lots, sorted by expiry, with [AT RISK] flag)
 *   3. PROCUREMENT PLAN       (planned orders with coverage summary + status labels)
 *   4. ORDERS MADE            (in-flight POs in full + delivered history)
 */

import { apiGet } from "../../api";
import type { HorizonForecast, HorizonForecastLine, TrackASnapshot } from "../../track_a/types";
import { latestForecasts } from "../../track_a/helpers";
import type { Ingredient, InventoryLot } from "../../types";

// ─── Local mirror of ProcurementPanel's internal types ───────────────────────

interface PoLine {
  id: number;
  ingredient_id: number;
  ingredient_name: string;
  qty: number;
  qty_display: string;
  unit: string;
  unit_price: number | null;
  line_total: number | null;
}

interface PurchaseOrder {
  id: number;
  supplier_id: number;
  supplier_name: string;
  status: string;
  urgency: "at_risk" | "uncoverable" | null;
  created_at: number | null;
  expected_delivery: number | null;
  total_cost: number | null;
  lines: PoLine[];
}

interface PlanItem {
  id: number;
  ingredient_name: string;
  supplier_name: string;
  qty: number;
  unit: string;
  unit_price: number | null;
  order_date_label: string;
  delivery_date_label: string;
  status: "planned" | "at_risk" | "uncoverable" | "placed" | "superseded";
  reason: string;
  shortage_if_late: number;
}

interface PlanResponse {
  plan_run_id: number | null;
  generated_at: number;
  coverage_ok: boolean;
  total_short: number;
  reliability_premium: number;
  exposed_value_baseline: number;
  exposed_value_protected: number;
  items: PlanItem[];
}

interface ProcurementWarning {
  ingredient_name: string;
  short_qty: number;
  unit: string;
  reason: string;
}

interface TodayBreakdown {
  generated_at: number;
  sold_today: number;   // menu-item units already sold since the day opened
  remaining: number;    // live forecast for the rest of today (drives the plan)
  full_day: number;     // sold_today + remaining
}

// ─── Data bundle ─────────────────────────────────────────────────────────────

export interface StateBundle {
  snapshot: TrackASnapshot | null;
  horizonLines: HorizonForecastLine[] | null;   // null = no horizon available
  todayBreakdown: TodayBreakdown | null;         // today full-day/sold/remaining reconciliation
  lots: InventoryLot[];
  ingredients: Ingredient[];
  plan: PlanResponse | null;
  orderedPOs: PurchaseOrder[];
  deliveredPOs: PurchaseOrder[];
  warnings: ProcurementWarning[];
  errors: string[];                              // section-level fetch errors (non-fatal)
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const AT_RISK_THRESHOLD_S = 172800; // 2 days in sim-seconds

function dayLabel(simS: number | null | undefined): string {
  if (simS == null) return "—";
  const day = Math.floor(simS / 86400);
  return `Day ${day} (${DOW[day % 7]})`;
}

function formatMoney(v: number | null | undefined): string {
  if (v == null) return "—";
  return `€${v.toFixed(2)}`;
}

function formatQty(qty: number, unit: string): string {
  if (unit === "g" && qty >= 1000) return `${(qty / 1000).toFixed(2)} kg`;
  if (unit === "ml" && qty >= 1000) return `${(qty / 1000).toFixed(2)} L`;
  return `${qty.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${unit}`;
}

function banner(title: string): string {
  const line = "━".repeat(70);
  return `\n${line}\n${title}\n${line}\n`;
}

// ─── Fetch ───────────────────────────────────────────────────────────────────

export async function gatherState(): Promise<StateBundle> {
  const errors: string[] = [];

  // Fetch all sections in parallel; each is individually guarded.
  const [snapshot, todayData, horizonData, lotData, ingredientData, planData, orderedData, deliveredData, warningData] =
    await Promise.all([
      apiGet<TrackASnapshot>("/api/track-a/snapshot").catch(() => {
        errors.push("Forecast snapshot unavailable");
        return null;
      }),

      apiGet<TodayBreakdown>("/api/track-a/forecast/today-breakdown").catch(
        () => null
      ),

      // horizons list → then pick latest → then fetch its lines
      apiGet<{ horizons: HorizonForecast[] }>("/api/track-a/forecast/horizons")
        .then(async ({ horizons }) => {
          if (horizons.length === 0) return null;
          const latest = horizons.reduce((a, b) =>
            (b.generated_at ?? 0) > (a.generated_at ?? 0) ? b : a
          );
          if (latest.id == null) return null;
          const { lines } = await apiGet<{ lines: HorizonForecastLine[] }>(
            `/api/track-a/forecast/horizon/${latest.id}`
          );
          return lines;
        })
        .catch(() => {
          // non-fatal; will fall back to per-daypart in buildReport
          return null;
        }),

      apiGet<InventoryLot[]>("/api/inventory/lots?status=active").catch(() => {
        errors.push("Inventory lots unavailable");
        return [] as InventoryLot[];
      }),

      apiGet<Ingredient[]>("/api/ingredients").catch(() => {
        errors.push("Ingredient names unavailable");
        return [] as Ingredient[];
      }),

      apiGet<PlanResponse>("/api/track-b/procurement/plan").catch(() => {
        errors.push("Procurement plan unavailable");
        return null;
      }),

      apiGet<PurchaseOrder[]>(
        "/api/purchase-orders?status=approved,placed&include_lines=true"
      ).catch(() => {
        errors.push("In-flight orders unavailable");
        return [] as PurchaseOrder[];
      }),

      apiGet<PurchaseOrder[]>(
        "/api/purchase-orders?status=delivered&include_lines=true&limit=50"
      ).catch(() => {
        errors.push("Delivered orders unavailable");
        return [] as PurchaseOrder[];
      }),

      apiGet<ProcurementWarning[]>("/api/track-b/procurement/warnings").catch(
        () => [] as ProcurementWarning[]
      ),
    ]);

  return {
    snapshot,
    horizonLines: horizonData,
    todayBreakdown: todayData,
    lots: lotData as InventoryLot[],
    ingredients: ingredientData as Ingredient[],
    plan: planData,
    orderedPOs: orderedData as PurchaseOrder[],
    deliveredPOs: deliveredData as PurchaseOrder[],
    warnings: warningData as ProcurementWarning[],
    errors,
  };
}

// ─── Report builder ──────────────────────────────────────────────────────────

export function buildReport(bundle: StateBundle): string {
  const parts: string[] = [];

  const now = bundle.snapshot?.sim_state?.sim_time ?? 0;

  // ── Header ──
  const nowLabel = now > 0
    ? (() => {
        const day = Math.floor(now / 86400);
        const secs = Math.floor(now % 86400);
        const h = Math.floor(secs / 3600).toString().padStart(2, "0");
        const m = Math.floor((secs % 3600) / 60).toString().padStart(2, "0");
        return `Day ${day} (${DOW[day % 7]}) ${h}:${m}`;
      })()
    : "—";

  parts.push(`ROBA SIMULATION STATE EXPORT`);
  parts.push(`Captured at: ${nowLabel}`);
  if (bundle.errors.length > 0) {
    parts.push(`⚠ Some sections could not be loaded: ${bundle.errors.join("; ")}`);
  }

  // ── Section 1: Forecast — This Week ──────────────────────────────────────
  parts.push(banner("FORECAST — THIS WEEK"));

  // Today reconciliation: the "Today" figures below are a point-in-time
  // forecast, but the procurement plan orders against the REMAINING part of the
  // day.  Show full-day / already-sold / remaining so the two can be reconciled.
  const tb = bundle.todayBreakdown;
  if (tb) {
    parts.push(
      "\n  Today — reconciliation (menu-item units):",
      `    full-day forecast : ${String(Math.round(tb.full_day)).padStart(5)}`,
      `    already sold today: ${String(Math.round(tb.sold_today)).padStart(5)}`,
      `    remaining (drives plan): ${String(Math.round(tb.remaining)).padStart(5)}`,
    );
  }

  if (bundle.horizonLines && bundle.horizonLines.length > 0) {
    // Aggregate per (day_index, menu_item_id) across dayparts
    type DayItemKey = string;
    const agg = new Map<DayItemKey, { dayIndex: number; name: string; qty: number; baseline: number; conf: number; count: number }>();
    for (const line of bundle.horizonLines) {
      if (line.day_index < 0 || line.day_index > 6) continue;
      const key: DayItemKey = `${line.day_index}:${line.menu_item_id}`;
      const existing = agg.get(key);
      if (existing) {
        existing.qty += line.qty;
        existing.baseline += line.baseline;
        existing.conf += (line.confidence ?? 0);
        existing.count += 1;
      } else {
        agg.set(key, {
          dayIndex: line.day_index,
          name: line.item_name,
          qty: line.qty,
          baseline: line.baseline,
          conf: line.confidence ?? 0,
          count: 1,
        });
      }
    }

    // Sort by day_index, then name
    const rows = Array.from(agg.values()).sort((a, b) =>
      a.dayIndex !== b.dayIndex ? a.dayIndex - b.dayIndex : a.name.localeCompare(b.name)
    );

    let lastDay = -1;
    for (const row of rows) {
      if (row.dayIndex !== lastDay) {
        const dayStr = row.dayIndex === 0 ? "Today" : `Day +${row.dayIndex} (${DOW[row.dayIndex % 7]})`;
        parts.push(`\n  ${dayStr}`);
        lastDay = row.dayIndex;
      }
      const avgConf = row.count > 0 ? Math.round((row.conf / row.count) * 100) : 0;
      parts.push(
        `    ${row.name.padEnd(28)} forecast: ${String(Math.round(row.qty)).padStart(4)}  baseline: ${String(Math.round(row.baseline)).padStart(4)}  conf: ${avgConf}%`
      );
    }

    if (rows.length === 0) parts.push("  (no forecast lines for this week)");
  } else if (bundle.snapshot) {
    // Fallback: latest per-daypart forecasts
    parts.push("  (no weekly horizon — showing latest per-daypart forecasts)");
    const forecasts = latestForecasts(bundle.snapshot);
    if (forecasts.length === 0) {
      parts.push("  (no forecasts available)");
    } else {
      for (const f of forecasts) {
        const name = bundle.snapshot.menu_items.find((m) => m.id === f.menu_item_id)?.name ?? `Item ${f.menu_item_id}`;
        const conf = Math.round((f.confidence ?? 0) * 100);
        parts.push(
          `  ${name.padEnd(28)} ${f.daypart.padEnd(8)}  forecast: ${String(f.forecast_qty).padStart(4)}  baseline: ${String(Math.round(f.baseline_qty)).padStart(4)}  conf: ${conf}%`
        );
      }
    }
  } else {
    parts.push("  (unavailable)");
  }

  // ── Section 2: Inventory ──────────────────────────────────────────────────
  parts.push(banner("INVENTORY — CURRENT STATE WITH EXPIRY"));

  if (bundle.lots.length === 0) {
    parts.push("  (no active inventory lots)");
  } else {
    const ingMap = new Map(bundle.ingredients.map((i) => [i.id, i.name]));
    const sorted = [...bundle.lots].sort((a, b) => {
      if (a.expiry_date == null && b.expiry_date == null) return 0;
      if (a.expiry_date == null) return 1;
      if (b.expiry_date == null) return -1;
      return a.expiry_date - b.expiry_date;
    });

    for (const lot of sorted) {
      const name = ingMap.get(lot.ingredient_id) ?? `#${lot.ingredient_id}`;
      const qtyStr = formatQty(lot.qty_on_hand, lot.unit);
      let expiryStr = "no expiry";
      let atRisk = false;
      if (lot.expiry_date != null) {
        expiryStr = `expires ${dayLabel(lot.expiry_date)}`;
        const remaining = lot.expiry_date - now;
        atRisk = remaining <= AT_RISK_THRESHOLD_S;
      }
      const riskTag = atRisk ? "  [AT RISK — expiring soon]" : "";
      parts.push(`  ${name.padEnd(28)} ${qtyStr.padEnd(18)} ${expiryStr}${riskTag}`);
    }
  }

  // ── Section 3: Procurement Plan ──────────────────────────────────────────
  parts.push(banner("PROCUREMENT PLAN"));

  if (!bundle.plan) {
    parts.push("  (unavailable)");
  } else {
    const meta = bundle.plan;
    const coverageLine = meta.coverage_ok
      ? "Coverage: OK"
      : `Coverage: SHORT  total_short=${meta.total_short.toFixed(0)}`;
    const premiumLine = meta.reliability_premium > 0
      ? `  Reliability premium: ${formatMoney(meta.reliability_premium)}`
      : "";
    parts.push(`  ${coverageLine}${premiumLine}`);

    if (meta.exposed_value_protected > 0) {
      parts.push(
        `  Exposed value: ${formatMoney(meta.exposed_value_baseline)} baseline  /  ${formatMoney(meta.exposed_value_protected)} protected`
      );
    }

    if (meta.items.length === 0 && bundle.warnings.length === 0) {
      parts.push("\n  (no planned orders)");
    } else {
      parts.push("");
      for (const item of meta.items) {
        const statusTag =
          item.status === "at_risk" ? "  [AT RISK]" :
          item.status === "uncoverable" ? "  [UNCOVERABLE]" :
          item.status === "placed" ? "  [PLACED]" : "";
        const price = item.unit_price != null ? ` @ ${formatMoney(item.unit_price)}/${item.unit}` : "";
        const shortage = item.shortage_if_late > 0
          ? `  shortage-if-late: ${item.shortage_if_late.toFixed(0)} ${item.unit}` : "";
        const reason = item.reason ? `  (${item.reason})` : "";
        parts.push(
          `  ${item.ingredient_name.padEnd(26)} ${formatQty(item.qty, item.unit).padEnd(16)}${price}`
        );
        parts.push(
          `    via ${item.supplier_name}  order: ${item.order_date_label}  deliver: ${item.delivery_date_label}${statusTag}${shortage}${reason}`
        );
      }
    }

    if (bundle.warnings.length > 0) {
      parts.push("\n  Uncoverable ingredients:");
      for (const w of bundle.warnings) {
        parts.push(`    ⚠ ${w.ingredient_name}: short ${w.short_qty.toFixed(0)} ${w.unit} — ${w.reason}`);
      }
    }
  }

  // ── Section 4: Orders Made ────────────────────────────────────────────────
  parts.push(banner("ORDERS MADE"));

  const renderPO = (po: PurchaseOrder, prefix: string): void => {
    const urgencyTag =
      po.urgency === "at_risk" ? "  [AT RISK]" :
      po.urgency === "uncoverable" ? "  [UNCOVERABLE]" : "";
    const deliver = po.expected_delivery != null ? dayLabel(po.expected_delivery) : "—";
    const cost = formatMoney(po.total_cost);
    parts.push(
      `${prefix}PO#${po.id} ${po.status.toUpperCase().padEnd(8)} ${po.supplier_name}  deliver: ${deliver}  total: ${cost}${urgencyTag}`
    );
    for (const line of po.lines ?? []) {
      const lineCost = line.line_total != null ? `  = ${formatMoney(line.line_total)}` : "";
      parts.push(`${prefix}  - ${line.ingredient_name}: ${line.qty_display} ${line.unit}${lineCost}`);
    }
  };

  if (bundle.orderedPOs.length === 0 && bundle.deliveredPOs.length === 0) {
    parts.push("  (no orders on record)");
  } else {
    if (bundle.orderedPOs.length > 0) {
      parts.push("  [IN-FLIGHT]");
      for (const po of bundle.orderedPOs) renderPO(po, "  ");
    } else {
      parts.push("  [IN-FLIGHT]\n  (none)");
    }

    parts.push("");
    if (bundle.deliveredPOs.length > 0) {
      parts.push("  [DELIVERED — recent history]");
      for (const po of bundle.deliveredPOs) renderPO(po, "  ");
    } else {
      parts.push("  [DELIVERED — recent history]\n  (none)");
    }
  }

  return parts.join("\n");
}
