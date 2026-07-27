# Catch-up — "what happened while I was away"

Status: **capture + summary implemented** (Phase 5); **merge and auto-capture
are future work** (Phase 6). This doc covers both.

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

## Summary built now (manager.py, Phase 5)

`POST /admin/api/instances/{id}/catchups/{n}/summarize` fills the `summary`
slot and returns it. Idempotent — re-summarizing overwrites, and the raw
`events` are never touched, so a better prompt can always be tried again.

```json
{"generated_at": 1784480000.0, "model": "gemini-2.5-pro", "error": null,
 "buckets": [{"bucket": "procurement", "event_count": 3, "truncated": 0,
              "bullets": [{"text": "Two POs were placed with Verdura Fresca and
                                    City Wholesale for EUR 262.85.",
                           "event_ids": [1, 2]}]}],
 "incidents": [{"incident_id": 3, "category": "staff_no_show",
                "summary": "Marco is sick.", "opened_at": 33000.0,
                "status": "resolved", "resolved_at": 1784480000.0}]}
```

How it works, and why:

1. **Bucket first, prompt second.** `bucket_events` groups the window by
   `EventLog.category` into `procurement | inventory | demand | staffing |
   menu | promos | market | other`, in that fixed order. It is pure and
   unit-tested, so the grouping never depends on the model. Each non-empty
   bucket gets **its own prompt** — the LLM never sees the raw firehose.
   Note §2 above says `event_type`; the column is `category`.
   Note also there is **no `kitchen` or `reviews` bucket**: nothing in the sim
   writes `event_log` rows for either (the review agent writes none at all),
   so those buckets would be permanently empty. Add them the day something
   logs to them.
2. **Bullets cite their sources.** Every bullet carries the `event_ids` it was
   written from, and ids the model invented are filtered out against the
   prompt's own event set — an expander never resolves to nothing. This is what
   `GET .../catchups/{n}` is expanded against in the UI.
3. **Incidents come from the store, not from signals.** The `incidents` list is
   `incidents_in_window()` over the Phase 4 SQLite (see
   [incidents.md](incidents.md)) — rows *opened* inside `(since_sim, until_sim]`
   including ones that have since resolved. A live-signal read could not tell
   you about those: the signal is long expired by the time you catch up.
4. **A degraded model is an error, never prose.** `GEMINI_REASONER_MODEL`
   (2.5-pro) is used, and the result is checked for `note == CANNED_NOTE`
   before anything is stored. On a canned or malformed answer the whole summary
   comes back `error: <what to check>` with `buckets: []`, and the UI renders a
   danger block. A *partial* summary is treated as failure too — half a briefing
   reads as "nothing else happened", which is worse than an honest error.
5. **UI:** a "Catch-ups" drawer per restaurant card lists the captured windows,
   summarizes one on demand, and expands any bullet into its raw events.

### Viewing previous catch-ups + merging

- Listing and the per-restaurant history drawer are built (Phase 5).
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
