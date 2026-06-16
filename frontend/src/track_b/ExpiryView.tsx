// ExpiryView — lots with expiry countdowns, at-risk highlights, and
// active/proposed promotions (02 §B6).
// Consumes `signal_emitted(EXPIRY_RISK / PROMO_PROPOSAL)`.

import { useEffect, useMemo, useState } from "react";
import { apiGet } from "../api";
import { wsClient } from "../ws";
import { useSimState } from "../store";
import type { Ingredient, InventoryLot, Promotion, SignalEnvelope } from "../types";

export function ExpiryView() {
  const sim = useSimState();
  const [lots, setLots] = useState<InventoryLot[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  const [promotions, setPromotions] = useState<Promotion[]>([]);
  const [atRisk, setAtRisk] = useState<Set<number>>(new Set());

  function reloadLots() {
    apiGet<InventoryLot[]>("/api/inventory/lots?status=active")
      .then(setLots)
      .catch(() => undefined);
  }

  function reloadPromotions() {
    apiGet<Promotion[]>("/api/promotions").then(setPromotions).catch(() => undefined);
  }

  useEffect(() => {
    reloadLots();
    reloadPromotions();
    apiGet<Ingredient[]>("/api/ingredients").then(setIngredients).catch(() => undefined);
  }, []);

  useEffect(() => {
    const off = wsClient.on("signal_emitted", (p) => {
      const signal = (p as { signal?: SignalEnvelope }).signal;
      if (!signal) return;
      if (signal.type === "EXPIRY_RISK") {
        const lotId = signal.payload.lot_id as number | undefined;
        if (lotId != null) {
          setAtRisk((prev) => new Set(prev).add(lotId));
        }
        reloadLots();
      } else if (signal.type === "PROMO_PROPOSAL") {
        reloadPromotions();
      }
    });
    const offResolved = wsClient.on("approval_resolved", () => {
      // A promo approval resolving (active) or a PO delivering (new lot) both
      // change what this panel shows.
      reloadLots();
      reloadPromotions();
    });
    return () => {
      off();
      offResolved();
    };
  }, []);

  const ingredientName = useMemo(() => {
    const map = new Map(ingredients.map((i) => [i.id, i.name]));
    return (id: number) => map.get(id) ?? `#${id}`;
  }, [ingredients]);

  const now = sim?.sim_time ?? 0;

  return (
    <div data-track="b" data-panel="Expiry" className="flex h-full flex-col gap-4 overflow-auto rounded-lg bg-surface/40 p-3">
      <div>
        <h2 className="mb-2 text-sm font-semibold text-text">Lots & expiry countdown</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-text/40">
              <th className="py-1">Ingredient</th>
              <th className="py-1">Qty</th>
              <th className="py-1">Expires in</th>
              <th className="py-1">Status</th>
            </tr>
          </thead>
          <tbody>
            {lots.map((lot) => {
              const remaining = lot.expiry_date != null ? lot.expiry_date - now : null;
              const risky = atRisk.has(lot.id) || (remaining != null && remaining <= 172800);
              return (
                <tr key={lot.id} className={"border-t border-muted " + (risky ? "bg-warning/10" : "")}>
                  <td className="py-1 font-medium text-text">{ingredientName(lot.ingredient_id)}</td>
                  <td className="py-1 text-text/70">{lot.qty_on_hand.toFixed(1)} {lot.unit}</td>
                  <td className="py-1 text-text/70">
                    {remaining != null ? `${(remaining / 3600).toFixed(1)}h` : "—"}
                  </td>
                  <td className="py-1">
                    {risky ? (
                      <span className="rounded bg-warning/30 px-2 py-0.5 text-xs font-medium text-warning">
                        at risk
                      </span>
                    ) : (
                      <span className="text-xs text-text/40">ok</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {lots.length === 0 && (
              <tr>
                <td colSpan={4} className="py-4 text-center text-text/40">
                  No active lots.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div>
        <h2 className="mb-2 text-sm font-semibold text-text">Promotions</h2>
        {promotions.length === 0 ? (
          <p className="text-xs text-text/40">No promotions proposed yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {promotions.map((promo) => (
              <li
                key={promo.id}
                className="flex items-center justify-between rounded border border-muted bg-surface p-2 text-sm"
              >
                <span>
                  <span className="font-medium text-text">
                    {promo.type === "combo" ? "Combo" : "Discount"} — {promo.discount_pct}% off
                  </span>{" "}
                  <span className="text-text/50">({promo.trigger}, {promo.channel})</span>
                </span>
                <span
                  className={
                    "rounded px-2 py-0.5 text-xs font-medium " +
                    (promo.status === "active"
                      ? "bg-success/20 text-success"
                      : promo.status === "proposed"
                        ? "bg-muted text-text/60"
                        : "bg-danger/20 text-danger")
                  }
                >
                  {promo.status}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
