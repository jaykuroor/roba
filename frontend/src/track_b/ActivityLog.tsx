// ActivityLog — the event_log stream (the "what + why" narrative): reorders,
// toggles, promos, waste, negotiations (02 §B6). Scaffold: an empty mounted
// panel. It will consume `event_logged` WS events in a later milestone.

export function ActivityLog() {
  return (
    <div
      data-track="b"
      data-panel="Activity Log"
      className="flex h-full items-center justify-center rounded-lg border border-dashed border-muted bg-surface/40 text-text/40"
    >
      <span className="text-sm">Track B · Activity Log</span>
    </div>
  );
}
