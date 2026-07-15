/**
 * CookPanels — the Tasks and Staff panels for the cook desk.
 *
 * Both mirror the Batches panel's look: a scrollable list of cards with a big
 * bold title, a big time, a confirm/check control, and a details disclosure.
 * Each panel is self-contained (owns its polling), so CookVoice only has to
 * decide which to render for the active mode.
 */
import { useState, useEffect, useCallback } from "react";
import {
  Loader2, ClipboardList, ChevronDown, ChevronUp, CheckSquare,
  Users, UserCheck, Clock, AlertTriangle, ThermometerSnowflake,
  Sparkles, DoorOpen, DoorClosed, ShieldCheck, UtensilsCrossed,
  Ban, Pencil, Mic, X, Send,
} from "lucide-react";
import { apiGet, apiPost } from "../api";

export type TaskOutcome = "done" | "not_done" | "pending";

// ---------------------------------------------------------------------------
// Shared helpers (kept local to avoid a CookVoice import cycle)
// ---------------------------------------------------------------------------

function fmtSimTime(secs: number): string {
  return `${String(Math.floor(secs / 3600) % 24).padStart(2, "0")}:${String(Math.floor((secs % 3600) / 60)).padStart(2, "0")}`;
}

function relativeTime(target: number, nowSim: number): string {
  const mins = Math.round((target - nowSim) / 60);
  if (Math.abs(mins) <= 1) return "now";
  if (mins > 0) return mins >= 60 ? `in ${Math.round(mins / 60)} h` : `in ${mins} min`;
  const late = Math.abs(mins);
  return late >= 60 ? `overdue ${Math.round(late / 60)} h` : `overdue ${late} min`;
}

// ===========================================================================
// Tasks panel
// ===========================================================================

export type Severity = "low" | "medium" | "high";

export interface KitchenTask {
  id: number;
  template_key: string;
  title: string;
  category: string;
  station: string | null;
  due_sim_time: number | null;
  details: string[];
  status: "pending" | "done" | "not_done" | "skipped";
  note: string | null;
  severity: Severity | null;
  overdue: boolean;
  done_at: number | null;
  done_by: string | null;
}

export interface ReportResult {
  status: "recorded" | "needs_input";
  question?: string;
  outcome?: "done" | "not_done";
  severity?: Severity;
  note?: string;
}

export interface TaskBoard {
  generated_at_sim: number;
  sim_day: number;
  counts: { done: number; pending: number; overdue: number; not_done: number; skipped: number };
  tasks: KitchenTask[];
}

// ---------------------------------------------------------------------------
// useTasksBoard — shared task fetch + outcome mutation (TasksPanel + All feed)
// ---------------------------------------------------------------------------

export function useTasksBoard() {
  const [board, setBoard] = useState<TaskBoard | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() =>
    apiGet<TaskBoard>("/api/kitchen/tasks/board")
      .then(b => { if (b) setBoard(b); })
      .catch(() => undefined)
      .finally(() => setLoading(false)),
  []);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 5000);
    return () => clearInterval(id);
  }, [load]);

  const setOutcome = useCallback(async (task: KitchenTask, status: TaskOutcome, note?: string) => {
    // optimistic
    setBoard(prev => prev && ({
      ...prev,
      tasks: prev.tasks.map(t => t.id === task.id
        ? { ...t, status, note: note ?? (status === "pending" ? null : t.note), overdue: status === "pending" ? t.overdue : false }
        : t),
    }));
    try {
      await apiPost(`/api/kitchen/tasks/${task.id}/outcome`, { status, note });
    } catch { void load(); }
  }, [load]);

  // Written report → Roba (LLM) interprets it. May return needs_input (caller
  // shows an overlay) or record the outcome; on record we refresh the board.
  const reportIssue = useCallback(async (
    task: KitchenTask, text: string, priorQa?: { q: string; a: string }[],
  ): Promise<ReportResult> => {
    const res = await apiPost<ReportResult>(`/api/kitchen/tasks/${task.id}/report`, { text, prior_qa: priorQa });
    if (res.status === "recorded") void load();
    return res;
  }, [load]);

  return { board, loading, setOutcome, reportIssue, reload: load };
}

const CATEGORY_META: Record<string, { label: string; icon: React.ElementType; cls: string }> = {
  opening:  { label: "Opening",   icon: DoorOpen,             cls: "bg-accent/20 text-accent" },
  temp:     { label: "Temp check", icon: ThermometerSnowflake, cls: "bg-sky-500/20 text-sky-400" },
  cleaning: { label: "Cleaning",  icon: Sparkles,             cls: "bg-violet-500/20 text-violet-400" },
  closing:  { label: "Closing",   icon: DoorClosed,           cls: "bg-amber-500/20 text-amber-400" },
  prep:     { label: "Prep",      icon: UtensilsCrossed,      cls: "bg-emerald-500/20 text-emerald-400" },
  safety:   { label: "Safety",    icon: ShieldCheck,          cls: "bg-rose-500/20 text-rose-400" },
};

const SEVERITY_META: Record<Severity, { label: string; cls: string }> = {
  low:    { label: "Low",      cls: "bg-muted/60 text-text/60" },
  medium: { label: "Medium",   cls: "bg-warning/20 text-warning" },
  high:   { label: "Critical", cls: "bg-danger/20 text-danger" },
};

function TaskReportOverlay({ taskTitle, question, busy, onAnswer, onExplainToRoba, onClose }: {
  taskTitle: string;
  question: string;
  busy: boolean;
  onAnswer: (answer: string) => void;
  onExplainToRoba?: () => void;
  onClose: () => void;
}) {
  const [answer, setAnswer] = useState("");
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-2xl border border-muted/60 bg-surface p-5 shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <Mic size={16} className="text-accent" />
            <span className="text-sm font-semibold text-text">Roba needs a detail</span>
          </div>
          <button onClick={onClose} className="text-text/30 hover:text-text/60" aria-label="Close"><X size={16} /></button>
        </div>
        <p className="mt-1 text-xs text-text/40">{taskTitle}</p>
        <p className="mt-3 text-base font-medium text-text">{question}</p>
        <textarea
          value={answer}
          onChange={e => setAnswer(e.target.value)}
          autoFocus
          rows={3}
          placeholder="Type your answer…"
          className="mt-3 w-full resize-none rounded-lg border border-muted bg-primary px-3 py-2 text-sm text-text placeholder:text-text/30 focus:border-accent focus:outline-none"
        />
        <div className="mt-3 flex items-center gap-2">
          <button
            onClick={() => answer.trim() && onAnswer(answer.trim())}
            disabled={!answer.trim() || busy}
            className="flex-1 flex items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2.5 text-sm font-semibold text-white hover:bg-accent/90 disabled:opacity-40"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
            {busy ? "Recording…" : "Continue"}
          </button>
          {onExplainToRoba && (
            <button
              onClick={onExplainToRoba}
              className="flex items-center justify-center gap-1.5 rounded-lg border border-accent/50 bg-accent/10 px-3 py-2.5 text-sm font-medium text-accent hover:bg-accent/20"
            >
              <Mic size={14} /> Explain to Roba
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function TaskCard({ task, nowSim, onOutcome, onReport, onAssignVoice }: {
  task: KitchenTask;
  nowSim: number;
  onOutcome: (t: KitchenTask, status: TaskOutcome, note?: string) => void;
  onReport?: (t: KitchenTask, text: string, priorQa?: { q: string; a: string }[]) => Promise<ReportResult>;
  onAssignVoice?: (t: KitchenTask) => void;
}) {
  const [detailOpen, setDetailOpen] = useState(false);
  const [notDoneOpen, setNotDoneOpen] = useState(false);
  const [typing, setTyping] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  // Clarification overlay when Roba needs one more detail.
  const [clarify, setClarify] = useState<{ question: string; originalText: string } | null>(null);
  const cat = CATEGORY_META[task.category] ?? { label: task.category, icon: ClipboardList, cls: "bg-muted/40 text-text/50" };
  const CatIcon = cat.icon;
  const isDone = task.status === "done";
  const isNotDone = task.status === "not_done";
  const resolved = isDone || isNotDone;
  const sev = task.severity ? SEVERITY_META[task.severity] : null;
  const rel = task.due_sim_time != null ? relativeTime(task.due_sim_time, nowSim) : null;

  async function submitReason() {
    const text = reason.trim();
    if (!text || busy) return;
    // Fall back to a direct not-done write if no LLM report handler is wired.
    if (!onReport) { onOutcome(task, "not_done", text); setTyping(false); setNotDoneOpen(false); setReason(""); return; }
    setBusy(true);
    try {
      const res = await onReport(task, text);
      if (res.status === "needs_input" && res.question) {
        setClarify({ question: res.question, originalText: text });
      } else {
        setTyping(false); setNotDoneOpen(false); setReason("");
      }
    } catch { /* leave the box open to retry */ }
    finally { setBusy(false); }
  }

  async function submitClarification(answer: string) {
    if (!onReport || !clarify || busy) return;
    setBusy(true);
    try {
      await onReport(task, clarify.originalText, [{ q: clarify.question, a: answer }]);
      setClarify(null); setTyping(false); setNotDoneOpen(false); setReason("");
    } catch { /* keep overlay open */ }
    finally { setBusy(false); }
  }

  return (
    <div
      className={[
        "rounded-xl border shadow-sm transition-opacity",
        isDone
          ? "border-success/30 bg-success/5 opacity-70"
          : isNotDone
            ? "border-warning/40 bg-warning/5"
            : task.overdue
              ? "border-danger/50 bg-danger/5"
              : "border-muted/50 bg-surface",
      ].join(" ")}
    >
      {/* Title + category */}
      <div className="flex items-start justify-between gap-3 px-4 pt-4 pb-2">
        <h3 className={["text-xl font-bold leading-tight", isDone ? "line-through text-text/40" : "text-text"].join(" ")}>
          {task.title}
        </h3>
        <div className="flex flex-col items-end gap-1 shrink-0">
          <span className={`flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold ${cat.cls}`}>
            <CatIcon size={12} /> {cat.label}
          </span>
          {task.station && <span className="text-xs text-text/40">{task.station}</span>}
        </div>
      </div>

      {/* Big time row */}
      {task.due_sim_time != null && (
        <div className="flex items-baseline gap-3 px-4 pb-1">
          <span className="text-3xl font-bold tabular-nums text-text">{fmtSimTime(task.due_sim_time)}</span>
          {!resolved && (
            <span className={["text-base font-semibold", task.overdue ? "text-danger" : "text-text/50"].join(" ")}>{rel}</span>
          )}
          {isDone && task.done_at != null && (
            <span className="text-base font-semibold text-success">done {fmtSimTime(task.done_at)}</span>
          )}
          {isNotDone && (
            <span className="text-base font-semibold text-warning">not done</span>
          )}
          {isNotDone && sev && (
            <span className={`ml-auto rounded-full px-2 py-0.5 text-xs font-semibold ${sev.cls}`}>{sev.label}</span>
          )}
          {task.overdue && !resolved && (
            <span className="ml-auto flex items-center gap-1 rounded-full bg-danger/20 px-2 py-0.5 text-xs font-medium text-danger">
              <AlertTriangle size={12} /> sent to manager
            </span>
          )}
        </div>
      )}

      {/* Not-done note (reported reason) */}
      {isNotDone && task.note && (
        <div className="mx-4 mb-1 mt-1 flex items-start gap-2 rounded-lg bg-warning/10 px-3 py-2 text-sm text-text/70">
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warning" />
          <span className="flex-1">{task.note}</span>
          <span className="shrink-0 rounded-full bg-warning/20 px-2 py-0.5 text-xs font-medium text-warning">reported to manager</span>
        </div>
      )}

      {/* Controls */}
      <div className="px-4 pb-4 pt-2 space-y-2">
        {resolved ? (
          <button
            onClick={() => onOutcome(task, "pending")}
            className="w-full flex items-center justify-center gap-3 rounded-xl border border-muted/60 bg-surface px-4 py-3 text-base font-bold text-text/50 hover:bg-muted/20 transition-colors"
          >
            <CheckSquare size={20} /> Undo
          </button>
        ) : (
          <>
            <button
              onClick={() => onOutcome(task, "done")}
              className="w-full flex items-center justify-center gap-3 rounded-xl bg-success/85 hover:bg-success px-4 py-4 text-lg font-bold text-white transition-colors shadow-sm"
            >
              <CheckSquare size={24} /> Confirm done
            </button>

            {/* Report an issue → type a note (Roba interprets it) or hand to voice */}
            {!notDoneOpen ? (
              <button
                onClick={() => setNotDoneOpen(true)}
                className="w-full flex items-center justify-center gap-2 rounded-xl border border-warning/40 bg-warning/5 px-4 py-2.5 text-sm font-semibold text-warning hover:bg-warning/10 transition-colors"
              >
                <Ban size={16} /> Report an issue
              </button>
            ) : (
              <div className="rounded-xl border border-warning/30 bg-warning/5 p-2.5 space-y-2">
                {!typing ? (
                  <div className="flex flex-wrap gap-2">
                    <button
                      onClick={() => setTyping(true)}
                      className="flex-1 flex items-center justify-center gap-1.5 rounded-lg border border-muted/60 bg-surface px-3 py-2 text-sm font-medium text-text/70 hover:bg-muted/20"
                    >
                      <Pencil size={14} /> Type it
                    </button>
                    {onAssignVoice && (
                      <button
                        onClick={() => { onAssignVoice(task); setNotDoneOpen(false); }}
                        className="flex-1 flex items-center justify-center gap-1.5 rounded-lg border border-accent/50 bg-accent/10 px-3 py-2 text-sm font-medium text-accent hover:bg-accent/20"
                      >
                        <Mic size={14} /> Tell Roba
                      </button>
                    )}
                    <button
                      onClick={() => setNotDoneOpen(false)}
                      className="flex items-center justify-center rounded-lg border border-muted/60 bg-surface px-2 py-2 text-text/40 hover:text-text/70"
                      aria-label="Cancel"
                    >
                      <X size={14} />
                    </button>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <textarea
                      value={reason}
                      onChange={e => setReason(e.target.value)}
                      autoFocus
                      rows={2}
                      placeholder="What happened? Roba will read this and log it."
                      className="w-full resize-none rounded-lg border border-muted bg-surface px-3 py-2 text-sm text-text placeholder:text-text/30 focus:border-accent focus:outline-none"
                    />
                    <div className="flex gap-2">
                      <button
                        onClick={submitReason}
                        disabled={!reason.trim() || busy}
                        className="flex-1 flex items-center justify-center gap-1.5 rounded-lg bg-warning/85 px-3 py-2 text-sm font-semibold text-white hover:bg-warning disabled:opacity-40"
                      >
                        {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
                        {busy ? "Roba is reading…" : "Send to Roba"}
                      </button>
                      <button
                        onClick={() => { setTyping(false); setReason(""); }}
                        className="rounded-lg border border-muted/60 bg-surface px-3 py-2 text-sm text-text/50 hover:bg-muted/20"
                      >
                        Back
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* Clarification overlay — Roba needs one more detail */}
      {clarify && (
        <TaskReportOverlay
          taskTitle={task.title}
          question={clarify.question}
          busy={busy}
          onAnswer={submitClarification}
          onExplainToRoba={onAssignVoice ? () => { setClarify(null); setNotDoneOpen(false); onAssignVoice(task); } : undefined}
          onClose={() => setClarify(null)}
        />
      )}

      {/* Details */}
      {task.details.length > 0 && (
        <div className="px-4 pb-3">
          <button
            onClick={() => setDetailOpen(v => !v)}
            className="flex items-center gap-1.5 text-sm text-text/40 hover:text-text/60 transition-colors"
          >
            <ClipboardList size={14} /> Details
            {detailOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          </button>
          {detailOpen && (
            <ul className="mt-2 border-t border-muted/30 pt-3 space-y-1">
              {task.details.map((step, i) => (
                <li key={i} className="flex items-baseline gap-2 text-sm">
                  <span className="text-text/30 w-4 shrink-0 text-right">{i + 1}.</span>
                  <span className="text-text/70">{step}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export function TasksPanel({ onAssignVoice }: { onAssignVoice?: (t: KitchenTask) => void }) {
  const { board, loading, setOutcome, reportIssue } = useTasksBoard();
  const nowSim = board?.generated_at_sim ?? 0;
  const c = board?.counts;

  return (
    <section className="flex flex-1 flex-col min-h-0 rounded-xl border border-muted/40 bg-surface/60 overflow-hidden">
      <div className="shrink-0 flex items-center justify-between px-4 py-3 border-b border-muted/30">
        <div className="flex items-center gap-2">
          <ClipboardList size={14} className="text-accent" />
          <span className="text-sm font-semibold text-text">Tasks</span>
        </div>
        {c && (
          <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs justify-end">
            {c.done > 0 && <span className="text-success font-medium">{c.done} done</span>}
            {c.pending > 0 && <span className="text-text/50 font-medium">{c.pending} to do</span>}
            {c.overdue > 0 && <span className="text-danger font-medium">{c.overdue} overdue</span>}
            {c.not_done > 0 && <span className="text-warning font-medium">{c.not_done} not done</span>}
          </div>
        )}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3">
        {loading ? (
          <div className="flex items-center justify-center py-10 text-text/40">
            <Loader2 size={20} className="animate-spin mr-2" /> Loading tasks…
          </div>
        ) : !board || board.tasks.length === 0 ? (
          <div className="py-10 text-center text-sm text-text/30">No tasks scheduled today.</div>
        ) : (
          board.tasks.map(t => <TaskCard key={t.id} task={t} nowSim={nowSim} onOutcome={setOutcome} onReport={reportIssue} onAssignVoice={onAssignVoice} />)
        )}
      </div>
    </section>
  );
}

// ===========================================================================
// Staff panel
// ===========================================================================

interface StaffRow {
  staff_id: number;
  name: string;
  role: string;
  status: "present" | "late" | "absent" | "upcoming";
  shift_start: number;
  checked_in_at: number | null;
  source: "auto" | "manual";
}

interface StaffBoard {
  generated_at_sim: number;
  sim_day: number;
  mode: "sim_auto" | "manual";
  counts: { present: number; late: number; absent: number; upcoming: number };
  staff: StaffRow[];
}

const STAFF_PILL: Record<string, { label: string; cls: string }> = {
  present:  { label: "Present",  cls: "bg-success/20 text-success" },
  late:     { label: "Late",     cls: "bg-warning/20 text-warning" },
  absent:   { label: "Absent",   cls: "bg-danger/20 text-danger" },
  upcoming: { label: "Upcoming", cls: "bg-muted/50 text-text/50" },
};

function StaffCard({ row, mode, onCheckin }: {
  row: StaffRow; mode: "sim_auto" | "manual"; onCheckin: (row: StaffRow, checkIn: boolean) => void;
}) {
  const pill = STAFF_PILL[row.status] ?? { label: row.status, cls: "bg-muted/40 text-text/50" };
  const checkedIn = row.status === "present" || row.status === "late";
  const canAct = mode === "manual";

  return (
    <div className={[
      "rounded-xl border shadow-sm px-4 py-3",
      row.status === "absent" ? "border-danger/40 bg-danger/5" : "border-muted/50 bg-surface",
    ].join(" ")}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-xl font-bold leading-tight text-text truncate">{row.name}</h3>
          <p className="text-sm text-text/50 capitalize">{row.role?.replace(/_/g, " ")}</p>
        </div>
        <span className={`shrink-0 rounded-full px-3 py-1 text-sm font-semibold ${pill.cls}`}>{pill.label}</span>
      </div>

      <div className="mt-2 flex items-center justify-between gap-3">
        <span className="flex items-center gap-1.5 text-sm text-text/40">
          <Clock size={13} /> shift {fmtSimTime(row.shift_start)}
          {checkedIn && row.checked_in_at != null && (
            <span className="text-text/60">· in {fmtSimTime(row.checked_in_at)}</span>
          )}
        </span>
        {canAct && (
          <button
            onClick={() => onCheckin(row, !checkedIn)}
            className={[
              "flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-semibold transition-colors",
              checkedIn
                ? "border border-muted/60 bg-surface text-text/50 hover:bg-muted/20"
                : "bg-success/85 hover:bg-success text-white",
            ].join(" ")}
          >
            <UserCheck size={15} /> {checkedIn ? "Undo" : "Check in"}
          </button>
        )}
      </div>
    </div>
  );
}

export function StaffPanel() {
  const [board, setBoard] = useState<StaffBoard | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() =>
    apiGet<StaffBoard>("/api/kitchen/staff/board")
      .then(b => { if (b) setBoard(b); })
      .catch(() => undefined)
      .finally(() => setLoading(false)),
  []);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 5000);
    return () => clearInterval(id);
  }, [load]);

  async function setMode(mode: "sim_auto" | "manual") {
    setBoard(prev => prev && ({ ...prev, mode }));
    try { await apiPost("/api/kitchen/staff/mode", { mode }); } finally { void load(); }
  }

  async function handleCheckin(row: StaffRow, checkIn: boolean) {
    setBoard(prev => prev && ({
      ...prev,
      staff: prev.staff.map(s => s.staff_id === row.staff_id
        ? { ...s, status: checkIn ? "present" : "absent", checked_in_at: checkIn ? prev.generated_at_sim : null }
        : s),
    }));
    try {
      await apiPost(`/api/kitchen/staff/${row.staff_id}/checkin${checkIn ? "" : "?undo=true"}`, {});
    } finally { void load(); }
  }

  const c = board?.counts;
  const mode = board?.mode ?? "sim_auto";

  return (
    <section className="flex flex-1 flex-col min-h-0 rounded-xl border border-muted/40 bg-surface/60 overflow-hidden">
      <div className="shrink-0 flex flex-col gap-2 px-4 py-3 border-b border-muted/30">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Users size={14} className="text-accent" />
            <span className="text-sm font-semibold text-text">Staff</span>
          </div>
          {c && (
            <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs justify-end">
              {c.present > 0 && <span className="text-success font-medium">{c.present} present</span>}
              {c.late > 0 && <span className="text-warning font-medium">{c.late} late</span>}
              {c.absent > 0 && <span className="text-danger font-medium">{c.absent} absent</span>}
              {c.upcoming > 0 && <span className="text-text/40">{c.upcoming} upcoming</span>}
            </div>
          )}
        </div>
        {/* Mode toggle */}
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Check-in</span>
          <div className="inline-flex rounded-lg border border-muted/60 p-0.5">
            {(["sim_auto", "manual"] as const).map(m => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={[
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  mode === m ? "bg-accent text-white" : "text-text/50 hover:text-text",
                ].join(" ")}
              >
                {m === "sim_auto" ? "Sim-auto" : "Manual"}
              </button>
            ))}
          </div>
          <span className="text-xs text-text/30">
            {mode === "sim_auto" ? "all auto-present" : "check in each shift"}
          </span>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3">
        {loading ? (
          <div className="flex items-center justify-center py-10 text-text/40">
            <Loader2 size={20} className="animate-spin mr-2" /> Loading staff…
          </div>
        ) : !board || board.staff.length === 0 ? (
          <div className="py-10 text-center text-sm text-text/30">No staff on the roster.</div>
        ) : (
          board.staff.map(s => <StaffCard key={s.staff_id} row={s} mode={mode} onCheckin={handleCheckin} />)
        )}
      </div>
    </section>
  );
}
