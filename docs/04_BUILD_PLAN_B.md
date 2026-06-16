# 04 — Build Plan & Test Playbook: Track B (Inventory & Procurement)
### For Programmer B. Read after `00_ARCHITECTURE.md` + `02_TRACK_B.md`.

> Execution guide for Track B: build order, how to drive AI coding agents (Claude Code / Codex), how to split the work, and what to test at each step. The *what* is in `00`/`02`; this is the *how* and *when*.

---

## 1. Before you start (prerequisites — do not skip)

1. **`core` exists and is green** — all `00 §24` criteria pass (clock, bus, POS sim + **the order-line callback** `bus.register_order_line_handler`, voice, calls, approvals queue/inbox, weather, `docker compose up`). Track B depends hard on the order-line callback (§10) and the approvals helper (§19.4) — verify both before building.
2. **Branch.** `git checkout main && git pull && git checkout -b track-b`. (Git rules: §6.)
3. **Read both docs** + skim `00 §15` (signals), `§18.4` (depletion), `§18.8` (reorder/toggle/expiry/promo), `§19.2–19.3` (your tables), `§22` (constants).
4. **Env.** `.env` set; confirm `DEMO_MODE=track_b` runs and shows the empty Track B tabs + the (core-provided) Approval Inbox.
5. **Boundary.** You create files only under `track_b/` and `frontend/src/track_b/`. You never edit `core/` or `track_a/`, and you reach demand **only** through `DEMAND_FORECAST`/`BATCH_DECISION` signals (real or mocked).

---

## 2. How to drive an AI coding agent (one task per session)

Same discipline as Track A. Each session prompt contains, in order:

1. **Context:** "Building Track B per `00_ARCHITECTURE.md` + `02_TRACK_B.md`; `core` is built and frozen — do not modify it."
2. **The milestone goal + exact files** (from `02 §B2`).
3. **The relevant spec by §:** e.g. for the Ledger, paste `00 §18.4 + §15 (LOW_STOCK/STOCKOUT_RISK/EXPIRY_RISK/WASTE_EVENT) + 02 §B4.1`.
4. **Hard constraints (every time):** "Do not modify `core/` or `track_a/`. Inventory is written **only** by the Ledger. Communicate via signals + reads of `core`/reference tables; never import `track_a`. Raise approvals via the core `approvals.create(...)` helper — do not write `approval_requests` directly. Every emitted signal matches its §15 payload. Add the listed tests and pass them."
5. **The milestone's acceptance tests** (§4).
6. **Stop + summarize** when green.

Small PR per milestone (§6).

---

## 3. Build order (dependency-ordered milestones)

**Build `MockForecaster` first — it drives the entire track.** Then the Ledger (the source of truth), then everything that reads it.

| # | Milestone | Depends on | Output |
|---|---|---|---|
| **B0** | Scaffold `track_b` + **`MockForecaster`** (critical) + register order-line handler + frontend tab shells | core | demand signals flow; panels mount |
| **B1** | **Inventory Ledger** — FIFO depletion | B0 | `on_hand` tracks; ledger == truth |
| **B2** | **Stock signals** + receipts + reconciliation + waste | B1 | `LOW_STOCK`/`STOCKOUT_RISK`/`EXPIRY_RISK`/`WASTE_EVENT` |
| **B3** | **Procurement + Optimizer reorder** | B2 | PO build, supplier scoring, `REORDER_PLACED`, approvals |
| **B4** | **Menu toggle** | B2, B3 | `MENU_TOGGLE` (disable/enable) |
| **B5** | **Expiry → promo + approval handlers** | B2, B3 | `PROMO_PROPOSAL`; acts on `APPROVAL_RESOLVED` |
| **B6** | **Market Spectator** (prices + supplier negotiation call) | core calls | `SUPPLIER_PRICE_UPDATE`, `negotiations` |
| **UI** | Panels for each (built alongside) | per-piece | Inventory/Expiry/Supplier/ActivityLog |

### Milestone detail

**B0 — Scaffold + MockForecaster (critical placeholder).** Create `track_b/agents/__init__.py` (register agents + triggers), empty `BaseAgent` subclasses, register the order-line handler, and `track_b/mocks/mock_forecaster.py` per `02 §B5` (emit `DEMAND_FORECAST` per item each interval + `BATCH_DECISION` at each `decide_by`, numbers from seed history). Frontend `index.ts` mounts four empty panels. *DoD:* with `DEMO_MODE=track_b`, demand signals appear in the core signal log and the panels mount. **Do this well — every later milestone is validated against this mock.**

**B1 — Inventory Ledger (the source of truth).** Implement `00 §18.4` + `02 §B4.1` depletion: on each sold order-line (callback) and each `BATCH_DECISION(cook)`, deplete FIFO across `inventory_lots` (oldest expiry first), append `inventory_ledger`, update `inventory_levels.on_hand_cached`, broadcast `inventory_updated`. **on_hand is always the ledger sum.** *DoD:* as the mock's orders flow, stock falls correctly; `sum(ledger) == on_hand` invariant holds in a test.

**B2 — Stock signals + receipts + reconciliation + waste.** Add threshold logic (`LOW_STOCK` at `safety_stock`, `STOCKOUT_RISK` before resupply), `record_receipt` voice fact → new lot, `add_inventory_count` → reconciliation ledger entry + visible drift, and the waste relay (overproduction/expiry → `waste_events` + `WASTE_EVENT`, `00 §16`). *DoD:* forcing low stock raises the right signals; a voice count shows drift; expiry produces a waste row + signal.

**B3 — Procurement + Optimizer reorder.** Implement `00 §18.8` reorder + `02 §B4.2/§B4.4`: when `on_hand ≤ reorder_point`, Optimizer sizes `qty = par − on_hand` (rounded to `pack_size`), picks a supplier by score, hands to Procurement; over `APPROVAL_PO_THRESHOLD` → `approvals.create(type=purchase_order)` and wait; else auto-place; `REORDER_PLACED`; register a delivery trigger at `expected_delivery` → mark delivered → `ledger.receive(po)`. *DoD:* below-threshold POs auto-place; above-threshold appear in the (core) Approval Inbox; approving places it and a later delivery restocks via a receipt lot.

**B4 — Menu toggle.** Implement `00 §18.8` toggle: when an ingredient's `projected_runout < resupply_eta` and it's shared, disable the lowest `margin × velocity` dish using it (set `menu_items.active=0`, write `menu_toggles`, emit `MENU_TOGGLE(disable)`); re-enable when `on_hand > reorder_point`. *DoD:* forcing an ingredient short disables the correct dish; restock re-enables it; the signal is what Track A reacts to.

**B5 — Expiry → promo + approval handlers.** On `EXPIRY_RISK`, create a `promotions(proposed)` row + `approvals.create(type=promo)` + emit `PROMO_PROPOSAL`; on the `APPROVAL_RESOLVED(approved, promo)` → activate it. Implement `track_b/approval/handlers.py` (`02 §B4.5`): subscribe to `APPROVAL_RESOLVED`, act on `purchase_order` (place) and `promo` (activate); ignore other types. *DoD:* a near-expiry lot → promo card in the inbox → approving activates it; approving a PO card places that PO.

**B6 — Market Spectator.** Implement `02 §B4.3`: price monitoring (vs `supplier_price_history`) → `SUPPLIER_PRICE_UPDATE(via="market")`; negotiation call via `calls.request_call(...)` → on `CALL_OUTCOME`, if agreed, update `supplier_catalog.current_price` + append `supplier_price_history` + write `negotiations` + emit `SUPPLIER_PRICE_UPDATE(via="call")`; spoilage reaction on repeated `WASTE_EVENT(spoilage)`. *DoD:* the negotiation call runs end-to-end (approval → freeze → roleplay → price drop) and the new price flows out; declining auto-resolves.

**UI** — Build each panel after its piece (`02 §B6`): InventoryDashboard after B1/B2 (on_hand vs par/reorder/safety + live depletion + **theoretical-vs-counted drift**), ExpiryView after B2/B5, SupplierEditor after B6 (editable catalog + "Negotiate" button), ActivityLog after B3 (the `event_log` "what + why" narrative — the headline for this track). WS consumers, relative paths, no logic. The Approval Inbox is the core shell's, not yours.

---

## 4. Test checklist (what "tested" means here)

Run `make test` after every milestone. Required coverage:

- **Unit / math:** FIFO depletion across multiple lots; the `sum(ledger) == on_hand` invariant; reorder qty + supplier choice; auto-vs-approval threshold; toggle target selection (`margin × velocity`) and re-enable; expiry → promo; reconciliation drift.
- **Signal-contract tests (`test_contract_b`):** every signal Track B emits validates against its `00 §15` payload; each agent subscribes **only** to its groups; **a static check asserts no module under `track_b/` imports `track_a`** and that B writes only its own tables (and never writes `approval_requests` directly).
- **Lifecycle tests:** PO `proposed → approved → placed → delivered → receive`; approval handler acts on `APPROVAL_RESOLVED` for the right types only; negotiation `request → CALL_OUTCOME → price update + signal`.
- **Standalone demo:** all seven checks in `02 §B7` pass with `DEMO_MODE=track_b`.
- **LLM resilience:** disable LLM keys → Market Spectator turns fall back to canned negotiation lines; **nothing crashes** (the deterministic ledger/optimizer must be entirely LLM-free anyway — verify there's no hidden LLM dependency in the core inventory math).
- **Edge cases:** depletion that crosses a lot boundary; an ingredient with two suppliers (score tiebreak); a PO rejected at approval (no placement); a count that increases stock (positive reconciliation); concurrent `LOW_STOCK` for the same ingredient (dedup collapses).

**Definition of done (whole track):** all above green; on_hand always equals the ledger; the inventory/expiry/supplier/activity panels read correctly; the supplier-negotiation call works end-to-end; `track-b` rebases cleanly on `main`.

---

## 5. How to split the work across coding agents (parallelization)

- **Sequential spine (one agent, related files):** B0 → B1 → B2 → B3 → B4 → B5. This is a tight dependency chain (Ledger → signals → procurement/optimizer → toggle → promo) touching overlapping files (`ledger.py`, `optimizer.py`, `procurement.py`) — keep it in order.
- **Parallel pieces (separate sessions, independent files):** **Market Spectator (B6)** and the **UI panels** are largely independent and can run concurrently once B0 lands (Market Spectator only needs the call subsystem + supplier tables; SupplierEditor/ActivityLog are pure UI). A practical fan-out: Agent-1 does the spine (B0–B5), Agent-2 does Market Spectator + SupplierEditor, Agent-3 does the remaining panels.
- **Rule:** don't let two concurrent sessions edit `agents/__init__.py` or `ledger.py` at once. Serialize any two that both register triggers.

For a solo build, walk B0→B6 top to bottom — and resist optimizing the order; the Ledger must be rock-solid before anything reads it.

---

## 6. Git workflow

- **`main` is protected** (PR + green CI to merge).
- All Track B work on **`track-b`**; one **PR per milestone** (B0, B1, …) with that milestone's tests passing.
- **No `core` changes on `track-b`.** A genuine `core` gap → separate **`core-fix/<thing>`** branch + labeled PR against `main`, merge, then rebase `track-b`. (Keeps `core` single-owned so Track A isn't disrupted.)
- Rebase `track-b` on `main` before Phase 2.
- **Phase 2 (merge):** both tracks on `main`, `DEMO_MODE=combined`, run **"Friday Rush"** (`00 §18.9`). Track B needs **no code change** — verify reorder/toggle/expiry now respond to *real* `DEMAND_FORECAST`/`BATCH_DECISION` from Track A's Forecaster instead of `MockForecaster`. The "tomato delivery delayed → LOW_STOCK → pasta MENU_TOGGLE" beat is the key cross-track moment to watch.

*End of 04_BUILD_PLAN_B.md.*
