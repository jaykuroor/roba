// ActivityLog panel renders the event_log narrative from a sample payload,
// appends a new line on an event_logged WS event, and uses relative paths only
// (02 §B9).
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EventLogEntry } from "../../types";

const apiGet = vi.fn();
vi.mock("../../api", () => ({
  apiGet: (path: string) => apiGet(path),
}));
vi.mock("../../ws", async () => {
  const { wsClientMock } = await import("../../test/wsMock");
  return { wsClient: wsClientMock };
});

import { ActivityLog } from "../ActivityLog";
import { emitWs, resetWsMock } from "../../test/wsMock";
import { assertRelativePaths } from "../../test/relativePaths";

const EVENTS: EventLogEntry[] = [
  {
    id: 1,
    sim_time: 120,
    category: "reorder",
    actor: "optimizer",
    summary: "Reordered 20kg Mozzarella from Bella Foods",
    detail: null,
  },
];

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockImplementation((path: string) => {
    if (path === "/api/events") return Promise.resolve(EVENTS);
    return Promise.resolve([]);
  });
});

afterEach(() => {
  resetWsMock();
});

describe("ActivityLog", () => {
  it("renders an event-log line from the sample payload", async () => {
    render(<ActivityLog />);
    expect(
      await screen.findByText("Reordered 20kg Mozzarella from Bella Foods"),
    ).toBeInTheDocument();
    expect(screen.getByText("reorder")).toBeInTheDocument();
  });

  it("appends a line on an event_logged WS event", async () => {
    render(<ActivityLog />);
    await screen.findByText("Reordered 20kg Mozzarella from Bella Foods");

    const event: EventLogEntry = {
      id: 2,
      sim_time: 180,
      category: "promo",
      actor: "optimizer",
      summary: "Proposed 25% off Margherita",
      detail: null,
    };
    emitWs("event_logged", { event });

    await waitFor(() => {
      expect(screen.getByText("Proposed 25% off Margherita")).toBeInTheDocument();
    });
  });

  it("only fetches over relative paths", async () => {
    render(<ActivityLog />);
    await screen.findByText("Reordered 20kg Mozzarella from Bella Foods");
    assertRelativePaths(apiGet);
  });
});
