// ExpiryView — lots with expiry countdowns, at-risk highlights, and
// active/proposed promotions (02 §B6). Scaffold: an empty mounted panel. It will
// consume `signal_emitted(EXPIRY_RISK / PROMO_PROPOSAL)` in a later milestone.

export function ExpiryView() {
  return (
    <div
      data-track="b"
      data-panel="Expiry"
      className="flex h-full items-center justify-center rounded-lg border border-dashed border-muted bg-surface/40 text-text/40"
    >
      <span className="text-sm">Track B · Expiry View</span>
    </div>
  );
}
