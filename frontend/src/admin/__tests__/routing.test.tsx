// The dashboard is roba's entry point: "/" and unknown paths redirect to
// /admin, and a bogus instance id never mounts an operator console
// (docs/fable/manager-dashboard.md).
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

// Network + WS are irrelevant here — the assertions are about routing only.
vi.mock("../../api", () => ({
  INSTANCE_ID_RE: /^[a-z]+_[a-z]+\d*$/,
  instanceId: () => null,
  instancePrefix: () => "",
  instancePagePrefix: () => "",
  apiGet: vi.fn(() => new Promise(() => undefined)),
  apiPost: vi.fn(() => new Promise(() => undefined)),
  apiPatch: vi.fn(() => new Promise(() => undefined)),
  apiDelete: vi.fn(() => new Promise(() => undefined)),
}));
vi.mock("../../ws", async () => {
  const { wsClientMock } = await import("../../test/wsMock");
  return { wsClient: wsClientMock };
});

import App from "../../App";

async function expectDashboard() {
  await waitFor(() =>
    expect(screen.getByText("Roba Manager")).toBeInTheDocument(),
  );
}

describe("entry routing", () => {
  it("redirects / to the manager dashboard", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await expectDashboard();
  });

  it("redirects unknown paths to the manager dashboard", async () => {
    render(
      <MemoryRouter initialEntries={["/no/such/page"]}>
        <App />
      </MemoryRouter>,
    );
    await expectDashboard();
  });

  it("redirects an invalid instance id to the dashboard instead of mounting a console", async () => {
    render(
      <MemoryRouter initialEntries={["/not-an-instance"]}>
        <App />
      </MemoryRouter>,
    );
    await expectDashboard();
  });

  it("mounts the operator console for a valid instance id", async () => {
    render(
      <MemoryRouter initialEntries={["/running_fox"]}>
        <App />
      </MemoryRouter>,
    );
    // The instance nav shows the id (identity fetch never resolves in tests).
    await waitFor(() =>
      expect(screen.getAllByText("running_fox").length).toBeGreaterThan(0),
    );
  });
});
