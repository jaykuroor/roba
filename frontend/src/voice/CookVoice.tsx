import { useState, useEffect, useRef } from "react";
import { Loader2, ChefHat, Trash2, Send, RefreshCw, ChevronUp, ChevronDown, Check, X } from "lucide-react";
import { useVoiceLive } from "./useVoiceLive";
import { MicButton } from "./MicButton";
import { PlanConfirmCard } from "./PlanConfirmCard";
import { ModeToggle } from "./ModeToggle";
import { MicModeToggle } from "./MicModeToggle";
import { apiGet, apiPost } from "../api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface KitchenBatch {
  id: number;
  menu_item_id: number;
  menu_item_name?: string;
  planned_qty: number;
  status: string;
  cook_by?: number;
}

interface KitchenBoard {
  clock: string;
  counts: { cooked: number; approved: number; pending: number; skipped: number };
  batches: BoardBatch[];
}
interface BoardBatch {
  id: number;
  dish: string;
  state: "cooked" | "ready_to_cook" | "awaiting_approval" | "skipped";
  planned_qty: number | null;
  actual_made_qty: number | null;
  cook_by: number | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtSimTime(secs: number): string {
  return `${String(Math.floor(secs / 3600) % 24).padStart(2, "0")}:${String(Math.floor((secs % 3600) / 60)).padStart(2, "0")}`;
}

const STATE_PILL: Record<string, { label: string; cls: string }> = {
  cooked:            { label: "Cooked",           cls: "bg-success/20 text-success" },
  ready_to_cook:     { label: "Ready to cook",    cls: "bg-accent/20 text-accent" },
  awaiting_approval: { label: "Needs approval",   cls: "bg-warning/20 text-warning" },
  skipped:           { label: "Skipped",           cls: "bg-muted/40 text-text/50" },
};

// ---------------------------------------------------------------------------
// Next batch card
// ---------------------------------------------------------------------------

function BatchCard({
  batch,
  onMarkCooked,
}: {
  batch: KitchenBatch;
  onMarkCooked: (batch: KitchenBatch, qty: number) => void;
}) {
  const [qty, setQty] = useState(String(batch.planned_qty));

  return (
    <div className="rounded-xl border border-accent/40 bg-surface p-4 shadow-md">
      <div className="flex items-start justify-between gap-2">
        <div>
          <span className="inline-flex items-center gap-1 rounded-full bg-accent/20 px-2 py-0.5 text-xs font-semibold text-accent">
            <ChefHat size={11} />
            Next batch
          </span>
          <h2 className="mt-1 text-base font-bold text-text">
            {batch.menu_item_name ?? `Item #${batch.menu_item_id}`}
          </h2>
        </div>
        <span
          className={[
            "rounded-full px-2 py-0.5 text-xs font-medium capitalize",
            batch.status === "approved"
              ? "bg-success/20 text-success"
              : "bg-warning/20 text-warning",
          ].join(" ")}
        >
          {batch.status}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
        <div>
          <span className="text-text/40 text-xs">Planned qty</span>
          <p className="font-semibold text-text">{batch.planned_qty}</p>
        </div>
        {batch.cook_by != null && (
          <div>
            <span className="text-text/40 text-xs">Cook by</span>
            <p className="font-semibold text-text">
              {String(Math.floor(batch.cook_by / 3600) % 24).padStart(2, "0")}:
              {String(Math.floor((batch.cook_by % 3600) / 60)).padStart(2, "0")}
            </p>
          </div>
        )}
      </div>

      <div className="mt-3 flex gap-2 items-end">
        <div className="flex flex-col gap-0.5">
          <label className="text-xs text-text/40">Actual qty made</label>
          <input
            type="number"
            min={0}
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            className="w-24 rounded-lg border border-muted bg-primary/60 px-2 py-1.5 text-sm text-text focus:border-accent focus:outline-none"
          />
        </div>
        <button
          onClick={() => onMarkCooked(batch, Number(qty) || batch.planned_qty)}
          className="flex items-center gap-1.5 rounded-lg bg-success/80 px-3 py-2 text-sm font-medium text-white hover:bg-success transition-colors"
        >
          <ChefHat size={14} />
          Mark cooked
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Text fallback
// ---------------------------------------------------------------------------

function TextFallback({ onSend }: { onSend: (t: string) => void }) {
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
        placeholder='e.g. "I made 12 burgers, throwing 3 away"'
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
// CookVoice
// ---------------------------------------------------------------------------

export function CookVoice() {
  const live = useVoiceLive("cook");
  const [batches, setBatches] = useState<KitchenBatch[]>([]);
  const [loading, setLoading] = useState(true);
  const [board, setBoard] = useState<KitchenBoard | null>(null);
  const [boardExpanded, setBoardExpanded] = useState<boolean>(() => {
    try { return localStorage.getItem("roba.cook.board") !== "false"; } catch { return true; }
  });
  const transcriptEndRef = useRef<HTMLDivElement>(null);

  const loadBatches = () =>
    apiGet<KitchenBatch[]>("/api/kitchen/batches?status=approved,decided")
      .then((list) => setBatches(list ?? []))
      .catch(() => undefined)
      .finally(() => setLoading(false));

  const loadBoard = () =>
    apiGet<KitchenBoard>("/api/kitchen/board?window_hours=6")
      .then((b) => setBoard(b ?? null))
      .catch(() => undefined);

  useEffect(() => {
    loadBatches();
    const id = setInterval(loadBatches, 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    loadBoard();
    const id = setInterval(loadBoard, 5000);
    return () => clearInterval(id);
  }, []);

  const toggleBoard = () => {
    setBoardExpanded((v) => {
      const next = !v;
      try { localStorage.setItem("roba.cook.board", String(next)); } catch {}
      return next;
    });
  };

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [live.transcript]);

  async function handleMarkCooked(batch: KitchenBatch, qty: number) {
    try {
      await apiPost(`/api/kitchen/batches/${batch.id}/cooked`, { actual_made_qty: qty });
      setBatches((prev) => prev.filter((b) => b.id !== batch.id));
    } catch { /* ignore */ }
  }

  async function handleClarify(planId: string, answer: string) {
    try {
      await apiPost("/api/voice/clarify", { plan_id: planId, answer });
    } catch { /* ignore */ }
  }

  const nextBatch = batches[0] ?? null;
  const isUnavailable = live.state === "unavailable";

  return (
    <div className="flex flex-col gap-5">
      {/* Mode toggles */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Plan mode</span>
          <ModeToggle mode={live.mode} onChange={live.setMode} disabled={live.state === "listening"} />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Mic mode</span>
          <MicModeToggle micMode={live.micMode} onChange={live.setMicMode} disabled={live.state === "listening"} />
        </div>
      </div>

      {/* Error strip */}
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

      {/* Next batch */}
      {loading ? (
        <div className="flex items-center justify-center py-6 text-text/40">
          <Loader2 size={20} className="animate-spin mr-2" />
          Loading batches…
        </div>
      ) : nextBatch ? (
        <BatchCard batch={nextBatch} onMarkCooked={handleMarkCooked} />
      ) : (
        <div className="rounded-xl border border-muted/40 bg-surface/50 py-6 text-center text-sm text-text/40">
          No batches in queue
        </div>
      )}

      {/* Batch board */}
      {board && (
        <section className="rounded-xl border border-muted/40 bg-surface/60 overflow-hidden">
          {/* Header — always visible */}
          <div className="flex items-center justify-between px-4 py-2.5">
            <span className="text-xs font-semibold uppercase tracking-wide text-text/40">
              Batch board
              {board.clock && (
                <span className="ml-2 normal-case font-normal text-text/30">{board.clock}</span>
              )}
            </span>
            <div className="flex items-center gap-3">
              {/* Counts — always shown even when collapsed */}
              <div className="flex gap-2 text-xs">
                {board.counts.cooked > 0 && (
                  <span className="text-success font-medium">{board.counts.cooked} cooked</span>
                )}
                {board.counts.approved > 0 && (
                  <span className="text-accent font-medium">{board.counts.approved} to cook</span>
                )}
                {board.counts.pending > 0 && (
                  <span className="text-warning font-medium">{board.counts.pending} pending</span>
                )}
                {board.counts.skipped > 0 && (
                  <span className="text-text/30">{board.counts.skipped} skipped</span>
                )}
              </div>
              <button
                onClick={toggleBoard}
                className="text-text/30 hover:text-text/60 transition-colors"
                aria-label={boardExpanded ? "Collapse batch board" : "Expand batch board"}
              >
                {boardExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
            </div>
          </div>

          {/* Expandable list */}
          {boardExpanded && board.batches.length > 0 && (
            <div className="max-h-56 overflow-y-auto border-t border-muted/30 divide-y divide-muted/20">
              {board.batches.map((b) => {
                const pill = STATE_PILL[b.state] ?? { label: b.state, cls: "bg-muted/40 text-text/50" };
                return (
                  <div key={b.id} className="flex items-center justify-between gap-2 px-4 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-text truncate">{b.dish}</p>
                      <p className="text-xs text-text/40">
                        {b.state === "cooked"
                          ? `${b.actual_made_qty ?? "?"}/${b.planned_qty ?? "?"} made`
                          : `Planned: ${b.planned_qty ?? "?"}`}
                        {b.cook_by != null && b.state !== "cooked" && (
                          <span className="ml-2">by {fmtSimTime(b.cook_by)}</span>
                        )}
                      </p>
                    </div>
                    <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${pill.cls}`}>
                      {pill.label}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          {boardExpanded && board.batches.length === 0 && (
            <div className="border-t border-muted/30 py-4 text-center text-xs text-text/30">
              No batches in the last 6 hours
            </div>
          )}
        </section>
      )}

      {/* Voice answer card */}
      {live.lastStatus && (
        <div className="rounded-xl border border-accent/30 bg-surface p-4 shadow-md">
          <div className="flex items-start justify-between gap-2 mb-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-accent">
              Answer
            </span>
            <button
              onClick={live.clearStatus}
              className="text-text/30 hover:text-text/60 text-xs"
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
          {/* Summary line */}
          {live.lastStatus.summary != null && (
            <p className="text-sm font-medium text-text mb-2">
              {String(live.lastStatus.summary)}
            </p>
          )}
          {/* Dish-specific answer */}
          {live.lastStatus.answer != null && (
            <div className="flex flex-wrap gap-2 mt-1">
              {(() => {
                const ans = live.lastStatus.answer as { prepared?: boolean; should_cook?: boolean; awaiting_approval?: boolean; made_qty?: number | null };
                if (ans.prepared) {
                  return (
                    <>
                      <span className="rounded-full px-2 py-0.5 text-xs font-medium bg-success/20 text-success">Cooked</span>
                      {ans.made_qty != null && (
                        <span className="text-xs text-text/60">{ans.made_qty} made</span>
                      )}
                    </>
                  );
                } else if (ans.should_cook) {
                  return <span className="rounded-full px-2 py-0.5 text-xs font-medium bg-accent/20 text-accent">Should cook now</span>;
                } else if (ans.awaiting_approval) {
                  return <span className="rounded-full px-2 py-0.5 text-xs font-medium bg-warning/20 text-warning">Awaiting approval</span>;
                }
                return null;
              })()}
            </div>
          )}
          {/* Board-level counts (when no specific dish) */}
          {live.lastStatus.counts != null && live.lastStatus.answer == null && (
            <div className="flex gap-3 text-xs mt-1">
              {(live.lastStatus.counts as { cooked?: number; approved?: number; pending?: number }).cooked != null && (
                <span className="text-success">{(live.lastStatus.counts as { cooked: number }).cooked} cooked</span>
              )}
              {(live.lastStatus.counts as { approved?: number }).approved != null && (
                <span className="text-accent">{(live.lastStatus.counts as { approved: number }).approved} to cook</span>
              )}
              {(live.lastStatus.counts as { pending?: number }).pending != null && (
                <span className="text-warning">{(live.lastStatus.counts as { pending: number }).pending} pending</span>
              )}
            </div>
          )}
        </div>
      )}

      {/* Mic area */}
      {isUnavailable ? (
        <TextFallback onSend={live.sendText} />
      ) : (
        <div className="flex flex-col items-center gap-3">
          <MicButton
            state={live.state}
            micMode={live.micMode}
            size="md"
            onStart={live.startListening}
            onStop={live.stopListening}
          />
          {/* Quick action buttons */}
          {nextBatch && (
            <div className="flex gap-2">
              <button
                disabled={live.state === "listening" || live.state === "thinking"}
                onClick={() =>
                  live.sendText(
                    `I cooked the ${nextBatch.menu_item_name ?? "batch"}, made ${nextBatch.planned_qty}`,
                  )
                }
                className="flex items-center gap-1.5 rounded-lg border border-success/40 bg-success/10 px-3 py-1.5 text-sm text-success hover:bg-success/20 disabled:opacity-40"
              >
                <ChefHat size={13} />
                Batch done
              </button>
              <button
                disabled={live.state === "listening" || live.state === "thinking"}
                onClick={() =>
                  live.sendText(
                    `I threw away some ${nextBatch.menu_item_name ?? "the batch"}`,
                  )
                }
                className="flex items-center gap-1.5 rounded-lg border border-danger/40 bg-danger/10 px-3 py-1.5 text-sm text-danger hover:bg-danger/20 disabled:opacity-40"
              >
                <Trash2 size={13} />
                Report waste
              </button>
            </div>
          )}
        </div>
      )}

      {/* Plan / clarification */}
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

      {/* Transcript */}
      {live.transcript.length > 0 && (
        <section>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-text/40">Transcript</span>
            <button onClick={live.clearTranscript} className="text-xs text-text/30 hover:text-text/60">
              Clear
            </button>
          </div>
          <div className="max-h-40 overflow-y-auto rounded-lg border border-muted/40 bg-surface/50 p-3 space-y-2">
            {live.transcript.slice(-8).map((line) => (
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

      {/* Silent fallback */}
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
