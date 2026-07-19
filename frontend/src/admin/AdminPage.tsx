import { useState } from "react";
import { Check, ExternalLink, Play, Plus, Square, Trash2, X } from "lucide-react";
import { apiDelete, apiPost } from "../api";
import {
  type ActionItem,
  type AdminApproval,
  type InstanceCard,
  fmtMoney,
  fmtSim,
  useAdminData,
} from "./useAdminData";

// Manager dashboard (/admin): portfolio of restaurant instances with a
// priority action queue, combined approvals, incidents and a daily summary.
// All data comes from the manager server (docs/fable/manager-dashboard.md).

const STATUS_STYLE: Record<string, string> = {
  normal: "bg-success/20 text-success",
  warning: "bg-amber-500/20 text-amber-400",
  critical: "bg-danger/20 text-danger",
  offline: "bg-muted text-text/50",
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
      className={`rounded-full px-2 py-0.5 text-xs font-semibold uppercase tracking-wide ${STATUS_STYLE[status] ?? STATUS_STYLE.offline}`}
    >
      {status}
    </span>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-text/40">{label}</div>
      <div className="text-sm font-medium text-text">{value}</div>
    </div>
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
      setCatchupNote(`Catch-up #${res.n} captured ${res.event_count} events`);
    } catch {
      setCatchupNote("Catch-up failed (instance offline?)");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-muted bg-surface p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold text-text">{card.title}</h3>
          <div className="text-xs text-text/40">
            {card.id} · {card.preset} · {fmtSim(card.sim?.sim_time)}
          </div>
        </div>
        <StatusPill status={card.status} />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
        <Metric
          label="Sales / forecast"
          value={`${fmtMoney(card.sales_today)} / ${fmtMoney(card.forecast_today)}`}
        />
        <Metric label="Orders today" value={card.orders_today?.toString() ?? "—"} />
        <Metric
          label="Staff present"
          value={
            card.staff_present == null
              ? "—"
              : `${card.staff_present}/${card.staff_total}${card.absent.length ? ` (out: ${card.absent.join(", ")})` : ""}`
          }
        />
        <Metric
          label="Stock risk"
          value={
            card.stock_risks.length === 0
              ? "none"
              : card.stock_risks
                  .map((r) => `${r.ingredient} (${r.status})`)
                  .join(", ")
          }
        />
        <Metric label="Approvals" value={`${card.pending_approvals} pending`} />
        {/* Not implemented yet — see docs/fable/portfolio-overview.md */}
        <Metric label="Tickets / safety" value="not yet tracked" />
      </div>

      {catchupNote && <p className="mt-2 text-xs text-text/50">{catchupNote}</p>}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {/* Full page load so the per-instance console starts with clean state. */}
        <a
          href={`/${card.id}`}
          className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white"
        >
          <ExternalLink size={14} /> Open
        </a>
        <button
          type="button"
          disabled={busy || !card.online}
          onClick={catchUp}
          className="rounded-md bg-muted px-3 py-1.5 text-sm font-medium text-text disabled:opacity-50"
        >
          Catch up
        </button>
        {card.online ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(() => apiPost(`/admin/api/instances/${card.id}/stop`))}
            className="flex items-center gap-1 rounded-md bg-muted px-3 py-1.5 text-sm text-text/70 disabled:opacity-50"
          >
            <Square size={13} /> Stop
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(() => apiPost(`/admin/api/instances/${card.id}/start`))}
            className="flex items-center gap-1 rounded-md bg-muted px-3 py-1.5 text-sm text-text/70 disabled:opacity-50"
          >
            <Play size={13} /> Start
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => run(() => apiDelete(`/admin/api/instances/${card.id}`))}
          className="ml-auto rounded-md p-1.5 text-text/40 hover:bg-muted hover:text-danger disabled:opacity-50"
          aria-label={`Remove ${card.id}`}
        >
          <Trash2 size={15} />
        </button>
      </div>
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
        className={`rounded px-2 py-0.5 text-xs font-semibold uppercase ${SEVERITY_STYLE[action.severity] ?? SEVERITY_STYLE.low}`}
      >
        {action.severity}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-text">
          <span className="text-text/50">{action.restaurant}:</span> {action.problem}
        </div>
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
      <div className="flex items-center gap-1.5">
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
        <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-semibold text-accent">
          {approval.restaurant}
        </span>
        <span className="rounded bg-muted px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-text/70">
          {approval.type}
        </span>
        {approval.urgency !== "normal" && (
          <span className="rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-400">
            {approval.urgency}
          </span>
        )}
      </div>
      <h4 className="mt-1 text-sm font-semibold text-text">{approval.title}</h4>
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

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text/50">
        {title}
      </h2>
      {children}
    </section>
  );
}

export default function AdminPage() {
  const data = useAdminData();
  const [preset, setPreset] = useState("");
  const [creating, setCreating] = useState(false);
  const [restaurantFilter, setRestaurantFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");

  async function createInstance() {
    const chosen = preset || data.presets[0];
    if (!chosen) return;
    setCreating(true);
    try {
      await apiPost("/admin/api/instances", { preset: chosen });
      data.refresh();
    } finally {
      setCreating(false);
    }
  }

  const approvalTypes = [...new Set(data.approvals.map((a) => a.type))];
  const filteredApprovals = data.approvals.filter(
    (a) =>
      (restaurantFilter === "all" || a.instance_id === restaurantFilter) &&
      (typeFilter === "all" || a.type === typeFilter),
  );

  const selectClass =
    "rounded-md border border-muted bg-surface px-2 py-1.5 text-sm text-text";

  return (
    <div className="min-h-screen bg-primary px-6 py-4 text-text">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Roba Manager</h1>
        <span className="text-xs text-text/40">
          {data.instances.length} restaurants
        </span>
        {data.error && (
          <span className="rounded bg-danger/20 px-2 py-0.5 text-xs text-danger">
            {data.error}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <select
            value={preset || data.presets[0] || ""}
            onChange={(e) => setPreset(e.target.value)}
            className={selectClass}
            aria-label="Preset"
          >
            {data.presets.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={creating || data.presets.length === 0}
            onClick={createInstance}
            className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            <Plus size={15} /> {creating ? "Starting…" : "New restaurant"}
          </button>
        </div>
      </header>

      <Section title="Restaurants">
        {data.instances.length === 0 ? (
          <p className="text-sm text-text/40">
            No restaurants yet — pick a preset and press “New restaurant”.
          </p>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {data.instances.map((card) => (
              <RestaurantCard key={card.id} card={card} onChanged={data.refresh} />
            ))}
          </div>
        )}
      </Section>

      <Section title="Priority action queue">
        {data.actions.length === 0 ? (
          <p className="text-sm text-text/40">Nothing needs attention.</p>
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
        )}
      </Section>

      <Section title="Approvals — all restaurants">
        <div className="mb-2 flex gap-2">
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
          <p className="text-sm text-text/40">No pending approvals.</p>
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
      </Section>

      <Section title="Incidents">
        {data.incidents.length === 0 ? (
          <p className="text-sm text-text/40">No open incidents.</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {data.incidents.map((incident, i) => (
              <div
                key={i}
                className="flex items-center gap-2 rounded-md border border-muted bg-surface px-3 py-1.5 text-sm"
              >
                <span className="rounded bg-muted px-2 py-0.5 text-xs font-medium uppercase text-text/60">
                  {incident.category.replace(/_/g, " ")}
                </span>
                <span className="text-text/50">{incident.restaurant}:</span>
                <span className="min-w-0 flex-1 truncate text-text">
                  {incident.summary}
                </span>
                <span className="text-xs text-text/40">
                  {fmtSim(incident.created_at)}
                </span>
              </div>
            ))}
          </div>
        )}
        <p className="mt-2 text-xs text-text/30">
          Not yet detected: {data.unavailableCategories.join(", ").replace(/_/g, " ")}{" "}
          (see docs/fable/incidents.md)
        </p>
      </Section>

      <Section title="Daily summary">
        {data.summary == null ? (
          <p className="text-sm text-text/40">Loading…</p>
        ) : (
          <div className="rounded-lg border border-muted bg-surface p-4 text-sm">
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
              <Metric
                label="Portfolio sales / forecast"
                value={`${fmtMoney(data.summary.totals.sales_today)} / ${fmtMoney(data.summary.totals.forecast_today)}`}
              />
              <Metric label="Waste today" value={fmtMoney(data.summary.totals.waste_today)} />
              <Metric
                label="Stock risks"
                value={data.summary.totals.stock_risks.toString()}
              />
              <Metric
                label="Staff absent"
                value={data.summary.totals.staff_absent.toString()}
              />
            </div>
            <SummaryList label="Major incidents" items={data.summary.major_incidents} />
            <SummaryList label="Pending decisions" items={data.summary.pending_decisions} />
            <SummaryList label="Risks for the next day" items={data.summary.next_day_risks} />
          </div>
        )}
      </Section>
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
