// Track B panel registry (02 §B6). The core shell mounts these panels into
// the Track B tab slots. Each `name` matches its tab label so the shell can
// render by name; the components are WS consumers with no business logic of
// their own (00 §23).

import type { ComponentType } from "react";
import { InventoryDashboard } from "./InventoryDashboard";
import { ExpiryView } from "./ExpiryView";
import { SupplierEditor } from "./SupplierEditor";
import { ActivityLog } from "./ActivityLog";
import { ProcurementPanel } from "./ProcurementPanel";

export interface TrackPanel {
  name: string;
  component: ComponentType;
}

export const TRACK_B_PANELS: TrackPanel[] = [
  { name: "Inventory", component: InventoryDashboard },
  { name: "Expiry", component: ExpiryView },
  { name: "Suppliers", component: SupplierEditor },
  { name: "Procurement", component: ProcurementPanel },
  { name: "Activity Log", component: ActivityLog },
];

export { InventoryDashboard, ExpiryView, SupplierEditor, ProcurementPanel, ActivityLog };
