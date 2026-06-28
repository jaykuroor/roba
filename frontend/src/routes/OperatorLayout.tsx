import { useEffect } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { wsClient } from "../ws";
import { apiGet } from "../api";
import { actions, store } from "../store";
import type { SimState, Weather } from "../types";

// All operator routes (/, /control, /panels) render inside this layout, which
// owns the single WebSocket connection + store hydration. Because the customer
// menu route (/menu) is mounted OUTSIDE this layout, it never opens the WS
// firehose — keeping the public page lightweight (see docs/06).

const NAV = [
  { to: "/", label: "Console", end: true },
  { to: "/control", label: "Control", end: false },
  { to: "/panels", label: "Panels", end: false },
  { to: "/voice", label: "Roba Desk", end: false },
  { to: "/menu", label: "Menu site", end: false },
];

function OperatorNav() {
  return (
    <nav className="flex items-center gap-1 border-b border-muted bg-primary px-4 py-1.5">
      <span className="mr-2 text-xs font-semibold uppercase tracking-wide text-text/40">
        Roba
      </span>
      {NAV.map((item) => (
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
      ))}
    </nav>
  );
}

export function OperatorLayout() {
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
  }, []);

  return (
    <div className="min-h-full bg-primary text-text">
      <OperatorNav />
      <Outlet />
    </div>
  );
}
