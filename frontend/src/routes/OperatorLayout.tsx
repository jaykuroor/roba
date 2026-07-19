import { useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, useParams } from "react-router-dom";
import { wsClient } from "../ws";
import { apiGet, INSTANCE_ID_RE } from "../api";
import { actions, store, useSimState } from "../store";
import { RestaurantLogo } from "../shell/RestaurantLogo";
import type { SimState, Weather } from "../types";

// Every operator route is instance-scoped (/<instance_id>/...). This layout
// owns the single WebSocket connection + store hydration, and shows which
// restaurant you are in (logo + name). The customer menu route is mounted
// OUTSIDE this layout so it never opens the WS firehose (see docs/06).
// api/ws traffic is proxied per instance (docs/fable/manager-dashboard.md).

const NAV = [
  { to: "", label: "Console", end: true },
  { to: "/control", label: "Control", end: false },
  { to: "/panels", label: "Panels", end: false },
  { to: "/voice", label: "Roba Desk", end: false },
  { to: "/menu", label: "Menu site", end: false },
];

function OperatorNav({ instanceId }: { instanceId: string }) {
  const base = `/${instanceId}`;
  const simState = useSimState();
  const [title, setTitle] = useState("");

  useEffect(() => {
    apiGet<{ title?: string }>("/api/settings/identity")
      .then((identity) => setTitle(identity.title || ""))
      .catch(() => undefined);
  }, [instanceId]);

  return (
    <nav className="flex items-center gap-1 border-b border-muted bg-primary px-4 py-1.5">
      {/* Full-page navigation so the per-instance store/WS state resets. */}
      <a
        href="/admin"
        className="rounded-md px-2 py-1 text-xs font-medium text-text/50 hover:bg-muted/50 hover:text-text"
        title="All restaurants"
      >
        ←
      </a>
      {/* Which restaurant am I in? Logo + name, always visible. */}
      <div className="mr-3 flex items-center gap-2">
        <RestaurantLogo
          id={instanceId}
          title={title}
          preset={simState?.active_seed_id}
          size={26}
        />
        <div className="leading-tight">
          <div className="text-xs font-semibold text-text">
            {title || instanceId}
          </div>
          <div className="text-[10px] text-text/40">{instanceId}</div>
        </div>
      </div>
      {NAV.map((item) => (
        <NavLink
          key={item.to}
          to={`${base}${item.to}`}
          end={item.end}
          className={({ isActive }) =>
            isActive
              ? "rounded-md bg-muted px-2.5 py-1 text-xs font-medium text-text"
              : "rounded-md px-2.5 py-1 text-xs font-medium text-text/50 hover:bg-muted/50 hover:text-text"
          }
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

export function OperatorLayout() {
  const { instanceId } = useParams();
  const validInstance =
    instanceId !== undefined && INSTANCE_ID_RE.test(instanceId);

  useEffect(() => {
    if (!validInstance) return;
    wsClient.connect();
    const hydrateSim = () => {
      apiGet<Partial<SimState>>("/api/sim/state")
        .then((s) => actions.setSimState(s))
        .catch(() => undefined);
    };
    const hydrateWeather = () => {
      apiGet<Weather>("/api/weather")
        .then((w) => actions.setWeather(w))
        .catch(() => undefined);
    };
    // Hydrate once so the bar is populated before the first WS message.
    hydrateSim();
    hydrateWeather();
    // While the socket is up, sim_tick / sim_state_changed / weather_updated
    // keep the store current — no polling needed. We only poll as a fallback
    // while the socket is DOWN, and re-hydrate once each time it reconnects
    // (covers state that changed during a brief outage). This avoids the
    // once-per-second GET /api/sim/state that the old unconditional poll caused.
    let prevConnected = store.getState().wsConnected;
    const unsubscribe = store.subscribe(() => {
      const connected = store.getState().wsConnected;
      if (connected && !prevConnected) {
        hydrateSim();
        hydrateWeather();
      }
      prevConnected = connected;
    });
    const fallbackPoll = setInterval(() => {
      if (!store.getState().wsConnected) {
        hydrateSim();
        hydrateWeather();
      }
    }, 2000);
    return () => {
      unsubscribe();
      clearInterval(fallbackPoll);
      wsClient.close();
    };
    // Re-open the socket when the instance changes so it targets the right
    // per-instance proxy path (url is derived from location at connect time).
  }, [instanceId, validInstance]);

  // A first path segment that is not a real instance id (e.g. a stale link)
  // has no backend to talk to — send it to the dashboard.
  if (!validInstance) {
    return <Navigate to="/admin" replace />;
  }

  return (
    <div className="min-h-full bg-primary text-text">
      <OperatorNav instanceId={instanceId} />
      <Outlet />
    </div>
  );
}
