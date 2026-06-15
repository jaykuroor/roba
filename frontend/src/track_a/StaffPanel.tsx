import { RefreshCw, UserMinus, UserCheck } from "lucide-react";
import { apiPost } from "../api";
import { latestCoverageSignals, menuByStation } from "./helpers";
import { EmptyState, Pill, TrackAShell } from "./ui";
import { useTrackAData } from "./useTrackAData";

export function StaffPanel() {
  const { data, loading, error, refresh } = useTrackAData();

  async function recompute() {
    await apiPost("/api/track-a/staff/recompute");
    await refresh();
  }

  async function setStation(stationId: number, status: "sick" | "present") {
    await apiPost("/api/track-a/staff/call-in-sick", {
      station_id: stationId,
      status,
      reason: status === "sick" ? "demo call in sick" : "demo restored coverage",
    });
    await refresh();
  }

  if (loading) return <TrackAShell eyebrow="Track A" title="Staff"><EmptyState label="Loading staff coverage" /></TrackAShell>;
  if (error || !data) return <TrackAShell eyebrow="Track A" title="Staff"><EmptyState label={error ?? "Staff data unavailable"} /></TrackAShell>;

  const coverage = latestCoverageSignals(data);

  return (
    <TrackAShell
      eyebrow="Station capacity"
      title="Staff coverage"
      action={
        <button type="button" onClick={recompute} className="inline-flex items-center gap-2 rounded-md border border-muted px-3 py-2 text-sm text-text/80 hover:bg-muted/50">
          <RefreshCw size={16} />
          Recompute
        </button>
      }
    >
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {data.stations.map((station) => {
          const signal = coverage.find((row) => Number(row.payload.station_id) === station.id);
          const covered = signal ? Boolean(signal.payload.covered) : true;
          const roster = data.staff_stations
            .filter((link) => link.station_id === station.id)
            .map((link) => data.staff.find((member) => member.id === link.staff_id))
            .filter(Boolean);
          return (
            <article key={station.id} className="rounded-md border border-muted bg-primary/30 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-lg font-semibold">{station.name}</h3>
                  <div className="mt-1 text-sm text-text/55">
                    {menuByStation(data, station.id).map((item) => item.name).join(", ") || "No active items"}
                  </div>
                </div>
                <Pill tone={covered ? "good" : "bad"}>{covered ? "Covered" : "Uncovered"}</Pill>
              </div>
              <div className="mt-4 space-y-2">
                {roster.map((member) => (
                  <div key={member!.id} className="flex items-center justify-between rounded bg-surface/70 px-3 py-2 text-sm">
                    <span>{member!.name}</span>
                    <span className="text-text/50">{member!.role}</span>
                  </div>
                ))}
              </div>
              <div className="mt-4 grid grid-cols-2 gap-2">
                <button type="button" onClick={() => setStation(station.id, "sick")} className="inline-flex items-center justify-center gap-2 rounded-md border border-danger/50 px-3 py-2 text-sm text-danger hover:bg-danger/10">
                  <UserMinus size={16} />
                  Sick
                </button>
                <button type="button" onClick={() => setStation(station.id, "present")} className="inline-flex items-center justify-center gap-2 rounded-md border border-success/50 px-3 py-2 text-sm text-success hover:bg-success/10">
                  <UserCheck size={16} />
                  Restore
                </button>
              </div>
            </article>
          );
        })}
      </div>

      <div className="mt-4 rounded-md border border-muted bg-primary/30 p-4">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-text/50">Recent attendance</h3>
        <div className="mt-3 flex flex-wrap gap-2">
          {data.attendance.length === 0 ? (
            <span className="text-sm text-text/50">No exceptions recorded.</span>
          ) : (
            data.attendance.slice(0, 12).map((row) => (
              <Pill key={row.id} tone={row.status === "present" ? "good" : "warn"}>
                {row.staff_id ? `Staff ${row.staff_id}` : "Unknown"} · {row.status} · {row.daypart ?? "day"}
              </Pill>
            ))
          )}
        </div>
      </div>
    </TrackAShell>
  );
}
