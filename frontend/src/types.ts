// Shapes mirror the backend contracts (00 §19–§21). The frontend is a pure
// consumer of these; it never computes business logic over them.

export type SimStatus = "stopped" | "running" | "paused" | "call_frozen";

export type WeatherCondition = "clear" | "clouds" | "rain" | "storm" | "snow";

/** Snapshot of the sim clock, fed by GET /api/sim/state + the sim_tick WS event. */
export interface SimState {
  sim_time: number;
  day_number: number;
  day_of_week?: number;
  /** Present on sim_tick events; "HH:MM" within the operating day. */
  time_of_day?: string;
  speed: number;
  status: SimStatus;
  call_mode?: "freeze" | "slow";
}

/** Canonical weather struct (00 §9.1) as carried by weather_updated / GET /api/weather. */
export interface Weather {
  temp_c: number;
  condition: WeatherCondition;
  precip_mm: number;
  wind_kph: number;
  source: "api" | "override";
}

export type ApprovalType =
  | "purchase_order"
  | "menu_change"
  | "promo"
  | "outbound_call"
  | "other";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

/** approval_requests row (00 §19.3). */
export interface ApprovalRequest {
  id: number;
  type: ApprovalType;
  title: string;
  summary: string;
  payload: unknown;
  urgency: number | string | null;
  status: ApprovalStatus;
  created_at: number | null;
  resolved_at: number | null;
  resolved_by: string | null;
  ref_id: number | null;
}

export type CallStatus =
  | "requested"
  | "approved"
  | "rejected"
  | "active"
  | "completed"
  | "failed"
  | "auto_resolved";

/** One streamed roleplay turn (call_turn WS event + calls.transcript entries). */
export interface CallTurn {
  role: "agent" | "counterparty";
  text: string;
  sim_ts?: number;
}

/** calls row (00 §19.3). */
export interface Call {
  id: number;
  agent: "market_spectator" | "competitor_intel";
  counterparty_type: "supplier" | "competitor";
  counterparty_id: number | null;
  purpose: string | null;
  status: CallStatus;
  approval_id: number | null;
  transcript: CallTurn[] | null;
  outcome: unknown;
  started_at: number | null;
  ended_at: number | null;
  clock_action: "freeze" | "slow" | null;
}

/** A scenario row + its events (GET /api/scenarios). */
export interface Scenario {
  id: number;
  name: string;
  description: string | null;
  is_active: number;
  events?: unknown[];
}
