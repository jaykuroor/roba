import { useCallback, useEffect, useState } from "react";
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

export function useTrackAData() {
  const [data, setData] = useState<TrackASnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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
    const unsubscribers = REFRESH_EVENTS.map((event) =>
      wsClient.on(event, () => {
        void refresh();
      }),
    );
    return () => {
      unsubscribers.forEach((unsubscribe) => unsubscribe());
    };
  }, [refresh]);

  return { data, error, loading, refresh };
}
