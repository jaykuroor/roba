// ActivityLog — the event_log stream (the "what + why" narrative): reorders,
// toggles, promos, waste, negotiations (02 §B6). Consumes `event_logged`.

import { useEffect, useState } from "react";
import { apiGet } from "../api";
import { wsClient } from "../ws";
import type { EventLogEntry } from "../types";

const MAX_ROWS = 200;

export function ActivityLog() {
  const [events, setEvents] = useState<EventLogEntry[]>([]);

  useEffect(() => {
    apiGet<EventLogEntry[]>("/api/events")
      .then((rows) => setEvents(rows.slice(-MAX_ROWS)))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    return wsClient.on("event_logged", (p) => {
      const event = (p as { event?: EventLogEntry }).event;
      if (!event) return;
      setEvents((prev) => [...prev, event].slice(-MAX_ROWS));
    });
  }, []);

  return (
    <div data-track="b" data-panel="Activity Log" className="flex h-full flex-col gap-2 overflow-auto rounded-lg bg-surface/40 p-3">
      <h2 className="text-sm font-semibold text-text">Activity Log</h2>
      {events.length === 0 ? (
        <p className="text-xs text-text/40">No activity yet.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {[...events].reverse().map((event) => (
            <li
              key={event.id}
              className="flex items-start gap-2 rounded border border-muted bg-surface px-2 py-1 text-sm"
            >
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-text/60">
                {event.category}
              </span>
              <span className="text-text/80">{event.summary}</span>
              <span className="ml-auto shrink-0 text-[10px] text-text/30">
                {event.sim_time.toFixed(0)}s
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
