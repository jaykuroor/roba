import { useEffect, useState } from "react";
import { ControlBar } from "./shell/ControlBar";
import { ApprovalInbox } from "./shell/ApprovalInbox";
import { SettingsDrawer } from "./shell/SettingsDrawer";
import { wsClient } from "./ws";
import { apiGet } from "./api";
import { actions } from "./store";
import type { SimState, Weather } from "./types";
import { TRACK_B_PANELS } from "./track_b";

// Track tab groups (00 §23). Track A panels are still empty labelled
// placeholders; Track B mounts its four real panels from the track_b registry.
const TRACK_A_TABS = ["Forecast", "Competitors", "Reviews", "Staff", "Signal Feed"];
const TRACK_B_TABS = TRACK_B_PANELS.map((p) => p.name);

type Track = "A" | "B";
interface ActiveTab {
  track: Track;
  name: string;
}

function TrackAPlaceholder({ label }: { label: string }) {
  return (
    <div
      data-track="a"
      data-panel={label}
      className="flex h-full items-center justify-center rounded-lg border border-dashed border-muted bg-surface/40 text-text/40"
    >
      <span className="text-sm">Track A · {label}</span>
    </div>
  );
}

function TrackBPanel({ label }: { label: string }) {
  const panel = TRACK_B_PANELS.find((p) => p.name === label);
  if (!panel) return null;
  const Panel = panel.component;
  return <Panel />;
}

function TabButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        active
          ? "rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white"
          : "rounded-md px-3 py-1.5 text-sm font-medium text-text/60 hover:bg-muted/50 hover:text-text"
      }
    >
      {label}
    </button>
  );
}

export default function App() {
  const [inboxOpen, setInboxOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<ActiveTab>({
    track: "A",
    name: TRACK_A_TABS[0],
  });

  useEffect(() => {
    wsClient.connect();
    // Hydrate from REST so the bar is populated before the first WS tick.
    apiGet<Partial<SimState>>("/api/sim/state")
      .then((s) => actions.setSimState(s))
      .catch(() => undefined);
    apiGet<Weather>("/api/weather")
      .then((w) => actions.setWeather(w))
      .catch(() => undefined);
    return () => wsClient.close();
  }, []);

  return (
    <div className="min-h-full bg-primary text-text">
      <ControlBar
        onToggleInbox={() => setInboxOpen((open) => !open)}
        onToggleSettings={() => setSettingsOpen((open) => !open)}
      />

      <main className="px-4 py-4">
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-4 rounded-lg bg-surface p-2">
            <div className="flex items-center gap-2">
              <span className="px-1 text-xs font-semibold uppercase tracking-wide text-text/40">
                Track A
              </span>
              {TRACK_A_TABS.map((name) => (
                <TabButton
                  key={name}
                  label={name}
                  active={activeTab.track === "A" && activeTab.name === name}
                  onClick={() => setActiveTab({ track: "A", name })}
                />
              ))}
            </div>
            <div className="h-6 w-px bg-muted" />
            <div className="flex items-center gap-2">
              <span className="px-1 text-xs font-semibold uppercase tracking-wide text-text/40">
                Track B
              </span>
              {TRACK_B_TABS.map((name) => (
                <TabButton
                  key={name}
                  label={name}
                  active={activeTab.track === "B" && activeTab.name === name}
                  onClick={() => setActiveTab({ track: "B", name })}
                />
              ))}
            </div>
          </div>

          <div className="min-h-[60vh]">
            {activeTab.track === "A" ? (
              <TrackAPlaceholder label={activeTab.name} />
            ) : (
              <TrackBPanel label={activeTab.name} />
            )}
          </div>
        </div>
      </main>

      <ApprovalInbox open={inboxOpen} onClose={() => setInboxOpen(false)} />
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
