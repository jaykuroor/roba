import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import {
  Bell,
  Pause,
  Play,
  RotateCcw,
  Sparkles,
  Square,
  StepForward,
  Wifi,
  WifiOff,
} from "lucide-react";
import { apiGet, apiPatch, apiPost } from "../api";
import {
  actions,
  useApprovals,
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
  realtime_task: {
    dot: "bg-accent animate-pulse",
    label: "real-time task",
    textCls: "text-accent",
  },
};

function StatusPill({
  status,
  pendingAction,
  title,
}: {
  status: SimStatus | string;
  pendingAction: string | null;
  title?: string;
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
    <span
      className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium"
      title={title}
    >
      <span className={`h-2 w-2 rounded-full ${cfg.dot}`} />
      <span className={cfg.textCls}>{cfg.label}</span>
    </span>
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
  const [suggesting, setSuggesting] = useState(false);
  const [suggestNote, setSuggestNote] = useState<string | null>(null);

  const status = simState?.status ?? "stopped";
  const speed = simState?.speed ?? 1;
  const realtimeTasks = simState?.realtime_tasks ?? [];
  const realtimeActive = realtimeTasks.length > 0;
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

  async function suggestBatchChange() {
    if (suggesting) return;
    setSuggesting(true);
    setSuggestNote(null);
    try {
      // Runs the batch advisor; each proposal lands in the approval inbox as a
      // "batch" card. The bell count updates itself off the WS event.
      const r = await apiPost<{ created?: number }>(
        "/api/track-a/batches/suggest",
      );
      const n = r.created ?? 0;
      setSuggestNote(
        n > 0
          ? `${n} batch suggestion${n === 1 ? "" : "s"} in the inbox`
          : "no change to suggest",
      );
    } catch {
      setSuggestNote("advisor failed");
    } finally {
      setSuggesting(false);
      window.setTimeout(() => setSuggestNote(null), 8000);
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
              disabled={realtimeActive}
              onChange={(event) => void setSpeed(Number(event.target.value))}
              title={
                realtimeActive
                  ? `Locked during real-time task (${realtimeTasks.join(", ")}) — will resume at ${speed}×`
                  : undefined
              }
              className="h-8 rounded-md border border-muted bg-primary px-2 text-sm font-medium text-text outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-50"
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
            <StatusPill
              status={realtimeActive && status === "running" ? "realtime_task" : status}
              pendingAction={pendingAction}
              title={realtimeActive ? realtimeTasks.join(", ") : undefined}
            />
          </Section>

          <Section label="Velocity">
            <VelocitySlider />
          </Section>

          <Section label="Batches">
            <button
              type="button"
              onClick={() => void suggestBatchChange()}
              disabled={suggesting}
              title="Ask the batch advisor for a batch/quantity change — the proposal arrives as an approval card"
              className="flex h-8 items-center gap-1.5 rounded-md border border-muted bg-primary px-2.5 text-sm font-medium text-text hover:border-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Sparkles
                size={14}
                className={suggesting ? "animate-pulse" : undefined}
              />
              {suggesting ? "Thinking…" : "Suggest change"}
            </button>
            {suggestNote && (
              <span className="text-xs text-text/50">{suggestNote}</span>
            )}
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
