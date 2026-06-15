import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import {
  Bell,
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
  useActiveCall,
  useApprovals,
  useCallTurns,
  useSimState,
  useWeather,
  useWsConnected,
} from "../store";
import type { Scenario, SimState, WeatherCondition } from "../types";

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];
const CONDITIONS: WeatherCondition[] = ["clear", "clouds", "rain", "storm", "snow"];
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

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
    <div className="flex flex-col gap-1">
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
  children,
}: {
  onClick: () => void;
  title: string;
  active?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      className={
        "flex h-8 w-8 items-center justify-center rounded-md " +
        (active
          ? "bg-accent text-white"
          : "bg-muted text-text hover:bg-muted/70")
      }
    >
      {children}
    </button>
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
            🎭 You are playing: {counterparty}
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
      const res = await apiPost<unknown>("/api/voice/transcript", { text: value });
      setResult(res);
    } catch {
      setResult({ error: "Voice request failed" });
    } finally {
      setBusy(false);
    }
  }

  const intent =
    result != null &&
    typeof result === "object" &&
    "extracted" in (result as Record<string, unknown>)
      ? ((result as { extracted?: { intent?: string } }).extracted?.intent ?? null)
      : null;

  return (
    <div className="flex min-w-[20rem] flex-col gap-1">
      <div className="flex items-start gap-1">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={1}
          placeholder={`e.g. "There's a parade on our street this Monday"`}
          className="h-8 flex-1 resize-y rounded-md border border-muted bg-primary px-2 py-1 text-sm text-text outline-none focus:border-accent"
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
      {result != null && (
        <div className="rounded bg-primary/60 p-2 text-xs">
          {intent && (
            <div className="mb-1 text-text">
              Intent:{" "}
              <span className="rounded bg-accent px-1.5 py-0.5 font-medium text-white">
                {intent}
              </span>
            </div>
          )}
          <pre className="max-h-24 overflow-auto text-text/60">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Weather override panel
// ---------------------------------------------------------------------------

function WeatherOverride() {
  const weather = useWeather();
  const [temp, setTemp] = useState(20);
  const [condition, setCondition] = useState<WeatherCondition>("clear");
  const [precip, setPrecip] = useState(0);
  const [wind, setWind] = useState(5);

  async function apply() {
    try {
      await apiPost("/api/weather/override", {
        temp_c: temp,
        condition,
        precip_mm: precip,
        wind_kph: wind,
      });
    } catch {
      /* ignore; weather_updated WS event reflects the applied value */
    }
  }

  return (
    <div className="flex items-end gap-1">
      <label className="flex flex-col text-[10px] text-text/40">
        °C
        <input
          type="number"
          value={temp}
          onChange={(e) => setTemp(Number(e.target.value))}
          className="w-14 rounded-md border border-muted bg-primary px-1 py-1 text-sm text-text outline-none focus:border-accent"
        />
      </label>
      <label className="flex flex-col text-[10px] text-text/40">
        Condition
        <select
          value={condition}
          onChange={(e) => setCondition(e.target.value as WeatherCondition)}
          className="rounded-md border border-muted bg-primary px-1 py-1 text-sm text-text outline-none focus:border-accent"
        >
          {CONDITIONS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col text-[10px] text-text/40">
        precip
        <input
          type="number"
          value={precip}
          onChange={(e) => setPrecip(Number(e.target.value))}
          className="w-12 rounded-md border border-muted bg-primary px-1 py-1 text-sm text-text outline-none focus:border-accent"
        />
      </label>
      <label className="flex flex-col text-[10px] text-text/40">
        wind
        <input
          type="number"
          value={wind}
          onChange={(e) => setWind(Number(e.target.value))}
          className="w-12 rounded-md border border-muted bg-primary px-1 py-1 text-sm text-text outline-none focus:border-accent"
        />
      </label>
      <button
        type="button"
        onClick={() => void apply()}
        className="rounded-md bg-muted px-2 py-1.5 text-sm text-text hover:bg-muted/70"
      >
        Set
      </button>
      <span className="pb-1.5 text-xs text-text/60">
        now: {weather ? weather.condition : "—"}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scenario + seed pickers
// ---------------------------------------------------------------------------

function ScenarioPicker() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);

  useEffect(() => {
    apiGet<Scenario[]>("/api/scenarios")
      .then(setScenarios)
      .catch(() => undefined);
  }, []);

  const activeId = scenarios.find((s) => s.is_active)?.id ?? "";

  async function onChange(value: string) {
    const prevActive = scenarios.find((s) => s.is_active);
    try {
      if (value === "") {
        if (prevActive) await apiPost(`/api/scenarios/${prevActive.id}/deactivate`);
      } else {
        if (prevActive && prevActive.id !== Number(value)) {
          await apiPost(`/api/scenarios/${prevActive.id}/deactivate`);
        }
        await apiPost(`/api/scenarios/${value}/activate`);
      }
      const refreshed = await apiGet<Scenario[]>("/api/scenarios");
      setScenarios(refreshed);
    } catch {
      /* ignore */
    }
  }

  return (
    <select
      value={activeId}
      onChange={(e) => void onChange(e.target.value)}
      className="rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
    >
      <option value="">No scenario</option>
      {scenarios.map((s) => (
        <option key={s.id} value={s.id}>
          {s.name}
        </option>
      ))}
    </select>
  );
}

function SeedPicker() {
  const [presets, setPresets] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    apiGet<string[]>("/api/seed/presets")
      .then((rows) => {
        setPresets(rows);
        if (rows.length > 0) setSelected(rows[0]);
      })
      .catch(() => undefined);
  }, []);

  async function load() {
    if (!selected || loading) return;
    setLoading(true);
    try {
      await apiPost(`/api/seed/preset/${selected}`);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex items-center gap-1">
      <select
        value={selected}
        onChange={(e) => setSelected(e.target.value)}
        className="rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
      >
        {presets.length === 0 && <option value="">no presets</option>}
        {presets.map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => void load()}
        disabled={!selected || loading}
        className="rounded-md bg-muted px-2 py-1.5 text-sm text-text hover:bg-muted/70 disabled:opacity-50"
      >
        {loading ? "Loading…" : "Load"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Velocity slider
// ---------------------------------------------------------------------------

function VelocitySlider() {
  const [velocity, setVelocity] = useState(1.0);

  function commit(value: number) {
    apiPatch("/api/sim/pos", { velocity: value }).catch(() => undefined);
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
        onMouseUp={() => commit(velocity)}
        onTouchEnd={() => commit(velocity)}
        onKeyUp={() => commit(velocity)}
        className="w-28 accent-accent"
      />
      <span className="w-10 text-sm tabular-nums text-text">{velocity.toFixed(1)}×</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Control bar root
// ---------------------------------------------------------------------------

export function ControlBar({ onToggleInbox }: { onToggleInbox: () => void }) {
  const simState = useSimState();
  const wsConnected = useWsConnected();
  const approvals = useApprovals();

  const status = simState?.status ?? "stopped";
  const speed = simState?.speed ?? 1;

  function sim(action: string) {
    apiPost(`/api/sim/${action}`).catch(() => undefined);
  }

  function setSpeed(value: number) {
    apiPost("/api/sim/speed", { speed: value }).catch(() => undefined);
  }

  return (
    <header className="sticky top-0 z-30 border-b border-muted bg-surface">
      <div className="flex min-h-18 flex-wrap items-end gap-4 px-4 py-2">
        <Section label="Transport">
          <TransportButton
            onClick={() => sim("play")}
            title="Play"
            active={status === "running"}
          >
            <Play size={16} />
          </TransportButton>
          <TransportButton
            onClick={() => sim("pause")}
            title="Pause"
            active={status === "paused"}
          >
            <Pause size={16} />
          </TransportButton>
          <TransportButton onClick={() => sim("stop")} title="Stop">
            <Square size={16} />
          </TransportButton>
          <TransportButton onClick={() => sim("restart")} title="Restart">
            <RotateCcw size={16} />
          </TransportButton>
          <TransportButton onClick={() => sim("step")} title="Step">
            <StepForward size={16} />
          </TransportButton>
        </Section>

        <Section label="Speed">
          {SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setSpeed(s)}
              className={
                "rounded-md px-2 py-1.5 text-xs font-medium " +
                (speed === s
                  ? "bg-accent text-white"
                  : "bg-muted text-text hover:bg-muted/70")
              }
            >
              {s}×
            </button>
          ))}
        </Section>

        <Section label="Sim time">
          <span className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium tabular-nums text-text">
            {formatSimTime(simState)}
          </span>
          <span className="ml-1 rounded-md bg-primary px-2 py-1.5 text-xs uppercase text-text/60">
            {status}
          </span>
        </Section>

        <Section label="Velocity">
          <VelocitySlider />
        </Section>

        <Section label="Scenario">
          <ScenarioPicker />
        </Section>

        <Section label="Seed preset">
          <SeedPicker />
        </Section>

        <Section label="Weather override">
          <WeatherOverride />
        </Section>

        <Section label="Voice console">
          <VoiceConsole />
        </Section>

        <div className="ml-auto flex items-end gap-3">
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
