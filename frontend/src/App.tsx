import { useEffect, useState } from "react";
import { ControlBar } from "./shell/ControlBar";
import { ApprovalInbox } from "./shell/ApprovalInbox";
import { wsClient } from "./ws";
import { CompetitorPanel } from "./track_a/CompetitorPanel";
import { ForecastDashboard } from "./track_a/ForecastDashboard";
import { ReviewPanel } from "./track_a/ReviewPanel";
import { SignalFeed } from "./track_a/SignalFeed";
import { StaffPanel } from "./track_a/StaffPanel";

// Track tab groups (00 §23). The real panels are mounted by the two
// programmers into these slots; here they are empty labelled placeholders.
const TRACK_A_TABS = ["Forecast", "Competitors", "Reviews", "Staff", "Signal Feed"];
const TRACK_B_TABS = ["Inventory", "Expiry", "Suppliers", "Activity Log"];

type Track = "A" | "B";
interface ActiveTab {
  track: Track;
  name: string;
}

function TrackAPanel({ label }: { label: string }) {
  if (label === "Forecast") return <ForecastDashboard />;
  if (label === "Competitors") return <CompetitorPanel />;
  if (label === "Reviews") return <ReviewPanel />;
  if (label === "Staff") return <StaffPanel />;
  if (label === "Signal Feed") return <SignalFeed />;
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

function TrackBPlaceholder({ label }: { label: string }) {
  return (
    <div
      data-track="b"
      data-panel={label}
      className="flex h-full items-center justify-center rounded-lg border border-dashed border-muted bg-surface/40 text-text/40"
    >
      <span className="text-sm">Track B · {label}</span>
    </div>
  );
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
  const [activeTab, setActiveTab] = useState<ActiveTab>({
    track: "A",
    name: TRACK_A_TABS[0],
  });

  useEffect(() => {
    wsClient.connect();
    return () => wsClient.close();
  }, []);

  return (
    <div className="min-h-full bg-primary text-text">
      <ControlBar onToggleInbox={() => setInboxOpen((open) => !open)} />

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
              <TrackAPanel label={activeTab.name} />
            ) : (
              <TrackBPlaceholder label={activeTab.name} />
            )}
          </div>
        </div>
      </main>

      <ApprovalInbox open={inboxOpen} onClose={() => setInboxOpen(false)} />
    </div>
  );
}
