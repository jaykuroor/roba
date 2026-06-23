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
    cost: float
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
}
