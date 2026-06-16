// SupplierEditor — suppliers + supplier_catalog (price/availability/lead) edited
// live, negotiation history, and a per-supplier "Negotiate" button (02 §B6).
// Scaffold: an empty mounted panel. It will consume
// `signal_emitted(SUPPLIER_PRICE_UPDATE)` + `call_*` and PATCH /suppliers later.

export function SupplierEditor() {
  return (
    <div
      data-track="b"
      data-panel="Suppliers"
      className="flex h-full items-center justify-center rounded-lg border border-dashed border-muted bg-surface/40 text-text/40"
    >
      <span className="text-sm">Track B · Supplier Editor</span>
    </div>
  );
}
