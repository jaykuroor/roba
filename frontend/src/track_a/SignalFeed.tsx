import { useState } from "react";
import { useSimState } from "../store";
import { formatSimTime, stationName } from "./helpers";
import { EmptyState, JsonPayload, Pill, TrackAShell } from "./ui";
import { useTrackAData } from "./useTrackAData";

export function SignalFeed() {
  const { data, loading, error } = useTrackAData();
  const sim = useSimState();
  const [openId, setOpenId] = useState<string | null>(null);

  if (loading) return <TrackAShell eyebrow="Track A" title="Signal Feed"><EmptyState label="Loading signals" /></TrackAShell>;
  if (error || !data) return <TrackAShell eyebrow="Track A" title="Signal Feed"><EmptyState label={error ?? "Signal data unavailable"} /></TrackAShell>;

  return (
    <TrackAShell eyebrow="Bus visibility" title="Signal feed">
      {data.signals.length === 0 ? (
        <EmptyState label="No live Track A signals yet." />
      ) : (
        <div className="space-y-3">
          {data.signals.map((signal) => {
            const open = openId === signal.signal_id;
            const expiresIn = signal.expires_at == null || sim?.sim_time == null ? null : Math.max(0, signal.expires_at - sim.sim_time);
            return (
              <article key={signal.signal_id} className="rounded-md border border-muted bg-primary/30">
                <button
                  type="button"
                  onClick={() => setOpenId(open ? null : signal.signal_id)}
                  className="flex w-full flex-wrap items-center justify-between gap-3 px-4 py-3 text-left"
                >
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold">{signal.type}</span>
                      <Pill tone={signal.priority >= 4 ? "bad" : signal.priority >= 3 ? "warn" : "neutral"}>
                        P{signal.priority}
                      </Pill>
                      <Pill>{signal.source}</Pill>
                    </div>
                    <div className="mt-1 text-sm text-text/55">
                      {signal.type === "STAFF_COVERAGE" && typeof signal.payload.station_id === "number"
                        ? stationName(data, signal.payload.station_id)
                        : signal.dedup_key ?? signal.signal_id}
                    </div>
                  </div>
                  <div className="text-right text-xs text-text/50">
                    <div>{formatSimTime(signal.created_at)}</div>
                    <div>{expiresIn == null ? "window-bound" : `${Math.ceil(expiresIn / 60)} sim min left`}</div>
                  </div>
                </button>
                {open ? (
                  <div className="border-t border-muted p-3">
                    <div className="mb-2 flex flex-wrap gap-1.5">
                      {(signal.groups ?? []).map((group) => <Pill key={group}>{group}</Pill>)}
                    </div>
                    <JsonPayload value={signal.payload} />
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
    </TrackAShell>
  );
}
