// Restaurant-card rendering with real data, driven through the real
// useAdminData hook by stubbing apiGet per path. The pattern for later phases:
// add a field to the stub payload and assert what the card shows.
import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { InstanceCard } from "../useAdminData";

const overview: { instances: InstanceCard[]; actions: [] } = {
  instances: [],
  actions: [],
};

vi.mock("../../api", () => ({
  apiGet: vi.fn((path: string) => {
    if (path.startsWith("/admin/api/overview")) return Promise.resolve(overview);
    if (path.startsWith("/admin/api/incidents"))
      return Promise.resolve({ incidents: [], unavailable_categories: [] });
    if (path.startsWith("/admin/api/presets")) return Promise.resolve([]);
    if (path.startsWith("/admin/api/approvals")) return Promise.resolve([]);
    return new Promise(() => undefined); // /admin/api/summary — never resolves
  }),
  apiPost: vi.fn(() => new Promise(() => undefined)),
  apiPatch: vi.fn(() => new Promise(() => undefined)),
  apiDelete: vi.fn(() => new Promise(() => undefined)),
}));

import AdminPage from "../AdminPage";

function card(overrides: Partial<InstanceCard> = {}): InstanceCard {
  return {
    id: "running_fox",
    preset: "bellas_kitchen",
    port: 8101,
    title: "Bella's",
    online: true,
    status: "normal",
    sim: { sim_time: 30000, day_number: 0 },
    sales_today: 120,
    forecast_today: 100,
    orders_today: 9,
    staff_present: 2,
    staff_total: 3,
    absent: [],
    stock_risks: [],
    pending_approvals: 0,
    orders_waiting: 0,
    ticket_time_min: null,
    safety_issues: null,
    issues: [],
    ...overrides,
  };
}

/** Render the dashboard and return just the card's Tickets metric block, so the
 *  assertions cannot accidentally match another metric's value. */
async function renderTicketMetric(overrides: Partial<InstanceCard>) {
  overview.instances = [card(overrides)];
  render(<AdminPage />);
  await waitFor(() => expect(screen.getByText("Bella's")).toBeInTheDocument());
  return screen.getByText("Tickets").parentElement!;
}

describe("restaurant card — Tickets metric", () => {
  it("shows the backlog and the average ticket time", async () => {
    const metric = await renderTicketMetric({ orders_waiting: 11, ticket_time_min: 7.5 });
    expect(within(metric).getByText("11")).toBeInTheDocument();
    expect(within(metric).getByText("7.5m avg")).toBeInTheDocument();
  });

  it("shows a dash when the instance reports no ticket data", async () => {
    const metric = await renderTicketMetric({ orders_waiting: null, ticket_time_min: null });
    expect(within(metric).getByText("—")).toBeInTheDocument();
  });

  it("drops the average when nothing has been served yet", async () => {
    const metric = await renderTicketMetric({ orders_waiting: 4, ticket_time_min: null });
    expect(within(metric).getByText("4")).toBeInTheDocument();
    expect(within(metric).queryByText(/m avg/)).not.toBeInTheDocument();
  });
});
