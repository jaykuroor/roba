import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { OperatorLayout } from "./routes/OperatorLayout";
import ConsolePage from "./routes/ConsolePage";
import ControlPage from "./routes/ControlPage";
import PanelsPage from "./routes/PanelsPage";

// The customer menu is a public, REST-only surface. Lazy-load it so it is
// code-split out of the operator bundle and never imports the WS/store-heavy
// console code (see docs/06).
const MenuPage = lazy(() => import("./menu/MenuPage"));

// Voice page: staff-facing, uses its own WS (Gemini Live) — NOT the operator
// WS firehose. Mounted outside OperatorLayout to keep it lightweight.
const VoicePage = lazy(() => import("./voice/VoicePage"));

// Call page: dedicated live-voice call surface, auto-opened in a new tab when
// a call is confirmed. Also supports direct-open with a party chooser.
const CallPage = lazy(() => import("./call/CallPage"));

// Manager dashboard for all restaurant instances (docs/fable/manager-dashboard.md).
// This is the entry point: every restaurant is created and opened from here,
// so `/` and any unknown path redirect to it.
const AdminPage = lazy(() => import("./admin/AdminPage"));

const Spinner = (
  <div className="flex min-h-screen items-center justify-center bg-primary text-text/50">
    Loading…
  </div>
);

export default function App() {
  return (
    <Routes>
      <Route
        path="/admin"
        element={
          <Suspense fallback={Spinner}>
            <AdminPage />
          </Suspense>
        }
      />
      {/* Instance-scoped operator routes (/<instance_id>/...): one restaurant's
          console; api/ws traffic is proxied via /i/<id>/. OperatorLayout
          redirects to /admin when the segment is not a valid instance id. */}
      <Route path="/:instanceId" element={<OperatorLayout />}>
        <Route index element={<ConsolePage />} />
        <Route path="control" element={<ControlPage />} />
        <Route path="panels" element={<PanelsPage />} />
      </Route>
      {/* Public customer menu — outside OperatorLayout, so no WS firehose. */}
      <Route
        path="/:instanceId/menu"
        element={
          <Suspense fallback={Spinner}>
            <MenuPage />
          </Suspense>
        }
      />
      {/* Voice interface — staff-facing, outside OperatorLayout (no WS firehose). */}
      <Route
        path="/:instanceId/voice"
        element={
          <Suspense fallback={Spinner}>
            <VoicePage />
          </Suspense>
        }
      />
      {/* Call page — dedicated live-voice call surface, auto-opened in a new tab. */}
      <Route
        path="/:instanceId/call"
        element={
          <Suspense fallback={Spinner}>
            <CallPage />
          </Suspense>
        }
      />
      {/* Everything else (including /) lands on the manager dashboard. */}
      <Route path="*" element={<Navigate to="/admin" replace />} />
    </Routes>
  );
}
