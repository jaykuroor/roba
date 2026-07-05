/**
 * DashboardView — unified operator dashboard with domain-grouped tabs.
 * Replaces PanelsView. All panel components are unchanged; only the tab
 * container changes — no Track A / Track B wording or group dividers.
 */
import { useState } from "react";
import { Radio } from "lucide-react";
import { useActiveCall } from "../store";
import { SpectateOverlay } from "./SpectateOverlay";
import { CompetitorPanel } from "../track_a/CompetitorPanel";
import { ForecastDashboard } from "../track_a/ForecastDashboard";
import { ReviewPanel } from "../track_a/ReviewPanel";
import { SignalFeed } from "../track_a/SignalFeed";
import { StaffPanel } from "../track_a/StaffPanel";
import { PosMonitor } from "../pos/PosMonitor";
import { TRACK_B_PANELS } from "../track_b";

// ---------------------------------------------------------------------------
// Tab definitions (flat strip, domain names — no "Track A / Track B")
// ---------------------------------------------------------------------------

interface TabDef {
  id: string;
  label: string;
}

const TABS: TabDef[] = [
  { id: "operations",  label: "Operations" },
  { id: "forecast",    label: "Forecast" },
  { id: "staff",       label: "Staff" },
  { id: "inventory",   label: "Inventory" },
  { id: "expiry",      label: "Expiry" },
  { id: "competitors", label: "Competitors" },
  { id: "reviews",     label: "Reviews" },
  { id: "suppliers",   label: "Suppliers" },
  { id: "procurement", label: "Procurement" },
  { id: "activity",    label: "Activity" },
  { id: "signals",     label: "Signals" },
];

function Panel({ id }: { id: string }) {
  switch (id) {
    case "operations":  return <PosMonitor />;
    case "forecast":    return <ForecastDashboard />;
    case "staff":       return <StaffPanel />;
    case "inventory": {
      const p = TRACK_B_PANELS.find((p) => p.name === "Inventory");
      return p ? <p.component /> : null;
    }
    case "expiry": {
      const p = TRACK_B_PANELS.find((p) => p.name === "Expiry");
      return p ? <p.component /> : null;
    }
    case "competitors": return <CompetitorPanel />;
    case "reviews":     return <ReviewPanel />;
    case "suppliers": {
      const p = TRACK_B_PANELS.find((p) => p.name === "Suppliers");
      return p ? <p.component /> : null;
    }
    case "procurement": {
      const p = TRACK_B_PANELS.find((p) => p.name === "Procurement");
      return p ? <p.component /> : null;
    }
    case "activity": {
      const p = TRACK_B_PANELS.find((p) => p.name === "Activity Log");
      return p ? <p.component /> : null;
    }
    case "signals": return <SignalFeed />;
    default: return <div className="text-text/30 text-sm p-4">Panel not found: {id}</div>;
  }
}

function TabButton({
  label,
  active,
  onClick,
  dot,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  dot?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "relative rounded-md px-3 py-1.5 text-sm font-medium",
        active
          ? "bg-accent text-white"
          : "text-text/60 hover:bg-muted/50 hover:text-text",
      ].join(" ")}
    >
      {label}
      {dot && (
        <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-accent animate-pulse ring-2 ring-primary" />
      )}
    </button>
  );
}

interface DashboardViewProps {
  /** When true, the panel is read-only (no side effects — reserved for the /panels route). */
  readOnly?: boolean;
}

export function DashboardView({ readOnly: _readOnly }: DashboardViewProps) {
  const [activeId, setActiveId] = useState(TABS[0].id);
  const [spectating, setSpectating] = useState(false);
  const activeCall = useActiveCall();

  // Pulsing dot: supplier call → suppliers tab; competitor call → competitors tab.
  const callDotTab =
    activeCall?.counterparty_type === "supplier"
      ? "suppliers"
      : activeCall?.counterparty_type === "competitor"
      ? "competitors"
      : null;

  return (
    <main className="px-4 py-4">
      <div className="flex flex-col gap-3">
        {/* Live-call banner — shown when a call is active */}
        {activeCall && (
          <div className="flex items-center gap-2 rounded-lg border border-accent/30 bg-accent/5 px-3 py-2">
            <Radio size={13} className="text-accent animate-pulse shrink-0" />
            <span className="flex-1 text-xs font-medium text-accent">
              Live {activeCall.counterparty_type} call in progress
              {activeCall.purpose ? ` — ${activeCall.purpose}` : ""}
            </span>
            <button
              onClick={() => setSpectating(true)}
              className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium text-accent hover:bg-accent/30"
            >
              Spectate
            </button>
          </div>
        )}
        {spectating && <SpectateOverlay onClose={() => setSpectating(false)} />}

        {/* Flat tab strip — no Track A / Track B labels */}
        <div className="flex flex-wrap items-center gap-1.5 rounded-lg bg-surface p-2">
          {TABS.map((tab) => (
            <TabButton
              key={tab.id}
              label={tab.label}
              active={activeId === tab.id}
              onClick={() => setActiveId(tab.id)}
              dot={callDotTab === tab.id}
            />
          ))}
        </div>

        {/* Panel */}
        <div className="min-h-[60vh]">
          <Panel id={activeId} />
        </div>
      </div>
    </main>
  );
}
