import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Camera,
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  History,
  Play,
  Plus,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { apiDelete, apiGet, apiPost } from "../api";
import { PRESET_EMOJI, RestaurantLogo } from "../shell/RestaurantLogo";
import {
  type ActionItem,
  type AdminApproval,
  type CatchupEvent,
  type CatchupMarker,
  type CatchupRecord,
  type CatchupSummary,
  type Incident,
  type IncidentHistoryRow,
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

// Mirrors core.config.BACKLOG_WARN / BACKLOG_CRIT (and manager.py's copy) — the
// card colours the backlog at the same points derive_status changes status.
const BACKLOG_WARN = 8;
const BACKLOG_CRIT = 20;

const INCIDENT_STATUS_STYLE: Record<string, string> = {
  open: "bg-danger/15 text-danger",
  acked: "bg-amber-500/15 text-amber-400",
  resolved: "bg-success/15 text-success",
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

/** Kitchen tickets waiting + average ticket time, and any failed food-safety
 *  check — the two things that make a kitchen unsafe to leave alone. */
function TicketBacklog({ card }: { card: InstanceCard }) {
  const safety = card.safety_issues ?? 0;
  if (card.orders_waiting == null && safety === 0) {
    return <span className="text-text/40">—</span>;
  }
  return (
    <span className="flex flex-wrap items-baseline gap-1.5">
      {card.orders_waiting ?? "—"}
      {card.ticket_time_min != null && (
        <span className="text-xs text-text/40">{card.ticket_time_min}m avg</span>
      )}
      {safety > 0 && (
        <span
          className="rounded bg-danger/15 px-1 text-[11px] font-semibold text-danger"
          title="Failed temperature / food-safety checks"
        >
          {safety} safety
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
  const [showCatchups, setShowCatchups] = useState(false);

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

  const backlog = card.orders_waiting ?? 0;
  const ticketAccent =
    (card.safety_issues ?? 0) > 0 || backlog >= BACKLOG_CRIT
      ? "text-danger"
      : backlog >= BACKLOG_WARN
        ? "text-amber-400"
        : undefined;
  // Only worth the manager's attention when checks are actually being missed.
  const compliance = card.task_compliance?.rate;

  const issueBadges = [
    compliance != null && compliance < 1 && {
      label: `tasks ${Math.round(compliance * 100)}% on time`,
      cls: compliance < 0.75 ? "bg-danger/15 text-danger" : "bg-amber-500/15 text-amber-400",
    },
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

      <div className="mt-3 grid grid-cols-4 gap-x-4 gap-y-2">
        <Metric label="Sales today" value={<SalesVsForecast card={card} />} />
        <Metric label="Orders" value={card.orders_today ?? "—"} />
        <Metric
          label="Staff"
          value={
            card.staff_present == null ? "—" : `${card.staff_present}/${card.staff_total}`
          }
          accent={card.absent.length ? "text-amber-400" : undefined}
        />
        <Metric label="Tickets" value={<TicketBacklog card={card} />} accent={ticketAccent} />
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
          <Camera size={13} /> Catch up
        </button>
        <button
          type="button"
          onClick={() => setShowCatchups(true)}
          className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-text/60 hover:bg-muted"
          title={`Previous catch-ups for ${card.title}`}
        >
          {/* Not labelled "History" — the incidents tab owns that word, and two
              History buttons on one screen is a coin flip for the reader. */}
          <History size={13} /> Catch-ups
        </button>
        {showCatchups && (
          <CatchupDrawer
            instanceId={card.id}
            title={card.title}
            onClose={() => setShowCatchups(false)}
          />
        )}
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

/** One summary bullet. Click expands it into the raw events it was written
 *  from — the whole point of capturing events rather than a (since, until)
 *  marker (docs/fable/catchup.md). A bullet with no source ids is not
 *  expandable, so it renders as plain text. */
function CatchupBullet({
  bullet,
  events,
}: {
  bullet: { text: string; event_ids: number[] };
  events: CatchupEvent[];
}) {
  const [open, setOpen] = useState(false);

  if (bullet.event_ids.length === 0) {
    return <li className="py-0.5 pl-5 text-sm text-text/80">{bullet.text}</li>;
  }
  const ids = new Set(bullet.event_ids);
  const sources = events.filter((e) => ids.has(e.id));

  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-1 rounded px-1 py-0.5 text-left text-sm text-text/80 hover:bg-muted/60"
      >
        <span className="mt-0.5 shrink-0 text-text/40">
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
        <span className="flex-1">{bullet.text}</span>
        <span className="shrink-0 rounded bg-muted px-1 text-[11px] font-medium text-text/50">
          {bullet.event_ids.length}
        </span>
      </button>
      {open && (
        <div className="ml-5 flex flex-col gap-0.5 border-l border-muted pl-2 pb-1">
          {sources.length === 0 ? (
            <p className="text-xs text-text/40">
              Source events are no longer in this capture.
            </p>
          ) : (
            sources.map((e) => (
              <div key={e.id} className="text-xs text-text/60">
                <span className="text-text/40">
                  {fmtSim(e.sim_time)} · {e.category} · {e.actor}
                </span>{" "}
                {e.summary}
              </div>
            ))
          )}
        </div>
      )}
    </li>
  );
}

/** The summary of one capture, or the error that replaced it.
 *  A degraded LLM call (bad model id, no credentials, truncation) comes back
 *  with `error` set and no buckets — it is rendered as an error and never as
 *  prose. That trap has bitten this project twice; see §4 of
 *  docs/fable/progress.md. */
function CatchupSummaryView({
  summary,
  events,
}: {
  summary: CatchupSummary;
  events: CatchupEvent[];
}) {
  if (summary.error) {
    return (
      <div className="rounded-md border border-danger/40 bg-danger/10 p-3">
        <div className="flex items-center gap-1.5 text-sm font-semibold text-danger">
          <AlertTriangle size={14} /> Summary failed — no summary was written
        </div>
        <p className="mt-1 text-xs text-danger/90">{summary.error}</p>
        <p className="mt-1 text-[11px] text-text/40">model: {summary.model}</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {summary.buckets.length === 0 && (
        <p className="text-sm text-text/40">The summary is empty.</p>
      )}
      {summary.buckets.map((bucket) => (
        <div key={bucket.bucket}>
          <div className="flex items-baseline gap-2">
            <h5 className="text-xs font-semibold uppercase tracking-wide text-text/60">
              {bucket.bucket.replace(/_/g, " ")}
            </h5>
            <span className="text-[11px] text-text/40">{bucket.event_count} events</span>
          </div>
          <ul className="mt-0.5">
            {bucket.bullets.map((bullet, i) => (
              <CatchupBullet key={i} bullet={bullet} events={events} />
            ))}
          </ul>
          {bucket.truncated > 0 && (
            <p className="pl-1 text-[11px] text-text/30">
              {bucket.truncated} older events not summarized
            </p>
          )}
        </div>
      ))}

      {summary.incidents.length > 0 && (
        <div>
          <h5 className="text-xs font-semibold uppercase tracking-wide text-text/60">
            Incidents opened in this window
          </h5>
          <div className="mt-1 flex flex-col gap-1">
            {summary.incidents.map((inc) => (
              <div
                key={inc.incident_id}
                className="flex items-center gap-2 rounded border border-muted px-2 py-1 text-xs"
              >
                <span className="w-24 shrink-0 truncate rounded bg-muted px-1 text-center text-[11px] uppercase text-text/60">
                  {inc.category.replace(/_/g, " ")}
                </span>
                <span className="min-w-0 flex-1 text-text/70">{inc.summary}</span>
                <span
                  className={`shrink-0 rounded px-1.5 text-[11px] font-semibold ${INCIDENT_STATUS_STYLE[inc.status] ?? INCIDENT_STATUS_STYLE.open}`}
                >
                  {inc.status === "resolved" ? "resolved" : "still open"}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      <p className="text-[11px] text-text/30">model: {summary.model}</p>
    </div>
  );
}

/** Per-restaurant catch-up history. Fetched on open rather than polled — past
 *  captures do not change while you read them, and summarizing is an explicit
 *  (slow) act. */
function CatchupDrawer({
  instanceId,
  title,
  onClose,
}: {
  instanceId: string;
  title: string;
  onClose: () => void;
}) {
  const [markers, setMarkers] = useState<CatchupMarker[] | null>(null);
  const [record, setRecord] = useState<CatchupRecord | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [summarizing, setSummarizing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<CatchupMarker[]>(`/admin/api/instances/${instanceId}/catchups`)
      .then(setMarkers)
      .catch(() => setMarkers([]));
  }, [instanceId]);

  async function select(n: number) {
    setSelected(n);
    setRecord(null);
    setError(null);
    try {
      setRecord(
        await apiGet<CatchupRecord>(`/admin/api/instances/${instanceId}/catchups/${n}`),
      );
    } catch {
      setError("Could not load that catch-up.");
    }
  }

  async function summarize() {
    if (record == null) return;
    setSummarizing(true);
    setError(null);
    try {
      const summary = await apiPost<CatchupSummary>(
        `/admin/api/instances/${instanceId}/catchups/${record.n}/summarize`,
      );
      setRecord({ ...record, summary });
      setMarkers(
        (rows) => rows?.map((m) => (m.n === record.n ? { ...m, summary } : m)) ?? rows,
      );
    } catch {
      setError("Summarizing failed — the manager could not reach the model.");
    } finally {
      setSummarizing(false);
    }
  }

  const newestFirst = markers ? [...markers].reverse() : [];

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/40" onClick={onClose} aria-hidden />
      <aside className="fixed right-0 top-0 z-50 flex h-full w-[34rem] max-w-full flex-col bg-primary text-text shadow-2xl">
        <header className="flex items-center gap-2 border-b border-muted px-4 py-3">
          <History size={16} className="text-text/50" />
          <h3 className="flex-1 text-sm font-semibold">Catch-ups — {title}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close catch-up history"
            className="rounded-md p-1 text-text/40 hover:bg-muted hover:text-text"
          >
            <X size={16} />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {markers == null ? (
            <p className="py-8 text-center text-sm text-text/40">Loading…</p>
          ) : markers.length === 0 ? (
            <p className="py-8 text-center text-sm text-text/40">
              No catch-ups captured yet — press “Catch up” on the card.
            </p>
          ) : (
            <div className="flex flex-col gap-1">
              {newestFirst.map((m) => (
                <button
                  key={m.n}
                  type="button"
                  onClick={() => select(m.n)}
                  className={
                    selected === m.n
                      ? "flex items-center gap-2 rounded-md border border-accent bg-surface px-2 py-1.5 text-left text-xs"
                      : "flex items-center gap-2 rounded-md border border-muted bg-surface px-2 py-1.5 text-left text-xs hover:border-text/30"
                  }
                >
                  <span className="w-8 shrink-0 font-semibold text-text/60">#{m.n}</span>
                  <span className="min-w-0 flex-1 text-text/70">
                    {fmtSim(m.since_sim)} → {fmtSim(m.until_sim)}
                  </span>
                  <span className="shrink-0 text-text/40">{m.event_count} events</span>
                  {m.summary && (
                    <span
                      className={
                        m.summary.error
                          ? "shrink-0 rounded bg-danger/15 px-1.5 text-[11px] font-semibold text-danger"
                          : "shrink-0 rounded bg-success/15 px-1.5 text-[11px] font-semibold text-success"
                      }
                    >
                      {m.summary.error ? "failed" : "summarized"}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}

          {selected != null && (
            <div className="mt-4 border-t border-muted pt-3">
              {record == null ? (
                <p className="text-sm text-text/40">{error ?? "Loading…"}</p>
              ) : (
                <>
                  <div className="mb-2 flex items-center gap-2">
                    <span className="text-xs text-text/50">
                      Catch-up #{record.n} · {record.event_count} events
                    </span>
                    <button
                      type="button"
                      disabled={summarizing}
                      onClick={summarize}
                      className="ml-auto rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50"
                    >
                      {summarizing
                        ? "Summarizing…"
                        : record.summary
                          ? "Re-summarize"
                          : "Summarize"}
                    </button>
                  </div>
                  {error && <p className="mb-2 text-xs text-danger">{error}</p>}
                  {record.summary ? (
                    <CatchupSummaryView summary={record.summary} events={record.events} />
                  ) : (
                    <p className="text-sm text-text/40">
                      Not summarized yet.
                    </p>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </aside>
    </>
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
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-medium text-text">{action.problem}</span>
          {action.impact_eur != null && (
            <span
              className="rounded bg-danger/15 px-1.5 text-[11px] font-semibold text-danger"
              title="Forecast revenue riding on the affected dishes today"
            >
              {fmtMoney(action.impact_eur)} at stake
            </span>
          )}
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
      <div className="flex shrink-0 items-center gap-1.5">
        {action.approval_id != null &&
          (action.approval_kind === "notice" ? (
            // Informational — nothing is gated on a decision; just acknowledge.
            <button
              type="button"
              disabled={busy}
              onClick={() => resolve("approve")}
              className="flex items-center gap-1 rounded-md bg-muted px-2.5 py-1 text-xs font-medium text-text disabled:opacity-50"
            >
              <Check size={13} /> Acknowledge
            </button>
          ) : (
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
          ))}
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

  const isNotice = approval.kind === "notice";
  return (
    <div className="rounded-lg border border-muted bg-surface p-3">
      <div className="flex items-center gap-2">
        <RestaurantLogo id={approval.instance_id} title={approval.restaurant} size={24} />
        <span className="text-xs font-semibold text-text/70">{approval.restaurant}</span>
        <span className="rounded bg-muted px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-text/70">
          {approval.type}
        </span>
        {isNotice && (
          <span className="rounded bg-muted/60 px-2 py-0.5 text-xs text-text/50">
            notice
          </span>
        )}
        {approval.urgency !== "normal" && (
          <span className="rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-400">
            {approval.urgency}
          </span>
        )}
      </div>
      <h4 className="mt-1.5 text-sm font-semibold text-text">{approval.title}</h4>
      <p className="text-sm text-text/70">{approval.summary}</p>
      <div className="mt-2 flex gap-2">
        {isNotice ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => resolve("approve")}
            className="flex flex-1 items-center justify-center gap-1 rounded-md bg-muted px-3 py-1.5 text-sm font-medium text-text disabled:opacity-50"
          >
            <Check size={15} /> Acknowledge
          </button>
        ) : (
          <>
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
          </>
        )}
      </div>
    </div>
  );
}

type Tab = "queue" | "approvals" | "incidents" | "briefing";

/** One live incident, with the two things a manager can do about it.
 *  Acknowledge = "seen, being handled"; Resolve = close it by hand, which is
 *  the only way out for a source signal that cannot be retracted. */
function IncidentRow({
  incident,
  onChanged,
}: {
  incident: Incident;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const acked = incident.status === "acked";

  async function act(decision: "ack" | "resolve") {
    if (incident.incident_id == null) return;
    setBusy(true);
    try {
      await apiPost(`/admin/api/incidents/${incident.incident_id}/${decision}`);
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className={`flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm ${
        acked ? "border-muted/60 bg-surface/50" : "border-muted bg-surface"
      }`}
    >
      <span className="w-28 shrink-0 rounded bg-muted px-2 py-0.5 text-center text-[11px] font-medium uppercase text-text/60">
        {incident.category.replace(/_/g, " ")}
      </span>
      <RestaurantLogo id={incident.instance_id} title={incident.restaurant} size={22} />
      <span className={`min-w-0 flex-1 ${acked ? "text-text/50" : "text-text"}`}>
        {incident.summary}
      </span>
      {acked && (
        <span className="shrink-0 rounded bg-muted px-1.5 text-[11px] font-medium text-text/60">
          acked{incident.acked_by ? ` · ${incident.acked_by}` : ""}
        </span>
      )}
      {incident.count > 1 && (
        <span className="shrink-0 rounded-full bg-muted px-1.5 text-[11px] font-semibold text-text/60">
          ×{incident.count}
        </span>
      )}
      <span className="shrink-0 text-xs text-text/40">{fmtSim(incident.created_at)}</span>
      {incident.incident_id != null && (
        <div className="flex shrink-0 items-center gap-1">
          {!acked && (
            <button
              type="button"
              disabled={busy}
              onClick={() => act("ack")}
              className="rounded-md bg-muted px-2 py-1 text-xs font-medium text-text disabled:opacity-50"
            >
              Acknowledge
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => act("resolve")}
            className="rounded-md bg-success px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
          >
            Resolve
          </button>
        </div>
      )}
    </div>
  );
}

/** Every incident ever opened, resolved ones included. Fetched on open rather
 *  than polled — history does not change while you are reading it. */
function IncidentHistory() {
  const [rows, setRows] = useState<IncidentHistoryRow[] | null>(null);

  useEffect(() => {
    apiGet<{ incidents: IncidentHistoryRow[] }>("/admin/api/incidents/history")
      .then((d) => setRows(d.incidents))
      .catch(() => setRows([]));
  }, []);

  if (rows == null) return <p className="py-8 text-center text-sm text-text/40">Loading…</p>;
  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-text/40">No incidents recorded yet.</p>;
  }
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((row) => (
        <div
          key={row.incident_id}
          className="flex items-center gap-2 rounded-md border border-muted bg-surface px-3 py-1.5 text-sm"
        >
          <span className="w-28 shrink-0 rounded bg-muted px-2 py-0.5 text-center text-[11px] font-medium uppercase text-text/60">
            {row.category.replace(/_/g, " ")}
          </span>
          <span className="w-28 shrink-0 truncate text-xs text-text/40">{row.instance_id}</span>
          <span className="min-w-0 flex-1 text-text/70">{row.summary}</span>
          <span
            className={`shrink-0 rounded px-1.5 text-[11px] font-semibold ${INCIDENT_STATUS_STYLE[row.status] ?? INCIDENT_STATUS_STYLE.open}`}
          >
            {row.status}
          </span>
          <span className="shrink-0 text-xs text-text/40">{fmtSim(row.opened_at)}</span>
        </div>
      ))}
    </div>
  );
}

export default function AdminPage() {
  const data = useAdminData();
  const [tab, setTab] = useState<Tab>("queue");
  const [showHistory, setShowHistory] = useState(false);
  const [restaurantFilter, setRestaurantFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [kindFilter, setKindFilter] = useState("all");

  const critical = data.instances.filter((c) => c.status === "critical").length;
  const warning = data.instances.filter((c) => c.status === "warning").length;
  const offline = data.instances.filter((c) => c.status === "offline").length;

  const approvalTypes = [...new Set(data.approvals.map((a) => a.type))];
  const filteredApprovals = data.approvals.filter(
    (a) =>
      (restaurantFilter === "all" || a.instance_id === restaurantFilter) &&
      (typeFilter === "all" || a.type === typeFilter) &&
      (kindFilter === "all" || (a.kind ?? "decision") === kindFilter),
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
              <select
                value={kindFilter}
                onChange={(e) => setKindFilter(e.target.value)}
                className={selectClass}
                aria-label="Filter decisions vs notices"
              >
                <option value="all">Decisions + notices</option>
                <option value="decision">Decisions only</option>
                <option value="notice">Notices only</option>
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
            <div className="mb-2 flex justify-end">
              <button
                type="button"
                onClick={() => setShowHistory((v) => !v)}
                className={
                  showHistory
                    ? "flex items-center gap-1 rounded-md bg-muted px-2.5 py-1 text-xs font-medium text-text"
                    : "flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium text-text/60 hover:bg-muted"
                }
              >
                <History size={13} /> {showHistory ? "Showing history" : "History"}
              </button>
            </div>
            {showHistory ? (
              <IncidentHistory />
            ) : data.incidents.length === 0 ? (
              <p className="py-8 text-center text-sm text-text/40">
                No open incidents.
              </p>
            ) : (
              <div className="flex flex-col gap-1.5">
                {data.incidents.map((incident, i) => (
                  <IncidentRow
                    key={incident.incident_id ?? i}
                    incident={incident}
                    onChanged={data.refresh}
                  />
                ))}
              </div>
            )}
            {data.unavailableCategories.length > 0 && (
              <p className="mt-3 text-xs text-text/30">
                Not yet detected:{" "}
                {data.unavailableCategories.join(", ").replace(/_/g, " ")} (see
                docs/fable/incidents.md)
              </p>
            )}
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
