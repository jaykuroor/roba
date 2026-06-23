"""All SQLAlchemy tables — the single source of truth (00_ARCHITECTURE.md §19).

Conventions (§19):
- Every table has ``id INTEGER PRIMARY KEY AUTOINCREMENT`` unless noted
  (``signals`` is the only exception: it uses ``signal_id TEXT`` as PK).
- All ``*_at`` / ``*_time`` / ``expiry`` / ``expires_at`` columns are Float
  (sim-seconds since sim-epoch). Never wall-clock.
- JSON columns use ``sqlalchemy.JSON`` (TEXT holding JSON under SQLite).
- Booleans are stored as Integer (0/1).
- FKs are named ``<entity>_id``.

Tables are grouped exactly as in the spec:
  §19.1 reference/config, §19.2 state/transactional,
  §19.3 intelligence/agent I/O, §19.4 simulation/control.
"""

from sqlalchemy import Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import mapped_column

from .db import Base


def _pk():
    return mapped_column(Integer, primary_key=True, autoincrement=True)


# ---------------------------------------------------------------------------
# §19.1 Reference / config
# ---------------------------------------------------------------------------

class Ingredient(Base):
    __tablename__ = "ingredients"

    id = _pk()
    name = mapped_column(String)
    category = mapped_column(String)
    base_unit = mapped_column(String)          # g | ml | each
    perishable = mapped_column(Integer)        # bool 0/1
    shelf_life_days = mapped_column(Float)
    allergen_flags = mapped_column(JSON)
    weather_tags = mapped_column(JSON)
    notes = mapped_column(Text)

    def __repr__(self):
        return f"<Ingredient id={self.id} name={self.name!r} category={self.category!r}>"


class Station(Base):
    __tablename__ = "stations"

    id = _pk()
    name = mapped_column(String)

    def __repr__(self):
        return f"<Station id={self.id} name={self.name!r}>"


class MenuItem(Base):
    __tablename__ = "menu_items"

    id = _pk()
    name = mapped_column(String)
    category = mapped_column(String)
    station_id = mapped_column(ForeignKey("stations.id"))
    dine_in_price = mapped_column(Float)
    online_price = mapped_column(Float)
    prep_time_min = mapped_column(Float)
    is_batchable = mapped_column(Integer)      # bool 0/1
    active = mapped_column(Integer, default=1)  # bool 0/1
    weather_tags = mapped_column(JSON)
    description = mapped_column(Text)

    def __repr__(self):
        return f"<MenuItem id={self.id} name={self.name!r} active={self.active}>"


class Recipe(Base):
    __tablename__ = "recipes"

    id = _pk()
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))

    def __repr__(self):
        return f"<Recipe id={self.id} menu_item_id={self.menu_item_id}>"


class RecipeLine(Base):
    __tablename__ = "recipe_lines"

    id = _pk()
    recipe_id = mapped_column(ForeignKey("recipes.id"))
    ingredient_id = mapped_column(ForeignKey("ingredients.id"))
    qty = mapped_column(Float)
    unit = mapped_column(String)
    optional = mapped_column(Integer, default=0)  # bool 0/1

    def __repr__(self):
        return (f"<RecipeLine id={self.id} recipe_id={self.recipe_id} "
                f"ingredient_id={self.ingredient_id} qty={self.qty}>")


class BatchDefinition(Base):
    __tablename__ = "batch_definitions"

    id = _pk()
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    applicable_menus = mapped_column(JSON)
    dayparts = mapped_column(JSON)
    prep_lead_time_min = mapped_column(Float)
    batch_size_min = mapped_column(Float)
    batch_size_step = mapped_column(Float)
    batch_size_max = mapped_column(Float)
    decide_by_offset_min = mapped_column(Float)
    prepared_shelf_life_min = mapped_column(Float)
    station_id = mapped_column(ForeignKey("stations.id"))
    required_skill = mapped_column(String)
    default_cadence_min = mapped_column(Float)
    historical_attach_rate = mapped_column(Float)

    def __repr__(self):
        return f"<BatchDefinition id={self.id} menu_item_id={self.menu_item_id}>"


class Staff(Base):
    __tablename__ = "staff"

    id = _pk()
    name = mapped_column(String)
    role = mapped_column(String)
    skill_level = mapped_column(Integer)
    hourly_cost = mapped_column(Float)
    active = mapped_column(Integer, default=1)  # bool 0/1

    def __repr__(self):
        return f"<Staff id={self.id} name={self.name!r} role={self.role!r}>"


class StaffStation(Base):
    __tablename__ = "staff_stations"  # M:N coverage

    id = _pk()
    staff_id = mapped_column(ForeignKey("staff.id"))
    station_id = mapped_column(ForeignKey("stations.id"))

    def __repr__(self):
        return f"<StaffStation id={self.id} staff_id={self.staff_id} station_id={self.station_id}>"


class StaffDishSkill(Base):
    __tablename__ = "staff_dish_skills"  # dish-level exceptions

    id = _pk()
    staff_id = mapped_column(ForeignKey("staff.id"))
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))

    def __repr__(self):
        return f"<StaffDishSkill id={self.id} staff_id={self.staff_id} menu_item_id={self.menu_item_id}>"


class Supplier(Base):
    __tablename__ = "suppliers"

    id = _pk()
    name = mapped_column(String)
    lead_time_days = mapped_column(Float)
    reliability_score = mapped_column(Float)
    min_order_value = mapped_column(Float)
    contact = mapped_column(String)

    def __repr__(self):
        return f"<Supplier id={self.id} name={self.name!r} lead_time_days={self.lead_time_days}>"


class SupplierCatalog(Base):
    __tablename__ = "supplier_catalog"

    id = _pk()
    supplier_id = mapped_column(ForeignKey("suppliers.id"))
    ingredient_id = mapped_column(ForeignKey("ingredients.id"))
    current_price = mapped_column(Float)
    unit = mapped_column(String)
    pack_size = mapped_column(Float)
    availability = mapped_column(String)       # in_stock | limited | out
    updated_at = mapped_column(Float)

    def __repr__(self):
        return (f"<SupplierCatalog id={self.id} supplier_id={self.supplier_id} "
                f"ingredient_id={self.ingredient_id} current_price={self.current_price}>")


# ---------------------------------------------------------------------------
# §19.2 State / transactional
# ---------------------------------------------------------------------------

class InventoryLot(Base):
    __tablename__ = "inventory_lots"

    id = _pk()
    ingredient_id = mapped_column(ForeignKey("ingredients.id"))
    qty_on_hand = mapped_column(Float)
    unit = mapped_column(String)
    purchase_price = mapped_column(Float)
    purchase_date = mapped_column(Float)
    received_date = mapped_column(Float)
    expiry_date = mapped_column(Float)
    supplier_id = mapped_column(ForeignKey("suppliers.id"))
    storage_location = mapped_column(String)
    status = mapped_column(String)             # active | depleted | expired

    def __repr__(self):
        return (f"<InventoryLot id={self.id} ingredient_id={self.ingredient_id} "
                f"qty_on_hand={self.qty_on_hand} status={self.status!r}>")


class InventoryLedger(Base):
    __tablename__ = "inventory_ledger"  # append-only; source of truth

    id = _pk()
    ingredient_id = mapped_column(ForeignKey("ingredients.id"))
    lot_id = mapped_column(ForeignKey("inventory_lots.id"))
    delta_qty = mapped_column(Float)
    reason = mapped_column(String)             # receipt | sale_depletion | batch_depletion | waste | reconciliation
    ref_id = mapped_column(Integer)
    sim_time = mapped_column(Float)
    balance_after = mapped_column(Float)

    def __repr__(self):
        return (f"<InventoryLedger id={self.id} ingredient_id={self.ingredient_id} "
                f"reason={self.reason!r} delta_qty={self.delta_qty}>")


class InventoryLevel(Base):
    __tablename__ = "inventory_levels"

    id = _pk()
    ingredient_id = mapped_column(ForeignKey("ingredients.id"), unique=True)
    par_level = mapped_column(Float)
    reorder_point = mapped_column(Float)
    safety_stock = mapped_column(Float)
    yield_factor = mapped_column(Float, default=1.0)
    on_hand_cached = mapped_column(Float)
    last_counted_at = mapped_column(Float)
    last_counted_qty = mapped_column(Float)

    def __repr__(self):
        return (f"<InventoryLevel id={self.id} ingredient_id={self.ingredient_id} "
                f"on_hand_cached={self.on_hand_cached}>")


class Order(Base):
    __tablename__ = "orders"

    id = _pk()
    sim_time = mapped_column(Float)
    service_mode = mapped_column(String)       # dine_in | delivery | takeout
    table_no = mapped_column(String)
    staff_id = mapped_column(ForeignKey("staff.id"))
    guest_count = mapped_column(Integer)
    status = mapped_column(String)             # open | closed | cancelled
    channel = mapped_column(String)
    total = mapped_column(Float)

    def __repr__(self):
        return f"<Order id={self.id} sim_time={self.sim_time} status={self.status!r} total={self.total}>"


class OrderLine(Base):
    __tablename__ = "order_lines"

    id = _pk()
    order_id = mapped_column(ForeignKey("orders.id"))
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    qty = mapped_column(Float)
    unit_price = mapped_column(Float)
    modifiers = mapped_column(JSON)
    discount = mapped_column(Float)
    line_total = mapped_column(Float)
    status = mapped_column(String)             # sold | voided | comped
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<OrderLine id={self.id} order_id={self.order_id} "
                f"menu_item_id={self.menu_item_id} qty={self.qty} status={self.status!r}>")


class Batch(Base):
    __tablename__ = "batches"

    id = _pk()
    batch_definition_id = mapped_column(ForeignKey("batch_definitions.id"))
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    decided_at = mapped_column(Float)
    serve_window = mapped_column(JSON)
    decision = mapped_column(String)           # cook | skip
    planned_qty = mapped_column(Integer)
    actual_made_qty = mapped_column(Float)
    sold_qty = mapped_column(Float)
    wasted_qty = mapped_column(Float)
    status = mapped_column(String)             # decided | prepping | ready | served | expired
    by = mapped_column(String)                 # agent | human

    def __repr__(self):
        return (f"<Batch id={self.id} menu_item_id={self.menu_item_id} "
                f"decision={self.decision!r} status={self.status!r}>")


class WasteEvent(Base):
    __tablename__ = "waste_events"

    id = _pk()
    waste_type = mapped_column(String)         # overproduction | spoilage | cancelled_order | prep_error | expiry
    ingredient_id = mapped_column(ForeignKey("ingredients.id"), nullable=True)
    menu_item_id = mapped_column(ForeignKey("menu_items.id"), nullable=True)
    lot_id = mapped_column(ForeignKey("inventory_lots.id"), nullable=True)
    qty = mapped_column(Float)
    unit = mapped_column(String)
    cost = mapped_column(Float)
    reason = mapped_column(String)
    sim_time = mapped_column(Float)
    source = mapped_column(String)

    def __repr__(self):
        return f"<WasteEvent id={self.id} waste_type={self.waste_type!r} cost={self.cost}>"


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = _pk()
    supplier_id = mapped_column(ForeignKey("suppliers.id"))
    status = mapped_column(String)             # proposed | approved | placed | delivered | cancelled
    created_at = mapped_column(Float)
    expected_delivery = mapped_column(Float)
    total_cost = mapped_column(Float)
    created_by = mapped_column(String)
    approval_id = mapped_column(ForeignKey("approval_requests.id"), nullable=True)

    def __repr__(self):
        return (f"<PurchaseOrder id={self.id} supplier_id={self.supplier_id} "
                f"status={self.status!r} total_cost={self.total_cost}>")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id = _pk()
    po_id = mapped_column(ForeignKey("purchase_orders.id"))
    ingredient_id = mapped_column(ForeignKey("ingredients.id"))
    qty = mapped_column(Float)
    unit = mapped_column(String)
    unit_price = mapped_column(Float)
    line_total = mapped_column(Float)

    def __repr__(self):
        return (f"<PurchaseOrderLine id={self.id} po_id={self.po_id} "
                f"ingredient_id={self.ingredient_id} qty={self.qty}>")


class Attendance(Base):
    __tablename__ = "attendance"

    id = _pk()
    staff_id = mapped_column(ForeignKey("staff.id"))
    date_sim_day = mapped_column(Integer)      # sim day number
    status = mapped_column(String)             # present | leave | sick
    daypart = mapped_column(String, nullable=True)  # null = whole day
    reason = mapped_column(String, nullable=True)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<Attendance id={self.id} staff_id={self.staff_id} "
                f"date_sim_day={self.date_sim_day} status={self.status!r}>")


class MenuToggle(Base):
    __tablename__ = "menu_toggles"

    id = _pk()
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    action = mapped_column(String)             # disable | enable
    reason = mapped_column(String)
    triggered_by = mapped_column(String)
    sim_time = mapped_column(Float)
    active = mapped_column(Integer)            # bool 0/1

    def __repr__(self):
        return (f"<MenuToggle id={self.id} menu_item_id={self.menu_item_id} "
                f"action={self.action!r} active={self.active}>")


# ---------------------------------------------------------------------------
# §19.3 Intelligence / agent I/O
# ---------------------------------------------------------------------------

class Forecast(Base):
    __tablename__ = "forecasts"

    id = _pk()
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    window = mapped_column(JSON)
    daypart = mapped_column(String)
    forecast_qty = mapped_column(Integer)
    baseline_qty = mapped_column(Float)
    multipliers = mapped_column(JSON)
    confidence = mapped_column(Float)
    generated_at = mapped_column(Float)
    trigger_reason = mapped_column(String)

    def __repr__(self):
        return (f"<Forecast id={self.id} menu_item_id={self.menu_item_id} "
                f"daypart={self.daypart!r} forecast_qty={self.forecast_qty}>")


class ForecastOverride(Base):
    __tablename__ = "forecast_overrides"

    id = _pk()
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    daypart = mapped_column(String)
    window = mapped_column(JSON)
    operation = mapped_column(String)             # set_target | hard_zero_production
    value = mapped_column(JSON)
    reason = mapped_column(Text)
    source = mapped_column(String)                # human | llm
    authority = mapped_column(String)             # human_locked | approved_llm
    status = mapped_column(String)                # active | superseded | expired
    created_at = mapped_column(Float)
    valid_until = mapped_column(Float)
    evidence = mapped_column(JSON)

    def __repr__(self):
        return (f"<ForecastOverride id={self.id} menu_item_id={self.menu_item_id} "
                f"operation={self.operation!r} status={self.status!r}>")


class ForecastTrace(Base):
    __tablename__ = "forecast_traces"

    id = _pk()
    forecast_id = mapped_column(ForeignKey("forecasts.id"))
    run_id = mapped_column(String)
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    daypart = mapped_column(String)
    window = mapped_column(JSON)
    trace = mapped_column(JSON)
    summary = mapped_column(Text)
    created_at = mapped_column(Float)

    def __repr__(self):
        return (f"<ForecastTrace id={self.id} forecast_id={self.forecast_id} "
                f"run_id={self.run_id!r}>")


class ForecastAdjustment(Base):
    __tablename__ = "forecast_adjustments"

    id = _pk()
    forecast_id = mapped_column(ForeignKey("forecasts.id"))
    run_id = mapped_column(String)
    menu_item_id = mapped_column(ForeignKey("menu_items.id"))
    stage = mapped_column(String)
    source = mapped_column(String)
    modifier_key = mapped_column(String)
    operation = mapped_column(String)
    value = mapped_column(JSON)
    reason = mapped_column(Text)
    evidence = mapped_column(JSON)
    created_at = mapped_column(Float)

    def __repr__(self):
        return (f"<ForecastAdjustment id={self.id} forecast_id={self.forecast_id} "
                f"modifier_key={self.modifier_key!r}>")


class DemandForecasterMemory(Base):
    __tablename__ = "demand_forecaster_memory"

    id = _pk()
    scope_type = mapped_column(String)         # global | menu_item | station | weather | constraint
    scope_ref = mapped_column(String)
    insight = mapped_column(JSON)
    evidence = mapped_column(JSON)
    confidence = mapped_column(Float)
    created_at = mapped_column(Float)
    last_seen_at = mapped_column(Float)
    valid_until = mapped_column(Float)
    source = mapped_column(String)             # deterministic | llm | evaluator | user_fact

    def __repr__(self):
        return (f"<DemandForecasterMemory id={self.id} "
                f"scope_type={self.scope_type!r} scope_ref={self.scope_ref!r}>")


class Signal(Base):
    __tablename__ = "signals"

    # NOTE: per §19.3 this is the one table whose PK is signal_id TEXT,
    # not the conventional autoincrement integer id.
    signal_id = mapped_column(String, primary_key=True)
    type = mapped_column(String)
    source = mapped_column(String)
    groups = mapped_column(JSON)
    priority = mapped_column(Integer)
    payload = mapped_column(JSON)
    created_at = mapped_column(Float)
    expires_at = mapped_column(Float)
    dedup_key = mapped_column(String)
    status = mapped_column(String)             # live | consumed | expired
    correlation_id = mapped_column(String)

    __table_args__ = (
        Index("ix_signals_status", "status"),
        Index("ix_signals_dedup_key_status", "dedup_key", "status"),
    )

    def __repr__(self):
        return (f"<Signal signal_id={self.signal_id!r} type={self.type!r} "
                f"status={self.status!r} dedup_key={self.dedup_key!r}>")


class Competitor(Base):
    __tablename__ = "competitors"

    id = _pk()
    name = mapped_column(String)
    platform = mapped_column(String)
    cuisine = mapped_column(JSON)
    distance_km = mapped_column(Float)
    rating = mapped_column(Float)
    is_open = mapped_column(Integer)           # bool 0/1
    price_tier = mapped_column(String)
    updated_at = mapped_column(Float)

    def __repr__(self):
        return f"<Competitor id={self.id} name={self.name!r} rating={self.rating}>"


class CompetitorOffer(Base):
    __tablename__ = "competitor_offers"

    id = _pk()
    competitor_id = mapped_column(ForeignKey("competitors.id"))
    dish_or_combo = mapped_column(String)
    price = mapped_column(Float)
    description = mapped_column(Text)
    updated_at = mapped_column(Float)

    def __repr__(self):
        return (f"<CompetitorOffer id={self.id} competitor_id={self.competitor_id} "
                f"dish_or_combo={self.dish_or_combo!r} price={self.price}>")


class CompetitorIntel(Base):
    __tablename__ = "competitor_intel"

    id = _pk()
    competitor_id = mapped_column(ForeignKey("competitors.id"))
    method = mapped_column(String)             # call | aggregator | discovery
    popular_dishes = mapped_column(JSON)
    price_points = mapped_column(JSON)
    notes = mapped_column(Text)
    call_id = mapped_column(ForeignKey("calls.id"), nullable=True)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<CompetitorIntel id={self.id} competitor_id={self.competitor_id} "
                f"method={self.method!r}>")


class CompetitorObservation(Base):
    __tablename__ = "competitor_observations"

    id = _pk()
    competitor_id = mapped_column(ForeignKey("competitors.id"), nullable=True)
    source_channel = mapped_column(String)       # aggregator | web | probe | scenario
    platform = mapped_column(String)
    signal_kind = mapped_column(String)
    direction = mapped_column(String)            # opportunity | threat | drag | watch
    impact_score = mapped_column(Float)
    confidence = mapped_column(Float)
    affected_menu_items = mapped_column(JSON)
    affected_categories = mapped_column(JSON)
    window = mapped_column(JSON)
    evidence = mapped_column(JSON)
    raw = mapped_column(JSON)
    state_hash = mapped_column(String)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<CompetitorObservation id={self.id} kind={self.signal_kind!r} "
                f"competitor_id={self.competitor_id}>")


class CompetitorMenuSnapshot(Base):
    __tablename__ = "competitor_menu_snapshots"

    id = _pk()
    competitor_id = mapped_column(ForeignKey("competitors.id"))
    source_channel = mapped_column(String)
    platform = mapped_column(String)
    menu_hash = mapped_column(String)
    items = mapped_column(JSON)
    compliance = mapped_column(JSON)
    fetched_at = mapped_column(Float)

    def __repr__(self):
        return (f"<CompetitorMenuSnapshot id={self.id} competitor_id={self.competitor_id} "
                f"menu_hash={self.menu_hash!r}>")


class CompetitorProbeResult(Base):
    __tablename__ = "competitor_probe_results"

    id = _pk()
    competitor_id = mapped_column(ForeignKey("competitors.id"))
    source_channel = mapped_column(String)
    platform = mapped_column(String)
    estimated_wait_min = mapped_column(Float)
    availability = mapped_column(String)
    tactic_labels = mapped_column(JSON)
    confidence = mapped_column(Float)
    transcript = mapped_column(JSON)
    raw = mapped_column(JSON)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<CompetitorProbeResult id={self.id} competitor_id={self.competitor_id} "
                f"wait={self.estimated_wait_min}>")


class Review(Base):
    __tablename__ = "reviews"

    id = _pk()
    source = mapped_column(String)
    rating = mapped_column(Float)
    text = mapped_column(Text)
    dish_mentions = mapped_column(JSON)
    sentiment = mapped_column(String)
    sim_time = mapped_column(Float)
    processed = mapped_column(Integer, default=0)  # bool 0/1

    def __repr__(self):
        return f"<Review id={self.id} source={self.source!r} rating={self.rating} processed={self.processed}>"


class ReviewInsight(Base):
    __tablename__ = "review_insights"

    id = _pk()
    review_id = mapped_column(ForeignKey("reviews.id"), nullable=True)
    insight_type = mapped_column(String)
    summary = mapped_column(Text)
    suggested_action = mapped_column(Text)
    severity = mapped_column(String)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<ReviewInsight id={self.id} review_id={self.review_id} "
                f"insight_type={self.insight_type!r} severity={self.severity!r}>")


class SupplierPriceHistory(Base):
    __tablename__ = "supplier_price_history"

    id = _pk()
    supplier_id = mapped_column(ForeignKey("suppliers.id"))
    ingredient_id = mapped_column(ForeignKey("ingredients.id"))
    price = mapped_column(Float)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<SupplierPriceHistory id={self.id} supplier_id={self.supplier_id} "
                f"ingredient_id={self.ingredient_id} price={self.price}>")


class Negotiation(Base):
    __tablename__ = "negotiations"

    id = _pk()
    supplier_id = mapped_column(ForeignKey("suppliers.id"))
    ingredient_id = mapped_column(ForeignKey("ingredients.id"))
    call_id = mapped_column(ForeignKey("calls.id"), nullable=True)
    transcript = mapped_column(JSON)
    outcome = mapped_column(JSON)
    savings = mapped_column(Float)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<Negotiation id={self.id} supplier_id={self.supplier_id} "
                f"ingredient_id={self.ingredient_id} savings={self.savings}>")


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = _pk()
    type = mapped_column(String)               # purchase_order | menu_change | promo | outbound_call | other
    title = mapped_column(String)
    summary = mapped_column(Text)
    payload = mapped_column(JSON)
    urgency = mapped_column(String)
    status = mapped_column(String)             # pending | approved | rejected | expired
    created_at = mapped_column(Float)
    resolved_at = mapped_column(Float)
    resolved_by = mapped_column(String)
    ref_id = mapped_column(Integer)

    def __repr__(self):
        return (f"<ApprovalRequest id={self.id} type={self.type!r} "
                f"status={self.status!r} title={self.title!r}>")


class ForecastJob(Base):
    __tablename__ = "forecast_jobs"

    id = _pk()
    job_id = mapped_column(String, unique=True, index=True)
    kind = mapped_column(String)               # deterministic_forecast | llm_finalizer
    status = mapped_column(String)             # queued | running | succeeded | failed | superseded | stale
    sim_time = mapped_column(Float)
    daypart = mapped_column(String)
    window = mapped_column(JSON)
    requested_by = mapped_column(String)
    trigger_reason = mapped_column(String)
    created_at = mapped_column(Float)
    started_at = mapped_column(Float, nullable=True)
    finished_at = mapped_column(Float, nullable=True)
    error = mapped_column(Text, nullable=True)
    result = mapped_column(JSON)

    def __repr__(self):
        return (f"<ForecastJob id={self.id} job_id={self.job_id!r} "
                f"kind={self.kind!r} status={self.status!r}>")


class Promotion(Base):
    __tablename__ = "promotions"

    id = _pk()
    type = mapped_column(String)               # combo | discount
    menu_items = mapped_column(JSON)
    trigger = mapped_column(String)            # expiry | slow_mover | intel
    discount_pct = mapped_column(Float)
    channel = mapped_column(String)            # menu | aggregator | both
    status = mapped_column(String)             # proposed | approved | active | expired
    approval_id = mapped_column(ForeignKey("approval_requests.id"), nullable=True)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return (f"<Promotion id={self.id} type={self.type!r} "
                f"trigger={self.trigger!r} status={self.status!r}>")


class UserFact(Base):
    __tablename__ = "user_facts"

    id = _pk()
    raw_text = mapped_column(Text)
    source = mapped_column(String)             # voice | text
    extracted = mapped_column(JSON)
    applied = mapped_column(Integer)           # bool 0/1
    resulting_writes = mapped_column(JSON)
    sim_time = mapped_column(Float)

    def __repr__(self):
        return f"<UserFact id={self.id} source={self.source!r} applied={self.applied}>"


class WeatherLog(Base):
    __tablename__ = "weather_log"

    id = _pk()
    sim_time = mapped_column(Float)
    source = mapped_column(String)             # api | override
    temp_c = mapped_column(Float)
    condition = mapped_column(String)          # clear | clouds | rain | storm | snow
    precip_mm = mapped_column(Float)
    wind_kph = mapped_column(Float)
    applied = mapped_column(Integer)           # bool 0/1

    def __repr__(self):
        return (f"<WeatherLog id={self.id} sim_time={self.sim_time} "
                f"condition={self.condition!r} temp_c={self.temp_c}>")


class Call(Base):
    __tablename__ = "calls"

    id = _pk()
    agent = mapped_column(String)              # market_spectator | competitor_intel
    counterparty_type = mapped_column(String)  # supplier | competitor
    counterparty_id = mapped_column(Integer)
    purpose = mapped_column(Text)
    status = mapped_column(String)             # requested | approved | rejected | active | completed | failed | auto_resolved
    approval_id = mapped_column(ForeignKey("approval_requests.id"), nullable=True)
    transcript = mapped_column(JSON)
    outcome = mapped_column(JSON)
    started_at = mapped_column(Float)
    ended_at = mapped_column(Float)
    clock_action = mapped_column(String)       # freeze | slow

    def __repr__(self):
        return (f"<Call id={self.id} agent={self.agent!r} "
                f"counterparty_type={self.counterparty_type!r} status={self.status!r}>")


# ---------------------------------------------------------------------------
# §19.4 Simulation / control
# ---------------------------------------------------------------------------

class SimState(Base):
    __tablename__ = "sim_state"  # id=1 singleton

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    sim_time = mapped_column(Float)
    day_number = mapped_column(Integer)
    day_of_week = mapped_column(Integer)
    speed = mapped_column(Float)
    status = mapped_column(String)             # stopped | running | paused | call_frozen
    operating_window = mapped_column(JSON)
    skip_closed_hours = mapped_column(Integer, default=1)  # bool 0/1
    call_mode = mapped_column(String)          # freeze | slow
    active_seed_id = mapped_column(String)

    def __repr__(self):
        return f"<SimState id={self.id} sim_time={self.sim_time} status={self.status!r} speed={self.speed}>"


class SimSettings(Base):
    __tablename__ = "sim_settings"  # id=1 singleton

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_orders_per_day = mapped_column(Integer, default=300)
    velocity = mapped_column(Float, default=1.0)
    dish_mix_weights = mapped_column(JSON)
    daypart_curve = mapped_column(JSON)
    channel_mix = mapped_column(JSON)
    anomaly_injections = mapped_column(JSON)

    def __repr__(self):
        return (f"<SimSettings id={self.id} base_orders_per_day={self.base_orders_per_day} "
                f"velocity={self.velocity}>")


class Scenario(Base):
    __tablename__ = "scenarios"

    id = _pk()
    name = mapped_column(String)
    description = mapped_column(Text)
    is_active = mapped_column(Integer)         # bool 0/1

    def __repr__(self):
        return f"<Scenario id={self.id} name={self.name!r} is_active={self.is_active}>"


class ScenarioEvent(Base):
    __tablename__ = "scenario_events"

    id = _pk()
    scenario_id = mapped_column(ForeignKey("scenarios.id"))
    at_sim_time = mapped_column(Float)
    event_type = mapped_column(String)
    payload = mapped_column(JSON)
    fired = mapped_column(Integer, default=0)  # bool 0/1

    def __repr__(self):
        return (f"<ScenarioEvent id={self.id} scenario_id={self.scenario_id} "
                f"at_sim_time={self.at_sim_time} event_type={self.event_type!r}>")


class EventLog(Base):
    __tablename__ = "event_log"  # the activity-log / narrative feed

    id = _pk()
    sim_time = mapped_column(Float)
    category = mapped_column(String)
    actor = mapped_column(String)
    summary = mapped_column(Text)
    detail = mapped_column(JSON)

    def __repr__(self):
        return (f"<EventLog id={self.id} sim_time={self.sim_time} "
                f"category={self.category!r} actor={self.actor!r}>")


# ---------------------------------------------------------------------------
# Table groupings (used by db.reset_db).
#
# These mostly mirror the §19 sections, with one deliberate deviation:
# `competitors` and `competitor_offers` are physically defined in the §19.3
# (intelligence) class block but are treated as *reference* data — they are
# seeded curated entities, so they survive a reset_db(keep_reference=True).
# Only the derived sensing intelligence (competitor_intel, review_insights,
# negotiations, supplier_price_history) plus the agent/runtime I/O tables are
# wiped.
# ---------------------------------------------------------------------------

REFERENCE_MODELS = [
    Ingredient, Station, MenuItem, Recipe, RecipeLine, BatchDefinition,
    Staff, StaffStation, StaffDishSkill, Supplier, SupplierCatalog,
    Competitor, CompetitorOffer,
]

TRANSACTIONAL_MODELS = [
    InventoryLot, InventoryLedger, InventoryLevel, Order, OrderLine,
    Batch, WasteEvent, PurchaseOrder, PurchaseOrderLine, MenuToggle,
    Attendance,
]

INTELLIGENCE_MODELS = [
    Forecast, ForecastOverride, ForecastTrace, ForecastAdjustment,
    DemandForecasterMemory, Signal, CompetitorIntel, CompetitorObservation,
    CompetitorMenuSnapshot, CompetitorProbeResult, Review, ReviewInsight,
    SupplierPriceHistory, Negotiation,
    ApprovalRequest, ForecastJob, Promotion, UserFact, WeatherLog, Call,
]

CONTROL_MODELS = [
    SimState, SimSettings, Scenario, ScenarioEvent, EventLog,
]
