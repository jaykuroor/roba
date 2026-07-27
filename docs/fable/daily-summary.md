# Daily Summary — portfolio briefing

Status: **implemented** — numeric briefing, LLM prose briefing, next-day
coverage risk and the end-of-day archive (Phase 6).

## What exists

`GET /admin/api/summary` reuses the overview fan-out per instance and adds
today's waste (`/api/waste`, cost summed from the current sim-day start):

- `totals` — portfolio sales vs forecast, waste cost, stock risks, staff
  absent, pending approvals, offline count.
- `restaurants` — the per-instance cards (incl. `waste_today`).
- `major_incidents` — critical-severity queue entries.
- `pending_decisions` — the approval-kind queue entries.
- `next_day_risks` — stock-kind queue entries (uncoverables + low stock) **plus
  coverage-kind rows** (below).

Rendered as the "Daily briefing" tab of `/admin`, polled every 30s.

## Prose briefing (Phase 6)

`POST /admin/api/briefing` writes six lines of portfolio prose over the JSON
above and returns
`{generated_at, model, error, prose}`.

- **POST, and only on demand.** The summary endpoint is polled every 30s; a GET
  briefing would invite exactly the once-a-poll billing this doc warned about.
- **`briefing_context()` trims the input** to the headline totals, a per-card
  line, and the already-phrased action rows — the full payload carries every
  restaurant's nested snapshot, which has no business in a prompt.
- **A degraded provider is an error, never prose.** Checked for
  `note == CANNED_NOTE`, *and* for an empty or whitespace-only string: a blank
  briefing panel reads as "all quiet across the estate", which is the most
  expensive thing this dashboard could get wrong. Only `gemini-2.5-*` models
  exist for this project; a bad id degrades silently and this is the guard.

## Next-day risk (Phase 6)

"Risks for the next day" is no longer just today's stock risks.
`next_day_coverage_risks()` joins the procurement plan's `covers_until` against
the forecaster's horizon (`/api/track-a/forecast/horizons`) — in
`manager.daily_summary`, not the frontend, so the archive below carries it too.

A coverage row is raised only when cover lapses **during tomorrow**: cover that
already lapsed is today's problem and `build_issues` has raised it as low stock,
so counting it again would list the same ingredient in two places. Each row
carries tomorrow's forecast covers when a horizon reaching tomorrow exists, and
says so plainly when none has been generated yet.

## End-of-day archive (Phase 6)

On each sim-day rollover the manager snapshots this JSON to
`dbdata/summaries/<instance_id>/day-NNN.json`, next to the catch-up captures and
driven by the same rollover watcher ([catchup.md](catchup.md) §Auto-capture).

`NNN` is the day that **ended** — it is an end-of-day record. The file holds the
whole *portfolio* summary; `instance_id` records only whose rollover triggered
it. That is what makes two archived days comparable rather than a per-restaurant
fragment. Served by `GET /admin/api/instances/{id}/summaries` (markers, newest
first) and `.../summaries/{day}`, and rendered by the "Day archive" side of the
catch-up drawer through the same component as the live briefing tab.
