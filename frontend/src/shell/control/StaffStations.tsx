import { useEffect, useState } from "react";
import { Save, UserX, Users } from "lucide-react";
import { apiGet, apiPatch, apiPost } from "../../api";
import type { EntityRow, Station } from "../../types";
import { SectionHeading } from "./shared";

interface StaffRow extends EntityRow {
  id: number;
  name: string;
  role: string | null;
  skill_level: number | null;
  hourly_cost: number | null;
  active: number;
}

function StationsPanel() {
  const [stations, setStations] = useState<Station[]>([]);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    apiGet<Station[]>("/api/stations").then(setStations).catch(() => undefined);
  }, []);

  async function create() {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const created = await apiPost<Station>("/api/stations", { name: newName.trim() });
      setStations((prev) => [...prev, created]);
      setNewName("");
    } catch { /* ignore */ } finally { setCreating(false); }
  }

  async function saveName(station: Station, name: string) {
    if (!name.trim() || name === station.name) return;
    const updated = await apiPatch<Station>(`/api/stations/${station.id}`, { name });
    setStations((prev) => prev.map((s) => s.id === station.id ? updated : s));
  }

  return (
    <div>
      <SectionHeading>Kitchen Stations</SectionHeading>
      <div className="space-y-2">
        {stations.map((s) => (
          <StationRow key={s.id} station={s} onSave={(name) => void saveName(s, name)} />
        ))}
        {stations.length === 0 && <p className="text-sm text-text/40">No stations. Load a seed or add one.</p>}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <input
          value={newName} onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void create(); }}
          placeholder="New station name…"
          className="flex-1 max-w-xs rounded-md border border-muted bg-primary px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
        />
        <button type="button" onClick={() => void create()} disabled={creating || !newName.trim()}
          className="rounded-md bg-accent px-3 py-1.5 text-sm text-white disabled:opacity-50">
          {creating ? "…" : "Add Station"}
        </button>
      </div>
    </div>
  );
}

function StationRow({ station, onSave }: { station: Station; onSave: (name: string) => void }) {
  const [name, setName] = useState(station.name);
  const dirty = name !== station.name;

  return (
    <div className="flex items-center gap-2 rounded border border-muted/60 bg-surface px-3 py-1.5">
      <input
        value={name} onChange={(e) => setName(e.target.value)}
        className="flex-1 bg-transparent text-sm text-text outline-none"
      />
      {dirty && (
        <button type="button" onClick={() => onSave(name)}
          className="flex items-center gap-1 rounded bg-accent px-2 py-0.5 text-[10px] text-white">
          <Save size={10} /> Save
        </button>
      )}
    </div>
  );
}

interface AttendanceRow {
  id: number;
  staff_id: number | null;
  date_sim_day: number;
  status: string;
  daypart: string | null;
  sim_time: number;
}

interface StaffStationLink {
  id: number;
  staff_id: number;
  station_id: number;
}

function currentStaffStatus(
  staffId: number,
  attendance: AttendanceRow[],
): "present" | "leave" | "sick" {
  const rows = attendance
    .filter((r) => r.staff_id === staffId)
    .sort((a, b) => b.sim_time - a.sim_time);
  return (rows[0]?.status as "present" | "leave" | "sick") ?? "present";
}

function StaffPanel() {
  const [staff, setStaff] = useState<StaffRow[]>([]);
  const [edits, setEdits] = useState<Record<number, Partial<StaffRow>>>({});
  const [busyId, setBusyId] = useState<number | null>(null);
  const [statusBusy, setStatusBusy] = useState<number | null>(null);
  const [attendance, setAttendance] = useState<AttendanceRow[]>([]);
  const [staffStations, setStaffStations] = useState<StaffStationLink[]>([]);
  const [stations, setStations] = useState<Station[]>([]);

  async function refreshTrackA() {
    try {
      const snap = await apiGet<{
        attendance: AttendanceRow[];
        staff_stations: StaffStationLink[];
        stations: Station[];
      }>("/api/track-a/snapshot");
      setAttendance(snap.attendance ?? []);
      setStaffStations(snap.staff_stations ?? []);
      setStations(snap.stations ?? []);
    } catch { /* ignore */ }
  }

  useEffect(() => {
    apiGet<StaffRow[]>("/api/staff").then(setStaff).catch(() => undefined);
    void refreshTrackA();
  }, []);

  function patchRow(id: number, patch: Partial<StaffRow>) {
    setEdits((prev) => ({ ...prev, [id]: { ...(prev[id] ?? {}), ...patch } }));
  }

  async function saveRow(s: StaffRow) {
    const patch = edits[s.id];
    if (!patch || Object.keys(patch).length === 0) return;
    setBusyId(s.id);
    try {
      const updated = await apiPatch<StaffRow>(`/api/staff/${s.id}`, patch);
      setStaff((prev) => prev.map((r) => r.id === s.id ? { ...r, ...updated } : r));
      setEdits((prev) => { const n = { ...prev }; delete n[s.id]; return n; });
    } catch { /* ignore */ } finally { setBusyId(null); }
  }

  async function setStatus(staffId: number, status: "present" | "leave" | "sick") {
    setStatusBusy(staffId);
    try {
      await apiPost("/api/track-a/staff/call-in-sick", { staff_id: staffId, status });
      await refreshTrackA();
    } catch { /* ignore */ } finally { setStatusBusy(null); }
  }

  async function recompute() {
    await apiPost("/api/track-a/staff/recompute").catch(() => undefined);
    await refreshTrackA();
  }

  function val(s: StaffRow, key: keyof StaffRow) {
    return String((edits[s.id]?.[key] ?? s[key]) ?? "");
  }

  // Derive station coverage from staff_stations + current attendance.
  const stationCoverage = stations.map((station) => {
    const links = staffStations.filter((l) => l.station_id === station.id);
    const covered =
      links.length === 0 ||
      links.some((l) => currentStaffStatus(l.staff_id, attendance) === "present");
    return { station, covered };
  });

  return (
    <div>
      <div className="mb-3 flex items-center gap-3">
        <SectionHeading>Staff</SectionHeading>
        <button type="button" onClick={() => void recompute()}
          className="flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs text-text hover:bg-muted/70">
          <Users size={12} /> Recompute coverage
        </button>
      </div>
      <div className="overflow-x-auto rounded-lg border border-muted">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-muted bg-surface/60">
              {["Name", "Role", "Skill", "Cost/hr", "Active", "Status", "Actions"].map((h) => (
                <th key={h} className="px-2 py-1.5 font-medium text-text/50">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {staff.map((s) => {
              const hasPatch = Object.keys(edits[s.id] ?? {}).length > 0;
              const merged = { ...s, ...(edits[s.id] ?? {}) };
              const status = currentStaffStatus(s.id, attendance);
              const isBusy = statusBusy === s.id;
              return (
                <tr key={s.id} className="border-b border-muted/40 last:border-0 hover:bg-surface/20">
                  <td className="px-2 py-1">
                    <input value={val(s, "name")} onChange={(e) => patchRow(s.id, { name: e.target.value })}
                      className="w-28 bg-transparent outline-none focus:border-b focus:border-accent" />
                  </td>
                  <td className="px-2 py-1">
                    <input value={val(s, "role")} onChange={(e) => patchRow(s.id, { role: e.target.value })}
                      className="w-24 bg-transparent outline-none focus:border-b focus:border-accent" />
                  </td>
                  <td className="px-2 py-1">
                    <input type="number" min={1} max={5} value={val(s, "skill_level")}
                      onChange={(e) => patchRow(s.id, { skill_level: Number(e.target.value) })}
                      className="w-12 bg-transparent outline-none focus:border-b focus:border-accent" />
                  </td>
                  <td className="px-2 py-1">
                    <input type="number" step="0.01" value={val(s, "hourly_cost")}
                      onChange={(e) => patchRow(s.id, { hourly_cost: Number(e.target.value) })}
                      className="w-16 bg-transparent outline-none focus:border-b focus:border-accent" />
                  </td>
                  <td className="px-2 py-1">
                    <input type="checkbox" checked={merged.active === 1}
                      onChange={(e) => patchRow(s.id, { active: e.target.checked ? 1 : 0 })}
                      className="accent-accent" />
                  </td>
                  <td className="px-2 py-1">
                    <span className={`inline-block rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                      status === "present"
                        ? "bg-success/20 text-success"
                        : status === "leave"
                          ? "bg-warning/20 text-warning"
                          : "bg-danger/20 text-danger"
                    }`}>
                      {status === "present" ? "Present" : status === "leave" ? "On Leave" : "Sick"}
                    </span>
                  </td>
                  <td className="flex items-center gap-1 px-2 py-1">
                    {hasPatch && (
                      <button type="button" onClick={() => void saveRow(s)} disabled={busyId === s.id}
                        className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-white disabled:opacity-50">
                        {busyId === s.id ? "…" : "Save"}
                      </button>
                    )}
                    {status === "present" ? (
                      <>
                        <button type="button" onClick={() => void setStatus(s.id, "leave")} disabled={isBusy}
                          title="Mark on leave"
                          className="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] text-warning hover:bg-warning/30 disabled:opacity-50">
                          {isBusy ? "…" : "Leave"}
                        </button>
                        <button type="button" onClick={() => void setStatus(s.id, "sick")} disabled={isBusy}
                          title="Call in sick"
                          className="flex items-center gap-0.5 rounded bg-danger/20 px-1.5 py-0.5 text-[10px] text-danger hover:bg-danger/30 disabled:opacity-50">
                          <UserX size={10} /> Sick
                        </button>
                      </>
                    ) : (
                      <button type="button" onClick={() => void setStatus(s.id, "present")} disabled={isBusy}
                        title="Mark present"
                        className="flex items-center gap-0.5 rounded bg-success/20 px-1.5 py-0.5 text-[10px] text-success hover:bg-success/30 disabled:opacity-50">
                        {isBusy ? "…" : "Restore"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {staff.length === 0 && (
              <tr><td colSpan={7} className="py-4 text-center text-text/30">No staff. Load a seed.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {stationCoverage.length > 0 && (
        <div className="mt-4">
          <SectionHeading>Station Coverage</SectionHeading>
          <div className="mt-2 flex flex-wrap gap-2">
            {stationCoverage.map(({ station, covered }) => (
              <div
                key={station.id}
                className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
                  covered
                    ? "border-success/30 bg-success/10 text-success"
                    : "border-danger/40 bg-danger/10 text-danger"
                }`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${covered ? "bg-success" : "bg-danger"}`} />
                {station.name}
                {!covered && <span className="ml-1 font-semibold">UNSTAFFED — dishes disabled</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function StaffStations() {
  return (
    <div className="space-y-8">
      <StationsPanel />
      <StaffPanel />
    </div>
  );
}
