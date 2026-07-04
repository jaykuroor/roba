/**
 * SpectateOverlay — live call viewer + coach-hint input.
 *
 * Shows the live transcript of the active call (sourced from the store's
 * callTurns, pushed by the call_turn WS event) and a text input that sends
 * coaching hints to Roba via POST /api/calls/{id}/hint.
 *
 * Roba receives the hint as a private coaching message and can adjust its
 * strategy mid-call without the counterparty seeing the hint text.
 *
 * Usage (from ManagerVoice or DashboardView):
 *   <SpectateOverlay callId={call.id} onClose={() => ...} />
 */

import { useRef, useEffect, useState } from "react";
import { X, Send, Radio, Mic2 } from "lucide-react";
import { useCallTurns, useActiveCall } from "../store";
import { apiPost } from "../api";

interface SpectateOverlayProps {
  /** Call id to spectate. Defaults to the active call if omitted. */
  callId?: number;
  onClose: () => void;
}

export function SpectateOverlay({ callId, onClose }: SpectateOverlayProps) {
  const activeCall = useActiveCall();
  const resolvedId = callId ?? activeCall?.id;
  const turns = useCallTurns();
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const [hint, setHint] = useState("");
  const [sent, setSent] = useState<string[]>([]);
  const [hintBusy, setHintBusy] = useState(false);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  async function sendHint() {
    if (!hint.trim() || resolvedId == null) return;
    setHintBusy(true);
    const text = hint.trim();
    setHint("");
    try {
      await apiPost(`/api/calls/${resolvedId}/hint`, { text });
      setSent((prev) => [...prev.slice(-9), text]);
    } catch {
      // silently ignore
    } finally {
      setHintBusy(false);
    }
  }

  return (
    /* Modal backdrop */
    <div
      className="fixed inset-0 z-50 flex items-end justify-center sm:items-center p-4 bg-black/60 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="w-full max-w-lg rounded-2xl border border-muted/60 bg-primary shadow-2xl flex flex-col max-h-[80vh]">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-muted/40 px-4 py-3 shrink-0">
          <div className="flex items-center gap-2">
            <Radio size={15} className="text-accent animate-pulse" />
            <span className="text-sm font-semibold text-text">
              Spectating call #{resolvedId}
            </span>
          </div>
          <button
            onClick={onClose}
            className="text-text/30 hover:text-text/70"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Transcript */}
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-2">
          {turns.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-8 text-center text-text/30">
              <Mic2 size={28} />
              <p className="text-sm">Waiting for the call to start…</p>
            </div>
          ) : (
            turns.map((t, i) => {
              const isRoba = t.role === "agent";
              return (
                <div key={i} className={isRoba ? "text-left" : "text-right"}>
                  <span className={`text-xs font-medium ${isRoba ? "text-accent" : "text-text/50"} mb-0.5 block`}>
                    {isRoba ? "Roba" : "Counterparty"}
                  </span>
                  <span
                    className={[
                      "inline-block rounded-xl px-3 py-1.5 text-sm max-w-[85%]",
                      isRoba ? "bg-accent/15 text-text" : "bg-muted text-text/80",
                    ].join(" ")}
                  >
                    {t.text}
                  </span>
                </div>
              );
            })
          )}
          <div ref={transcriptEndRef} />
        </div>

        {/* Sent hints preview */}
        {sent.length > 0 && (
          <div className="px-4 pb-2 shrink-0">
            <p className="text-xs font-semibold text-text/30 uppercase tracking-wide mb-1">
              Coaching hints sent
            </p>
            {sent.slice(-3).map((h, i) => (
              <p key={i} className="text-xs text-text/40 italic">"{h}"</p>
            ))}
          </div>
        )}

        {/* Hint input */}
        <div className="border-t border-muted/40 p-3 shrink-0">
          <form
            className="flex gap-2"
            onSubmit={(e) => { e.preventDefault(); sendHint(); }}
          >
            <input
              type="text"
              value={hint}
              onChange={(e) => setHint(e.target.value)}
              placeholder="Coach Roba (private, not spoken aloud)…"
              className="flex-1 rounded-lg border border-muted bg-surface px-3 py-2 text-sm text-text placeholder:text-text/30 focus:border-accent focus:outline-none"
            />
            <button
              type="submit"
              disabled={!hint.trim() || hintBusy || resolvedId == null}
              className="rounded-lg bg-accent px-3 py-2 text-white hover:bg-accent/90 disabled:opacity-40"
            >
              <Send size={15} />
            </button>
          </form>
          <p className="mt-1.5 text-xs text-text/30">
            Your hint is injected as a private coaching note — Roba reads it but it's not spoken.
          </p>
        </div>
      </div>
    </div>
  );
}
