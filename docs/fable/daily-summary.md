# Daily Summary — portfolio briefing

Status: **implemented** (numeric briefing; LLM prose is future work).

## What exists

`GET /admin/api/summary` reuses the overview fan-out per instance and adds
today's waste (`/api/waste`, cost summed from the current sim-day start):

- `totals` — portfolio sales vs forecast, waste cost, stock risks, staff
  absent, pending approvals, offline count.
- `restaurants` — the per-instance cards (incl. `waste_today`).
- `major_incidents` — critical-severity queue entries.
- `pending_decisions` — the approval-kind queue entries.
- `next_day_risks` — the stock-kind queue entries (uncoverables + low stock).

Rendered as the "Daily summary" section of `/admin`, polled every 30s.

## Guidance

- **LLM prose briefing**: the JSON is deliberately shaped as briefing input.
  Feed it to `core/llm.py`'s Gemini provider with a "write a 6-line portfolio
  briefing" prompt. Two cautions: (1) run it on demand or once per sim-day, not
  per poll; (2) a bad `GEMINI_MODEL` degrades to a canned no-op **silently** —
  surface provider errors in the UI rather than showing canned text as if it
  were real (this bit us before; only `gemini-2.5-*` models exist for this
  project).
- "Risks for the next day" currently equals stock risks. Tomorrow-specific
  risk (demand vs. tomorrow's coverage) can come from the forecaster's horizon
  (`/api/track-a/forecast/horizons`) joined with `covers_until` on procurement
  plan items — add it in `manager.daily_summary`, not the frontend.
- A true end-of-day archive (yesterday's summary, comparable over time) should
  snapshot this JSON to disk once per sim-day rollover, next to the catch-up
  captures. The catch-up marker machinery in `manager.py` is the pattern to
  copy.
