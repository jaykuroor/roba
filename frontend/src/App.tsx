import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
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
const AdminPage = lazy(() => import("./admin/AdminPage"));

const Spinner = (
  <div className="flex min-h-screen items-center justify-center bg-primary text-text/50">
    Loading…
  </div>
);

export default function App() {
  return (
    <Routes>
      {/* Operator routes share one WS connection + store via OperatorLayout. */}
      <Route element={<OperatorLayout />}>
        <Route path="/" element={<ConsolePage />} />
        <Route path="/control" element={<ControlPage />} />
        <Route path="/panels" element={<PanelsPage />} />
      </Route>
      {/* Manager dashboard — talks to the manager server, not one instance. */}
      <Route
        path="/admin"
        element={
          <Suspense fallback={Spinner}>
            <AdminPage />
          </Suspense>
        }
      />
      {/* Instance-scoped operator routes (/<instance_id>/...): the same
          console, proxied to that instance's backend via /i/<id>/ (static
          routes above always outrank the :instanceId param). */}
      <Route path="/:instanceId" element={<OperatorLayout />}>
        <Route index element={<ConsolePage />} />
        <Route path="control" element={<ControlPage />} />
        <Route path="panels" element={<PanelsPage />} />
      </Route>
      <Route
        path="/:instanceId/menu"
        element={
          <Suspense fallback={Spinner}>
            <MenuPage />
          </Suspense>
        }
      />
      <Route
        path="/:instanceId/voice"
        element={
          <Suspense fallback={Spinner}>
            <VoicePage />
          </Suspense>
        }
      />
      <Route
        path="/:instanceId/call"
        element={
          <Suspense fallback={Spinner}>
            <CallPage />
          </Suspense>
        }
      />
      {/* Public customer menu — outside OperatorLayout, so no WS firehose. */}
      <Route
        path="/menu"
        element={
          <Suspense fallback={Spinner}>
            <MenuPage />
          </Suspense>
        }
      />
      {/* Voice interface — staff-facing, outside OperatorLayout (no WS firehose). */}
      <Route
        path="/voice"
        element={
          <Suspense fallback={Spinner}>
            <VoicePage />
          </Suspense>
        }
      />
      {/* Call page — dedicated live-voice call surface, auto-opened in a new tab. */}
      <Route
        path="/call"
        element={
          <Suspense fallback={Spinner}>
            <CallPage />
          </Suspense>
        }
      />
    </Routes>
  );
}
