# Manager Dashboard — multi-restaurant infrastructure

Status: **implemented** (this phase). This doc is the architecture reference the
per-feature docs in this folder build on.

## What exists

A manager server (`manager.py`, FastAPI, port **8100**) that owns a portfolio of
roba instances. Each instance is a full single-restaurant backend
(`core.api:app`) run as a **subprocess** with its own SQLite DB — zero changes
to the per-restaurant architecture were needed; a restaurant does not know it is
managed.

```
browser ── vite :5173 ──┬── /api, /ws            → legacy single backend :8000 (optional)
                        ├── /admin/api/*         → manager :8100 (aggregation + registry)
                        └── /i/<instance_id>/*   → manager :8100 → child backend :<port>
                                                    (HTTP and WebSocket, incl. /ws/voice/live)

manager :8100
  ├─ registry: dbdata/manager_registry.json   {id, preset, port, title, created_at}
  ├─ children: uvicorn core.api:app, DB_PATH=dbdata/<instance_id>.db, log dbdata/<instance_id>.log
  └─ catch-up captures: dbdata/catchups/<instance_id>/NNNNNN.json
```

### Instance lifecycle

- `POST /admin/api/instances {preset}` — spawns a child on a free port, waits
  for `/api/health`, seeds the preset (`data/<preset>.json`), reads the
  restaurant title from `/api/settings/identity`. The id is a generated
  `adjective_animal` name (`running_fox` style, `manager.generate_instance_id`).
- `POST /admin/api/instances/{id}/stop` / `.../start` — stop keeps the DB;
  start respawns on a fresh port (no reseed — state persists in the DB file).
- `DELETE /admin/api/instances/{id}` — removes the registry entry; the DB and
  log files are deliberately kept on disk (delete `dbdata/<id>.db*` manually).
- Manager shutdown terminates the children it spawned; the registry survives,
  so instances can be started again next run.

### Routing rules (frontend)

- Instance ids match `^[a-z]+_[a-z]+\d*$`. `frontend/src/api.ts` exports
  `instanceId()` / `instancePrefix()` which read the **first URL path segment**
  at request time; when it matches, every `/api/...` call and both WebSockets
  (`ws.ts`, `voice/RobaLiveClient.ts`) are rewritten to `/i/<id>/...`.
  No component plumbing — a page mounted under `/<id>/...` is automatically
  instance-scoped.
- Routes: `/admin` → manager dashboard; `/<id>`, `/<id>/control`,
  `/<id>/panels`, `/<id>/menu`, `/<id>/voice`, `/<id>/call` → the existing
  pages, scoped to that instance. Static routes always outrank `/:instanceId`.
- Cross-instance navigation (admin → instance, instance → admin) uses plain
  `<a href>` full-page loads on purpose: the operator store is a singleton, and
  a fresh load guarantees no state bleed between restaurants.

### Running it

```
make manager                 # local: manager on :8100 (+ cd frontend && npm run dev)
docker compose up            # containers: manager service on :8100, children inside it
```

Open `https://localhost:5173/admin`.

## Guidelines for future features

- **Add per-restaurant data by adding a child endpoint** (in `core/api.py`),
  then fan out in `manager.py` with `_get(inst, path)` + `asyncio.gather`.
  `/api/ops/snapshot` (added this phase) is the model: one cheap JSON call the
  manager can poll. Do not reach into child DBs directly — the HTTP surface is
  the contract, and it keeps children swappable for remote deployments.
- **Aggregation logic lives in pure functions** at the top of `manager.py`
  (`derive_status`, `build_issues`, `rank_issues`) tested in
  `tests/test_manager.py`. Keep new derivations pure and unit-tested the same
  way; the async endpoints should only fetch and delegate.
- **Manager has no DB.** Registry and catch-ups are JSON files. If manager
  state outgrows that (per-user prefs, incident acknowledgements), add a small
  SQLite next to the registry rather than complicating the children.
- **No auth exists** anywhere in roba; `/admin` inherits that. Whatever auth is
  added later must cover `/i/*` too (it is a full-power proxy into each
  restaurant, including CRUD and sim control).
- Scaling past a handful of instances: the fan-out endpoints are O(instances)
  per poll with a 4s per-call timeout; fine for the 2-instance demo. Beyond
  ~20, cache per-instance snapshots (the children could push on their WS
  instead of being polled).
