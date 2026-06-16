// InventoryDashboard — per-ingredient on_hand vs par/reorder_point/safety_stock,
// theoretical-vs-counted drift, live depletion, and disabled menu items (02 §B6).
// Scaffold: an empty mounted panel. It will consume `inventory_updated`,
// `menu_toggled`, and `order_created` WS events in a later milestone.

export function InventoryDashboard() {
  return (
    <div
      data-track="b"
      data-panel="Inventory"
      className="flex h-full items-center justify-center rounded-lg border border-dashed border-muted bg-surface/40 text-text/40"
    >
      <span className="text-sm">Track B · Inventory Dashboard</span>
    </div>
  );
}
