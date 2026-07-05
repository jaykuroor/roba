/**
 * ManagerVoice — Manager Roba Desk.
 *
 * Two-column layout (lg+):
 *   Left  — cards board: active call, approvals, change cards, placeholders
 *   Right — voice desk: mic, plan/forecast cards, transcript, type fallback
 *
 * Single column on small screens (stacked).
 */

import { useState, useEffect, useRef } from "react";
import {
  CheckCircle2,
  XCircle,
  Bell,
  Send,
  RefreshCw,
  Check,
  X,
  PhoneCall,
  PhoneOff,
  ExternalLink,
  ArrowLeftRight,
  RotateCcw,
  ChevronDown,
  ChevronUp,
  Radio,
} from "lucide-react";
import { useVoiceLive } from "./useVoiceLive";
import { MicButton } from "./MicButton";
import { PlanConfirmCard } from "./PlanConfirmCard";
import { ForecastCard } from "./ForecastCard";
import { ModeToggle } from "./ModeToggle";
import { MicModeToggle } from "./MicModeToggle";
import { ModelToggle } from "./ModelToggle";
import { apiGet, apiPost } from "../api";
import { useActiveCall, useCallTurns, useLastCompletedCall, useManagerChangeVersion, actions } from "../store";
import type { ApprovalRequest, Call, ManagerChange } from "../types";
import { SpectateOverlay } from "../shell/SpectateOverlay";
import { wsClient } from "../ws";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtSimClock(secs: number): string {
  const h = Math.floor(secs / 3600) % 24;
  const m = Math.floor((secs % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Approval item
// ---------------------------------------------------------------------------

interface BatchProposalPayload {
  proposal_type?: string;
  dish_name?: string;
  target_window_start?: number;
  target_qty?: number;
  forecast_demand?: number;
  projected_benefit?: string;
  reasoning?: string;
}

function ApprovalItem({
  approval,
  onResolve,
}: {
  approval: ApprovalRequest;
  onResolve: (id: number, decision: "approve" | "reject") => void;
}) {
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const isBatch = approval.type === "batch";
  const payload = isBatch ? (approval.payload as BatchProposalPayload | null) : null;

  return (
    <div className="rounded-lg border border-muted/60 bg-surface p-3 space-y-2">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <span className="inline-block rounded bg-muted px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide text-text/50">
            {approval.type.replace(/_/g, " ")}
          </span>
          <p className="mt-0.5 text-sm font-medium text-text">{approval.title}</p>
          {approval.summary && (
            <p className="text-xs text-text/60 mt-0.5 line-clamp-2">{approval.summary}</p>
          )}
        </div>
        <div className="flex shrink-0 flex-col gap-1.5 items-end">
          <div className="flex gap-1.5">
            <button
              disabled={busy}
              onClick={() => { setBusy(true); onResolve(approval.id, "approve"); }}
              className="rounded-md bg-success/20 px-2 py-1 text-xs font-medium text-success hover:bg-success/30 disabled:opacity-40"
            >
              <CheckCircle2 size={12} className="inline mr-0.5" />
              Approve
            </button>
            <button
              disabled={busy}
              onClick={() => { setBusy(true); onResolve(approval.id, "reject"); }}
              className="rounded-md bg-danger/20 px-2 py-1 text-xs font-medium text-danger hover:bg-danger/30 disabled:opacity-40"
            >
              <XCircle size={12} className="inline mr-0.5" />
              Reject
            </button>
          </div>
          {isBatch && payload && (
            <button
              onClick={() => setExpanded(v => !v)}
              className="text-xs text-text/40 hover:text-text/70"
            >
              {expanded ? "Hide detail ▲" : "See reasoning ▼"}
            </button>
          )}
        </div>
      </div>

      {isBatch && payload && expanded && (
        <div className="border-t border-muted/30 pt-2 space-y-1.5 text-xs">
          {payload.dish_name && (
            <div className="flex gap-2">
              <span className="text-text/40 w-28 shrink-0">Dish</span>
              <span className="text-text font-medium">{payload.dish_name}</span>
            </div>
          )}
          {payload.target_window_start != null && (
            <div className="flex gap-2">
              <span className="text-text/40 w-28 shrink-0">Target window</span>
              <span className="text-text">{fmtSimClock(payload.target_window_start)}</span>
            </div>
          )}
          {payload.target_qty != null && (
            <div className="flex gap-2">
              <span className="text-text/40 w-28 shrink-0">Suggested qty</span>
              <span className="text-text">{payload.target_qty} portions</span>
            </div>
          )}
          {payload.forecast_demand != null && (
            <div className="flex gap-2">
              <span className="text-text/40 w-28 shrink-0">Forecast demand</span>
              <span className="text-text">{payload.forecast_demand.toFixed(1)} portions</span>
            </div>
          )}
          {payload.projected_benefit && (
            <div className="flex gap-2">
              <span className="text-text/40 w-28 shrink-0">Benefit</span>
              <span className="text-accent">{payload.projected_benefit}</span>
            </div>
          )}
          {payload.reasoning && (
            <div className="mt-1 rounded bg-muted/30 p-2 text-text/70 leading-relaxed">
              {payload.reasoning}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Active call card
// ---------------------------------------------------------------------------

function ActiveCallCard() {
  const call = useActiveCall();
  const turns = useCallTurns();
  const [spectating, setSpectating] = useState(false);

  if (!call) return null;

  const callRole =
    call.counterparty_type === "competitor" ? "competitor_call" : "supplier_call";

  function openCallTab() {
    window.open(`/call?call_id=${call!.id}&role=${callRole}`, "_blank");
  }

  return (
    <>
      <div className="rounded-xl border border-accent/30 bg-accent/5 p-3 space-y-2">
        <div className="flex items-center gap-2">
          <Radio size={14} className="text-accent animate-pulse shrink-0" />
          <span className="text-xs font-semibold uppercase tracking-wide text-accent">
            Live Call
          </span>
          <span className="ml-auto text-xs text-text/40">#{call.id}</span>
        </div>

        <div>
          <p className="text-sm font-medium text-text capitalize">
            {call.counterparty_type} #{call.counterparty_id}
          </p>
          {call.purpose && (
            <p className="text-xs text-text/50 mt-0.5">{call.purpose}</p>
          )}
        </div>

        {/* Recent turns preview */}
        {turns.length > 0 && (
          <div className="rounded-lg border border-muted/40 bg-surface/50 p-2 space-y-1 max-h-28 overflow-y-auto">
            {turns.slice(-4).map((t, i) => (
              <p key={i} className={`text-xs ${t.role === "agent" ? "text-accent/80" : "text-text/70"}`}>
                <span className="font-medium mr-1">
                  {t.role === "agent" ? "Roba:" : "Counterparty:"}
                </span>
                {t.text}
              </p>
            ))}
          </div>
        )}

        <div className="flex gap-2">
          <button
            onClick={() => setSpectating(true)}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-accent/20 py-1.5 text-xs font-medium text-accent hover:bg-accent/30"
          >
            <Radio size={12} />
            Spectate
          </button>
          <button
            onClick={openCallTab}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-accent/10 py-1.5 text-xs font-medium text-accent hover:bg-accent/20"
          >
            <ExternalLink size={12} />
            Open call tab
          </button>
        </div>
      </div>

      {spectating && (
        <SpectateOverlay callId={call.id} onClose={() => setSpectating(false)} />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Completed call summary card
// ---------------------------------------------------------------------------

function CompletedCallCard({
  call,
  changes,
}: {
  call: Call;
  changes: ManagerChange[];
}) {
  const [expanded, setExpanded] = useState(false);
  const callChanges = changes.filter(
    (c) =>
      c.details &&
      typeof c.details === "object" &&
      (c.details as Record<string, unknown>).call_id === call.id,
  );

  // Extract human-readable summary from outcome
  const outcome = call.outcome as Record<string, unknown> | null | undefined;
  const summaryText = outcome?.summary as string | undefined;

  const transcript = call.transcript ?? [];
  const counterpartyTurns = transcript.filter((t) => t.role === "counterparty");

  return (
    <div className="rounded-xl border border-muted/40 bg-surface/80 p-3 space-y-2">
      <div className="flex items-center gap-2">
        <PhoneOff size={13} className="text-muted shrink-0" />
        <span className="text-xs font-semibold uppercase tracking-wide text-text/40">
          Call ended
        </span>
        <span className="ml-auto text-xs text-text/30">#{call.id}</span>
        <button
          onClick={() => actions.dismissCompletedCall()}
          className="ml-1 text-text/30 hover:text-text/60"
          title="Dismiss"
        >
          <X size={12} />
        </button>
      </div>

      <div>
        <p className="text-sm font-medium text-text capitalize">
          {call.counterparty_type} #{call.counterparty_id}
        </p>
        {call.purpose && (
          <p className="text-xs text-text/50 mt-0.5">{call.purpose}</p>
        )}
      </div>

      {summaryText && (
        <div className="rounded-lg border border-muted/30 bg-surface p-2 text-xs text-text/70 whitespace-pre-wrap">
          {summaryText}
        </div>
      )}

      {callChanges.length > 0 && (
        <div className="text-xs text-text/60">
          <span className="font-medium">{callChanges.length} change card{callChanges.length !== 1 ? "s" : ""} created</span>
          {" — review in Changes awaiting approval below."}
        </div>
      )}

      {counterpartyTurns.length === 0 && (
        <p className="text-xs text-warning/70">
          ⚠ Caller turns not recorded — transcript may be incomplete.
        </p>
      )}

      {transcript.length > 0 && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-1 text-xs text-text/40 hover:text-text/60"
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          {expanded ? "Hide" : "Show"} transcript ({transcript.length} turns)
        </button>
      )}

      {expanded && (
        <div className="rounded-lg border border-muted/30 bg-surface/60 p-2 space-y-1 max-h-48 overflow-y-auto">
          {transcript.map((t, i) => (
            <p
              key={i}
              className={`text-xs ${t.role === "agent" ? "text-accent/80" : "text-text/70"}`}
            >
              <span className="font-medium mr-1">
                {t.role === "agent" ? "Roba:" : "Caller:"}
              </span>
              {t.text}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Change card (sourcing defaults / call price outcomes)
// ---------------------------------------------------------------------------

function ChangeCard({
  change,
  onRefresh,
}: {
  change: ManagerChange;
  onRefresh: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const isPending = change.status === "pending";
  const isApplied = change.status === "applied";

  const kindLabel: Record<string, string> = {
    sourcing_default: "Sourcing",
    call_price: "Price negotiated",
    onboarding: "New supplier",
    supplier_data: "Supplier update",
    supplier_term: "Supplier term",
  };

  async function act(action: "apply" | "revert" | "dismiss") {
    setBusy(action);
    try {
      await apiPost(`/api/manager/changes/${change.id}/${action}`);
      onRefresh();
    } catch {
      // ignore
    } finally {
      setBusy(null);
    }
  }

  async function toggleAutoApply() {
    setBusy("toggle");
    try {
      // Read current setting then toggle
      const cur = await apiGet<{ auto_apply: boolean }>("/api/settings/auto-apply");
      await apiPost("/api/settings/auto-apply", { auto_apply: !cur.auto_apply });
      onRefresh();
    } catch {
      // ignore
    } finally {
      setBusy(null);
    }
  }

  const details = change.details ?? {};

  return (
    <div className={[
      "rounded-lg border p-3 space-y-2",
      isPending ? "border-warning/40 bg-warning/5" : "border-muted/40 bg-surface",
    ].join(" ")}>
      <div className="flex items-start gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 mb-0.5">
            <span className={[
              "inline-block rounded px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide",
              isPending ? "bg-warning/20 text-warning" : "bg-muted text-text/50",
            ].join(" ")}>
              {kindLabel[change.kind] ?? change.kind}
            </span>
            {change.auto_applied === 1 && (
              <span className="inline-block rounded bg-success/15 px-1.5 py-0.5 text-xs text-success">
                auto-applied
              </span>
            )}
          </div>
          <p className="text-sm font-medium text-text leading-snug">{change.summary}</p>
        </div>
        <button
          onClick={() => setExpanded(v => !v)}
          className="shrink-0 text-text/30 hover:text-text/60 mt-0.5"
        >
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>

      {expanded && (
        <div className="rounded-lg bg-muted/30 p-2 space-y-1 text-xs text-text/70">
          {details.ingredient_name && (
            <p><span className="text-text/40">Ingredient:</span> {String(details.ingredient_name)}</p>
          )}
          {details.supplier_name_before && (
            <p>
              <span className="text-text/40">From:</span>{" "}
              {String(details.supplier_name_before)}{" → "}
              {String(details.supplier_name_after ?? "")}
            </p>
          )}
          {details.price_before != null && (
            <p>
              <span className="text-text/40">Price:</span>{" "}
              {Number(details.price_before).toFixed(4)}{" → "}
              {Number(details.price_after ?? 0).toFixed(4)}
            </p>
          )}
          {details.rationale && (
            <p className="text-text/60 italic">{String(details.rationale).slice(0, 120)}</p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        {isPending && (
          <button
            disabled={busy !== null}
            onClick={() => act("apply")}
            className="flex items-center gap-1 rounded-md bg-success/20 px-2 py-1 text-xs font-medium text-success hover:bg-success/30 disabled:opacity-40"
          >
            {busy === "apply" ? <RefreshCw size={10} className="animate-spin" /> : <Check size={10} />}
            Apply
          </button>
        )}
        {isApplied && (
          <button
            disabled={busy !== null}
            onClick={() => act("revert")}
            className="flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs font-medium text-text/60 hover:bg-muted/80 disabled:opacity-40"
          >
            {busy === "revert" ? <RefreshCw size={10} className="animate-spin" /> : <RotateCcw size={10} />}
            Revert
          </button>
        )}
        <button
          disabled={busy !== null}
          onClick={toggleAutoApply}
          title="Toggle auto-apply mode for all supplier changes"
          className="flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs font-medium text-text/50 hover:bg-muted/80 disabled:opacity-40"
        >
          {busy === "toggle" ? <RefreshCw size={10} className="animate-spin" /> : <ArrowLeftRight size={10} />}
          Switch mode
        </button>
        <button
          disabled={busy !== null}
          onClick={() => act("dismiss")}
          className="ml-auto rounded-md px-1.5 py-1 text-xs text-text/30 hover:text-text/60 disabled:opacity-40"
          title="Dismiss"
        >
          <X size={12} />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Text fallback
// ---------------------------------------------------------------------------

function TextFallback({ onSend }: { onSend: (text: string) => void }) {
  const [text, setText] = useState("");
  return (
    <form
      className="flex gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (!text.trim()) return;
        onSend(text.trim());
        setText("");
      }}
    >
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Type a note for Roba…"
        className="flex-1 rounded-lg border border-muted bg-surface px-3 py-2 text-sm text-text placeholder:text-text/30 focus:border-accent focus:outline-none"
      />
      <button
        type="submit"
        disabled={!text.trim()}
        className="rounded-lg bg-accent px-3 py-2 text-white hover:bg-accent/90 disabled:opacity-40"
      >
        <Send size={16} />
      </button>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Left column — cards board
// ---------------------------------------------------------------------------

function CardsBoard({
  approvals,
  onResolve,
}: {
  approvals: ApprovalRequest[];
  onResolve: (id: number, decision: "approve" | "reject") => void;
}) {
  const changeVersion = useManagerChangeVersion();
  const [changes, setChanges] = useState<ManagerChange[]>([]);

  function loadChanges() {
    apiGet<ManagerChange[]>("/api/manager/changes?status=pending,applied")
      .then((data) => {
        setChanges(data);
        actions.setManagerChanges(data);
      })
      .catch(() => undefined);
  }

  useEffect(() => {
    loadChanges();
    const id = setInterval(loadChanges, 10000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refetch when a manager_change WS event arrives
  useEffect(() => {
    if (changeVersion > 0) loadChanges();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changeVersion]);

  const activeCall = useActiveCall();
  const lastCompletedCall = useLastCompletedCall();
  const pendingChanges = changes.filter((c) => c.status === "pending");
  const recentChanges = changes.filter((c) => c.status === "applied").slice(0, 5);

  return (
    <div className="flex flex-col gap-3 p-3">
      {/* Active call — live indicator */}
      {activeCall && <ActiveCallCard />}
      {/* Completed call summary — shown after call ends, until dismissed */}
      {!activeCall && lastCompletedCall && (
        <CompletedCallCard call={lastCompletedCall} changes={changes} />
      )}

      {/* Pending changes */}
      {pendingChanges.length > 0 && (
        <section>
          <div className="mb-2 flex items-center gap-1.5">
            <Bell size={13} className="text-warning" />
            <span className="text-xs font-semibold uppercase tracking-wide text-text/50">
              Changes awaiting approval ({pendingChanges.length})
            </span>
          </div>
          <div className="space-y-2">
            {pendingChanges.map((c) => (
              <ChangeCard key={c.id} change={c} onRefresh={loadChanges} />
            ))}
          </div>
        </section>
      )}

      {/* Approvals inbox */}
      {approvals.length > 0 && (
        <section>
          <div className="mb-2 flex items-center gap-1.5">
            <Bell size={13} className="text-warning" />
            <span className="text-xs font-semibold uppercase tracking-wide text-text/50">
              Needs approval ({approvals.length})
            </span>
          </div>
          <div className="space-y-2">
            {approvals.map((a) => (
              <ApprovalItem key={a.id} approval={a} onResolve={onResolve} />
            ))}
          </div>
        </section>
      )}

      {/* Recent changes (informational) */}
      {recentChanges.length > 0 && (
        <section>
          <div className="mb-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-text/40">
              Recent changes
            </span>
          </div>
          <div className="space-y-2">
            {recentChanges.map((c) => (
              <ChangeCard key={c.id} change={c} onRefresh={loadChanges} />
            ))}
          </div>
        </section>
      )}

      {/* Empty state */}
      {!activeCall && pendingChanges.length === 0 && approvals.length === 0 && recentChanges.length === 0 && (
        <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-muted/40 py-10 text-center">
          <PhoneCall size={24} className="text-text/20" />
          <p className="text-sm text-text/30">No active calls or pending changes</p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ManagerVoice
// ---------------------------------------------------------------------------

export function ManagerVoice() {
  const live = useVoiceLive("manager");
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [showDev, setShowDev] = useState(false);
  const transcriptEndRef = useRef<HTMLDivElement>(null);

  // Connect the operator WS so the desk receives call_started / call_ended /
  // manager_change events and the ActiveCallCard lights up — mirroring MenuPage.
  // Do NOT call wsClient.close() on unmount: wsClient is a global singleton with
  // no refcounting; closing it here suppresses auto-reconnect for every other tab.
  useEffect(() => {
    wsClient.connect();
  }, []);

  useEffect(() => {
    const load = () =>
      apiGet<ApprovalRequest[]>("/api/approvals?status=pending")
        .then(setApprovals)
        .catch(() => undefined);
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [live.transcript]);

  async function handleResolve(id: number, decision: "approve" | "reject") {
    try {
      await apiPost(`/api/approvals/${id}/${decision}`);
      setApprovals((prev) => prev.filter((a) => a.id !== id));
    } catch {
      // keep in list
    }
  }

  async function handleClarify(planId: string, answer: string) {
    try {
      await apiPost("/api/voice/clarify", { plan_id: planId, answer });
      live.setDone("Clarification submitted.");
    } catch { /* ignore */ }
  }

  const isUnavailable = live.state === "unavailable";

  // ── Voice desk (right column) ────────────────────────────────────────────

  const modeToggles = (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Plan mode</span>
        <ModeToggle mode={live.mode} onChange={live.setMode} disabled={live.state === "listening"} />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Mic mode</span>
        <MicModeToggle micMode={live.micMode} onChange={live.setMicMode} disabled={live.state === "listening"} />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Voice model</span>
        <ModelToggle voiceModel={live.voiceModel} onChange={live.setVoiceModel} disabled={live.state === "listening"} />
      </div>
    </div>
  );

  const errorStrip = live.lastError ? (
    <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
      <span className="flex-1">{live.lastError}</span>
      <button
        className="shrink-0 text-warning/60 hover:text-warning"
        onClick={() => window.location.reload()}
        aria-label="Retry"
      >
        <RefreshCw size={13} />
      </button>
    </div>
  ) : null;

  const micArea = isUnavailable ? (
    <div className="flex flex-col gap-3">
      <TextFallback onSend={live.sendText} />
    </div>
  ) : (
    <div className="flex justify-center py-4">
      <MicButton
        state={live.state}
        micMode={live.micMode}
        size="lg"
        onStart={live.startListening}
        onStop={live.stopListening}
      />
    </div>
  );

  const voiceCards = (
    <>
      {live.pendingPlan && (
        <PlanConfirmCard
          plan={live.pendingPlan}
          clarification={live.clarification}
          onConfirm={live.confirmPlan}
          onCancel={live.cancelPlan}
          onClarify={handleClarify}
          status={live.cardStatus}
        />
      )}
      {!live.pendingPlan && live.lastApplied && (
        <div className="flex items-center gap-3 rounded-xl border border-green-500/30 bg-green-500/5 px-4 py-3 text-sm text-text">
          <Check size={16} className="shrink-0 text-green-500" />
          <span className="flex-1">{live.lastApplied.summary}</span>
          <button
            onClick={live.clearLastApplied}
            className="shrink-0 text-text/30 hover:text-text/60"
            aria-label="Dismiss"
          >
            <X size={14} />
          </button>
        </div>
      )}
      {live.lastStatus && (
        <div className="rounded-xl border border-accent/30 bg-surface p-4">
          <div className="flex items-start justify-between gap-2 mb-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-accent">Answer</span>
            <button onClick={live.clearStatus} className="text-text/30 hover:text-text/60 text-xs">✕</button>
          </div>
          {live.lastStatus.summary != null && <p className="text-sm text-text">{String(live.lastStatus.summary)}</p>}
        </div>
      )}
      {live.lastForecast && (
        <ForecastCard forecast={live.lastForecast} onDismiss={live.clearForecast} />
      )}
    </>
  );

  const transcriptSection = live.transcript.length > 0 ? (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Transcript</span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowDev(v => !v)}
            className={`px-2 py-1 text-xs rounded border ${showDev ? "bg-zinc-700 border-zinc-500 text-white" : "border-zinc-600 text-zinc-400 hover:text-zinc-200"}`}
          >
            Dev
          </button>
          <button onClick={live.clearTranscript} className="text-xs text-text/30 hover:text-text/60">
            Clear
          </button>
        </div>
      </div>
      <div className="max-h-52 overflow-y-auto rounded-lg border border-muted/40 bg-surface/50 p-3 space-y-2">
        {live.transcript.slice(-20).map((line) => (
          <div key={line.id} className={line.role === "user" ? "text-right" : "text-left"}>
            <span
              className={[
                "inline-block rounded-xl px-3 py-1.5 text-sm max-w-[85%]",
                line.role === "user" ? "bg-accent/20 text-text" : "bg-muted text-text/80",
              ].join(" ")}
            >
              {line.text}
            </span>
          </div>
        ))}
        <div ref={transcriptEndRef} />
      </div>
      {showDev && (
        <section className="mt-2 border border-zinc-700 rounded-lg bg-zinc-950 p-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs text-zinc-400 font-mono">Raw transcript frames ({live.rawFrames.length})</span>
            <button onClick={live.clearRawFrames} className="text-xs text-zinc-500 hover:text-zinc-300">Clear</button>
          </div>
          <div className="max-h-64 overflow-y-auto space-y-0.5 font-mono text-xs">
            {live.rawFrames.length === 0 && (
              <div className="text-zinc-600">No frames yet — speak something.</div>
            )}
            {live.rawFrames.map((f, i) => (
              <div key={i} className={`leading-tight ${f.role === "user" ? "text-blue-300" : "text-green-300"}`}>
                <span className="text-zinc-500">[{f.ts}]</span>{" "}
                <span className={f.final ? "font-semibold" : "opacity-70"}>[{f.role}]</span>{" "}
                {f.final ? "✓" : "…"}{" "}
                <span className="text-zinc-400">turn={f.turn_id.slice(0, 8)}</span>{" "}
                "{f.text}"
              </div>
            ))}
          </div>
        </section>
      )}
    </section>
  ) : null;

  const typeFallback = !isUnavailable && live.state !== "listening" ? (
    <details className="text-xs text-text/30">
      <summary className="cursor-pointer hover:text-text/50">Type instead</summary>
      <div className="mt-2">
        <TextFallback onSend={live.sendText} />
      </div>
    </details>
  ) : null;

  const cardsBoard = (
    <section className="flex flex-col min-h-0 rounded-xl border border-muted/40 bg-surface/60 overflow-hidden">
      <div className="shrink-0 flex items-center gap-2 border-b border-muted/30 px-3 py-2.5">
        <Radio size={14} className="text-accent" />
        <span className="text-sm font-semibold text-text">Ops Board</span>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <CardsBoard approvals={approvals} onResolve={handleResolve} />
      </div>
    </section>
  );

  const voiceDesk = (
    <div className="flex flex-col gap-3 min-h-0 overflow-hidden">
      {modeToggles}
      {errorStrip}
      {micArea}
      {voiceCards}
      {transcriptSection}
      {typeFallback}
    </div>
  );

  return (
    <>
      {/* ── Large screens: side-by-side ─────────────────────────────────── */}
      <div className="hidden lg:grid lg:grid-cols-[minmax(0,9fr)_minmax(0,11fr)] gap-4 h-full min-h-0">
        {/* Left — cards board (scrollable) */}
        {cardsBoard}
        {/* Right — voice desk */}
        {voiceDesk}
      </div>

      {/* ── Small screens: stacked ──────────────────────────────────────── */}
      <div className="flex flex-col gap-4 h-full min-h-0 lg:hidden">
        <div className="shrink-0 flex flex-col gap-3">
          {modeToggles}
          {errorStrip}
          {micArea}
          {voiceCards}
        </div>
        <div className="flex-1 min-h-0">
          {cardsBoard}
        </div>
        {transcriptSection}
        {typeFallback}
      </div>
    </>
  );
}
