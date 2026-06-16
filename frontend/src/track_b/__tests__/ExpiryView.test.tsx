// ExpiryView panel renders lots with expiry countdowns and promotions from
// sample payloads, flags a lot "at risk" on an EXPIRY_RISK signal_emitted WS
// event, and uses relative paths only (02 §B9).
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Ingredient, InventoryLot, Promotion, SignalEnvelope } from "../../types";

const apiGet = vi.fn();
vi.mock("../../api", () => ({
  apiGet: (path: string) => apiGet(path),
}));
vi.mock("../../ws", async () => {
  const { wsClientMock } = await import("../../test/wsMock");
  return { wsClient: wsClientMock };
});
// Pin the sim clock so the expiry countdown is deterministic.
vi.mock("../../store", () => ({
  useSimState: () => ({ sim_time: 0 }),
}));

import { ExpiryView } from "../ExpiryView";
import { emitWs, resetWsMock } from "../../test/wsMock";
import { assertRelativePaths } from "../../test/relativePaths";

const INGREDIENTS: Ingredient[] = [
  { id: 10, name: "Basil", category: "produce", base_unit: "g", perishable: 1, shelf_life_days: 3 },
];
const LOTS: InventoryLot[] = [
  {
    id: 5,
    ingredient_id: 10,
    qty_on_hand: 4,
    unit: "kg",
    purchase_price: 2,
    purchase_date: 0,
    received_date: 0,
    // 200h out -> well past the 48h at-risk threshold, so it starts "ok".
    expiry_date: 720000,
    supplier_id: 1,
    storage_location: "walk-in",
    status: "active",
  },
];
const PROMOS: Promotion[] = [
  {
    id: 7,
    type: "discount",
    menu_items: [100],
    trigger: "expiry",
    discount_pct: 25,
    channel: "menu",
    status: "proposed",
    approval_id: null,
    sim_time: 0,
  },
];

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockImplementation((path: string) => {
    if (path.startsWith("/api/inventory/lots")) return Promise.resolve(LOTS);
    if (path === "/api/promotions") return Promise.resolve(PROMOS);
    if (path === "/api/ingredients") return Promise.resolve(INGREDIENTS);
    return Promise.resolve([]);
  });
});

afterEach(() => {
  resetWsMock();
});

describe("ExpiryView", () => {
  it("renders a lot with its expiry countdown and a proposed promotion", async () => {
    render(<ExpiryView />);
    expect(await screen.findByText("Basil")).toBeInTheDocument();
    // 720000s / 3600 = 200.0h
    expect(screen.getByText("200.0h")).toBeInTheDocument();
    // promotion line
    expect(screen.getByText(/25% off/)).toBeInTheDocument();
    expect(screen.getByText("proposed")).toBeInTheDocument();
  });

  it("marks a lot at risk on an EXPIRY_RISK signal_emitted event", async () => {
    render(<ExpiryView />);
    await screen.findByText("Basil");
    expect(screen.queryByText("at risk")).not.toBeInTheDocument();

    const signal: SignalEnvelope = {
      signal_id: "sig-1",
      type: "EXPIRY_RISK",
      source: "ledger",
      groups: ["track_b"],
      priority: 1,
      payload: { lot_id: 5 },
      created_at: 0,
      expires_at: null,
      dedup_key: null,
      status: "live",
      correlation_id: null,
    };
    emitWs("signal_emitted", { signal });

    await waitFor(() => {
      expect(screen.getByText("at risk")).toBeInTheDocument();
    });
  });

  it("only fetches over relative paths", async () => {
    render(<ExpiryView />);
    await screen.findByText("Basil");
    assertRelativePaths(apiGet);
  });
});
