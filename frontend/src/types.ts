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
  /** Active preset id; changes when a restaurant is (re)seeded. */
  active_seed_id?: string | null;
  /** Operating window in seconds-since-midnight, e.g. { open: 28800, close: 82800 }. */
  operating_window?: { open?: number; close?: number } | null;
  /** When true the clock jumps over closed hours. */
  skip_closed_hours?: boolean;
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
  | "forecast_override_proposal"
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
  events?: ScenarioEvent[];
}

/** A single timed event within a scenario. */
export interface ScenarioEvent {
  id: number;
  scenario_id: number;
  at_sim_time: number;
  event_type: string;
  payload: unknown;
  fired: number;
}

/** Valid event_type values the scenario engine dispatches. */
export const SCENARIO_EVENT_TYPES = [
  "inject_signal",
  "change_setting",
  "inject_review",
  "set_competitor",
  "call_in_sick",
  "supplier_change",
  "weather_set",
  "velocity_mult",
] as const;

export type ScenarioEventType = (typeof SCENARIO_EVENT_TYPES)[number];

/** sim_settings singleton row (GET/PATCH /api/sim/pos). */
export interface SimSettings {
  id: number;
  base_orders_per_day: number | null;
  velocity: number | null;
  dish_mix_weights: Record<string, number> | null;
  daypart_curve: Record<string, number> | null;
  channel_mix: Record<string, number> | null;
  anomaly_injections: AnomalyInjection[] | null;
}

/** One windowed POS anomaly injection (§10). */
export interface AnomalyInjection {
  start?: number;
  end?: number;
  velocity_mult?: number;
  dish_mix_skew?: Record<string, number>;
}

/** menu_items row (GET /api/menu). */
export interface MenuItem {
  id: number;
  name: string;
  category: string | null;
  station_id: number | null;
  dine_in_price: number | null;
  online_price: number | null;
  prep_time_min: number | null;
  is_batchable: number;
  active: number;
  weather_tags: unknown;
  description: string | null;
}

/** Generic entity row for the entity editor (columns vary per resource). */
export type EntityRow = Record<string, unknown>;

/** stations row (GET/PATCH /api/stations). */
export interface Station {
  id: number;
  name: string;
}

/** recipes row (GET /api/recipes). */
export interface Recipe {
  id: number;
  menu_item_id: number;
}

/** recipe_lines row (GET/PATCH /api/recipe-lines). */
export interface RecipeLine {
  id: number;
  recipe_id: number;
  ingredient_id: number;
  qty: number | null;
  unit: string | null;
  optional: number;
}

/** batch_definitions row (GET/PATCH /api/batch-definitions). */
export interface BatchDefinition {
  id: number;
  menu_item_id: number;
  dayparts: string[] | null;
  prep_lead_time_min: number | null;
  batch_size_min: number | null;
  batch_size_step: number | null;
  batch_size_max: number | null;
  decide_by_offset_min: number | null;
  prepared_shelf_life_min: number | null;
  station_id: number | null;
  required_skill: string | null;
  default_cadence_min: number | null;
  historical_attach_rate: number | null;
}

/** orders row, as carried by GET /api/orders and the order_created WS payload. */
export interface PosOrder {
  id: number;
  sim_time: number;
  service_mode: string | null;
  channel: string | null;
  guest_count: number | null;
  status: string;
  total: number | null;
}

/** order_lines row. */
export interface PosOrderLine {
  id: number;
  order_id: number;
  menu_item_id: number;
  qty: number;
  unit_price: number | null;
  line_total: number | null;
  status: string;
  sim_time: number;
}

/** One POS event: an order plus its lines. The live order_created WS event also
 * carries a `velocity` map ({ menu_item_id: items/sec }); backfill omits it. */
export interface PosOrderEvent {
  order: PosOrder;
  lines: PosOrderLine[];
  velocity?: Record<string, number>;
}

// -- Track B (00 §19.2 / §19.3) ----------------------------------------------

/** inventory_levels row (GET /api/inventory). */
export interface InventoryLevel {
  id: number;
  ingredient_id: number;
  par_level: number | null;
  reorder_point: number | null;
  safety_stock: number | null;
  yield_factor: number | null;
  on_hand_cached: number | null;
  last_counted_at: number | null;
  last_counted_qty: number | null;
}

/** ingredients row (GET /api/ingredients is not exposed; names are looked up
 * from the menu/recipe reads where available, otherwise shown by id). */
export interface Ingredient {
  id: number;
  name: string;
  category: string | null;
  base_unit: "g" | "ml" | "each";
  perishable: number;
  shelf_life_days: number | null;
}

/** inventory_lots row (GET /api/inventory/lots). */
export interface InventoryLot {
  id: number;
  ingredient_id: number;
  qty_on_hand: number;
  unit: string;
  purchase_price: number | null;
  purchase_date: number | null;
  received_date: number | null;
  expiry_date: number | null;
  supplier_id: number | null;
  storage_location: string | null;
  status: "active" | "depleted" | "expired";
}

/** promotions row (GET /api/promotions). */
export interface Promotion {
  id: number;
  type: "combo" | "discount";
  menu_items: number[];
  trigger: "expiry" | "slow_mover" | "intel";
  discount_pct: number;
  channel: "menu" | "aggregator" | "both";
  status: "proposed" | "approved" | "active" | "expired";
  approval_id: number | null;
  sim_time: number;
}

/** negotiations row (GET /api/negotiations). */
export interface Negotiation {
  id: number;
  supplier_id: number;
  ingredient_id: number;
  call_id: number | null;
  transcript: unknown;
  outcome: Record<string, unknown> | null;
  savings: number | null;
  sim_time: number;
}

/** supplier_catalog row (GET/PATCH /api/supplier-catalog). */
export interface SupplierCatalogRow {
  id: number;
  supplier_id: number;
  ingredient_id: number;
  current_price: number;
  unit: string;
  pack_size: number;
  availability: "in_stock" | "limited" | "out";
  updated_at: number | null;
}

/** suppliers row (GET/PATCH /api/suppliers). */
export interface SupplierRow {
  id: number;
  name: string;
  lead_time_days: number | null;
  reliability_score: number | null;
  min_order_value: number | null;
  contact: string | null;
}

/** menu_toggles row, embedded in menu_toggled WS payloads. */
export interface MenuToggleEvent {
  menu_item_id: number;
  action: "disable" | "enable";
  reason?: string;
}

/** event_log row (GET /api/events + event_logged WS event). */
export interface EventLogEntry {
  id: number;
  sim_time: number;
  category: string;
  actor: string;
  summary: string;
  detail: unknown;
}

/** The bus envelope (00 §14.1), as carried by signal_emitted. */
export interface SignalEnvelope {
  signal_id: string;
  type: string;
  source: string;
  groups: string[];
  priority: number;
  payload: Record<string, unknown>;
  created_at: number;
  expires_at: number | null;
  dedup_key: string | null;
  status: "live" | "consumed" | "expired";
  correlation_id: string | null;
}

export interface InventorySignalPolicy {
  shortage_signals_enabled: boolean;
}
