/**
 * CallPage — dedicated live-voice call surface.
 *
 * Two entry modes:
 *   1. With ?call_id=N&role=<supplier_call|competitor_call|onboarding_call>
 *      Opens the live Gemini session bound to that call; Roba speaks first.
 *   2. Without call_id — Direct-open chooser: lists all suppliers and
 *      competitors from the loaded preset. On selection, POST /api/calls
 *      to create the call then reconnect with the returned call_id.
 *
 * Mounted OUTSIDE OperatorLayout (lazy route in App.tsx) so it never opens
 * the operator WS firehose.
 *
 * Auto-open behaviour (used from other parts of the UI):
 *   window.open(`/call?call_id=${id}&role=supplier_call`, '_blank')
 * This must happen inside a user-gesture handler to avoid popup blockers.
 */

import { useEffect, useRef, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import {
  Mic,
  PhoneCall,
  PhoneOff,
  Send,
  LayoutDashboard,
  RefreshCw,
  Radio,
} from "lucide-react";
import { useVoiceLive } from "../voice/useVoiceLive";
import { MicButton } from "../voice/MicButton";
import { apiGet, apiPost, instancePagePrefix } from "../api";
import type { SupplierParty, CompetitorParty } from "../types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function roleLabel(role: string): string {
  if (role === "supplier_call") return "Supplier";
  if (role === "competitor_call") return "Competitor";
  if (role === "onboarding_call") return "New Supplier";
  if (role === "inbound_supplier_call") return "Supplier";
  if (role === "inbound_competitor_call") return "Competitor";
  return "Counterparty";
}

function roleBanner(role: string, partyName?: string): string {
  if (role === "onboarding_call") return `Roba is welcoming ${partyName ?? "the new supplier"}`;
  if (role === "competitor_call") return `Roba is gathering intel on ${partyName ?? "the competitor"}`;
  if (role === "inbound_supplier_call")
    return `You are calling as ${partyName ?? "the supplier"} — Roba answers as the restaurant manager`;
  if (role === "inbound_competitor_call")
    return `You are calling as ${partyName ?? "the competitor"} — Roba answers as the restaurant manager`;
  return `Roba is negotiating with ${partyName ?? "the supplier"}`;
}

// ---------------------------------------------------------------------------
// Typing fallback
// ---------------------------------------------------------------------------

function TextFallback({ onSend, label }: { onSend: (t: string) => void; label: string }) {
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
        placeholder={`Type as ${label}…`}
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
// Party chooser (direct-open mode, no call_id in URL)
// ---------------------------------------------------------------------------

interface PartyChooserProps {
  onSelect: (type: "supplier" | "competitor", id: number, name: string) => void;
}

function PartyChooser({ onSelect }: PartyChooserProps) {
  const [suppliers, setSuppliers] = useState<SupplierParty[]>([]);
  const [competitors, setCompetitors] = useState<CompetitorParty[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    apiGet<{ suppliers: SupplierParty[]; competitors: CompetitorParty[] }>(
      "/api/presets/current/parties"
    )
      .then((d) => {
        setSuppliers(d.suppliers ?? []);
        setCompetitors(d.competitors ?? []);
      })
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) {
    return (
      <div className="rounded-lg border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
        {err}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="text-center">
        <h2 className="text-xl font-bold text-text">Call the restaurant</h2>
        <p className="mt-1 text-sm text-text/50">
          You play the supplier or competitor phoning in — Roba answers as the restaurant
          manager. Use this to simulate inbound calls: announce a price change, a delivery
          delay, new menu intel, or anything you'd want the restaurant to know.
        </p>
      </div>

      {suppliers.length > 0 && (
        <section>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-text/40">
            Call as a supplier
          </p>
          <div className="grid gap-2">
            {suppliers.map((s) => (
              <button
                key={s.id}
                disabled={busy !== null}
                onClick={() => { setBusy(`s-${s.id}`); onSelect("supplier", s.id, s.name); }}
                className="flex items-center justify-between rounded-xl border border-muted bg-surface px-4 py-3 text-left hover:border-accent/60 hover:bg-accent/5 disabled:opacity-50"
              >
                <div>
                  <p className="font-medium text-text">{s.name}</p>
                  {s.phone && <p className="text-xs text-text/50">{s.phone}</p>}
                </div>
                {busy === `s-${s.id}` ? (
                  <RefreshCw size={14} className="text-accent animate-spin" />
                ) : (
                  <PhoneCall size={14} className="text-accent/60" />
                )}
              </button>
            ))}
          </div>
        </section>
      )}

      {competitors.length > 0 && (
        <section>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-text/40">
            Call as a competitor
          </p>
          <div className="grid gap-2">
            {competitors.map((c) => (
              <button
                key={c.id}
                disabled={busy !== null}
                onClick={() => { setBusy(`c-${c.id}`); onSelect("competitor", c.id, c.name); }}
                className="flex items-center justify-between rounded-xl border border-muted bg-surface px-4 py-3 text-left hover:border-accent/60 hover:bg-accent/5 disabled:opacity-50"
              >
                <div>
                  <p className="font-medium text-text">{c.name}</p>
                  {c.cuisine && (
                    <p className="text-xs text-text/50">
                      {Array.isArray(c.cuisine) ? c.cuisine.join(", ") : c.cuisine}
                    </p>
                  )}
                  {c.phone && <p className="text-xs text-text/40">{c.phone}</p>}
                </div>
                {busy === `c-${c.id}` ? (
                  <RefreshCw size={14} className="text-accent animate-spin" />
                ) : (
                  <PhoneCall size={14} className="text-accent/60" />
                )}
              </button>
            ))}
          </div>
        </section>
      )}

      {suppliers.length === 0 && competitors.length === 0 && (
        <div className="rounded-lg border border-muted p-8 text-center text-text/40 text-sm">
          No parties found. Make sure a restaurant preset is loaded.
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Active call UI
// ---------------------------------------------------------------------------

interface ActiveCallProps {
  callId: number;
  role: string;
  partyName: string;
}

function ActiveCall({ callId, role, partyName }: ActiveCallProps) {
  const userLabel = roleLabel(role);
  const live = useVoiceLive(role, {
    extraParams: { call_id: String(callId) },
  });
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const [ended, setEnded] = useState(false);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [live.transcript]);

  const isUnavailable = live.state === "unavailable";

  function handleEnd() {
    setEnded(true);
    live.stopListening();
    // Deterministically finalize the call (extraction, outcome persistence,
    // call_ended broadcast). The WS-disconnect path is a fallback if this POST
    // can't fire (e.g. tab crash), but we should not rely on it alone.
    if (callId) {
      apiPost(`/api/calls/${callId}/end`).catch(() => undefined);
    }
    setTimeout(() => window.close(), 1200);
  }

  if (ended) {
    return (
      <div className="flex flex-col items-center gap-4 py-16 text-center">
        <PhoneOff size={40} className="text-muted" />
        <p className="text-lg font-semibold text-text">Call ended</p>
        <p className="text-sm text-text/50">The outcome will be processed by Roba.</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 max-w-2xl mx-auto w-full">
      {/* Banner */}
      <div className="flex items-center gap-3 rounded-xl border border-accent/30 bg-accent/5 px-4 py-3">
        <Radio size={16} className="text-accent animate-pulse shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-text">{roleBanner(role, partyName)}</p>
          <p className="text-xs text-text/50 mt-0.5">
            You are speaking as the <span className="font-medium text-accent">{userLabel}</span>
            {" · "}Call #{callId}
          </p>
        </div>
        <button
          onClick={handleEnd}
          className="shrink-0 flex items-center gap-1.5 rounded-lg bg-danger/20 px-3 py-1.5 text-xs font-medium text-danger hover:bg-danger/30"
        >
          <PhoneOff size={12} />
          End
        </button>
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

      {/* Mic area */}
      {isUnavailable ? (
        <div className="rounded-lg border border-muted p-4 text-center text-xs text-text/40">
          Live voice unavailable — use text fallback below.
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2 py-4">
          <MicButton
            state={live.state}
            micMode={live.micMode}
            size="lg"
            onStart={live.startListening}
            onStop={live.stopListening}
          />
          <p className="text-xs text-text/40">
            {live.state === "listening"
              ? `Roba hears you as the ${userLabel}…`
              : live.state === "thinking"
              ? "Roba is thinking…"
              : live.state === "speaking"
              ? "Roba is speaking…"
              : `Hold to speak as the ${userLabel}`}
          </p>
        </div>
      )}

      {/* Live transcript */}
      {live.transcript.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-text/40">
              Transcript
            </span>
            <button
              onClick={live.clearTranscript}
              className="text-xs text-text/30 hover:text-text/60"
            >
              Clear
            </button>
          </div>
          <div className="max-h-72 overflow-y-auto rounded-xl border border-muted/40 bg-surface/50 p-3 space-y-2">
            {live.transcript.slice(-30).map((line) => {
              const isRoba = line.role === "roba";
              return (
                <div key={line.id} className={isRoba ? "text-left" : "text-right"}>
                  <div className="mb-0.5">
                    <span className={`text-xs font-medium ${isRoba ? "text-accent" : "text-text/50"}`}>
                      {isRoba ? "Roba" : userLabel}
                    </span>
                  </div>
                  <span
                    className={[
                      "inline-block rounded-xl px-3 py-1.5 text-sm max-w-[85%]",
                      isRoba ? "bg-accent/15 text-text" : "bg-muted text-text/80",
                      !line.final ? "opacity-60 italic" : "",
                    ].join(" ")}
                  >
                    {line.text}
                  </span>
                </div>
              );
            })}
            <div ref={transcriptEndRef} />
          </div>
        </div>
      )}

      {/* Text fallback */}
      <TextFallback onSend={live.sendText} label={userLabel} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// CallPage root
// ---------------------------------------------------------------------------

export default function CallPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const callIdParam = searchParams.get("call_id");
  const roleParam = searchParams.get("role") ?? "supplier_call";

  const callId = callIdParam ? Number(callIdParam) : null;
  const [partyName, setPartyName] = useState<string>(
    searchParams.get("party_name") ?? ""
  );
  const [resolvedCallId, setResolvedCallId] = useState<number | null>(callId);
  const [resolvedRole, setResolvedRole] = useState<string>(roleParam);
  const [chooserError, setChooserError] = useState<string | null>(null);

  async function handlePartySelect(
    type: "supplier" | "competitor",
    id: number,
    name: string,
  ) {
    setChooserError(null);
    // Inbound: human plays the counterparty; Roba answers as the restaurant manager.
    const role = type === "competitor" ? "inbound_competitor_call" : "inbound_supplier_call";
    try {
      const res = await apiPost<{ call_id: number }>("/api/calls", {
        counterparty_type: type,
        counterparty_id: id,
        purpose: `inbound ${type} call`,
      });
      setPartyName(name);
      setResolvedRole(role);
      setResolvedCallId(res.call_id);
      setSearchParams({ call_id: String(res.call_id), role, party_name: name });
    } catch (e) {
      setChooserError(String(e));
    }
  }

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-primary text-text">
      {/* Top bar */}
      <header className="shrink-0 flex items-center justify-between border-b border-muted bg-surface px-5 py-3">
        <div className="flex items-center gap-2">
          <Mic size={18} className="text-accent" />
          <span className="text-sm font-bold tracking-wide text-text">Roba Call</span>
          {resolvedCallId && (
            <span className="ml-1 rounded-full bg-accent/20 px-2 py-0.5 text-xs font-medium text-accent">
              Live
            </span>
          )}
        </div>
        <Link
          to={instancePagePrefix() || "/admin"}
          className="flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium text-text/50 hover:bg-muted/50 hover:text-text"
        >
          <LayoutDashboard size={13} />
          Dashboard
        </Link>
      </header>

      <main className="flex-1 min-h-0 overflow-y-auto px-4 py-6">
        {chooserError && (
          <div className="max-w-2xl mx-auto mb-4 rounded-lg border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
            {chooserError}
          </div>
        )}

        {resolvedCallId ? (
          <ActiveCall
            callId={resolvedCallId}
            role={resolvedRole}
            partyName={partyName}
          />
        ) : (
          <div className="max-w-md mx-auto">
            <PartyChooser onSelect={handlePartySelect} />
          </div>
        )}
      </main>
    </div>
  );
}
