import { useState, useEffect, useRef } from "react";
import { CheckCircle2, XCircle, Bell, Send, RefreshCw, Check, X } from "lucide-react";
import { useVoiceLive } from "./useVoiceLive";
import { MicButton } from "./MicButton";
import { PlanConfirmCard } from "./PlanConfirmCard";
import { ModeToggle } from "./ModeToggle";
import { MicModeToggle } from "./MicModeToggle";
import { ModelToggle } from "./ModelToggle";
import { apiGet, apiPost } from "../api";
import type { ApprovalRequest } from "../types";

// ---------------------------------------------------------------------------
// Approval inbox item
// ---------------------------------------------------------------------------

function ApprovalItem({
  approval,
  onResolve,
}: {
  approval: ApprovalRequest;
  onResolve: (id: number, decision: "approve" | "reject") => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex items-start gap-3 rounded-lg border border-muted/60 bg-surface p-3">
      <div className="flex-1 min-w-0">
        <span className="inline-block rounded bg-muted px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide text-text/50">
          {approval.type.replace(/_/g, " ")}
        </span>
        <p className="mt-0.5 text-sm font-medium text-text truncate">{approval.title}</p>
        {approval.summary && (
          <p className="text-xs text-text/60 mt-0.5 line-clamp-2">{approval.summary}</p>
        )}
      </div>
      <div className="flex shrink-0 gap-1.5">
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
    </div>
  );
}

// ---------------------------------------------------------------------------
// Text input fallback (shown when Gemini Live is unavailable)
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
// ManagerVoice
// ---------------------------------------------------------------------------

export function ManagerVoice() {
  const live = useVoiceLive("manager");
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const transcriptEndRef = useRef<HTMLDivElement>(null);

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
    } catch { /* ignore */ }
  }

  const isUnavailable = live.state === "unavailable";

  return (
    <div className="flex flex-col gap-5">
      {/* Header row: mode toggles */}
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

      {/* Error / info strip */}
      {live.lastError && (
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
      )}

      {/* Main interaction area */}
      {isUnavailable ? (
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
      )}

      {/* Pending plan / confirm card */}
      {live.pendingPlan && (
        <PlanConfirmCard
          plan={live.pendingPlan}
          clarification={live.clarification}
          onConfirm={live.confirmPlan}
          onCancel={live.cancelPlan}
          onClarify={handleClarify}
        />
      )}

      {/* Auto-mode done card — brief confirmation of what was just applied */}
      {live.lastApplied && (
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

      {/* Voice answer card */}
      {live.lastStatus && (
        <div className="rounded-xl border border-accent/30 bg-surface p-4">
          <div className="flex items-start justify-between gap-2 mb-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-accent">Answer</span>
            <button onClick={live.clearStatus} className="text-text/30 hover:text-text/60 text-xs">✕</button>
          </div>
          {live.lastStatus.summary != null && <p className="text-sm text-text">{String(live.lastStatus.summary)}</p>}
        </div>
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
              <ApprovalItem key={a.id} approval={a} onResolve={handleResolve} />
            ))}
          </div>
        </section>
      )}

      {/* Transcript */}
      {live.transcript.length > 0 && (
        <section>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Transcript</span>
            <button onClick={live.clearTranscript} className="text-xs text-text/30 hover:text-text/60">
              Clear
            </button>
          </div>
          <div className="max-h-52 overflow-y-auto rounded-lg border border-muted/40 bg-surface/50 p-3 space-y-2">
            {live.transcript.slice(-12).map((line) => (
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
        </section>
      )}

      {/* Text fallback also available when unavailable (shown above) but also offer
          a secondary text box when voice is available, for silent environments */}
      {!isUnavailable && live.state !== "listening" && (
        <details className="text-xs text-text/30">
          <summary className="cursor-pointer hover:text-text/50">Type instead</summary>
          <div className="mt-2">
            <TextFallback onSend={live.sendText} />
          </div>
        </details>
      )}
    </div>
  );
}
