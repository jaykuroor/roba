# 06 — Frontend Pages, POS Monitor & Customer Menu

> **Scope:** the multi-page frontend restructure, the live POS monitoring view,
> and the public customer menu site. Branch: feature work off `main`.
> **Audience:** frontend + backend programmers extending these surfaces.

---

## 1. Why

Two gaps motivated this work:

1. **The POS stream was invisible.** The backend already broadcasts a rich
   `order_created` WS event (order + lines + per-item velocity) on every
   simulated order, but **no frontend code consumed it**. There was also no
   REST way to read orders. Operators had no live view of sales.
2. **No customer menu.** Nothing rendered the loaded restaurant's menu or showed
   which items are enabled/disabled. `menu_items.active` is the source of truth
   (the POS only sells `active=1` items) and is exposed via `GET /api/menu`, but
   was unused.

We also split the single-page console into **four addressable pages** so the
demo can run across multiple screens.

---

## 2. Routes

The app is now an SPA with `react-router-dom` (`frontend/src/main.tsx` wraps
`<App/>` in `<BrowserRouter>`; `frontend/src/App.tsx` is the route table).

| Path | Page | Contents | WS? |
|------|------|----------|-----|
| `/` | `ConsolePage` | Control Bar + Panels + drawers | yes (shared) |
| `/control` | `ControlPage` | Control Bar + drawers only | yes (shared) |
| `/panels` | `PanelsPage` | Panels only (POS Monitor + agent tabs) | yes (shared) |
| `/menu` | `MenuPage` (lazy) | Public customer menu | **no** |

**WS scoping rule (key efficiency decision):** the three operator routes render
inside `frontend/src/routes/OperatorLayout.tsx`, which owns the **single**
`wsClient.connect()` lifecycle + sim/weather hydration (moved out of the old
`App.tsx`). Because `<Outlet/>` keeps the layout mounted across operator
navigation, switching between `/`, `/control`, `/panels` does **not** drop or
reopen the socket. `/menu` is mounted **outside** `OperatorLayout`, so it never
opens the WS firehose.

**Hydration polling (efficiency):** the ControlBar clock/status come from the
store. While the socket is connected, `sim_tick` / `sim_state_changed` /
`weather_updated` keep the store current, so `OperatorLayout` does **not** poll.
`GET /api/sim/state` is fetched once on mount, once on each WS reconnect, and
otherwise only by a 2s fallback timer **while the socket is down**. (The earlier
unconditional 1s poll caused a steady stream of `GET /api/sim/state` calls even
when the WS was healthy.)

`MenuPage` is loaded via `React.lazy` + `<Suspense>`, so it is **code-split** out
of the operator bundle (verify: `dist/assets/MenuPage-*.js` is its own chunk).

A small top nav (`OperatorNav` in `OperatorLayout.tsx`) links the four screens.

---

## 3. POS Monitor

Files: `frontend/src/pos/usePosStream.ts` (live buffer),
`frontend/src/pos/usePosStats.ts` (windowed totals), and
`frontend/src/pos/PosMonitor.tsx` (view). Mounted as the **"POS Monitor"** tab in
`frontend/src/shell/PanelsView.tsx` (shared by `/` and `/panels`).

The monitor has **two data sources**, deliberately split:

- **Windowed analytics** (stat cards, channel split, top items, orders-over-time
  chart) — from the backend aggregate `GET /api/pos/stats` via `usePosStats`.
- **Live pulse** (the order ticker + per-item velocity) — from the streamed
  ring buffer via `usePosStream`.

### 3.1a Window selector

`PosMonitor` has a window selector (`usePosStats.POS_WINDOWS`): **Today** (the
current operating day — the default), **Last hour**, **Last 6 hours**, **This
week**. The selection maps to a `since` boundary (`day` → operating open of the
current sim-day; `week` → operating open of this week's first day, from
`day_of_week`; rolling windows → `now − N`). All windowed statistics are computed
**server-side** so they are accurate over a full day — the client's bounded live
buffer (cap 120) could never represent a whole day's totals. `usePosStats`
recomputes `since` from the latest `sim_time` at fetch time (not as an effect
dependency, so a rolling window slides without re-subscribing), refetches on
window change, and polls every 3s **only while running**.

`GET /api/pos/stats?since=<sim_time>` (`core/api.py`) returns
`{since, now, orders, revenue, lines, voided_lines, channel_split, top_items,
buckets}`, where `buckets` are ~24 adaptive time buckets across the window for
the chart. `since` is clamped `>= 0`, so seeded negative-`sim_time` history is
never counted.

### 3.1 Live buffer data flow

```
first mount (once) → GET /api/orders?since=0&limit=50   (current-run backfill)
                   → shared module-level ring buffer (cap 120, newest-first)
live  → wsClient.on("order_created")    (one app-wide subscription)
      → pending[] accumulator
flush every 500ms → prepend pending → cap buffer → emit() → subscribers
```

`usePosStream` now exposes just `{ orders, velocity, ready }` for the ticker and
velocity overlay; the old client-side `computeStats` was removed since windowed
totals come from the backend (one source of truth, no duplicate stats path).

The buffer is a **module-level singleton** in `usePosStream.ts`, exposed via
`useSyncExternalStore`. It is NOT per-component state: this is what makes the
monitor consistent as the user navigates between the Console (`/`) and Panels
(`/panels`) pages, which mount/unmount the monitor. Backfill runs **once** (on
the first mount, guarded by an `initialized` flag) and uses `since=0` to exclude
the seeded negative-`sim_time` history, so it shows only current-run orders and
never reloads stale orders on remount.

### 3.2 Efficiency strategy (the brief's explicit concern)

- **One app-wide subscription, lazily installed.** `usePosStream` installs the
  `order_created` subscription + flush timer exactly once, on the first monitor
  mount (operator routes only). The lazy `/menu` page never mounts the monitor,
  so it never installs them and never processes the order firehose.
- **Render throttling.** Incoming orders are pushed into a plain array and
  flushed to React state on a fixed 500ms interval. A burst at high sim speed
  (up to `MAX_ORDERS_PER_TICK = 25` per 250ms tick) produces **one render per
  flush**, not one per order.
- **Bounded buffer.** The buffer is capped at 120 events and the dedupe `seen`
  set is trimmed, so memory is constant over a long-running sim.
- **Windowed stats are backend-side.** Orders/revenue/void/channel/top/chart
  come from `GET /api/pos/stats` (§3.1a); the client buffer only feeds the live
  ticker + velocity, so there's no duplicate client stats computation.
- **No velocity backfill.** Velocity is an ephemeral in-memory ring buffer in
  `DataFormatter`; `GET /api/orders` omits it and the client derives rate from
  the streamed `velocity` map.
- **Feed line aggregation.** An order may carry several lines for the same dish
  (each qty 1); the live feed collapses them into one `N× Name` entry.

### 3.3 Reset semantics (stop ≠ start ≠ restart)

The monitor distinguishes the three controls (verified end-to-end):

| Control | Backend | Frontend buffer |
|---------|---------|-----------------|
| **Stop** | `clock.stop()` rewinds the clock; **orders are kept** | **kept** — monitor stays viewable with its window toggles |
| **Start** (play after stop) | `_wipe_live_orders()` deletes the previous run's `sim_time ≥ 0` orders (seeded negative history is preserved), emits `pos_reset` | cleared on the stopped→running transition (tracked via `lastStatus`; pause→resume and speed changes do **not** clear) |
| **Restart / reseed** | `_wipe_for_seed()` wipes everything + reseeds, emits `pos_reset` | cleared on `pos_reset` |

So a fresh start is genuinely fresh (the windowed stats don't double-count the
stopped run), stop leaves the data inspectable, and restart clears immediately.
The buffer is shared and never re-backfills, so cleared/kept state holds across
page navigation.

### 3.4 Settings ↔ seed refresh

`SettingsDrawer`'s `PosMixPanel` keys its menu + `/api/sim/pos` fetch on the
store's `sim_state_changed`-driven `active_seed_id`. Loading/reseeding a
restaurant changes that id, so the dish-mix list refreshes automatically without
toggling tabs.

### 3.5 Backend support — `GET /api/orders`

New read endpoint (`core/api.py`), the only required backend change. Orders were
previously write-only.

```
GET /api/orders?limit=50&since=<sim_time?>
```

- `limit` clamped to `[1, 200]`; `since` filters `Order.sim_time > since`.
- Returns **newest-first** `[{ "order": {...}, "lines": [...] }, ...]`, mirroring
  the live `order_created` payload (minus `velocity`).
- Lines fetched in a single `IN` query (no N+1).
- Serialization reuses `order_to_dict` / `line_to_dict`, now module-level
  functions in `core/formatter.py` shared with the WS payload so both stay in
  lockstep. The old `DataFormatter._order_to_dict` / `_line_to_dict` remain as
  thin `staticmethod` aliases.

> Note: presets seed historical orders (negative `sim_time`). The backfill uses
> `since=0`, so it returns only current-run (positive `sim_time`) orders and the
> seeded history never appears in the monitor.

### 3.6 Clock rewind must restart the POS schedule (backend)

Stop and restart rewind `sim_time` to the start of the day. Two backend caches
held stale future timestamps across that rewind and had to be reset, or **no new
orders generate after pressing play** (the monitor stays empty):

- **Orchestrator interval triggers.** Each trigger's `next_due` lay in the
  future relative to the rewound clock, so the POS trigger never fired.
  `SimClock.stop()`/`restart()` now call `Orchestrator.reset_schedules()`, which
  re-anchors every interval trigger to `sim_time + interval`.
- **`POSSimulator.next_order_due`.** The simulator's own next-arrival cache was
  also stale. `tick()` now detects a backward `sim_time` jump (via
  `_last_tick_sim_time`) and restarts the arrival schedule from the new time.

Together these make the first order arrive on the next tick after replay. The
frontend relies on this: since the monitor no longer re-backfills after a reset,
the live `order_created` stream is the only source, so it must resume promptly.

---

## 4. Customer Menu site (`/menu`)

File: `frontend/src/menu/MenuPage.tsx` (default export, lazy-loaded).

- **Display-only**, no cart/checkout (confirmed scope).
- **REST-only, no WebSocket.** Polls `GET /api/menu` every 10s rather than
  opening the event firehose — optimizes network for a public page.
- `GET /api/menu` returns **active and inactive** items. Items group by
  `category`; `active=1` render normally, `active=0` render greyed with a
  "Sold out" badge. `active` is the availability source of truth.
- Restaurant name derives from `GET /api/sim/state` `active_seed_id` (prettified;
  defaults to "Our Menu"). The friendly preset name lives in the preset's
  `meta.name` but is not currently exposed without loading — a future
  `GET /api/seed/presets` enrichment could surface it.
- Lighter customer theme (stone palette), self-contained, no operator chrome.

### How items get toggled

Today `active` is set at seed time and changed via `PATCH /api/menu/{id}`
(`{"active": 0|1}`). The `MENU_TOGGLE` signal, `MenuTogglePayload`, and
`menu_toggles` table exist as scaffolding but **no runtime code emits them or
flips `active`** yet (Track B Optimizer is the intended future owner). When that
lands, the menu site will reflect it automatically via the 10s poll; a future
optimization could instead listen for the `MENU_TOGGLE` `signal_emitted` WS event
(its groups already include `"frontend"`).

---

## 5. Files

| File | Change |
|------|--------|
| `core/api.py` | New `GET /api/orders`; import `order_to_dict`/`line_to_dict` |
| `core/formatter.py` | Module-level `order_to_dict`/`line_to_dict`; static aliases |
| `frontend/package.json` | + `react-router-dom` |
| `frontend/src/main.tsx` | Wrap app in `<BrowserRouter>` |
| `frontend/src/App.tsx` | Route table; lazy `/menu` |
| `frontend/src/routes/OperatorLayout.tsx` | **new** — WS lifecycle + nav + `<Outlet/>` |
| `frontend/src/routes/{ConsolePage,ControlPage,PanelsPage}.tsx` | **new** |
| `frontend/src/shell/PanelsView.tsx` | **new** — extracted tabs + POS Monitor tab |
| `frontend/src/pos/usePosStream.ts`, `pos/PosMonitor.tsx` | **new** |
| `frontend/src/menu/MenuPage.tsx` | **new** |
| `frontend/src/types.ts` | + `PosOrder`/`PosOrderLine`/`PosOrderEvent` |

---

## 6. Known limitation / future work

The WS hub (`core/api.py` `WebSocketHub`) broadcasts **every event to every
connected client** — there is no per-client subscription filtering. Operator
pages each receive the full stream regardless of which tab is active (the client
just ignores unsubscribed events). Per-client topic subscriptions on the hub
would let the server send only what each screen needs and is the natural next
optimization if connection counts grow.

---

## 7. Verification

1. **Backend:** `make up` + `make seed`; `curl 'localhost:8000/api/orders?limit=5'`
   returns newest-first `{order, lines}` after pressing play. `make test`
   (124 pass).
2. **POS Monitor:** open `/` or `/panels`, press play → recent orders backfill,
   new orders stream in, stats update; at 8× the UI stays smooth (throttled).
3. **Menu:** open `/menu` → grouped menu; `PATCH /api/menu/{id} {"active":0}` →
   item shows "Sold out" within ~10s; devtools shows **no** `/ws` connection.
4. **Pages:** `/control` (controls only), `/panels` (dashboards only), `/`
   (both); navigating among operator routes does not reconnect the WS.
