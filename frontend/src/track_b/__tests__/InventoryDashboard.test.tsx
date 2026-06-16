// InventoryDashboard panel renders from sample REST payloads, updates on an
// inventory_updated WS event, and only ever talks to the backend over relative
// paths (02 §B9).
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  Ingredient,
  InventoryLevel,
  MenuItem,
} from "../../types";

// --- mock the network + ws layers --------------------------------------------
const apiGet = vi.fn();
vi.mock("../../api", () => ({
  apiGet: (path: string) => apiGet(path),
}));
vi.mock("../../ws", async () => {
  const { wsClientMock } = await import("../../test/wsMock");
  return { wsClient: wsClientMock };
});

import { InventoryDashboard } from "../InventoryDashboard";
import { emitWs, resetWsMock } from "../../test/wsMock";
import { assertRelativePaths } from "../../test/relativePaths";

const LEVELS: InventoryLevel[] = [
  {
    id: 1,
    ingredient_id: 10,
    par_level: 50,
    reorder_point: 20,
    safety_stock: 10,
    yield_factor: 1,
    on_hand_cached: 8,
    last_counted_at: 0,
    last_counted_qty: 9,
  },
];
const INGREDIENTS: Ingredient[] = [
  { id: 10, name: "Mozzarella", category: "dairy", base_unit: "g", perishable: 1, shelf_life_days: 7 },
];
const MENU: MenuItem[] = [
  {
    id: 100,
    name: "Margherita Pizza",
    category: "mains",
    station_id: 1,
    dine_in_price: 12,
    online_price: 13,
    prep_time_min: 8,
    is_batchable: 0,
    active: 0,
    weather_tags: null,
    description: null,
  },
];

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockImplementation((path: string) => {
    if (path === "/api/inventory") return Promise.resolve(LEVELS);
    if (path === "/api/ingredients") return Promise.resolve(INGREDIENTS);
    if (path === "/api/menu") return Promise.resolve(MENU);
    return Promise.resolve([]);
  });
});

afterEach(() => {
  resetWsMock();
});

describe("InventoryDashboard", () => {
  it("renders an ingredient row from the sample payload", async () => {
    render(<InventoryDashboard />);
    expect(await screen.findByText("Mozzarella")).toBeInTheDocument();
    // on_hand_cached 8 -> "8.0"
    expect(screen.getByText("8.0")).toBeInTheDocument();
    // The inactive menu item shows up under "Disabled menu items".
    expect(screen.getByText("Margherita Pizza")).toBeInTheDocument();
  });

  it("updates the on-hand value when an inventory_updated WS event arrives", async () => {
    render(<InventoryDashboard />);
    await screen.findByText("Mozzarella");

    emitWs("inventory_updated", { ingredient_id: 10, on_hand: 42 });

    await waitFor(() => {
      expect(screen.getByText("42.0")).toBeInTheDocument();
    });
  });

  it("only fetches over relative paths", async () => {
    render(<InventoryDashboard />);
    await screen.findByText("Mozzarella");
    assertRelativePaths(apiGet);
  });
});
