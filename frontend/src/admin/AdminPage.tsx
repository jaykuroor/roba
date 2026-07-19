import { useState } from "react";
import {
  AlertTriangle,
  Check,
  ExternalLink,
  History,
  Play,
  Plus,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { apiDelete, apiPost } from "../api";
import { PRESET_EMOJI, RestaurantLogo } from "../shell/RestaurantLogo";
import {
  type ActionItem,
  type AdminApproval,
  type InstanceCard,
  fmtMoney,
  fmtSim,
  useAdminData,
} from "./useAdminData";

// Manager dashboard (/admin) — the entry point of roba
// (docs/fable/manager-dashboard.md). Workflow, top to bottom:
//
//   1. Restaurant strip: identity (logo), health, today's numbers, open/create.
//   2. Attention tabs: what needs a decision (queue), approvals, incidents,
//      and the daily briefing — one surface each, with counts in the tab.
//
// Everything actionable is actionable in place (approve/reject/open); the
// restaurant console is one click away and opens with a full page load so
// per-instance state starts clean.

const STATUS_PILL: Record<string, string> = {
  normal: "bg-success/20 text-success",
  warning: "bg-amber-500/20 text-amber-400",
  critical: "bg-danger/20 text-danger",
  offline: "bg-muted text-text/50",
};

const STATUS_BORDER: Record<string, string> = {
  normal: "border-l-success/50",
  warning: "border-l-amber-500",
  critical: "border-l-danger",
  offline: "border-l-muted",
};

const SEVERITY_STYLE: Record<string, string> = {
  critical: "bg-danger text-white",
  high: "bg-amber-500 text-white",
  medium: "bg-muted text-text/80",
  low: "bg-muted text-text/50",
};

function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${STATUS_PILL[status] ?? STATUS_PILL.offline}`}
    >
      {status}
    </span>
  );
}

function Metric({
  label,
  value,
  accent,
}: {
  label: string;
  value: React.ReactNode;
  accent?: string;
}) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-text/40">{label}</div>
      <div className={`text-sm font-medium ${accent ?? "text-text"}`}>{value}</div>
    </div>
  );
}

/** Sales against forecast with a signed % delta chip. */
function SalesVsForecast({ card }: { card: InstanceCard }) {
  if (card.sales_today == null) return <span className="text-text/40">—</span>;
  const forecast = card.forecast_today ?? 0;
  const delta =
    forecast > 0 ? Math.round(((card.sales_today - forecast) / forecast) * 100) : null;
  return (
    <span className="flex items-baseline gap-1.5">
      {fmtMoney(card.sales_today)}
      <span className="text-xs text-text/40">/ {fmtMoney(forecast)} fc</span>
      {delta != null && (
        <span
          className={`rounded px-1 text-[11px] font-semibold ${
            delta >= 0 ? "bg-success/15 text-success" : "bg-danger/15 text-danger"
          }`}
        >
          {delta >= 0 ? "+" : ""}
          {delta}%
        </span>
      )}
    </span>
  );
}

function RestaurantCard({
  card,
  onChanged,
}: {
  card: InstanceCard;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [catchupNote, setCatchupNote] = useState<string | null>(null);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  async function catchUp() {
    setBusy(true);
    try {
      const res = await apiPost<{ n: number; event_count: number }>(
        `/admin/api/instances/${card.id}/catchups`,
      );
      // Readable summaries are a future feature (docs/fable/catchup.md);
      // today this captures the event window for later summarization.
      setCatchupNote(`Catch-up #${res.n}: captured ${res.event_count} events`);
    } catch {
      setCatchupNote("Catch-up failed (instance offline?)");
    } finally {
      setBusy(false);
    }
  }

  function remove() {
    if (
      window.confirm(
        `Remove ${card.title} (${card.id}) from the dashboard?\nIts database file is kept on disk and can be recovered.`,
      )
    ) {
      run(() => apiDelete(`/admin/api/instances/${card.id}`));
    }
  }

  const issueBadges = [
    card.pending_approvals > 0 && {
      label: `${card.pending_approvals} approval${card.pending_approvals > 1 ? "s" : ""}`,
      cls: "bg-amber-500/15 text-amber-400",
    },
    card.stock_risks.length > 0 && {
      label: `${card.stock_risks.length} stock risk${card.stock_risks.length > 1 ? "s" : ""}`,
      cls: "bg-danger/15 text-danger",
    },
    card.absent.length > 0 && {
      label: `out: ${card.absent.join(", ")}`,
      cls: "bg-amber-500/15 text-amber-400",
    },
  ].filter(Boolean) as { label: string; cls: string }[];

  return (
    <div
      className={`rounded-lg border border-muted border-l-4 bg-surface p-4 ${STATUS_BORDER[card.status] ?? STATUS_BORDER.offline}`}
    >
      <div className="flex items-center gap-3">
        <RestaurantLogo id={card.id} title={card.title} preset={card.preset} size={44} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-base font-semibold text-text">{card.title}</h3>
            <StatusPill status={card.status} />
          </div>
          <div className="truncate text-xs text-text/40">
            {card.id} · {card.online ? fmtSim(card.sim?.sim_time) : "not running"}
            {card.note ? ` · ${card.note}` : ""}
          </div>
        </div>
        <a
          href={`/${card.id}`}
          className="flex shrink-0 items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white"
          title={`Open ${card.title}'s console`}
        >
          <ExternalLink size={14} /> Open
        </a>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-x-4 gap-y-2">
        <Metric label="Sales today" value={<SalesVsForecast card={card} />} />
        <Metric label="Orders" value={card.orders_today ?? "—"} />
        <Metric
          label="Staff"
          value={
            card.staff_present == null ? "—" : `${card.staff_present}/${card.staff_total}`
          }
          accent={card.absent.length ? "text-amber-400" : undefined}
        />
      </div>

      {issueBadges.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {issueBadges.map((badge) => (
            <span
              key={badge.label}
              className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${badge.cls}`}
            >
              {badge.label}
            </span>
          ))}
        </div>
      )}

      {catchupNote && <p className="mt-2 text-xs text-text/50">{catchupNote}</p>}

      <div className="mt-3 flex items-center gap-1.5 border-t border-muted/60 pt-2">
        <button
          type="button"
          disabled={busy || !card.online}
          onClick={catchUp}
          className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-text/60 hover:bg-muted disabled:opacity-40"
          title="Capture everything that happened since the last catch-up"
        >
          <History size={13} /> Catch up
        </button>
        {card.online ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(() => apiPost(`/admin/api/instances/${card.id}/stop`))}
            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-text/60 hover:bg-muted disabled:opacity-40"
          >
            <Square size={12} /> Stop
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(() => apiPost(`/admin/api/instances/${card.id}/start`))}
            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-text/60 hover:bg-muted disabled:opacity-40"
          >
            <Play size={12} /> Start
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={remove}
          className="ml-auto rounded-md p-1 text-text/30 hover:bg-muted hover:text-danger disabled:opacity-40"
          aria-label={`Remove ${card.id}`}
        >
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  );
}

/** Dashed "add" card — restaurant creation lives with the restaurants. */
function AddRestaurantCard({
  presets,
  onCreated,
}: {
  presets: string[];
  onCreated: () => void;
}) {
  const [preset, setPreset] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const chosen = preset || presets[0] || "";

  async function create() {
    if (!chosen) return;
    setCreating(true);
    setError(null);
    try {
      await apiPost("/admin/api/instances", { preset: chosen });
      onCreated();
    } catch {
      setError("Could not start the instance — is the manager running?");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="flex min-h-44 flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-muted p-4 text-center">
      <div className="text-sm font-medium text-text/60">New restaurant</div>
      <select
        value={chosen}
        onChange={(e) => setPreset(e.target.value)}
        disabled={creating}
        className="rounded-md border border-muted bg-surface px-2 py-1.5 text-sm text-text"
        aria-label="Preset"
      >
        {presets.map((p) => (
          <option key={p} value={p}>
            {PRESET_EMOJI[p] ? `${PRESET_EMOJI[p]} ` : ""}
            {p.replace(/_/g, " ")}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={creating || !chosen}
        onClick={create}
        className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
      >
        <Plus size={15} /> {creating ? "Starting… (seeds in ~10s)" : "Create"}
      </button>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}

function ActionRow({
  action,
  onResolved,
}: {
  action: ActionItem;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function resolve(decision: "approve" | "reject") {
    setBusy(true);
    try {
      await apiPost(
        `/admin/api/approvals/${action.instance_id}/${action.approval_id}/${decision}`,
      );
      onResolved();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-muted bg-surface px-3 py-2">
      <span
        className={`w-16 shrink-0 rounded px-2 py-0.5 text-center text-[11px] font-semibold uppercase ${SEVERITY_STYLE[action.severity] ?? SEVERITY_STYLE.low}`}
      >
        {action.severity}
      </span>
      <RestaurantLogo id={action.instance_id} title={action.restaurant} size={26} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-text">{action.problem}</div>
        <div className="text-xs text-text/50">
          {action.impact}
          {action.deadline_sim != null && (
            <span className="ml-2 text-amber-400">
              deadline {fmtSim(action.deadline_sim)}
            </span>
          )}
        </div>
        <div className="text-xs text-text/40">→ {action.recommended_action}</div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {action.approval_id != null && (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => resolve("approve")}
              className="flex items-center gap-1 rounded-md bg-success px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50"
            >
              <Check size={13} /> Approve
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => resolve("reject")}
              className="flex items-center gap-1 rounded-md bg-danger px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50"
            >
              <X size={13} /> Reject
            </button>
          </>
        )}
        <a
          href={`/${action.instance_id}`}
          className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium text-text/70"
        >
          Open
        </a>
      </div>
    </div>
  );
}

function ApprovalRow({
  approval,
  onResolved,
}: {
  approval: AdminApproval;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function resolve(decision: "approve" | "reject") {
    setBusy(true);
    try {
      await apiPost(
        `/admin/api/approvals/${approval.instance_id}/${approval.id}/${decision}`,
      );
      onResolved();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-muted bg-surface p-3">
      <div className="flex items-center gap-2">
        <RestaurantLogo id={approval.instance_id} title={approval.restaurant} size={24} />
        <span className="text-xs font-semibold text-text/70">{approval.restaurant}</span>
        <span className="rounded bg-muted px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-text/70">
          {approval.type}
        </span>
        {approval.urgency !== "normal" && (
          <span className="rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-400">
            {approval.urgency}
          </span>
        )}
      </div>
      <h4 className="mt-1.5 text-sm font-semibold text-text">{approval.title}</h4>
      <p className="text-sm text-text/70">{approval.summary}</p>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => resolve("approve")}
          className="flex flex-1 items-center justify-center gap-1 rounded-md bg-success px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          <Check size={15} /> Approve
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => resolve("reject")}
          className="flex flex-1 items-center justify-center gap-1 rounded-md bg-danger px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          <X size={15} /> Reject
        </button>
      </div>
    </div>
  );
}

type Tab = "queue" | "approvals" | "incidents" | "briefing";

export default function AdminPage() {
  const data = useAdminData();
  const [tab, setTab] = useState<Tab>("queue");
  const [restaurantFilter, setRestaurantFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");

  const critical = data.instances.filter((c) => c.status === "critical").length;
  const warning = data.instances.filter((c) => c.status === "warning").length;
  const offline = data.instances.filter((c) => c.status === "offline").length;

  const approvalTypes = [...new Set(data.approvals.map((a) => a.type))];
  const filteredApprovals = data.approvals.filter(
    (a) =>
      (restaurantFilter === "all" || a.instance_id === restaurantFilter) &&
      (typeFilter === "all" || a.type === typeFilter),
  );

  const tabs: { id: Tab; label: string; count: number | null }[] = [
    { id: "queue", label: "Needs attention", count: data.actions.length },
    { id: "approvals", label: "Approvals", count: data.approvals.length },
    { id: "incidents", label: "Incidents", count: data.incidents.length },
    { id: "briefing", label: "Daily briefing", count: null },
  ];

  const selectClass =
    "rounded-md border border-muted bg-surface px-2 py-1.5 text-sm text-text";

  return (
    <div className="min-h-screen bg-primary px-6 py-4 text-text">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Roba Manager</h1>
        {/* Portfolio pulse: one glance = is anything on fire? */}
        {data.instances.length > 0 && (
          <span
            className={`flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${
              critical > 0
                ? "bg-danger/15 text-danger"
                : warning + offline > 0
                  ? "bg-amber-500/15 text-amber-400"
                  : "bg-success/15 text-success"
            }`}
          >
            {critical + warning + offline > 0 && <AlertTriangle size={12} />}
            {critical > 0
              ? `${critical} critical`
              : warning > 0
                ? `${warning} need${warning === 1 ? "s" : ""} a look`
                : offline > 0
                  ? `${offline} offline`
                  : "all healthy"}
          </span>
        )}
        {data.error && (
          <span className="rounded bg-danger/20 px-2 py-0.5 text-xs text-danger">
            manager unreachable — {data.error}
          </span>
        )}
      </header>

      <section className="mt-4">
        <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
          {data.instances.map((card) => (
            <RestaurantCard key={card.id} card={card} onChanged={data.refresh} />
          ))}
          <AddRestaurantCard presets={data.presets} onCreated={data.refresh} />
        </div>
      </section>

      <nav className="mt-6 flex gap-1 border-b border-muted">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={
              tab === t.id
                ? "-mb-px flex items-center gap-1.5 border-b-2 border-accent px-3 py-2 text-sm font-medium text-text"
                : "-mb-px flex items-center gap-1.5 border-b-2 border-transparent px-3 py-2 text-sm font-medium text-text/50 hover:text-text"
            }
          >
            {t.label}
            {t.count != null && t.count > 0 && (
              <span
                className={`rounded-full px-1.5 text-[11px] font-semibold ${
                  tab === t.id ? "bg-accent text-white" : "bg-muted text-text/60"
                }`}
              >
                {t.count}
              </span>
            )}
          </button>
        ))}
      </nav>

      <section className="mt-4">
        {tab === "queue" &&
          (data.actions.length === 0 ? (
            <p className="py-8 text-center text-sm text-text/40">
              Nothing needs your attention. 🎉
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {data.actions.map((action, i) => (
                <ActionRow
                  key={`${action.instance_id}-${action.kind}-${action.approval_id ?? i}`}
                  action={action}
                  onResolved={data.refresh}
                />
              ))}
            </div>
          ))}

        {tab === "approvals" && (
          <>
            <div className="mb-3 flex gap-2">
              <select
                value={restaurantFilter}
                onChange={(e) => setRestaurantFilter(e.target.value)}
                className={selectClass}
                aria-label="Filter by restaurant"
              >
                <option value="all">All restaurants</option>
                {data.instances.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.title}
                  </option>
                ))}
              </select>
              <select
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value)}
                className={selectClass}
                aria-label="Filter by type"
              >
                <option value="all">All types</option>
                {approvalTypes.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </div>
            {filteredApprovals.length === 0 ? (
              <p className="py-8 text-center text-sm text-text/40">
                No pending approvals.
              </p>
            ) : (
              <div className="grid gap-3 lg:grid-cols-2">
                {filteredApprovals.map((a) => (
                  <ApprovalRow
                    key={`${a.instance_id}-${a.id}`}
                    approval={a}
                    onResolved={data.refresh}
                  />
                ))}
              </div>
            )}
          </>
        )}

        {tab === "incidents" && (
          <>
            {data.incidents.length === 0 ? (
              <p className="py-8 text-center text-sm text-text/40">
                No open incidents.
              </p>
            ) : (
              <div className="flex flex-col gap-1.5">
                {data.incidents.map((incident, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-2 rounded-md border border-muted bg-surface px-3 py-1.5 text-sm"
                  >
                    <span className="w-28 shrink-0 rounded bg-muted px-2 py-0.5 text-center text-[11px] font-medium uppercase text-text/60">
                      {incident.category.replace(/_/g, " ")}
                    </span>
                    <RestaurantLogo
                      id={incident.instance_id}
                      title={incident.restaurant}
                      size={22}
                    />
                    <span className="min-w-0 flex-1 truncate text-text">
                      {incident.summary}
                    </span>
                    <span className="shrink-0 text-xs text-text/40">
                      {fmtSim(incident.created_at)}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <p className="mt-3 text-xs text-text/30">
              Not yet detected:{" "}
              {data.unavailableCategories.join(", ").replace(/_/g, " ")} (see
              docs/fable/incidents.md)
            </p>
          </>
        )}

        {tab === "briefing" &&
          (data.summary == null ? (
            <p className="py-8 text-center text-sm text-text/40">Loading…</p>
          ) : (
            <div className="rounded-lg border border-muted bg-surface p-4 text-sm">
              <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
                <Metric
                  label="Portfolio sales / forecast"
                  value={`${fmtMoney(data.summary.totals.sales_today)} / ${fmtMoney(data.summary.totals.forecast_today)}`}
                />
                <Metric
                  label="Waste today"
                  value={fmtMoney(data.summary.totals.waste_today)}
                />
                <Metric label="Stock risks" value={data.summary.totals.stock_risks} />
                <Metric label="Staff absent" value={data.summary.totals.staff_absent} />
              </div>
              <SummaryList label="Major incidents" items={data.summary.major_incidents} />
              <SummaryList
                label="Pending decisions"
                items={data.summary.pending_decisions}
              />
              <SummaryList
                label="Risks for the next day"
                items={data.summary.next_day_risks}
              />
            </div>
          ))}
      </section>
    </div>
  );
}

function SummaryList({ label, items }: { label: string; items: ActionItem[] }) {
  return (
    <div className="mt-3">
      <div className="text-[11px] uppercase tracking-wide text-text/40">{label}</div>
      {items.length === 0 ? (
        <p className="text-sm text-text/40">none</p>
      ) : (
        <ul className="mt-0.5 list-inside list-disc text-sm text-text/80">
          {items.map((item, i) => (
            <li key={i}>
              <span className="text-text/50">{item.restaurant}:</span> {item.problem}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
