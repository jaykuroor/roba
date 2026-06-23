export interface MenuItem {
  id: number;
  name: string;
  category: string;
  station_id: number;
  active: number;
  weather_tags?: string[];
}

export interface Forecast {
  id: number;
  menu_item_id: number;
  window: { start: number; end: number };
  daypart: string;
  forecast_qty: number;
  baseline_qty: number;
  multipliers: Record<string, number>;
  confidence: number;
  generated_at: number;
  trigger_reason: string;
  run_id?: string | null;
  trace?: ForecastTrace | null;
}

export interface ForecastTrace {
  run_id?: string;
  version?: number;
  scope?: Record<string, unknown>;
  baseline?: Record<string, unknown>;
  deterministic_recommendation?: Record<string, unknown>;
  llm_final_decision?: Record<string, unknown>;
  adjustments?: Array<Record<string, unknown>>;
  constraints?: Array<Record<string, unknown>>;
  final?: Record<string, unknown>;
  summary?: string;
  optimized?: boolean;
  trigger?: string;
}

export interface DemandMemory {
  id: number;
  scope_type: string;
  scope_ref: string;
  insight: {
    title?: string;
    summary?: string;
    [key: string]: unknown;
  };
  evidence: Record<string, unknown>;
  confidence: number;
  created_at: number;
  last_seen_at: number;
  valid_until: number;
  source: string;
}

export interface ForecastOverride {
  id: number;
  menu_item_id: number;
  daypart: string;
  window: { start: number; end: number };
  operation: string;
  value: Record<string, unknown>;
  reason: string;
  source: string;
  authority: string;
  status: string;
  created_at: number;
  valid_until: number;
  evidence: Record<string, unknown>;
}

export interface ForecastTraceRow {
  id: number;
  forecast_id: number;
  run_id: string;
  menu_item_id: number;
  daypart: string;
  window: { start: number; end: number };
  trace: ForecastTrace;
  summary: string;
  created_at: number;
}

export interface ForecastAdjustment {
  id: number;
  forecast_id: number;
  run_id: string;
  menu_item_id: number;
  stage: string;
  source: string;
  modifier_key: string;
  operation: string;
  value: Record<string, unknown>;
  reason: string;
  evidence: Record<string, unknown>;
  created_at: number;
}

export interface ForecastJob {
  id: number;
  job_id: string;
  kind: "deterministic_forecast" | "llm_finalizer" | string;
  status: "queued" | "running" | "succeeded" | "failed" | "superseded" | "stale" | string;
  sim_time: number;
  daypart: string;
  window: { start: number; end: number };
  requested_by: string;
  trigger_reason: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  result: {
    approval_ids?: number[];
    needs_approval?: boolean;
    proposals?: unknown[];
    created?: number;
    reason?: string;
    [key: string]: unknown;
  } | null;
}

export interface Batch {
  id: number;
  batch_definition_id: number;
  menu_item_id: number;
  decided_at: number;
  serve_window: { start: number; end: number };
  decision: "cook" | "skip";
  planned_qty: number;
  status: string;
  by: string;
}

export interface Competitor {
  id: number;
  name: string;
  platform: string;
  cuisine: string[];
  distance_km: number;
  rating: number;
  is_open: number;
  price_tier: string;
}

export interface CompetitorOffer {
  id: number;
  competitor_id: number;
  dish_or_combo: string;
  price: number;
  description: string;
}

export interface CompetitorIntel {
  id: number;
  competitor_id: number;
  method: string;
  popular_dishes: string[];
  price_points: Record<string, number | string>;
  notes: string;
  call_id: number | null;
  sim_time: number;
}

export interface CompetitorObservation {
  id: number;
  competitor_id: number | null;
  source_channel: string;
  platform: string;
  signal_kind: string;
  direction: string;
  impact_score: number;
  confidence: number;
  affected_menu_items: number[];
  affected_categories: string[];
  window: { start: number; end: number };
  evidence: string[];
  raw: Record<string, unknown>;
  state_hash: string;
  sim_time: number;
}

export interface CompetitorMenuSnapshot {
  id: number;
  competitor_id: number;
  source_channel: string;
  platform: string;
  menu_hash: string;
  items: Array<Record<string, unknown>>;
  compliance: Record<string, unknown>;
  fetched_at: number;
}

export interface CompetitorProbeResult {
  id: number;
  competitor_id: number;
  source_channel: string;
  platform: string;
  estimated_wait_min: number;
  availability: string;
  tactic_labels: string[];
  confidence: number;
  transcript: Array<Record<string, unknown>>;
  raw: Record<string, unknown>;
  sim_time: number;
}

export interface Review {
  id: number;
  source: string;
  rating: number;
  text: string;
  dish_mentions: string[];
  sentiment: string;
  sim_time: number;
  processed: number;
}

export interface ReviewInsight {
  id: number;
  review_id: number | null;
  insight_type: string;
  summary: string;
  suggested_action: string;
  severity: "low" | "medium" | "high" | string;
  sim_time: number;
}

export interface Station {
  id: number;
  name: string;
}

export interface Staff {
  id: number;
  name: string;
  role: string;
  active: number;
}

export interface StaffStation {
  id: number;
  staff_id: number;
  station_id: number;
}

export interface Attendance {
  id: number;
  staff_id: number | null;
  date_sim_day: number;
  status: string;
  daypart: string | null;
  reason: string | null;
  sim_time: number;
}

export interface SignalRow {
  signal_id: string;
  type: string;
  source: string;
  groups: string[];
  priority: number;
  payload: Record<string, unknown>;
  created_at: number;
  expires_at: number | null;
  dedup_key: string | null;
  status: string;
  correlation_id: string | null;
}

export interface EventLog {
  id: number;
  sim_time: number;
  category: string;
  actor: string;
  summary: string;
  detail: unknown;
}

export interface TrackASnapshot {
  demo_mode: string;
  sim_state?: {
    sim_time: number;
    day_number: number;
    day_of_week: number;
    speed: number;
    status: string;
    call_mode: string;
  };
  forecast_agent?: {
    llm_auto_mode: boolean;
  };
  menu_items: MenuItem[];
  forecasts: Forecast[];
  batches: Batch[];
  demand_memory: DemandMemory[];
  forecast_overrides: ForecastOverride[];
  forecast_traces: ForecastTraceRow[];
  forecast_adjustments: ForecastAdjustment[];
  forecast_jobs: ForecastJob[];
  forecast_reasoning: EventLog[];
  competitors: Competitor[];
  competitor_offers: CompetitorOffer[];
  competitor_intel: CompetitorIntel[];
  competitor_observations: CompetitorObservation[];
  competitor_menu_snapshots: CompetitorMenuSnapshot[];
  competitor_probe_results: CompetitorProbeResult[];
  reviews: Review[];
  review_insights: ReviewInsight[];
  stations: Station[];
  staff: Staff[];
  staff_stations: StaffStation[];
  attendance: Attendance[];
  signals: SignalRow[];
  events: EventLog[];
}
