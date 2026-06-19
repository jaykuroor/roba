import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "../api";
import { wsClient } from "../ws";
import type { TrackASnapshot } from "./types";

const REFRESH_EVENTS = [
  "signal_emitted",
  "forecast_updated",
  "batch_decided",
  "competitor_update",
  "competitor_intel",
  "review_insight",
  "staff_coverage",
  "call_ended",
];

const REFRESH_DEBOUNCE_MS = 150;
const REFRESH_POLL_MS = 5000;

export function useTrackAData() {
  const [data, setData] = useState<TrackASnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await apiGet<TrackASnapshot>("/api/track-a/snapshot");
      setData(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load Track A");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial server snapshot load
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const scheduleRefresh = () => {
      if (refreshTimer.current !== null) {
        clearTimeout(refreshTimer.current);
      }
      refreshTimer.current = setTimeout(() => {
        refreshTimer.current = null;
        void refresh();
      }, REFRESH_DEBOUNCE_MS);
    };

    const poll = setInterval(() => {
      void refresh();
    }, REFRESH_POLL_MS);
    const unsubscribers = REFRESH_EVENTS.map((event) =>
      wsClient.on(event, scheduleRefresh),
    );
    return () => {
      clearInterval(poll);
      if (refreshTimer.current !== null) {
        clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
      unsubscribers.forEach((unsubscribe) => unsubscribe());
    };
  }, [refresh]);

  return { data, error, loading, refresh };
}
