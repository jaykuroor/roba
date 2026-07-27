"""Signal taxonomy: types, registry of defaults, and typed payloads (§15).

This module is the single source of truth for:

- :class:`SignalType` — every signal type from the §15 table, string-valued.
- :data:`SIGNAL_REGISTRY` — per-type default ``groups`` / ``priority`` /
  ``default_ttl_sim_s`` exactly as tabulated in §15. ``emit`` (``core/bus.py``)
  looks these up unless the caller overrides them.
- ``*Payload`` pydantic v2 models — one per signal type, matching the Payload
  column in §15. Optional fields use ``Optional[...]`` with a ``None`` default.

TTL conversions from §15 (sim-seconds):
  4h→14400, 6h→21600, 12h→43200, 24h→86400, 3h→10800, 2h→7200, 1h→3600.
  "until window end" / "call len" / "per fact" / "until expiry" /
  "until shift end" → ``None`` (the caller must supply a TTL at emit time).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class SignalType(str, Enum):
    """Every signal type from the §15 taxonomy table (string-valued)."""

    DEMAND_FORECAST = "DEMAND_FORECAST"
    BATCH_DECISION = "BATCH_DECISION"
    WASTE_EVENT = "WASTE_EVENT"
    LOW_STOCK = "LOW_STOCK"
    STOCKOUT_RISK = "STOCKOUT_RISK"
    EXPIRY_RISK = "EXPIRY_RISK"
    MENU_TOGGLE = "MENU_TOGGLE"
    REORDER_PLACED = "REORDER_PLACED"
    SUPPLIER_PRICE_UPDATE = "SUPPLIER_PRICE_UPDATE"
    COMPETITOR_UPDATE = "COMPETITOR_UPDATE"
    COMPETITOR_INTEL = "COMPETITOR_INTEL"
    COMPETITOR_MARKET_SIGNAL = "COMPETITOR_MARKET_SIGNAL"
    REVIEW_INSIGHT = "REVIEW_INSIGHT"
    STAFF_COVERAGE = "STAFF_COVERAGE"
    PROMO_PROPOSAL = "PROMO_PROPOSAL"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    WEATHER_UPDATE = "WEATHER_UPDATE"
    CALL_REQUEST = "CALL_REQUEST"
    CALL_STARTED = "CALL_STARTED"
    CALL_OUTCOME = "CALL_OUTCOME"
    USER_FACT = "USER_FACT"
    DEMAND_EVENT = "DEMAND_EVENT"
    PRODUCTION_CONSTRAINT = "PRODUCTION_CONSTRAINT"
    STAFF_AVAILABILITY = "STAFF_AVAILABILITY"
    MENU_TOGGLE_REQUEST = "MENU_TOGGLE_REQUEST"
    INVENTORY_RECEIPT_REPORTED = "INVENTORY_RECEIPT_REPORTED"
    INVENTORY_COUNT_REPORTED = "INVENTORY_COUNT_REPORTED"
    INGREDIENT_SHORTAGE_REPORTED = "INGREDIENT_SHORTAGE_REPORTED"
    EXPIRY_USE_PRIORITY = "EXPIRY_USE_PRIORITY"
    SUPPLIER_CATALOG_NOTE = "SUPPLIER_CATALOG_NOTE"
    CUSTOMER_FEEDBACK_NOTE = "CUSTOMER_FEEDBACK_NOTE"
    COMPETITOR_NOTE = "COMPETITOR_NOTE"
    OPERATIONAL_BRIEFING = "OPERATIONAL_BRIEFING"
    # Cook-reported batch progress (Stream B3): actual qty cooked, waste, status.
    BATCH_PROGRESS = "BATCH_PROGRESS"
    # Multi-day demand horizon from the forecaster (for procurement sizing).
    DEMAND_FORECAST_HORIZON = "DEMAND_FORECAST_HORIZON"
    # Ingredient demand cannot be covered — no lead-feasible / in-stock supply.
    INGREDIENT_UNCOVERABLE = "INGREDIENT_UNCOVERABLE"
    # A temp/safety kitchen check was reported not done, or ran past its final
    # overdue escalation — the HACCP failure behind the food_safety_checks
    # incident category (docs/fable/incidents.md).
    FOOD_SAFETY_CHECK = "FOOD_SAFETY_CHECK"
    # Kitchen ticket queue past its warn/crit depth — guests are waiting.
    ORDER_BACKLOG = "ORDER_BACKLOG"
    # A station is out of service for a sim-window (scenario-driven).
    EQUIPMENT_FAILURE = "EQUIPMENT_FAILURE"


# ---------------------------------------------------------------------------
# §15 registry — default groups / priority / TTL per type.
# None TTL == "caller must supply a TTL" (window-bound / call-bound / per-fact).
# ---------------------------------------------------------------------------

SIGNAL_REGISTRY: Dict[SignalType, Dict[str, Any]] = {
    SignalType.DEMAND_FORECAST: {
        "groups": ["forecasting", "inventory", "human"],
        "priority": 2,
        "default_ttl_sim_s": None,            # until window end
    },
    SignalType.BATCH_DECISION: {
        "groups": ["kitchen", "inventory", "human"],
        "priority": 3,
        "default_ttl_sim_s": 14400.0,         # 4h
    },
    SignalType.WASTE_EVENT: {
        "groups": ["inventory", "forecasting", "procurement", "human"],
        "priority": 3,
        "default_ttl_sim_s": 21600.0,         # 6h
    },
    SignalType.LOW_STOCK: {
        "groups": ["procurement", "inventory", "human"],
        "priority": 3,
        "default_ttl_sim_s": 14400.0,         # 4h
    },
    SignalType.STOCKOUT_RISK: {
        "groups": ["procurement", "inventory", "human", "frontend"],
        "priority": 4,
        "default_ttl_sim_s": 14400.0,         # 4h
    },
    SignalType.EXPIRY_RISK: {
        "groups": ["inventory", "procurement", "human"],
        "priority": 3,
        "default_ttl_sim_s": None,            # until expiry
    },
    SignalType.MENU_TOGGLE: {
        "groups": ["forecasting", "kitchen", "human", "frontend"],
        "priority": 3,
        "default_ttl_sim_s": 86400.0,         # 24h
    },
    SignalType.REORDER_PLACED: {
        "groups": ["procurement", "human"],
        "priority": 2,
        "default_ttl_sim_s": 86400.0,         # 24h
    },
    SignalType.SUPPLIER_PRICE_UPDATE: {
        "groups": ["inventory", "procurement", "forecasting"],
        "priority": 2,
        "default_ttl_sim_s": 86400.0,         # 24h
    },
    SignalType.COMPETITOR_UPDATE: {
        "groups": ["forecasting", "human"],
        "priority": 1,
        "default_ttl_sim_s": 43200.0,         # 12h
    },
    SignalType.COMPETITOR_INTEL: {
        "groups": ["forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": 86400.0,         # 24h
    },
    SignalType.COMPETITOR_MARKET_SIGNAL: {
        "groups": ["forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": None,            # per observation window
    },
    SignalType.REVIEW_INSIGHT: {
        "groups": ["forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": 43200.0,         # 12h
    },
    SignalType.STAFF_COVERAGE: {
        "groups": ["forecasting", "human"],
        "priority": 3,
        "default_ttl_sim_s": None,            # until shift end
    },
    SignalType.PROMO_PROPOSAL: {
        "groups": ["human"],
        "priority": 3,
        "default_ttl_sim_s": None,            # until expiry
    },
    SignalType.APPROVAL_REQUEST: {
        "groups": ["human"],
        "priority": 4,
        "default_ttl_sim_s": 21600.0,         # 6h
    },
    SignalType.APPROVAL_RESOLVED: {
        "groups": ["human", "procurement", "inventory", "kitchen"],
        "priority": 3,
        "default_ttl_sim_s": 7200.0,          # 2h
    },
    SignalType.WEATHER_UPDATE: {
        "groups": ["forecasting"],
        "priority": 1,
        "default_ttl_sim_s": 10800.0,         # 3h
    },
    SignalType.CALL_REQUEST: {
        "groups": ["human"],
        "priority": 4,
        "default_ttl_sim_s": 3600.0,          # 1h
    },
    SignalType.CALL_STARTED: {
        "groups": ["human", "frontend"],
        "priority": 2,
        "default_ttl_sim_s": None,            # call len
    },
    SignalType.CALL_OUTCOME: {
        "groups": ["forecasting", "procurement", "inventory", "human"],
        "priority": 2,
        "default_ttl_sim_s": 43200.0,         # 12h
    },
    SignalType.USER_FACT: {
        "groups": ["forecasting", "inventory", "procurement", "human"],
        "priority": 2,
        "default_ttl_sim_s": None,            # per fact
    },
    SignalType.DEMAND_EVENT: {
        "groups": ["forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": None,            # until event window end
    },
    SignalType.PRODUCTION_CONSTRAINT: {
        "groups": ["forecasting", "kitchen", "human", "frontend"],
        "priority": 4,
        "default_ttl_sim_s": None,            # until constraint window end
    },
    SignalType.STAFF_AVAILABILITY: {
        "groups": ["forecasting", "human"],
        "priority": 3,
        "default_ttl_sim_s": None,            # until shift/window end
    },
    SignalType.MENU_TOGGLE_REQUEST: {
        "groups": ["inventory", "procurement", "kitchen", "forecasting", "human", "frontend"],
        "priority": 3,
        "default_ttl_sim_s": 86400.0,
    },
    SignalType.INVENTORY_RECEIPT_REPORTED: {
        "groups": ["inventory", "procurement", "human"],
        "priority": 3,
        "default_ttl_sim_s": 21600.0,
    },
    SignalType.INVENTORY_COUNT_REPORTED: {
        "groups": ["inventory", "human"],
        "priority": 3,
        "default_ttl_sim_s": 21600.0,
    },
    SignalType.INGREDIENT_SHORTAGE_REPORTED: {
        "groups": ["inventory", "procurement", "forecasting", "human"],
        "priority": 3,
        "default_ttl_sim_s": 14400.0,
    },
    SignalType.EXPIRY_USE_PRIORITY: {
        "groups": ["inventory", "procurement", "forecasting", "human"],
        "priority": 3,
        "default_ttl_sim_s": None,            # until expiry/window end
    },
    SignalType.SUPPLIER_CATALOG_NOTE: {
        "groups": ["procurement", "inventory", "forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": 86400.0,
    },
    SignalType.CUSTOMER_FEEDBACK_NOTE: {
        "groups": ["sensing", "forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": 43200.0,
    },
    SignalType.COMPETITOR_NOTE: {
        "groups": ["sensing", "forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": 43200.0,
    },
    SignalType.OPERATIONAL_BRIEFING: {
        "groups": ["human", "frontend"],
        "priority": 1,
        "default_ttl_sim_s": 7200.0,
    },
    SignalType.BATCH_PROGRESS: {
        "groups": ["kitchen", "forecasting", "inventory", "human", "frontend"],
        "priority": 3,
        "default_ttl_sim_s": 14400.0,          # 4h
    },
    SignalType.DEMAND_FORECAST_HORIZON: {
        "groups": ["inventory", "procurement", "forecasting", "human"],
        "priority": 2,
        "default_ttl_sim_s": 172800.0,         # 2 sim-days — survives between daily emits
    },
    SignalType.INGREDIENT_UNCOVERABLE: {
        # Surfaced on the procurement page so the user knows sourcing has failed.
        # "frontend" group → auto-pushed via the orchestrator→WS signal_emitted bridge.
        # dedup_key = "uncoverable:{ingredient_id}" gives one live warning per ingredient.
        "groups": ["procurement", "inventory", "human", "frontend"],
        "priority": 5,
        "default_ttl_sim_s": 86400.0,          # 24h — refreshed each plan rebuild
    },
    SignalType.FOOD_SAFETY_CHECK: {
        # "inventory" is the group that actually has subscribers (ledger /
        # optimizer both ignore unknown types safely), so the emit is routed
        # rather than dead-lettered; "frontend" pushes it live to the cook desk
        # and "human"/"kitchen" carry the intent.
        "groups": ["kitchen", "inventory", "human", "frontend"],
        "priority": 5,
        "default_ttl_sim_s": 86400.0,          # 24h — a failed check stands all day
    },
    SignalType.ORDER_BACKLOG: {
        # Re-emitted with the same dedup_key while the queue stays deep, so the
        # row refreshes in place (payload + expires_at) rather than duplicating;
        # once the pass drains, nothing refreshes it and the TTL retires it.
        "groups": ["kitchen", "forecasting", "human", "frontend"],
        "priority": 4,
        "default_ttl_sim_s": 3600.0,           # 1h past the last refresh
    },
    SignalType.EQUIPMENT_FAILURE: {
        # TTL is supplied at emit time as (until_sim - now): the signal *is* the
        # outage window, so its expiry is what re-opens the station.
        "groups": ["kitchen", "forecasting", "human", "frontend"],
        "priority": 5,
        "default_ttl_sim_s": None,             # caller supplies the window
    },
}


# ---------------------------------------------------------------------------
# Typed payloads (one per signal type) — §15 Payload column.
# Fields marked with "?" in §15 are Optional[...] with a None default.
# ---------------------------------------------------------------------------

class DemandForecastPayload(BaseModel):
    menu_item_id: int
    window: Dict[str, float]                  # {start, end}
    daypart: str
    qty: int
    baseline: float
    multipliers: Dict[str, float] = {}
    confidence: float
    run_id: Optional[str] = None
    trace: Optional[Dict[str, Any]] = None


class BatchDecisionPayload(BaseModel):
    batch_definition_id: int
    menu_item_id: int
    serve_window: Dict[str, float]
    decision: str                             # "cook" | "skip"
    qty: int
    by: str                                   # "agent" | "human"


class WasteEventPayload(BaseModel):
    waste_type: str
    ingredient_id: Optional[int] = None
    menu_item_id: Optional[int] = None
    lot_id: Optional[int] = None
    qty: float
    unit: str
    cost: Optional[float] = None  # nullable: voice spoilage paths don't compute cost
    reason: str


class LowStockPayload(BaseModel):
    ingredient_id: int
    on_hand: float
    threshold: float
    projected_runout: float
    unit: str


class StockoutRiskPayload(BaseModel):
    ingredient_id: int
    on_hand: float
    projected_runout: float
    affected_items: List[int] = []


class ExpiryRiskPayload(BaseModel):
    ingredient_id: int
    lot_id: int
    qty: float
    expiry: float
    projected_usage_before_expiry: float


class MenuTogglePayload(BaseModel):
    menu_item_id: int
    action: str                               # "disable" | "enable"
    reason: str


class ReorderPlacedPayload(BaseModel):
    po_id: int
    supplier_id: int
    lines: List[Dict[str, Any]] = []          # [{ingredient_id, qty}]
    total: float
    eta: float


class SupplierPriceUpdatePayload(BaseModel):
    supplier_id: int
    ingredient_id: int
    old_price: float
    new_price: float
    availability: str
    via: str                                  # "market" | "call"


class CompetitorUpdatePayload(BaseModel):
    competitor_id: int
    is_open: bool
    offers_changed: bool
    summary: str


class CompetitorIntelPayload(BaseModel):
    competitor_id: int
    popular_dishes: List[str] = []
    price_points: Dict[str, Any] = {}
    method: str                               # "call" | "aggregator"
    call_id: Optional[int] = None


class CompetitorMarketSignalPayload(BaseModel):
    signal_kind: str
    source_channel: str                        # aggregator | web | probe | scenario
    platform: str
    competitor_id: Optional[int] = None
    affected_menu_items: List[int] = []
    affected_categories: List[str] = []
    direction: str                             # opportunity | threat | drag | watch
    impact_score: float
    confidence: float
    window: Dict[str, float]
    evidence: List[str] = []
    raw: Dict[str, Any] = {}


class ReviewInsightPayload(BaseModel):
    review_id: Optional[int] = None
    severity: str
    summary: str
    suggested_action: str
    dish_mentions: List[str] = []


class StaffCoveragePayload(BaseModel):
    station_id: int
    covered: bool
    affected_items: List[int] = []
    shortfall: float


class PromoProposalPayload(BaseModel):
    promo_id: int
    type: str                                 # "combo" | "discount"
    menu_items: List[int] = []
    discount_pct: float
    channel: str
    trigger: str


class ApprovalRequestPayload(BaseModel):
    approval_id: int
    type: str
    title: str
    summary: str
    payload: Dict[str, Any] = {}
    urgency: str


class ApprovalResolvedPayload(BaseModel):
    approval_id: int
    type: str
    decision: str                             # "approved" | "rejected"
    ref_id: int
    payload: Dict[str, Any] = {}


class WeatherUpdatePayload(BaseModel):
    temp_c: float
    condition: str
    precip_mm: float
    wind_kph: float
    source: str


class CallRequestPayload(BaseModel):
    call_id: int
    agent: str
    counterparty_type: str
    counterparty_id: int
    purpose: str


class CallStartedPayload(BaseModel):
    call_id: int


class CallOutcomePayload(BaseModel):
    call_id: int
    counterparty_type: str
    outcome: Dict[str, Any] = {}


class UserFactPayload(BaseModel):
    intent: str
    entity_type: str
    entity_ref: Any
    attribute: str
    value: Any
    effective_window: Optional[Dict[str, float]] = None
    raw_text: str


class DemandEventPayload(BaseModel):
    event_ref: str
    event_kind: str = "event"
    expected_attendance: Optional[float] = None
    demand_multiplier: Optional[float] = None
    affected_menu_item_ids: List[int] = []
    affected_categories: List[str] = []
    window: Optional[Dict[str, float]] = None
    raw_text: str
    confidence: float = 0.0


class ProductionConstraintPayload(BaseModel):
    constraint_ref: str
    constraint_type: str
    action: str = "block"
    affected_menu_item_ids: List[int] = []
    affected_categories: List[str] = []
    window: Optional[Dict[str, float]] = None
    reason: str = ""
    raw_text: str
    confidence: float = 0.0


class StaffAvailabilityPayload(BaseModel):
    staff_id: Optional[int] = None
    staff_name: Optional[str] = None
    station_id: Optional[int] = None
    station_ref: Optional[str] = None
    status: str
    window: Optional[Dict[str, float]] = None
    reason: str = ""
    raw_text: str
    confidence: float = 0.0


class MenuToggleRequestPayload(BaseModel):
    menu_item_id: Optional[int] = None
    item_ref: str
    action: str
    reason: str = ""
    window: Optional[Dict[str, float]] = None
    raw_text: str
    confidence: float = 0.0


class InventoryReceiptReportedPayload(BaseModel):
    ingredient_id: Optional[int] = None
    ingredient_ref: str
    qty: float
    unit: str = "each"
    supplier_id: Optional[int] = None
    supplier_ref: Optional[str] = None
    price: Optional[float] = None
    raw_text: str
    confidence: float = 0.0


class InventoryCountReportedPayload(BaseModel):
    ingredient_id: Optional[int] = None
    ingredient_ref: str
    qty: float
    unit: str = "each"
    raw_text: str
    confidence: float = 0.0


class IngredientShortageReportedPayload(BaseModel):
    ingredient_id: Optional[int] = None
    ingredient_ref: str
    severity: str = "low"
    qty: Optional[float] = None
    unit: Optional[str] = None
    raw_text: str
    confidence: float = 0.0


class ExpiryUsePriorityPayload(BaseModel):
    ingredient_id: Optional[int] = None
    ingredient_ref: str
    lot_id: Optional[int] = None
    expiry: Optional[float] = None
    qty: Optional[float] = None
    desired_action: str = "use_up"
    raw_text: str
    confidence: float = 0.0


class SupplierCatalogNotePayload(BaseModel):
    supplier_id: Optional[int] = None
    supplier_ref: str
    ingredient_id: Optional[int] = None
    ingredient_ref: Optional[str] = None
    availability: Optional[str] = None
    price: Optional[float] = None
    lead_time_days: Optional[float] = None
    raw_text: str
    confidence: float = 0.0


class CustomerFeedbackNotePayload(BaseModel):
    summary: str
    dish_mentions: List[str] = []
    sentiment: Optional[str] = None
    severity: Optional[str] = None
    raw_text: str
    confidence: float = 0.0


class CompetitorNotePayload(BaseModel):
    summary: str
    competitor_ref: Optional[str] = None
    affected_menu_item_ids: List[int] = []
    affected_categories: List[str] = []
    raw_text: str
    confidence: float = 0.0


class OperationalBriefingPayload(BaseModel):
    summary: str
    recommendations: List[str] = []
    source_signal_ids: List[str] = []
    confidence: float = 0.0


class BatchProgressPayload(BaseModel):
    """Cook-reported update to a batch (Stream B3)."""
    batch_id: int
    menu_item_id: int
    actual_made_qty: float
    planned_qty: Optional[float] = None         # for delta reconciliation
    sold_qty: Optional[float] = None
    wasted_qty: Optional[float] = None
    status: str = "cooked"                      # cooked | served | wasted
    source: str = "cook"                        # cook | system


class HorizonDayItem(BaseModel):
    menu_item_id: int
    qty: float        # transient-aware (includes event/competitor multipliers)
    baseline: float   # robust median baseline (transient-free, for par sizing)


class HorizonDay(BaseModel):
    day_index: int
    start: float   # sim-seconds
    end: float
    items: List[HorizonDayItem] = []


class DemandForecastHorizonPayload(BaseModel):
    """7-day rolling demand horizon emitted by the forecaster.

    Each day carries per-item qty (transient-aware) and baseline (median,
    transient-free).  Consumers use qty for immediate order sizing and
    baseline for steady-state par recomputation.
    """
    horizon_days: int                                   # number of days covered
    generated_at: float
    days: List[HorizonDay] = []
    # per item_id: median daily demand across the horizon (transient-free)
    item_daily_baseline_median: Dict[str, float] = {}  # key = str(menu_item_id)


class IngredientUncoverablePayload(BaseModel):
    """Payload for INGREDIENT_UNCOVERABLE — emitted when the optimizer cannot
    source enough of an ingredient to meet forecasted demand.

    Query: bus.live(type=SignalType.INGREDIENT_UNCOVERABLE) and match payload["ingredient_id"].
    """
    ingredient_id: int
    ingredient_name: str
    short_qty: float            # base units that cannot be covered
    unit: str
    reason: str                 # human-readable explanation from the planner


class OrderBacklogPayload(BaseModel):
    """Payload for ORDER_BACKLOG — the kitchen pass is deeper than
    ``config.BACKLOG_WARN`` (or ``BACKLOG_CRIT``) tickets.

    Emitted with a constant ``dedup_key`` while the queue stays deep, so the one
    live row's payload tracks the current depth instead of spawning a signal per
    tick; an unchanged depth is a total no-op (§14.3).
    """
    queued_count: int
    cooking_count: int
    level: str                  # "warning" | "critical"
    threshold: int              # the threshold this crossed
    cooks_present: int
    avg_ticket_minutes: Optional[float] = None


class EquipmentFailurePayload(BaseModel):
    """Payload for EQUIPMENT_FAILURE — a station is out of service until
    ``until_sim``. The signal's TTL *is* the outage window: when it expires,
    ``recompute_availability`` re-enables the station's dishes.
    """
    station_id: int
    station: str
    until_sim: float
    label: str                  # what broke, in kitchen language ("fryer")


class FoodSafetyCheckPayload(BaseModel):
    """Payload for FOOD_SAFETY_CHECK — a ``temp``/``safety`` kitchen task the
    cook reported not done, or one left pending past its final overdue tier.

    There is no distinct "failed" task status: ``not_done`` plus ``severity``
    *is* the failure (``core/kitchen_tasks.py``), which is why ``outcome``
    carries the distinction rather than a status string.
    """
    task_id: int
    title: str
    category: str               # "temp" | "safety"
    outcome: str                # "not_done" | "overdue"
    severity: str               # low | medium | high
    note: Optional[str] = None
    overdue_min: Optional[int] = None


# Convenience map: SignalType -> its payload model.
SIGNAL_PAYLOADS: Dict[SignalType, type[BaseModel]] = {
    SignalType.DEMAND_FORECAST: DemandForecastPayload,
    SignalType.BATCH_DECISION: BatchDecisionPayload,
    SignalType.WASTE_EVENT: WasteEventPayload,
    SignalType.LOW_STOCK: LowStockPayload,
    SignalType.STOCKOUT_RISK: StockoutRiskPayload,
    SignalType.EXPIRY_RISK: ExpiryRiskPayload,
    SignalType.MENU_TOGGLE: MenuTogglePayload,
    SignalType.REORDER_PLACED: ReorderPlacedPayload,
    SignalType.SUPPLIER_PRICE_UPDATE: SupplierPriceUpdatePayload,
    SignalType.COMPETITOR_UPDATE: CompetitorUpdatePayload,
    SignalType.COMPETITOR_INTEL: CompetitorIntelPayload,
    SignalType.COMPETITOR_MARKET_SIGNAL: CompetitorMarketSignalPayload,
    SignalType.REVIEW_INSIGHT: ReviewInsightPayload,
    SignalType.STAFF_COVERAGE: StaffCoveragePayload,
    SignalType.PROMO_PROPOSAL: PromoProposalPayload,
    SignalType.APPROVAL_REQUEST: ApprovalRequestPayload,
    SignalType.APPROVAL_RESOLVED: ApprovalResolvedPayload,
    SignalType.WEATHER_UPDATE: WeatherUpdatePayload,
    SignalType.CALL_REQUEST: CallRequestPayload,
    SignalType.CALL_STARTED: CallStartedPayload,
    SignalType.CALL_OUTCOME: CallOutcomePayload,
    SignalType.USER_FACT: UserFactPayload,
    SignalType.DEMAND_EVENT: DemandEventPayload,
    SignalType.PRODUCTION_CONSTRAINT: ProductionConstraintPayload,
    SignalType.STAFF_AVAILABILITY: StaffAvailabilityPayload,
    SignalType.MENU_TOGGLE_REQUEST: MenuToggleRequestPayload,
    SignalType.INVENTORY_RECEIPT_REPORTED: InventoryReceiptReportedPayload,
    SignalType.INVENTORY_COUNT_REPORTED: InventoryCountReportedPayload,
    SignalType.INGREDIENT_SHORTAGE_REPORTED: IngredientShortageReportedPayload,
    SignalType.EXPIRY_USE_PRIORITY: ExpiryUsePriorityPayload,
    SignalType.SUPPLIER_CATALOG_NOTE: SupplierCatalogNotePayload,
    SignalType.CUSTOMER_FEEDBACK_NOTE: CustomerFeedbackNotePayload,
    SignalType.COMPETITOR_NOTE: CompetitorNotePayload,
    SignalType.OPERATIONAL_BRIEFING: OperationalBriefingPayload,
    SignalType.BATCH_PROGRESS: BatchProgressPayload,
    SignalType.DEMAND_FORECAST_HORIZON: DemandForecastHorizonPayload,
    SignalType.INGREDIENT_UNCOVERABLE: IngredientUncoverablePayload,
    SignalType.FOOD_SAFETY_CHECK: FoodSafetyCheckPayload,
    SignalType.ORDER_BACKLOG: OrderBacklogPayload,
    SignalType.EQUIPMENT_FAILURE: EquipmentFailurePayload,
}
