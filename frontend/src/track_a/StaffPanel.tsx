import { RefreshCw, UserCheck, UserMinus, UserX } from "lucide-react";
import { apiPost } from "../api";
import { latestCoverageSignals, menuByStation } from "./helpers";
import { EmptyState, Pill, TrackAShell } from "./ui";
import { useTrackAData } from "./useTrackAData";
import type { Attendance } from "./types";

function currentStaffStatus(
  staffId: number,
  attendance: Attendance[],
): "present" | "leave" | "sick" {
  const rows = attendance
    .filter((r) => r.staff_id === staffId)
    .sort((a, b) => b.sim_time - a.sim_time);
  return (rows[0]?.status as "present" | "leave" | "sick") ?? "present";
}

export function StaffPanel() {
  const { data, loading, error, refresh } = useTrackAData();

  async function recompute() {
    await apiPost("/api/track-a/staff/recompute");
    await refresh();
  }

  async function setStaffStatus(staffId: number, status: "present" | "leave" | "sick") {
    await apiPost("/api/track-a/staff/call-in-sick", {
      staff_id: staffId,
      status,
      reason:
        status === "sick"
          ? "demo call in sick"
          : status === "leave"
            ? "demo on leave"
            : "demo restored coverage",
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
        <button type="button" onClick={() => void recompute()} className="inline-flex items-center gap-2 rounded-md border border-muted px-3 py-2 text-sm text-text/80 hover:bg-muted/50">
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
              {!covered && (
                <p className="mt-2 text-xs font-semibold text-danger">UNSTAFFED — dishes disabled</p>
              )}
              <div className="mt-4 space-y-2">
                {roster.map((member) => {
                  const staffStatus = currentStaffStatus(member!.id, data.attendance);
                  return (
                    <div key={member!.id} className="flex items-center justify-between rounded bg-surface/70 px-3 py-2 text-sm">
                      <div className="flex items-center gap-2">
                        <span>{member!.name}</span>
                        <span className="text-text/50">{member!.role}</span>
                        <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                          staffStatus === "present"
                            ? "bg-success/20 text-success"
                            : staffStatus === "leave"
                              ? "bg-warning/20 text-warning"
                              : "bg-danger/20 text-danger"
                        }`}>
                          {staffStatus === "present" ? "Present" : staffStatus === "leave" ? "On Leave" : "Sick"}
                        </span>
                      </div>
                      <div className="flex items-center gap-1">
                        {staffStatus === "present" ? (
                          <>
                            <button
                              type="button"
                              onClick={() => void setStaffStatus(member!.id, "leave")}
                              className="inline-flex items-center gap-1 rounded border border-warning/50 px-2 py-0.5 text-xs text-warning hover:bg-warning/10"
                            >
                              <UserMinus size={10} /> Leave
                            </button>
                            <button
                              type="button"
                              onClick={() => void setStaffStatus(member!.id, "sick")}
                              className="inline-flex items-center gap-1 rounded border border-danger/50 px-2 py-0.5 text-xs text-danger hover:bg-danger/10"
                            >
                              <UserX size={10} /> Sick
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => void setStaffStatus(member!.id, "present")}
                            className="inline-flex items-center gap-1 rounded border border-success/50 px-2 py-0.5 text-xs text-success hover:bg-success/10"
                          >
                            <UserCheck size={10} /> Restore
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
                {roster.length === 0 && (
                  <p className="text-xs text-text/40">No staff assigned</p>
                )}
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
            data.attendance.slice(0, 12).map((row) => {
              const member = data.staff.find((m) => m.id === row.staff_id);
              return (
                <Pill
                  key={row.id}
                  tone={row.status === "present" ? "good" : row.status === "sick" ? "bad" : "warn"}
                >
                  {member?.name ?? `Staff ${row.staff_id}`} · {row.status} · {row.daypart ?? "day"}
                </Pill>
              );
            })
          )}
        </div>
      </div>
    </TrackAShell>
  );
}
