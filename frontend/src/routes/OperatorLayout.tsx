import { useEffect } from "react";
import { NavLink, Outlet, useParams } from "react-router-dom";
import { wsClient } from "../ws";
import { apiGet } from "../api";
import { actions, store } from "../store";
import type { SimState, Weather } from "../types";

// All operator routes (/, /control, /panels) render inside this layout, which
// owns the single WebSocket connection + store hydration. Because the customer
// menu route (/menu) is mounted OUTSIDE this layout, it never opens the WS
// firehose — keeping the public page lightweight (see docs/06).
//
// When mounted under /:instanceId the same layout serves that restaurant
// instance — nav links are prefixed and api/ws traffic is proxied per
// instance (docs/fable/manager-dashboard.md).

const NAV = [
  { to: "/", label: "Console", end: true },
  { to: "/control", label: "Control", end: false },
  { to: "/panels", label: "Panels", end: false },
  { to: "/voice", label: "Roba Desk", end: false },
  { to: "/menu", label: "Menu site", end: false },
];

function OperatorNav({ instanceId }: { instanceId?: string }) {
  const base = instanceId ? `/${instanceId}` : "";
  return (
    <nav className="flex items-center gap-1 border-b border-muted bg-primary px-4 py-1.5">
      <span className="mr-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        Roba
      </span>
      {instanceId ? (
        <>
          {/* Full-page navigation so the per-instance store/WS state resets. */}
          <a
            href="/admin"
            className="rounded-md px-2.5 py-1 text-xs font-medium text-text/50 hover:bg-muted/50 hover:text-text"
          >
            ← All restaurants
          </a>
          <span className="rounded-md bg-muted/60 px-2 py-0.5 text-[11px] font-semibold text-text/70">
            {instanceId}
          </span>
        </>
      ) : null}
      {NAV.map((rawItem) => {
        const item = {
          ...rawItem,
          to: rawItem.to === "/" ? base || "/" : `${base}${rawItem.to}`,
        };
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              isActive
                ? "rounded-md bg-muted px-2.5 py-1 text-xs font-medium text-text"
                : "rounded-md px-2.5 py-1 text-xs font-medium text-text/50 hover:bg-muted/50 hover:text-text"
            }
          >
            {item.label}
          </NavLink>
        );
      })}
    </nav>
  );
}

export function OperatorLayout() {
  const { instanceId } = useParams();
  useEffect(() => {
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
  }, [instanceId]);

  return (
    <div className="min-h-full bg-primary text-text">
      <OperatorNav instanceId={instanceId} />
      <Outlet />
    </div>
  );
}
