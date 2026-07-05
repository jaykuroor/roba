import { useState } from "react";
import { CompetitorPanel } from "../track_a/CompetitorPanel";
import { ForecastDashboard } from "../track_a/ForecastDashboard";
import { ReviewPanel } from "../track_a/ReviewPanel";
import { SignalFeed } from "../track_a/SignalFeed";
import { StaffPanel } from "../track_a/StaffPanel";
import { PosMonitor } from "../pos/PosMonitor";
import { TRACK_B_PANELS } from "../track_b";

// The tab strip + panel grid, extracted from the old single-page App so it can
// be shared by the Console (/) and Panels (/panels) routes.

const TRACK_A_TABS = ["Forecast", "Competitors", "Reviews", "Staff", "Signal Feed"];
const TRACK_B_TABS = ["Inventory", "Expiry", "Suppliers", "Procurement", "Activity Log"];

type Track = "A" | "B" | "POS";
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

export function PanelsView() {
  const [activeTab, setActiveTab] = useState<ActiveTab>({
    track: "POS",
    name: "POS Monitor",
  });

  return (
    <main className="px-4 py-4">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-4 rounded-lg bg-surface p-2">
          <div className="flex items-center gap-2">
            <span className="px-1 text-xs font-semibold uppercase tracking-wide text-text/40">
              POS
            </span>
            <TabButton
              label="POS Monitor"
              active={activeTab.track === "POS"}
              onClick={() => setActiveTab({ track: "POS", name: "POS Monitor" })}
            />
          </div>
          <div className="h-6 w-px bg-muted" />
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
          {activeTab.track === "POS" ? (
            <PosMonitor />
          ) : activeTab.track === "A" ? (
            <TrackAPanel label={activeTab.name} />
          ) : (
            <TrackBPanel label={activeTab.name} />
          )}
        </div>
      </div>
    </main>
  );
}
