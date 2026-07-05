/**
 * Quantity / price unit formatter.
 *
 * Mirrors core/formatter.py so backend and frontend display identical values:
 *   ≥ 1000 g  → kg    (1500 g → "1.5 kg")
 *   ≥ 1000 ml → L     (2000 ml → "2 L")
 *   < 1000    → native unit
 *   "each"    → plain number
 */

/** Format a raw quantity (in base_unit) to a human-readable string. */
export function formatQty(value: number, baseUnit: string): string {
  const u = (baseUnit || "g").trim().toLowerCase();
  if (u === "g") {
    if (value >= 1000) {
      const kg = value / 1000;
      // Remove unnecessary trailing zeros
      const s = kg % 1 === 0 ? String(kg) : kg.toPrecision(4).replace(/\.?0+$/, "");
      return `${s} kg`;
    }
    return `${value % 1 === 0 ? value : value.toFixed(1)} g`;
  }
  if (u === "ml") {
    if (value >= 1000) {
      const l = value / 1000;
      const s = l % 1 === 0 ? String(l) : l.toPrecision(4).replace(/\.?0+$/, "");
      return `${s} L`;
    }
    return `${value % 1 === 0 ? value : value.toFixed(1)} ml`;
  }
  // "each" or unknown
  return String(value % 1 === 0 ? value : value.toFixed(1));
}

/**
 * Convert a per-base-unit price to a display price + unit.
 * Returns [displayPrice, displayUnit] — e.g. (0.004, "g") → [4.0, "kg"].
 */
export function formatPricePerUnit(
  pricePerBase: number,
  baseUnit: string
): [number, string] {
  const u = (baseUnit || "g").trim().toLowerCase();
  if (u === "g") return [parseFloat((pricePerBase * 1000).toFixed(4)), "kg"];
  if (u === "ml") return [parseFloat((pricePerBase * 1000).toFixed(4)), "L"];
  return [parseFloat(pricePerBase.toFixed(4)), baseUnit || "each"];
}
