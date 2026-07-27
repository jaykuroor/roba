import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../api";

// Data hook for the manager dashboard (docs/fable/manager-dashboard.md).
// Plain polling — the manager server has no WS today; 5s is fine for a
// portfolio view (children stream live via their own consoles).

export interface InstanceCard {
  id: string;
  preset: string;
  port: number;
  title: string;
  online: boolean;
  status: "normal" | "warning" | "critical" | "offline";
  /** Set when the instance process is alive but unresponsive (mid-solve). */
  note?: string | null;
  sim?: { sim_time?: number; day_number?: number; status?: string } | null;
  sales_today: number | null;
  forecast_today: number | null;
  orders_today: number | null;
  staff_present: number | null;
  staff_total: number | null;
  absent: string[];
  stock_risks: { ingredient?: string; status?: string; on_hand_display?: string }[];
  pending_approvals: number;
  /** Kitchen tickets still queued, and the last sim-hour's average ticket time. */
  orders_waiting: number | null;
  ticket_time_min: number | null;
  safety_issues: null; // not implemented — docs/fable/progress.md Phase 2
  issues: ActionItem[];
}

export interface ActionItem {
  instance_id: string;
  restaurant: string;
  kind: "approval" | "stock" | "staff";
  /** For kind==="approval": "decision" (approve/reject) or "notice" (acknowledge). */
  approval_kind?: "decision" | "notice";
  problem: string;
  severity: "critical" | "high" | "medium" | "low";
  deadline_sim: number | null;
  impact: string;
  recommended_action: string;
  approval_id: number | null;
}

export interface AdminApproval {
  id: number;
  instance_id: string;
  restaurant: string;
  type: string;
  kind?: "decision" | "notice";
  title: string;
  summary: string;
  urgency: string;
  status: string;
  created_at: number | null;
}

export interface Incident {
  instance_id: string;
  restaurant: string;
  category: string;
  signal_type: string;
  /** Human-readable, merged: similar items are batched per supplier/type. */
  summary: string;
  count: number;
  names: string[];
  created_at: number | null;
}

export interface Summary {
  generated_at: number;
  totals: {
    sales_today: number;
    forecast_today: number;
    waste_today: number;
    stock_risks: number;
    staff_absent: number;
    pending_approvals: number;
    offline: number;
  };
  major_incidents: ActionItem[];
  pending_decisions: ActionItem[];
  next_day_risks: ActionItem[];
}

export function useAdminData() {
  const [instances, setInstances] = useState<InstanceCard[]>([]);
  const [actions, setActions] = useState<ActionItem[]>([]);
  const [approvals, setApprovals] = useState<AdminApproval[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [unavailableCategories, setUnavailableCategories] = useState<string[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [presets, setPresets] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [overview, approvalRows, incidentData] = await Promise.all([
        apiGet<{ instances: InstanceCard[]; actions: ActionItem[] }>("/admin/api/overview"),
        apiGet<AdminApproval[]>("/admin/api/approvals?status=pending"),
        apiGet<{ incidents: Incident[]; unavailable_categories: string[] }>("/admin/api/incidents"),
      ]);
      setInstances(overview.instances);
      setActions(overview.actions);
      setApprovals(approvalRows);
      setIncidents(incidentData.incidents);
      setUnavailableCategories(incidentData.unavailable_categories);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "manager unreachable");
    }
  }, []);

  const refreshSummary = useCallback(() => {
    apiGet<Summary>("/admin/api/summary").then(setSummary).catch(() => undefined);
  }, []);

  useEffect(() => {
    apiGet<string[]>("/admin/api/presets").then(setPresets).catch(() => undefined);
    refresh();
    refreshSummary();
    const fast = setInterval(refresh, 5000);
    const slow = setInterval(refreshSummary, 30000);
    return () => {
      clearInterval(fast);
      clearInterval(slow);
    };
  }, [refresh, refreshSummary]);

  return {
    instances, actions, approvals, incidents, unavailableCategories,
    summary, presets, error, refresh, refreshSummary,
  };
}

/** Format sim-seconds as "Day N HH:MM". */
export function fmtSim(simSeconds: number | null | undefined): string {
  if (simSeconds == null) return "—";
  const day = Math.floor(simSeconds / 86400);
  const rest = simSeconds % 86400;
  const hh = String(Math.floor(rest / 3600)).padStart(2, "0");
  const mm = String(Math.floor((rest % 3600) / 60)).padStart(2, "0");
  return `Day ${day} ${hh}:${mm}`;
}

export function fmtMoney(v: number | null | undefined): string {
  return v == null ? "—" : `€${v.toFixed(2)}`;
}
