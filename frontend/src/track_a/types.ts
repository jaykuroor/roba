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
  menu_items: MenuItem[];
  forecasts: Forecast[];
  batches: Batch[];
  competitors: Competitor[];
  competitor_offers: CompetitorOffer[];
  competitor_intel: CompetitorIntel[];
  reviews: Review[];
  review_insights: ReviewInsight[];
  stations: Station[];
  staff: Staff[];
  staff_stations: StaffStation[];
  attendance: Attendance[];
  signals: SignalRow[];
  events: EventLog[];
}
