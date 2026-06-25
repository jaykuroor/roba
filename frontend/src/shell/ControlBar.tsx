import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  Mic,
  Pause,
  PhoneOff,
  Play,
  RotateCcw,
  Send,
  Square,
  StepForward,
  Wifi,
  WifiOff,
} from "lucide-react";
import { apiGet, apiPatch, apiPost } from "../api";
import {
  actions,
  useActiveCall,
  useApprovals,
  useCallTurns,
  useSimState,
  useWsConnected,
} from "../store";
import type {
  SimSettings,
  SimState,
  SimStatus,
} from "../types";

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
// Voice extraction can include a real LLM call; keep this above provider timeout.
const VOICE_TIMEOUT_MS = 45000;

// ---------------------------------------------------------------------------
// Display helpers (pure formatting of server state — not business logic)
// ---------------------------------------------------------------------------

function secondsToHHMM(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600) % 24;
  const m = Math.floor((total % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function formatSimTime(sim: SimState | null): string {
  if (!sim) return "—";
  const t = sim.sim_time ?? 0;
  const day = sim.day_number ?? Math.floor(t / 86400);
  const tod = sim.time_of_day ?? secondsToHHMM(t % 86400);
  const dow = DOW[(sim.day_of_week ?? day % 7) % 7];
  return `Day ${day} · ${dow} · ${tod}`;
}

function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        window.clearTimeout(timer);
        reject(err);
      },
    );
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

type VoiceExtraction = {
  intent?: string;
  entity_type?: string;
  entity_ref?: string | number | null;
  attribute?: string;
  value?: unknown;
  effective_window?: { start?: number; end?: number } | null;
  confidence?: number;
};

type VoiceResponse = {
  extracted?: VoiceExtraction;
  routes?: Array<{
    route_id: string;
    signal_type: string;
    target_modules: string[];
    status: string;
    signal_id?: string | null;
  }>;
  resulting_writes?: string[];
  signal_id?: string | null;
  error?: string;
};

function voiceResponse(result: unknown): VoiceResponse | null {
  if (!isRecord(result)) return null;
  return result as VoiceResponse;
}

function voiceActionLabel(extracted: VoiceExtraction | undefined): string {
  if (!extracted) return "Awaiting interpretation";
  const attribute = String(extracted.attribute ?? "");
  const value = extracted.value;
  const action = isRecord(value) ? String(value.action ?? "") : String(value ?? "");
  if (attribute === "production_unavailable" || action === "halt_production") {
    return "Production locked to zero";
  }
  if (attribute === "overstock" || action === "reduce_forecast") {
    return "Forecast locked to zero";
  }
  if (extracted.intent === "add_event") return "Demand event added";
  if (extracted.intent === "set_operational_constraint") return "Operational constraint stored";
  return String(extracted.intent ?? "Stored");
}

function voiceTargetLabel(extracted: VoiceExtraction | undefined): string {
  const target = extracted?.entity_ref;
  return target == null || target === "" ? "restaurant operation" : String(target);
}

function voiceWindowLabel(window: VoiceExtraction["effective_window"]): string {
  if (!window || window.start == null || window.end == null) return "No active window";
  return `${secondsToHHMM(window.start)}–${secondsToHHMM(window.end)}`;
}

function VoiceResultCard({ result }: { result: unknown }) {
  const response = voiceResponse(result);
  if (!response) return null;
  if (response.error) {
    return (
      <div className="flex min-h-7 items-center gap-2 rounded-md border border-danger/40 bg-danger/10 px-2 py-1 text-xs text-danger">
        <div className="flex items-center gap-1.5 font-medium">
          <AlertTriangle size={14} />
          Voice request failed
        </div>
        <div className="truncate text-danger/80">{response.error}</div>
      </div>
    );
  }

  const extracted = response.extracted;
  const confidence = Math.round(Number(extracted?.confidence ?? 0) * 100);
  const writes = response.resulting_writes ?? [];
  const routes = response.routes ?? [];
  const intent = extracted?.intent ? String(extracted.intent).replaceAll("_", " ") : null;

  return (
    <div className="flex min-h-8 flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-accent/30 bg-primary/70 px-2 py-1 text-xs">
      <div className="flex min-w-0 items-center gap-1.5 font-medium text-text">
        <CheckCircle2 size={14} className="shrink-0 text-success" />
        <span className="truncate">{voiceActionLabel(extracted)}</span>
      </div>
      {intent && (
        <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] font-semibold uppercase text-white">
          {intent}
        </span>
      )}
      <span className="truncate text-text/65">
        {voiceTargetLabel(extracted)} · {voiceWindowLabel(extracted?.effective_window)}
      </span>
      <span className="text-text/45">{confidence}%</span>
      {routes.length > 0 && (
        <span className="truncate text-text/55">
          {routes.map((route) => route.signal_type).join(", ")}
        </span>
      )}
      <span className="truncate text-text/45">{writes.length ? writes.join(", ") : "stored"}</span>
      {response.signal_id && (
        <span className="hidden truncate text-[10px] text-text/35 xl:inline">{response.signal_id}</span>
      )}
      <details className="relative ml-auto text-text/45">
        <summary className="cursor-pointer text-[10px] uppercase">Details</summary>
        <pre className="absolute right-0 z-40 mt-1 max-h-44 w-[min(34rem,80vw)] overflow-auto rounded-md border border-muted bg-surface p-2 text-[10px] shadow-xl">
          {JSON.stringify(result, null, 2)}
        </pre>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Web Speech API mic button (text fallback is always available alongside it)
// ---------------------------------------------------------------------------

type RecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
};

function getRecognitionCtor(): (new () => RecognitionLike) | null {
  const w = window as unknown as {
    SpeechRecognition?: new () => RecognitionLike;
    webkitSpeechRecognition?: new () => RecognitionLike;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

function MicButton({ onResult }: { onResult: (text: string) => void }) {
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<RecognitionLike | null>(null);
  const supported = getRecognitionCtor() !== null;

  function toggle() {
    const Ctor = getRecognitionCtor();
    if (!Ctor) return;
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const recognition = new Ctor();
    recognition.lang = "en-US";
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      const text = event.results?.[0]?.[0]?.transcript ?? "";
      if (text) onResult(text);
    };
    recognition.onerror = () => setListening(false);
    recognition.onend = () => setListening(false);
    recognitionRef.current = recognition;
    recognition.start();
    setListening(true);
  }

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={!supported}
      title={supported ? "Speak" : "Speech recognition unavailable"}
      className={
        "flex items-center justify-center rounded-md px-2 py-1.5 " +
        (listening
          ? "bg-accent text-white"
          : "bg-muted text-text hover:bg-muted/70 disabled:opacity-40")
      }
    >
      <Mic size={16} />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Section primitives
// ---------------------------------------------------------------------------

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-text/40">
        {label}
      </span>
      <div className="flex items-center gap-1">{children}</div>
    </div>
  );
}

function TransportButton({
  onClick,
  title,
  active,
  disabled,
  pending,
  children,
}: {
  onClick: () => void;
  title: string;
  active?: boolean;
  disabled?: boolean;
  pending?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={
        "flex h-8 w-8 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed " +
        (pending
          ? "animate-pulse bg-accent/70 text-white"
          : active
            ? "bg-accent text-white"
            : disabled
              ? "bg-muted/40 text-text/20"
              : "bg-muted text-text hover:bg-muted/70")
      }
    >
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Status pill — colour-coded with animated dot for running / frozen states
// ---------------------------------------------------------------------------

const STATUS_CFG: Record<
  string,
  { dot: string; label: string; textCls: string }
> = {
  running: {
    dot: "bg-success animate-pulse",
    label: "running",
    textCls: "text-success",
  },
  paused: {
    dot: "bg-warning",
    label: "paused",
    textCls: "text-warning",
  },
  stopped: {
    dot: "bg-text/30",
    label: "stopped",
    textCls: "text-text/40",
  },
  call_frozen: {
    dot: "bg-accent animate-pulse",
    label: "frozen",
    textCls: "text-accent",
  },
};

function StatusPill({
  status,
  pendingAction,
}: {
  status: SimStatus | string;
  pendingAction: string | null;
}) {
  if (pendingAction === "restart") {
    return (
      <span className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium">
        <span className="h-2 w-2 animate-spin rounded-full border border-warning border-t-transparent" />
        <span className="text-warning">restarting</span>
      </span>
    );
  }
  const cfg = STATUS_CFG[status] ?? {
    dot: "bg-text/30",
    label: status,
    textCls: "text-text/40",
  };
  return (
    <span className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium">
      <span className={`h-2 w-2 rounded-full ${cfg.dot}`} />
      <span className={cfg.textCls}>{cfg.label}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Voice console (normal + ROLEPLAY mode during a call)
// ---------------------------------------------------------------------------

function VoiceConsole() {
  const activeCall = useActiveCall();
  const callTurns = useCallTurns();
  const [text, setText] = useState("");
  const [result, setResult] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [callTurns.length]);

  // -- ROLEPLAY mode (00 §8, §23) -------------------------------------------
  if (activeCall) {
    const counterparty =
      activeCall.counterparty_type === "supplier" ? "Supplier" : "Competitor";

    async function sendTurn() {
      const value = text.trim();
      if (!value || !activeCall) return;
      setText("");
      try {
        await apiPost(`/api/calls/${activeCall.id}/turn`, {
          role: "counterparty",
          text: value,
        });
      } catch {
        /* surfaced via missing turn in the transcript */
      }
    }

    async function hangUp() {
      if (!activeCall) return;
      try {
        await apiPost(`/api/calls/${activeCall.id}/end`);
      } catch {
        /* call_ended WS event also clears the active call */
      }
    }

    return (
      <div className="flex min-w-[22rem] flex-col gap-1 rounded-md border border-accent bg-surface p-2">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold text-accent">
            You are playing: {counterparty}
            {activeCall.counterparty_id != null
              ? ` #${activeCall.counterparty_id}`
              : ""}
          </span>
          <button
            type="button"
            onClick={hangUp}
            className="flex items-center gap-1 rounded-md bg-danger px-2 py-1 text-xs font-medium text-white"
          >
            <PhoneOff size={14} /> Hang up
          </button>
        </div>
        <div className="h-20 overflow-auto rounded bg-primary/60 p-2 text-xs">
          {callTurns.length === 0 ? (
            <span className="text-text/40">Waiting for the agent…</span>
          ) : (
            callTurns.map((turn, i) => (
              <div key={i} className="mb-1">
                <span
                  className={
                    turn.role === "agent"
                      ? "font-semibold text-accent"
                      : "font-semibold text-success"
                  }
                >
                  {turn.role === "agent" ? "Agent" : counterparty}:
                </span>{" "}
                <span className="text-text/80">{turn.text}</span>
              </div>
            ))
          )}
          <div ref={transcriptEndRef} />
        </div>
        <div className="flex items-center gap-1">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void sendTurn();
            }}
            placeholder={`Reply as ${counterparty}…`}
            className="flex-1 rounded-md border border-muted bg-primary px-2 py-1 text-sm text-text outline-none focus:border-accent"
          />
          <MicButton onResult={(t) => setText(t)} />
          <button
            type="button"
            onClick={() => void sendTurn()}
            className="flex items-center justify-center rounded-md bg-accent px-2 py-1.5 text-white"
            aria-label="Send reply"
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    );
  }

  // -- normal voice intake (00 §11) -----------------------------------------
  async function submit() {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    try {
      const res = await withTimeout(
        apiPost<unknown>("/api/voice/transcript", { text: value }),
        VOICE_TIMEOUT_MS,
        "Voice request timed out",
      );
      setResult(res);
    } catch (err) {
      setResult({ error: err instanceof Error ? err.message : "Voice request failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-w-[22rem] max-w-[42rem] flex-1 flex-col gap-1">
      <div className="flex items-start gap-1">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={1}
          placeholder={`e.g. "There's a parade on our street this Monday"`}
          className="h-8 flex-1 resize-none rounded-md border border-muted bg-primary px-2 py-1 text-sm text-text outline-none focus:border-accent"
        />
        <MicButton onResult={(t) => setText(t)} />
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy}
          className="flex items-center justify-center rounded-md bg-accent px-2 py-1.5 text-white disabled:opacity-50"
          aria-label="Submit voice transcript"
        >
          <Send size={16} />
        </button>
      </div>
      {result != null && <VoiceResultCard result={result} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Velocity slider
// ---------------------------------------------------------------------------

function VelocitySlider() {
  const [velocity, setVelocity] = useState(1.0);

  useEffect(() => {
    apiGet<SimSettings>("/api/sim/pos")
      .then((settings) => setVelocity(settings.velocity ?? 1.0))
      .catch(() => undefined);
  }, []);

  async function commit(value: number) {
    try {
      await apiPatch("/api/sim/pos", { velocity: value });
      await apiPost("/api/track-a/forecast/run").catch(() => undefined);
    } catch {
      /* ignore; the slider remains optimistic until the next settings read */
    }
  }

  return (
    <div className="flex items-center gap-2">
      <input
        type="range"
        min={0.1}
        max={3.0}
        step={0.1}
        value={velocity}
        onChange={(e) => setVelocity(Number(e.target.value))}
        onMouseUp={() => void commit(velocity)}
        onTouchEnd={() => void commit(velocity)}
        onKeyUp={() => void commit(velocity)}
        className="w-28 accent-accent"
      />
      <span className="w-10 text-sm tabular-nums text-text">{velocity.toFixed(1)}×</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Control bar root
// ---------------------------------------------------------------------------

// Status → optimistic state applied immediately on click so the pill reacts
// before the server confirms (play→running, pause→paused, stop→stopped).
const OPTIMISTIC: Partial<Record<string, SimStatus>> = {
  play: "running",
  pause: "paused",
  stop: "stopped",
};

export function ControlBar({
  onToggleInbox,
}: {
  onToggleInbox: () => void;
}) {
  const simState = useSimState();
  const wsConnected = useWsConnected();
  const approvals = useApprovals();
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  const status = simState?.status ?? "stopped";
  const speed = simState?.speed ?? 1;
  const isBusy = pendingAction !== null;

  async function sim(action: string) {
    if (isBusy) return;
    setPendingAction(action);
    const optimistic = OPTIMISTIC[action];
    if (optimistic) actions.setSimState({ status: optimistic });
    try {
      const result = await apiPost<Partial<SimState>>(`/api/sim/${action}`);
      actions.setSimState(result);
    } catch {
      // Revert: re-fetch the authoritative state.
      apiGet<Partial<SimState>>("/api/sim/state")
        .then((s) => actions.setSimState(s))
        .catch(() => undefined);
    } finally {
      setPendingAction(null);
    }
  }

  async function setSpeed(value: number) {
    // Optimistic: update speed immediately so buttons feel instant.
    actions.setSimState({ speed: value });
    try {
      const result = await apiPost<Partial<SimState>>("/api/sim/speed", {
        speed: value,
      });
      actions.setSimState(result);
    } catch {
      /* WS reconciles on next tick */
    }
  }

  return (
    <header className="sticky top-0 z-30 border-b border-muted bg-surface">
      <div className="flex flex-col gap-2 px-3 py-2">
        <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
          <Section label="Transport">
            <TransportButton
              onClick={() => void sim("play")}
              title="Play"
              active={status === "running" && !pendingAction}
              disabled={isBusy || status === "running"}
              pending={pendingAction === "play"}
            >
              <Play size={16} />
            </TransportButton>
            <TransportButton
              onClick={() => void sim("pause")}
              title="Pause"
              active={status === "paused" && !pendingAction}
              disabled={isBusy || status === "stopped"}
              pending={pendingAction === "pause"}
            >
              <Pause size={16} />
            </TransportButton>
            <TransportButton
              onClick={() => void sim("stop")}
              title="Stop"
              disabled={isBusy || status === "stopped"}
              pending={pendingAction === "stop"}
            >
              <Square size={16} />
            </TransportButton>
            <TransportButton
              onClick={() => void sim("restart")}
              title="Restart"
              disabled={isBusy}
              pending={pendingAction === "restart"}
            >
              <RotateCcw
                size={16}
                className={
                  pendingAction === "restart" ? "animate-spin" : undefined
                }
              />
            </TransportButton>
            <TransportButton
              onClick={() => void sim("step")}
              title="Step"
              disabled={isBusy || status === "running"}
              pending={pendingAction === "step"}
            >
              <StepForward size={16} />
            </TransportButton>
          </Section>

          <Section label="Speed">
            <select
              value={speed}
              onChange={(event) => void setSpeed(Number(event.target.value))}
              className="h-8 rounded-md border border-muted bg-primary px-2 text-sm font-medium text-text outline-none focus:border-accent"
            >
              {SPEEDS.map((s) => (
                <option key={s} value={s}>
                  {s}×
                </option>
              ))}
            </select>
          </Section>

          <Section label="Sim time">
            <span className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium tabular-nums text-text">
              {pendingAction === "restart" ? "Restarting…" : formatSimTime(simState)}
            </span>
            <StatusPill status={status} pendingAction={pendingAction} />
          </Section>

          <div className="min-w-[22rem] flex-1">
            <Section label="Voice console">
              <VoiceConsole />
            </Section>
          </div>

          <Section label="Velocity">
            <VelocitySlider />
          </Section>

          <Section label="WS">
            <span
              className={
                "flex items-center gap-1 rounded-md px-2 py-1.5 text-xs font-medium " +
                (wsConnected
                  ? "bg-success/20 text-success"
                  : "bg-danger/20 text-danger")
              }
              title={wsConnected ? "WebSocket connected" : "WebSocket disconnected"}
            >
              {wsConnected ? <Wifi size={14} /> : <WifiOff size={14} />}
              {wsConnected ? "connected" : "offline"}
            </span>
          </Section>

          <button
            type="button"
            onClick={onToggleInbox}
            className="relative flex h-9 w-9 items-center justify-center rounded-md bg-muted text-text hover:bg-muted/70"
            aria-label="Toggle approval inbox"
            title="Approval inbox"
          >
            <Bell size={18} />
            <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-bold text-white">
              {approvals.length}
            </span>
          </button>
        </div>
      </div>
    </header>
  );
}
