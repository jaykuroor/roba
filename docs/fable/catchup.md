# Catch-up — "what happened while I was away"

Status: **infrastructure implemented**; the readable summary, expand-for-detail
and merge UX are future work. This doc covers both.

## Infrastructure built now (manager.py)

The hard requirement of catch-up is *lossless windows*: every catch-up covers
exactly the events since the previous one, even if the manager was restarted in
between. That is what exists:

- `POST /admin/api/instances/{id}/catchups` — reads the child's sim clock,
  fetches `/api/events?since=<end of previous catch-up>` (boundary rows
  deduped), and persists the whole window to
  `dbdata/catchups/<id>/NNNNNN.json`:

  ```json
  {"n": 3, "instance_id": "running_fox", "created_at": 1784480000.0,
   "since_sim": 86400.0, "until_sim": 129600.0,
   "event_count": 217, "events": [ ...raw EventLog rows... ],
   "summary": null}
  ```

- `GET /admin/api/instances/{id}/catchups` — marker list (metadata only).
- `GET /admin/api/instances/{id}/catchups/{n}` — full capture incl. events.
- UI: the "Catch up" button on each restaurant card creates a capture and
  shows the captured event count.

Why capture raw events instead of just a `(since, until)` marker: the child's
`EventLog` is wiped on reseed and owned by the child; snapshotting at capture
time means the summarizer can run later (or repeatedly, with better prompts)
without the source needing to still exist. `summary: null` is the slot the
summarizer fills in.

## Future work — implementation guidance

### Readable structured summary

1. Input: one capture's `events` (plus, ideally, resolved approvals and
   incident history for the window — see [incidents.md](incidents.md) for why
   incidents should become first-class).
2. Bucket events by subsystem before prompting (procurement, staffing, kitchen,
   reviews, promos...) — `EventLog.category` / `detail` carry enough to
   group deterministically; let the LLM write prose *per bucket*, not over the
   raw firehose. Target shape: the example in the product brief — "X task was
   done 5 min late", "promo applied on procurement (expand for details)".
3. Each summary bullet should reference the source event ids from the capture —
   that is what "click to expand" resolves against (`GET .../catchups/{n}`
   already returns the raw rows).
4. Write the result into the capture file's `summary` field (idempotent:
   re-summarizing overwrites). Add
   `POST /admin/api/instances/{id}/catchups/{n}/summarize` for it.
5. Use `GEMINI_REASONER_MODEL` (2.5-pro) — extraction-grade, latency
   irrelevant. Same silent-fallback caution as
   [daily-summary.md](daily-summary.md): never present canned output as a
   summary.

### Viewing previous catch-ups + merging

- Listing is already served by the markers endpoint; the UI needs a history
  drawer per restaurant.
- **Merge = concatenate windows.** Because captures are contiguous
  (`since_sim` of n+1 == `until_sim` of n), merging catch-ups `[a..b]` is just
  summarizing the concatenated `events` of those captures — no new capture
  format needed. Implement as
  `POST /admin/api/instances/{id}/catchups/merge {from, to}` returning a
  transient merged summary (do not delete the originals; they are the audit
  trail).

### Auto-capture

If catch-ups should exist even when nobody pressed the button (e.g. one per
sim-day), add a manager-side background task that posts a capture per instance
on day rollover — the capture endpoint is already idempotent w.r.t. windows, so
scheduled and manual captures compose cleanly.
