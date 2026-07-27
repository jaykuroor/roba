// Restaurant-card rendering with real data, driven through the real
// useAdminData hook by stubbing apiGet per path. The pattern for later phases:
// add a field to the stub payload and assert what the card shows.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  ActionItem,
  Briefing,
  CatchupMarker,
  CatchupRecord,
  CatchupSummary,
  Incident,
  IncidentHistoryRow,
  InstanceCard,
  MergedCatchup,
  SummaryArchive,
  SummaryArchiveMarker,
} from "../useAdminData";

const overview: { instances: InstanceCard[]; actions: ActionItem[] } = {
  instances: [],
  actions: [],
};

const incidentData: { incidents: Incident[]; unavailable_categories: string[] } = {
  incidents: [],
  unavailable_categories: [],
};
const history: { incidents: IncidentHistoryRow[] } = { incidents: [] };
const posted: string[] = [];
const postedBodies: Record<string, unknown> = {};
// Catch-ups are fetched on demand, so the stub answers the marker list and one
// record per `n` (docs/fable/catchup.md).
let catchupMarkers: CatchupMarker[] = [];
let catchupRecords: Record<number, CatchupRecord> = {};
// Day archive (docs/fable/daily-summary.md) — same shape: a marker list plus
// one snapshot per sim-day.
let archiveDays: SummaryArchiveMarker[] = [];
let archives: Record<number, SummaryArchive> = {};
// A merge either returns a transient summary or 400s with the manager's own
// sentence; the briefing is one on-demand LLM round trip. Both are thunks so
// the fixtures below are read at call time, not at module init.
let mergeAnswer: () => Promise<unknown> = () => Promise.resolve(mergedCatchup());
let briefingAnswer: () => Briefing = () => briefing();

vi.mock("../../api", () => ({
  apiGet: vi.fn((path: string) => {
    if (path.startsWith("/admin/api/overview")) return Promise.resolve(overview);
    if (path.startsWith("/admin/api/incidents/history")) return Promise.resolve(history);
    if (path.startsWith("/admin/api/incidents")) return Promise.resolve(incidentData);
    if (path.startsWith("/admin/api/presets")) return Promise.resolve([]);
    if (path.startsWith("/admin/api/approvals")) return Promise.resolve([]);
    const catchup = path.match(/\/catchups(?:\/(\d+))?$/);
    if (catchup) {
      return Promise.resolve(
        catchup[1] == null ? catchupMarkers : catchupRecords[Number(catchup[1])],
      );
    }
    const archive = path.match(/\/summaries(?:\/(\d+))?$/);
    if (archive) {
      return Promise.resolve(
        archive[1] == null ? archiveDays : archives[Number(archive[1])],
      );
    }
    return new Promise(() => undefined); // /admin/api/summary — never resolves
  }),
  apiPost: vi.fn((path: string, body?: unknown) => {
    posted.push(path);
    postedBodies[path] = body;
    // /summarize returns the CatchupSummary it just persisted.
    if (path.endsWith("/summarize")) return Promise.resolve(freshSummary);
    if (path.endsWith("/catchups/merge")) return mergeAnswer();
    if (path === "/admin/api/briefing") return Promise.resolve(briefingAnswer());
    return Promise.resolve({});
  }),
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
    safety_issues: 0,
    task_compliance: null,
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

  it("shows failed food-safety checks alongside the backlog", async () => {
    const metric = await renderTicketMetric({ orders_waiting: 2, safety_issues: 3 });
    expect(within(metric).getByText("2")).toBeInTheDocument();
    expect(within(metric).getByText("3 safety")).toBeInTheDocument();
  });

  it("stays silent when no safety check has failed", async () => {
    const metric = await renderTicketMetric({ orders_waiting: 2, safety_issues: 0 });
    expect(within(metric).queryByText(/safety/)).not.toBeInTheDocument();
  });
});

describe("restaurant card — task compliance", () => {
  async function renderCard(overrides: Partial<InstanceCard>) {
    overview.instances = [card(overrides)];
    render(<AdminPage />);
    await waitFor(() => expect(screen.getByText("Bella's")).toBeInTheDocument());
  }

  const counts = { done: 8, done_late: 2, pending: 0, overdue: 0, not_done: 0, accountable: 10 };

  it("badges the on-time rate when checks are being missed", async () => {
    await renderCard({ task_compliance: { ...counts, rate: 0.8 } });
    expect(screen.getByText("tasks 80% on time")).toBeInTheDocument();
  });

  it("says nothing at full compliance", async () => {
    await renderCard({ task_compliance: { ...counts, rate: 1 } });
    expect(screen.queryByText(/on time/)).not.toBeInTheDocument();
  });

  it("says nothing before anything is due", async () => {
    await renderCard({ task_compliance: { ...counts, accountable: 0, rate: null } });
    expect(screen.queryByText(/on time/)).not.toBeInTheDocument();
  });
});


function action(overrides: Partial<ActionItem> = {}): ActionItem {
  return {
    instance_id: "running_fox",
    restaurant: "Bella's",
    kind: "staff",
    problem: "Station Grill unstaffed",
    severity: "high",
    deadline_sim: null,
    impact: "Dishes blocked: Burger, Fries",
    impact_eur: null,
    recommended_action: "Reassign a qualified cook or disable the dishes.",
    approval_id: null,
    ...overrides,
  };
}

describe("priority queue — € at stake", () => {
  async function renderActions(rows: ActionItem[]) {
    overview.instances = [card()];
    overview.actions = rows;
    render(<AdminPage />);
    await waitFor(() => expect(screen.getByText(rows[0].problem)).toBeInTheDocument());
  }

  it("shows the revenue at stake when the row can be priced", async () => {
    await renderActions([action({ impact_eur: 165 })]);
    expect(screen.getByText("€165.00 at stake")).toBeInTheDocument();
  });

  it("shows no chip when the impact cannot be attributed", async () => {
    await renderActions([action({ impact_eur: null })]);
    expect(screen.queryByText(/at stake/)).not.toBeInTheDocument();
  });

  it("shows an order-by deadline on stock rows", async () => {
    await renderActions([
      action({ kind: "stock", problem: "Tomato below safety stock (200 g)",
               deadline_sim: 3000, impact_eur: null }),
    ]);
    expect(screen.getByText(/deadline Day 0 00:50/)).toBeInTheDocument();
  });
});


function incident(overrides: Partial<Incident> = {}): Incident {
  return {
    instance_id: "running_fox",
    restaurant: "Bella's",
    category: "order_backlog",
    signal_type: "ORDER_BACKLOG",
    summary: "24 tickets are backed up on the pass with nobody cooking.",
    count: 1,
    names: [],
    created_at: 43200,
    incident_id: 7,
    status: "open",
    acked_by: null,
    ...overrides,
  };
}

describe("incidents — acknowledge / resolve / history", () => {
  async function openIncidentsTab(rows: Incident[]) {
    overview.instances = [card()];
    incidentData.incidents = rows;
    posted.length = 0;
    render(<AdminPage />);
    await waitFor(() => expect(screen.getByText("Bella's")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Incidents"));
  }

  it("acknowledges an incident through the manager", async () => {
    await openIncidentsTab([incident()]);
    fireEvent.click(await screen.findByText("Acknowledge"));
    await waitFor(() =>
      expect(posted).toContain("/admin/api/incidents/7/ack"),
    );
  });

  it("resolves an incident through the manager", async () => {
    await openIncidentsTab([incident()]);
    fireEvent.click(await screen.findByText("Resolve"));
    await waitFor(() =>
      expect(posted).toContain("/admin/api/incidents/7/resolve"),
    );
  });

  it("marks an acknowledged incident and drops its Acknowledge button", async () => {
    await openIncidentsTab([incident({ status: "acked", acked_by: "jay" })]);
    expect(await screen.findByText("acked · jay")).toBeInTheDocument();
    expect(screen.queryByText("Acknowledge")).not.toBeInTheDocument();
    // Resolve stays available — acked is "seen", not "fixed".
    expect(screen.getByText("Resolve")).toBeInTheDocument();
  });

  it("shows resolved incidents in the history view", async () => {
    history.incidents = [{
      incident_id: 3, instance_id: "running_fox", category: "stockout",
      summary: "Running low on Basil.", opened_at: 100, status: "resolved",
      acked_by: "jay", resolved_at: 1700000000,
    }];
    await openIncidentsTab([]);
    fireEvent.click(screen.getByText("History"));
    expect(await screen.findByText("Running low on Basil.")).toBeInTheDocument();
    expect(screen.getByText("resolved")).toBeInTheDocument();
  });
});


// --- catch-up history drawer (docs/fable/catchup.md) ---------------------

const BULLET =
  "Two purchase orders were placed with Verdura Fresca and City Wholesale for a total of EUR 262.85.";
const EVENT_SUMMARY = "PO #12 placed with Verdura Fresca for 4 items.";

function bucketSummary(overrides: Partial<CatchupSummary> = {}): CatchupSummary {
  return {
    generated_at: 1700000100,
    model: "gemini-2.5-pro",
    error: null,
    buckets: [
      {
        bucket: "procurement",
        event_count: 2,
        truncated: 0,
        bullets: [{ text: BULLET, event_ids: [41] }],
      },
    ],
    incidents: [],
    ...overrides,
  };
}

const freshSummary = bucketSummary({
  buckets: [
    {
      bucket: "staffing",
      event_count: 1,
      truncated: 0,
      bullets: [{ text: "Marco was marked absent for the evening shift.", event_ids: [] }],
    },
  ],
});

function marker(overrides: Partial<CatchupMarker> = {}): CatchupMarker {
  return {
    n: 1,
    created_at: 1700000000,
    since_sim: 0,
    until_sim: 43200,
    event_count: 12,
    summary: null,
    ...overrides,
  };
}

function catchupRecord(m: CatchupMarker): CatchupRecord {
  return {
    ...m,
    instance_id: "running_fox",
    events: [
      {
        id: 41,
        sim_time: 45000,
        category: "po_placed",
        actor: "procurement",
        summary: EVENT_SUMMARY,
        detail: null,
      },
    ],
  };
}

/** Click something that kicks off a fetch, and let the resulting state update
 *  land inside act() — drawers and briefings load on demand, not on render. */
async function click(el: HTMLElement) {
  await act(async () => {
    fireEvent.click(el);
  });
}

// --- merged catch-ups, briefing prose, day archive (Phase 6) --------------

const MERGED_BULLET =
  "Across both windows four purchase orders were placed and two arrived late.";
const PROSE =
  "Sales are 12% ahead of forecast across the portfolio.\nBella's is short a cook tonight.";

function mergedCatchup(): MergedCatchup {
  return {
    from: 1,
    to: 2,
    since_sim: 0,
    until_sim: 86400,
    event_count: 14,
    events: catchupRecord(marker()).events,
    summary: bucketSummary({
      buckets: [
        {
          bucket: "procurement",
          event_count: 14,
          truncated: 0,
          bullets: [{ text: MERGED_BULLET, event_ids: [41] }],
        },
      ],
    }),
  };
}

function briefing(overrides: Partial<Briefing> = {}): Briefing {
  return {
    generated_at: 1700000200,
    model: "gemini-2.5-pro",
    error: null,
    prose: PROSE,
    ...overrides,
  };
}

const archivedDay: SummaryArchive = {
  day: 2,
  created_at: 1700000000,
  instance_id: "running_fox",
  summary: {
    generated_at: 1700000000,
    totals: {
      sales_today: 480,
      forecast_today: 500,
      waste_today: 12.5,
      stock_risks: 3,
      staff_absent: 1,
      pending_approvals: 2,
      offline: 0,
    },
    major_incidents: [action({ problem: "Freezer above 5°C for 40 minutes" })],
    pending_decisions: [],
    next_day_risks: [],
  },
};

describe("catch-up history drawer", () => {
  async function openDrawer(
    markers: CatchupMarker[],
    days: SummaryArchiveMarker[] = [],
  ) {
    overview.instances = [card()];
    overview.actions = [];
    catchupMarkers = markers;
    catchupRecords = Object.fromEntries(
      markers.map((m) => [m.n, catchupRecord(m)]),
    );
    archiveDays = days;
    mergeAnswer = () => Promise.resolve(mergedCatchup());
    posted.length = 0;
    render(<AdminPage />);
    await waitFor(() => expect(screen.getByText("Bella's")).toBeInTheDocument());
    await click(screen.getByText("Catch-ups"));
  }

  const summarized = marker({ n: 2, since_sim: 43200, until_sim: 86400,
                              event_count: 2, summary: bucketSummary() });

  it("lists the captured windows when opened", async () => {
    await openDrawer([marker(), summarized]);
    expect(await screen.findByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("Day 0 00:00 → Day 0 12:00")).toBeInTheDocument();
    expect(screen.getByText("12 events")).toBeInTheDocument();
    // Captures that already carry a summary say so.
    expect(screen.getByText("summarized")).toBeInTheDocument();
  });

  it("renders a selected capture's bullets and expands one to its raw events", async () => {
    await openDrawer([summarized]);
    await click(await screen.findByText("#2"));
    expect(await screen.findByText(BULLET)).toBeInTheDocument();
    expect(screen.getByText("procurement")).toBeInTheDocument();
    // Collapsed until asked.
    expect(screen.queryByText(EVENT_SUMMARY)).not.toBeInTheDocument();

    fireEvent.click(screen.getByText(BULLET));
    expect(await screen.findByText(EVENT_SUMMARY)).toBeInTheDocument();
    expect(
      screen.getByText("Day 0 12:30 · po_placed · procurement"),
    ).toBeInTheDocument();
  });

  it("renders a degraded summary as an error and never as bullets", async () => {
    // buckets is [] on a real error; carry one anyway so the test fails if the
    // view ever falls through to prose. This has shipped twice (progress.md §4).
    const failed = marker({
      n: 3,
      summary: bucketSummary({ error: "LLM returned the canned no-op response" }),
    });
    await openDrawer([failed]);
    await click(await screen.findByText("#3"));
    expect(
      await screen.findByText("LLM returned the canned no-op response"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Summary failed/)).toBeInTheDocument();
    expect(screen.queryByText(BULLET)).not.toBeInTheDocument();
  });

  it("summarizes an unsummarized capture through the manager", async () => {
    await openDrawer([marker()]);
    await click(await screen.findByText("#1"));
    await click(await screen.findByText("Summarize"));
    await waitFor(() =>
      expect(posted).toContain("/admin/api/instances/running_fox/catchups/1/summarize"),
    );
    // The returned summary replaces the "not summarized yet" state in place.
    expect(
      await screen.findByText("Marco was marked absent for the evening shift."),
    ).toBeInTheDocument();
    expect(screen.getByText("Re-summarize")).toBeInTheDocument();
  });

  it("renders a bucket name it does not know rather than blanking it", async () => {
    // The bucket set is enumerated from free-string event categories and will
    // grow — an unknown one must still show up, never render empty.
    const future = marker({
      n: 4,
      summary: bucketSummary({
        buckets: [
          {
            bucket: "delivery_ops",
            event_count: 1,
            truncated: 3,
            bullets: [{ text: "A courier no-showed.", event_ids: [] }],
          },
        ],
      }),
    });
    await openDrawer([future]);
    await click(await screen.findByText("#4"));
    expect(await screen.findByText("delivery ops")).toBeInTheDocument();
    expect(screen.getByText("A courier no-showed.")).toBeInTheDocument();
    expect(screen.getByText("3 older events not summarized")).toBeInTheDocument();
  });

  it("merges a range of captures into one transient summary", async () => {
    // The range defaults to every capture there is: #1 → #2.
    await openDrawer([marker(), summarized]);
    await click(await screen.findByText("Merge"));
    const path = "/admin/api/instances/running_fox/catchups/merge";
    expect(posted).toContain(path);
    expect(postedBodies[path]).toEqual({ from: 1, to: 2 });
    expect(await screen.findByText(MERGED_BULLET)).toBeInTheDocument();
    // Merging never writes: the captures stay the audit trail.
    expect(screen.getByText("Catch-ups #1–#2, merged")).toBeInTheDocument();
    expect(screen.getByText("not saved")).toBeInTheDocument();
  });

  it("cannot submit a range whose end precedes its start", async () => {
    await openDrawer([marker(), summarized]);
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("Merge from"), {
        target: { value: "2" },
      });
      fireEvent.change(screen.getByLabelText("Merge to"), { target: { value: "1" } });
    });
    expect(screen.getByText("Merge")).toBeDisabled();
    await click(screen.getByText("Merge"));
    expect(posted).not.toContain("/admin/api/instances/running_fox/catchups/merge");
  });

  it("shows the manager's own sentence when a range cannot be merged", async () => {
    await openDrawer([marker(), summarized]);
    mergeAnswer = () =>
      Promise.reject(
        Object.assign(new Error("POST /catchups/merge -> 400"), {
          detail: "Catch-up 2 is missing — the range is not contiguous.",
        }),
      );
    await click(screen.getByText("Merge"));
    expect(
      await screen.findByText("Catch-up 2 is missing — the range is not contiguous."),
    ).toBeInTheDocument();
    expect(screen.queryByText(MERGED_BULLET)).not.toBeInTheDocument();
  });

  it("lists archived sim-days and renders the selected day's snapshot", async () => {
    archives = { 2: archivedDay };
    await openDrawer([marker()], [
      { day: 2, created_at: 1700000000 },
      { day: 1, created_at: 1699913600 },
    ]);
    await click(await screen.findByText("Day archive"));
    expect(await screen.findByText("Day 2")).toBeInTheDocument();
    expect(screen.getByText("Day 1")).toBeInTheDocument();

    await click(screen.getByText("Day 2"));
    expect(await screen.findByText("End of sim-day 2")).toBeInTheDocument();
    // Same rendering as the live briefing tab, from the same Summary shape.
    expect(screen.getByText("€480.00 / €500.00")).toBeInTheDocument();
    expect(screen.getByText("€12.50")).toBeInTheDocument();
    expect(screen.getByText(/Freezer above 5°C/)).toBeInTheDocument();
  });
});


// --- daily briefing prose (docs/fable/daily-summary.md) -------------------

describe("daily briefing — written prose", () => {
  async function writeBriefing(b: Briefing) {
    overview.instances = [card()];
    overview.actions = [];
    briefingAnswer = () => b;
    posted.length = 0;
    render(<AdminPage />);
    await waitFor(() => expect(screen.getByText("Bella's")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Daily briefing"));
    await click(await screen.findByText("Write briefing"));
  }

  it("writes a briefing on demand and renders the prose", async () => {
    await writeBriefing(briefing());
    expect(posted).toContain("/admin/api/briefing");
    expect(await screen.findByText(/Sales are 12% ahead of forecast/)).toBeInTheDocument();
    expect(screen.getByText(/gemini-2\.5-pro/)).toBeInTheDocument();
  });

  it("renders a degraded briefing as an error and never as prose", async () => {
    // A real failure carries prose: ""; carry the real thing anyway so this
    // fails loudly if the view ever falls through (progress.md §4).
    await writeBriefing(briefing({ error: "LLM returned the canned no-op response" }));
    expect(
      await screen.findByText("LLM returned the canned no-op response"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Briefing failed/)).toBeInTheDocument();
    expect(screen.queryByText(/Sales are 12% ahead of forecast/)).not.toBeInTheDocument();
  });
});
