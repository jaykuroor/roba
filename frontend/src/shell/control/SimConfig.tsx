import { useEffect, useState } from "react";
import { apiGet, apiPatch } from "../../api";
import { useSimState } from "../../store";
import type { InventorySignalPolicy, SimState } from "../../types";
import { SectionHeading, ApplyButton } from "./shared";

export function SimConfigPanel() {
  const simState = useSimState();

  // Operating window (open/close hours within a sim day)
  const [openHour, setOpenHour] = useState(8);
  const [closeHour, setCloseHour] = useState(23);
  const [skipClosed, setSkipClosed] = useState(true);
  const [callMode, setCallMode] = useState<"freeze" | "slow">("freeze");
  const [inventoryPolicy, setInventoryPolicy] = useState<InventorySignalPolicy | null>(null);
  const [policyBusy, setPolicyBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    apiGet<SimState>("/api/sim/state").then((s) => {
      const win = s.operating_window;
      if (win) {
        setOpenHour(Math.floor((win.open ?? 28800) / 3600));
        setCloseHour(Math.floor((win.close ?? 82800) / 3600));
      }
      setSkipClosed(s.skip_closed_hours ?? true);
      setCallMode((s.call_mode as "freeze" | "slow") ?? "freeze");
    }).catch(() => undefined);
    apiGet<InventorySignalPolicy>("/api/runtime/inventory-signal-policy")
      .then(setInventoryPolicy)
      .catch(() => undefined);
  }, []);

  async function apply() {
    setBusy(true);
    try {
      await apiPatch("/api/sim/state", {
        operating_window: {
          open: openHour * 3600,
          close: closeHour * 3600,
        },
        skip_closed_hours: skipClosed,
        call_mode: callMode,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch { /* ignore */ } finally { setBusy(false); }
  }

  async function setShortageSignals(enabled: boolean) {
    setPolicyBusy(true);
    try {
      const next = await apiPatch<InventorySignalPolicy>(
        "/api/runtime/inventory-signal-policy",
        { shortage_signals_enabled: enabled },
      );
      setInventoryPolicy(next);
    } catch { /* server state remains source of truth */ } finally { setPolicyBusy(false); }
  }

  const shortageSignalsEnabled = inventoryPolicy?.shortage_signals_enabled ?? true;

  return (
    <div className="space-y-6">
      <div>
        <SectionHeading>Operating Hours</SectionHeading>
        <p className="mb-3 text-[10px] text-text/40">
          The sim day window. Outside these hours the POS stops and (if skip enabled) the clock jumps to the next open hour.
          Current sim time: <span className="text-text/70">{simState?.status ?? "—"}</span>
        </p>
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase text-text/40">Open (hour, 0–23)</span>
            <input
              type="number" min={0} max={22} value={openHour}
              onChange={(e) => setOpenHour(Number(e.target.value))}
              className="w-20 rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] font-medium uppercase text-text/40">Close (hour, 1–24)</span>
            <input
              type="number" min={1} max={24} value={closeHour}
              onChange={(e) => setCloseHour(Number(e.target.value))}
              className="w-20 rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
          </label>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <input
          type="checkbox" id="skip-closed" checked={skipClosed}
          onChange={(e) => setSkipClosed(e.target.checked)}
          className="h-4 w-4 accent-accent"
        />
        <label htmlFor="skip-closed" className="text-sm text-text">
          Skip closed hours (jump clock from close → next open)
        </label>
      </div>

      <div>
        <SectionHeading>Call Mode</SectionHeading>
        <p className="mb-2 text-[10px] text-text/40">
          How the sim clock behaves during an agent phone call.
        </p>
        <div className="flex gap-4">
          {(["freeze", "slow"] as const).map((mode) => (
            <label key={mode} className="flex items-center gap-2 text-sm text-text">
              <input
                type="radio" name="call-mode" value={mode}
                checked={callMode === mode}
                onChange={() => setCallMode(mode)}
                className="accent-accent"
              />
              <span className="capitalize">{mode}</span>
              <span className="text-xs text-text/40">
                {mode === "freeze" ? "— pause sim entirely" : "— clamp to 0.1×"}
              </span>
            </label>
          ))}
        </div>
      </div>

      <div>
        <SectionHeading>Inventory Signals</SectionHeading>
        <div className="flex items-center justify-between gap-4 rounded-md border border-muted bg-primary px-3 py-2">
          <div>
            <div className="text-sm font-medium text-text">Shortage signal output</div>
            <div className="text-[10px] text-text/40">LOW_STOCK and STOCKOUT_RISK</div>
          </div>
          <button
            type="button"
            disabled={policyBusy || inventoryPolicy == null}
            onClick={() => void setShortageSignals(!shortageSignalsEnabled)}
            className={
              "relative h-7 w-12 rounded-full border transition-colors disabled:opacity-50 " +
              (shortageSignalsEnabled ? "border-success bg-success/30" : "border-muted bg-muted")
            }
            aria-label="Toggle shortage signals"
            title="Toggle shortage signals"
          >
            <span
              className={
                "absolute top-0.5 h-5 w-5 rounded-full bg-white transition-transform " +
                (shortageSignalsEnabled ? "translate-x-5" : "translate-x-0.5")
              }
            />
          </button>
        </div>
      </div>

      <ApplyButton onClick={() => void apply()} busy={busy} label={saved ? "✓ Saved" : "Apply"} />
    </div>
  );
}
