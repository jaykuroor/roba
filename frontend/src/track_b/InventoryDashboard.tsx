// InventoryDashboard — per-ingredient on_hand vs par/reorder_point/safety_stock,
// theoretical-vs-counted drift, live depletion, and disabled menu items (02 §B6).
// Consumes `inventory_updated`, `menu_toggled`, `order_created` WS events.

import { useEffect, useMemo, useState } from "react";
import { apiGet } from "../api";
import { wsClient } from "../ws";
import type { Ingredient, InventoryLevel, MenuItem, MenuToggleEvent } from "../types";

export function InventoryDashboard() {
  const [levels, setLevels] = useState<InventoryLevel[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [menuItems, setMenuItems] = useState<MenuItem[]>([]);
  const [disabled, setDisabled] = useState<Set<number>>(new Set());
  const [lastOrderAt, setLastOrderAt] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiGet<InventoryLevel[]>("/api/inventory"),
      apiGet<Ingredient[]>("/api/ingredients"),
      apiGet<MenuItem[]>("/api/menu"),
    ])
      .then(([lvls, ings, items]) => {
        if (cancelled) return;
        setLevels(lvls);
        setIngredients(ings);
        setMenuItems(items);
        setDisabled(new Set(items.filter((m) => m.active === 0).map((m) => m.id)));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const offInventory = wsClient.on("inventory_updated", (p) => {
      const { ingredient_id, on_hand } = p as { ingredient_id: number; on_hand: number };
      setLevels((prev) =>
        prev.map((lvl) =>
          lvl.ingredient_id === ingredient_id ? { ...lvl, on_hand_cached: on_hand } : lvl,
        ),
      );
    });
    const offToggle = wsClient.on("menu_toggled", (p) => {
      const { menu_item_id, action } = p as unknown as MenuToggleEvent;
      setDisabled((prev) => {
        const next = new Set(prev);
        if (action === "disable") next.add(menu_item_id);
        else next.delete(menu_item_id);
        return next;
      });
    });
    const offOrder = wsClient.on("order_created", (p) => {
      const order = (p as { order?: { sim_time?: number } }).order;
      if (order?.sim_time != null) setLastOrderAt(order.sim_time);
    });
    return () => {
      offInventory();
      offToggle();
      offOrder();
    };
  }, []);

  const ingredientName = useMemo(() => {
    const map = new Map(ingredients.map((i) => [i.id, i.name]));
    return (id: number) => map.get(id) ?? `#${id}`;
  }, [ingredients]);

  const menuItemsByIngredient = useMemo(() => {
    // No recipe join is exposed over REST, so this only reflects items we can
    // already see are disabled — listed globally below the table instead.
    return menuItems.filter((m) => disabled.has(m.id));
  }, [menuItems, disabled]);

  return (
    <div data-track="b" data-panel="Inventory" className="flex h-full flex-col gap-3 overflow-auto rounded-lg bg-surface/40 p-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-text">Inventory Dashboard</h2>
        {lastOrderAt != null && (
          <span className="text-xs text-text/40">last order @ {lastOrderAt.toFixed(0)}s</span>
        )}
      </div>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-text/40">
            <th className="py-1">Ingredient</th>
            <th className="py-1">On hand</th>
            <th className="py-1">Safety stock</th>
            <th className="py-1">Reorder point</th>
            <th className="py-1">Par level</th>
            <th className="py-1">Drift (counted)</th>
          </tr>
        </thead>
        <tbody>
          {levels.map((lvl) => {
            const onHand = lvl.on_hand_cached ?? 0;
            const low = lvl.safety_stock != null && onHand <= lvl.safety_stock;
            const out = onHand <= 0;
            const drift =
              lvl.last_counted_qty != null ? lvl.last_counted_qty - onHand : null;
            return (
              <tr
                key={lvl.id}
                className={
                  "border-t border-muted " +
                  (out ? "bg-danger/10" : low ? "bg-warning/10" : "")
                }
              >
                <td className="py-1 font-medium text-text">{ingredientName(lvl.ingredient_id)}</td>
                <td className="py-1 text-text/80">{onHand.toFixed(1)}</td>
                <td className="py-1 text-text/60">{lvl.safety_stock?.toFixed(1) ?? "—"}</td>
                <td className="py-1 text-text/60">{lvl.reorder_point?.toFixed(1) ?? "—"}</td>
                <td className="py-1 text-text/60">{lvl.par_level?.toFixed(1) ?? "—"}</td>
                <td className="py-1 text-text/60">
                  {drift != null ? (drift === 0 ? "in sync" : drift.toFixed(1)) : "—"}
                </td>
              </tr>
            );
          })}
          {levels.length === 0 && (
            <tr>
              <td colSpan={6} className="py-4 text-center text-text/40">
                No inventory levels yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text/40">
          Disabled menu items
        </h3>
        {menuItemsByIngredient.length === 0 ? (
          <p className="text-xs text-text/40">All menu items active.</p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {menuItemsByIngredient.map((m) => (
              <li
                key={m.id}
                className="rounded bg-danger/20 px-2 py-0.5 text-xs font-medium text-danger"
              >
                {m.name}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
