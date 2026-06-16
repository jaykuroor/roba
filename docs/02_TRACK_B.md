# 02 — Track B Implementation Brief: Inventory & Procurement
### Owner: Programmer B. Depends entirely on `00_ARCHITECTURE.md` (read it first).

---

## B0. How to read this (rules)
- `00_ARCHITECTURE.md` is authoritative for contracts (signals §14–15, schema §19, REST/WS §20–21, algorithms §18.4/§18.8, constants §22). This file says **what Track B builds on top**.
- **Hard boundary:** Track B may read **`core` tables** and **signals**, and write only its **own tables** (`inventory_lots`, `inventory_ledger`, `inventory_levels`, `waste_events`, `purchase_orders(_lines)`, `menu_toggles` (+ `menu_items.active`), `promotions`, `negotiations`, `supplier_price_history`, and the **dynamic price/availability fields of `supplier_catalog`** — `core` seeds that table, §8.5/§18.8). **Approvals are core-owned** (the queue + inbox + `/approvals/*` endpoints live in `core`, §19.4): Track B only *creates* requests (via the core approvals helper) and *acts on* the `APPROVAL_RESOLVED` signal for its own types (PO/promo). It must **never** read/write Track A tables (`forecasts`, `batches`, `competitor_*`, `review_insights`). Demand reaches Track B **only via signals** (`DEMAND_FORECAST`, `BATCH_DECISION`).
- **Standalone:** in `DEMO_MODE=track_b`, `track_b/mocks/MockForecaster` supplies `DEMAND_FORECAST` + `BATCH_DECISION`. **This mock drives all of Track B** — build it early. In `combined`, mocks off, real Track A signals arrive — no code change.

## B1. Scope & deliverables
Three agents + two services + one mock + four UI panels:
1. **Inventory Ledger** (deterministic state) — `track_b/agents/ledger.py`
2. **Inventory Optimizer** (decisions) — `track_b/agents/optimizer.py`
3. **Market Spectator** (supplier costs + negotiation calls) — `track_b/agents/market_spectator.py`
4. **Procurement service** — `track_b/procurement/procurement.py`
5. **Approval handlers** (act on `APPROVAL_RESOLVED` for PO/promo; the queue + inbox + endpoints are in `core`) — `track_b/approval/handlers.py`
6. **MockForecaster** — `track_b/mocks/mock_forecaster.py`
7. **UI:** Inventory dashboard, Expiry view, Supplier editor, Activity log — `frontend/src/track_b/*`. (The **Approval Inbox is part of the core shell** §23, shown in every mode — Track B does not build it.)

## B2. Folder layout
```
track_b/                      # Python only
  agents/      ledger.py  optimizer.py  market_spectator.py
  procurement/ procurement.py
  approval/    handlers.py     # acts on APPROVAL_RESOLVED (PO/promo); no queue here
  mocks/       mock_forecaster.py
  tests/       test_ledger.py  test_optimizer.py  test_market.py  test_procurement.py  test_approval_handlers.py  test_contract_b.py
frontend/src/track_b/         # React panels (mounted by the core shell §23)
  InventoryDashboard.tsx  ExpiryView.tsx  SupplierEditor.tsx  ActivityLog.tsx
```

## B3. What you consume from `core`
`BaseAgent`, `bus`, `SimClock`, `orchestrator.register_trigger`, `llm.complete`, `calls.request_call` (§8), DB session + core/reference tables, and the **order-line callback** `bus.register_order_line_handler(fn)` (core's POS sim calls it per new line — §10; this is how depletion is driven without putting order-lines on the signal bus).

---

## B4. Agents & services

### B4.1 Inventory Ledger (`ledger.py`) — the source of truth
**Purpose:** maintain stock as an append-only ledger; raise stock/expiry/waste signals; handle receipts & reconciliation.
**Subscribe groups:** `inventory`. **Inputs:** order-line callback (sold lines); `BATCH_DECISION(cook)` (batch depletion); `USER_FACT(record_receipt|add_inventory_count)`; PO deliveries (from Procurement). **Triggers:** order-line callback; signals above; expiry scan interval `EXPIRY_SCAN_SIM_S`.
**Depletion (implement §18.4 exactly):** for each sold line and each cooked batch, for each `recipe_line`, `used = qty × recipe_qty / yield_factor`; deplete FIFO across `inventory_lots` (oldest `expiry` first); append `inventory_ledger(reason=sale_depletion|batch_depletion, delta=−used, ref_id, balance_after)`; update lot + `inventory_levels.on_hand_cached`; broadcast `inventory_updated`. **on_hand is always the ledger sum.**
**Thresholds:** after depletion, if `projected_remaining_day_demand` (from latest `DEMAND_FORECAST` × outstanding recipe usage) would push `on_hand` below `safety_stock` → `LOW_STOCK`; if below 0 before resupply eta → `STOCKOUT_RISK {affected_items}` (dedup_keys `low_stock:{ing}` / `stockout:{ing}`).
**Receipts:** on PO delivery (`procurement.receive(po)`) or `record_receipt` voice fact → create `inventory_lot` + `inventory_ledger(receipt, +qty)`.
**Reconciliation:** on `add_inventory_count` → set `inventory_levels.last_counted_*` + `inventory_ledger(reconciliation, delta = counted − ledger_on_hand)`; surface the drift to the dashboard (do **not** hide it).
**Waste:** overproduction at serve-window end and expired lots → `waste_events` row + `WASTE_EVENT` (§16 relay).

### B4.2 Inventory Optimizer (`optimizer.py`) — the decisions
**Purpose:** reorder, menu toggling, expiry-driven promos. Consumes demand to size everything.
**Subscribe groups:** `inventory, procurement`. **Inputs:** `DEMAND_FORECAST` (or mock), `LOW_STOCK`, `STOCKOUT_RISK`, `EXPIRY_RISK`, `BATCH_DECISION`, `WASTE_EVENT`. **Triggers:** those signals; reorder check interval; expiry handling.
**Reorder (implement §18.8):** when `on_hand ≤ reorder_point` (reorder_point uses forecasted daily usage × supplier lead, §12.2), build PO `qty = par_level − on_hand` rounded to `pack_size`; choose supplier by `score = availability_weight − price_norm − lead_norm` (read `suppliers`/`supplier_catalog`, core tables); hand to **Procurement**; if `PO_total > APPROVAL_PO_THRESHOLD` → Procurement routes through Approval, else auto-place; emit `REORDER_PLACED`.
**Menu toggle (implement §18.8):** when an ingredient's `projected_runout < resupply_eta` and it is shared with a faster mover, disable the dish with the lowest `margin × velocity` using it (margin = price − recipe cost via `supplier_catalog`; velocity via formatter), set `menu_items.active=0`, write `menu_toggles`, emit `MENU_TOGGLE(disable, reason)`; re-enable (`MENU_TOGGLE(enable)`) when `on_hand > reorder_point`. (Track A reacts via the signal.)
**Expiry → promo (implement §18.8):** on `EXPIRY_RISK`, create a `promotions` row (combo/discount `PROMO_DISCOUNT_PCT`, `status=proposed`) and an `approval_requests(type=promo)`; emit `PROMO_PROPOSAL`. On approval → `status=active`.
**Every action writes a clear `event_log` line** (the Activity log narrative).

### B4.3 Market Spectator (`market_spectator.py`) — supplier costs & negotiation
**Purpose:** track supplier prices; negotiate via approval-gated voice calls; react to spoilage.
**Subscribe groups:** `procurement, inventory`. **Triggers:** interval (price review); before large reorders; `WASTE_EVENT(spoilage)`.
**Monitoring:** watch `supplier_catalog` + `supplier_price_history`; when a price is above its historical median by a margin, or a big reorder is imminent, consider negotiating.
**Negotiation call (uses core call subsystem §8):**
1. `calls.request_call(agent="market_spectator", counterparty_type="supplier", counterparty_id, purpose="negotiate {ingredient} price")` → `CALL_REQUEST` + approval card.
2. On approve: clock freezes, ROLEPLAY console ("You are playing: {Supplier}"), turn loop. Agent goal = lower unit price / better terms; opens with current price from `supplier_catalog`; businesslike; confirms the agreed number. Turn text via `llm.complete` (§8.3 supplier prompt); canned = a scripted negotiation line.
3. On hangup: **core** parses the outcome and emits `CALL_OUTCOME` (§8.5) — core writes no Track B tables. **Market Spectator consumes `CALL_OUTCOME`** and, if `agreed`, updates `supplier_catalog.current_price` + appends `supplier_price_history` + writes `negotiations` + emits `SUPPLIER_PRICE_UPDATE(via="call")`.
**Spoilage reaction:** on repeated `WASTE_EVENT(spoilage)` for an ingredient, recommend ordering less/fresher (log_event + optionally adjust the par used for that ingredient).
**Fallback:** presenter declines → core auto-resolves with an LLM-simulated supplier seeded from the catalog price band.

### B4.4 Procurement service (`procurement.py`)
Turns Optimizer reorders into POs: create `purchase_orders(+lines)`; if over threshold, create `approval_requests(type=purchase_order)` (status pending) and wait; else `status=placed` + `expected_delivery=now+lead`. Register an orchestrator trigger at `expected_delivery` → mark `delivered` and call **`ledger.receive(po)`** (Ledger is the only inventory writer). Emit `REORDER_PLACED` on placement.

### B4.5 Approval handlers (`approval/handlers.py`) — act on resolutions
The approval **queue, inbox UI, `/approvals/*` endpoints, TTL expiry, and the `APPROVAL_RESOLVED` emit all live in `core`** (§19.4, §23). Track B only **subscribes to `APPROVAL_RESOLVED`** (group `procurement`) and, on `decision="approved"`, executes **its own** request types: `purchase_order` → `procurement.place(po)`; `promo` → `optimizer.activate_promo(promo_id)`. (`outbound_call` resolutions are handled by the core call subsystem, not here; ignore types that aren't yours.) To raise a request, Track B calls the core helper **`approvals.create(type, title, summary, payload, ref_id)`** (which writes the row and emits `APPROVAL_REQUEST`) — it never writes `approval_requests` directly.

---

## B5. Standalone placeholder — MockForecaster (`mocks/mock_forecaster.py`)  ← build first
Active only when `DEMO_MODE=track_b`. **Drives the whole track.** Every `FORECAST_INTERVAL_SIM_S`, for each active item, emit `DEMAND_FORECAST {qty = seed_historical_mean(item,daypart,dow) × daypart_curve, baseline, multipliers:{}, confidence:0.8}` (read history from core tables). At each `batch_definition.decide_by`, emit `BATCH_DECISION(cook, qty=forecast)`. Numbers must be realistic enough to exercise depletion → reorder → toggle → expiry. Removed automatically in `combined`.

---

## B6. UI panels (`frontend/src/track_b/*`, mount in core shell)
WS consumers; relative paths; no business logic. (The **Approval Inbox** is provided by the core shell §23 and is where PO/promo/outbound_call cards are approved — Track B does not build it.)
- **InventoryDashboard** — per ingredient: `on_hand` vs `par`/`reorder_point`/`safety_stock`, **theoretical-vs-counted drift**, live depletion as orders flow, and which menu items are currently disabled. Consumes `inventory_updated`, `menu_toggled`, `order_created`.
- **ExpiryView** — lots with **expiry countdowns**, at-risk highlights, and active/proposed promotions. Consumes `signal_emitted(EXPIRY_RISK/PROMO_PROPOSAL)`.
- **SupplierEditor** — suppliers + `supplier_catalog` (price, availability, lead time) **editable live** (`PATCH /suppliers`...), negotiation history, and a **"Negotiate" button** per supplier/ingredient (→ `CALL_REQUEST`). The presenter both edits supplier data here and **plays the supplier** during calls. Consumes `signal_emitted(SUPPLIER_PRICE_UPDATE)` + `call_*`.
- **ActivityLog** — the `event_log` stream (the "what + why" narrative): reorders, toggles, promos, waste, negotiations. Consumes `event_logged`.

---

## B7. Track B acceptance criteria (standalone, `DEMO_MODE=track_b`)
1. With MockForecaster running, orders deplete ingredients via the ledger; on_hand tracks correctly and matches the ledger sum.
2. An ingredient crossing `reorder_point` produces a PO; under threshold it auto-places, over threshold it appears in the Approval inbox; approving places it and a delivery later restocks via a receipt lot.
3. Forcing an ingredient low (fast sales / supplier "out") triggers `LOW_STOCK`/`STOCKOUT_RISK` and a `MENU_TOGGLE(disable)` on the lowest margin×velocity dish sharing it; restock re-enables it.
4. A near-expiry lot raises `EXPIRY_RISK` → a `PROMO_PROPOSAL` in the inbox → approving activates the promotion.
5. A voice count creates a reconciliation entry and the dashboard shows the drift.
6. "Negotiate" creates an approval; approving freezes the clock, opens the ROLEPLAY console, runs a supplier call, and (on agreement) lowers the catalog price + emits `SUPPLIER_PRICE_UPDATE`.
7. Waste events accumulate a visible waste-cost figure.

## B8. Phase-2 integration (combined)
Set `DEMO_MODE=combined`: MockForecaster off; real `DEMAND_FORECAST`/`BATCH_DECISION` now come from Track A's Forecaster. **No code changes** — verify reorder/toggle/expiry behaviors against real forecasts during "Friday Rush".

## B9. Tests (pytest unless noted)
- `test_ledger`: FIFO depletion math; ledger == on_hand; threshold signals; receipt + reconciliation; waste emission.
- `test_optimizer`: reorder qty + supplier choice; auto vs approval threshold; toggle target selection (margin×velocity); re-enable; expiry→promo.
- `test_market`: negotiation request → outcome → price update + signal; spoilage reaction.
- `test_procurement`: PO lifecycle proposed→approved→placed→delivered→receive.
- `test_approval_handlers`: on `APPROVAL_RESOLVED(approved)` the PO is placed / promo activated for Track B's own types; non-Track-B types are ignored. (Queue, endpoints, and TTL expiry are core's — tested there.)
- `test_contract_b`: every signal B emits validates against §15; B subscribes only to its groups; B never imports Track A modules (assert via import check).
- Frontend (vitest): panels render from sample WS payloads; relative paths only.

*End of 02_TRACK_B.md.*
