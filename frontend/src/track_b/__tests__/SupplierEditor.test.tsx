// SupplierEditor panel renders the supplier catalog from sample payloads, issues
// a relative-path PATCH when a price is edited and a relative-path POST when a
// negotiation is started, reloads on a SUPPLIER_PRICE_UPDATE WS signal, and uses
// relative paths only (02 §B9).
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  Ingredient,
  Negotiation,
  SignalEnvelope,
  SupplierCatalogRow,
  SupplierRow,
} from "../../types";

const apiGet = vi.fn();
const apiPatch = vi.fn((_path: string, _body: unknown) => Promise.resolve({}));
const apiPost = vi.fn((_path: string, _body: unknown) => Promise.resolve({}));
vi.mock("../../api", () => ({
  apiGet: (path: string) => apiGet(path),
  apiPatch: (path: string, body: unknown) => apiPatch(path, body),
  apiPost: (path: string, body: unknown) => apiPost(path, body),
}));
vi.mock("../../ws", async () => {
  const { wsClientMock } = await import("../../test/wsMock");
  return { wsClient: wsClientMock };
});
vi.mock("../../store", () => ({
  useActiveCall: () => null,
}));

import { SupplierEditor } from "../SupplierEditor";
import { emitWs, resetWsMock } from "../../test/wsMock";
import { assertRelativePaths } from "../../test/relativePaths";

const SUPPLIERS: SupplierRow[] = [
  { id: 1, name: "Bella Foods", lead_time_days: 2, reliability_score: 0.9, min_order_value: 50, contact: null },
];
const CATALOG: SupplierCatalogRow[] = [
  {
    id: 3,
    supplier_id: 1,
    ingredient_id: 10,
    current_price: 4.5,
    unit: "kg",
    pack_size: 1,
    availability: "in_stock",
    updated_at: 0,
  },
];
const INGREDIENTS: Ingredient[] = [
  { id: 10, name: "Tomato", category: "produce", base_unit: "g", perishable: 1, shelf_life_days: 7 },
];
const NEGOTIATIONS: Negotiation[] = [
  {
    id: 9,
    supplier_id: 1,
    ingredient_id: 10,
    call_id: null,
    transcript: null,
    outcome: null,
    savings: 12.5,
    sim_time: 0,
  },
];

beforeEach(() => {
  apiGet.mockReset();
  apiPatch.mockClear();
  apiPost.mockClear();
  apiGet.mockImplementation((path: string) => {
    if (path === "/api/suppliers") return Promise.resolve(SUPPLIERS);
    if (path === "/api/supplier-catalog") return Promise.resolve(CATALOG);
    if (path === "/api/negotiations") return Promise.resolve(NEGOTIATIONS);
    if (path === "/api/ingredients") return Promise.resolve(INGREDIENTS);
    return Promise.resolve([]);
  });
});

afterEach(() => {
  resetWsMock();
});

describe("SupplierEditor", () => {
  it("renders a supplier catalog row and negotiation history", async () => {
    render(<SupplierEditor />);
    // Supplier name appears in both the catalog row and the negotiation line.
    expect(await screen.findAllByText("Bella Foods")).not.toHaveLength(0);
    expect(screen.getAllByText("Tomato").length).toBeGreaterThan(0);
    expect(screen.getByText(/saved 12.50/)).toBeInTheDocument();
  });

  it("PATCHes a relative path when a price is edited", async () => {
    render(<SupplierEditor />);
    await screen.findAllByText("Bella Foods");

    const priceInput = screen.getByDisplayValue("4.5") as HTMLInputElement;
    fireEvent.change(priceInput, { target: { value: "5.25" } });
    fireEvent.blur(priceInput);

    await waitFor(() => expect(apiPatch).toHaveBeenCalledTimes(1));
    expect(apiPatch).toHaveBeenCalledWith("/api/supplier-catalog/3", { current_price: 5.25 });
  });

  it("POSTs a relative path when a negotiation is started", async () => {
    render(<SupplierEditor />);
    await screen.findAllByText("Bella Foods");

    fireEvent.click(screen.getByRole("button", { name: "Negotiate" }));

    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1));
    expect(apiPost).toHaveBeenCalledWith("/api/market/negotiate", {
      supplier_id: 1,
      ingredient_id: 10,
    });
  });

  it("reloads on a SUPPLIER_PRICE_UPDATE signal_emitted event", async () => {
    render(<SupplierEditor />);
    await screen.findAllByText("Bella Foods");
    const before = apiGet.mock.calls.length;

    const signal: SignalEnvelope = {
      signal_id: "sig-2",
      type: "SUPPLIER_PRICE_UPDATE",
      source: "market_spectator",
      groups: ["track_b"],
      priority: 1,
      payload: {},
      created_at: 0,
      expires_at: null,
      dedup_key: null,
      status: "live",
      correlation_id: null,
    };
    emitWs("signal_emitted", { signal });

    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(before));
  });

  it("only uses relative paths across get/patch/post", async () => {
    render(<SupplierEditor />);
    await screen.findAllByText("Bella Foods");

    const priceInput = screen.getByDisplayValue("4.5") as HTMLInputElement;
    fireEvent.change(priceInput, { target: { value: "6" } });
    fireEvent.blur(priceInput);
    fireEvent.click(screen.getByRole("button", { name: "Negotiate" }));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    assertRelativePaths(apiGet, apiPatch, apiPost);
  });
});
