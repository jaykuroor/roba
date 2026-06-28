/**
 * useVoiceLive — React hook wrapping RobaLiveClient.
 *
 * Manages the WS connection lifecycle, exposes a stable state object and
 * action callbacks, and cleans up on unmount.
 *
 * Timeouts:
 *   - Connection timeout: if "connecting" lasts > 8s → "unavailable"
 *   - Thinking timeout: if "thinking" lasts > 20s → reset to "ready" + error
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { RobaLiveClient } from "./RobaLiveClient";
import type { PlanResult, Clarification } from "./RobaLiveClient";

export type VoiceState =
  | "idle"         // not connected
  | "connecting"   // WS open, waiting for "connected" / "unavailable"
  | "ready"        // connected & ready to talk
  | "listening"    // mic is live, user speaking
  | "thinking"     // awaiting Roba response
  | "speaking"     // Roba audio playing back
  | "unavailable"; // no Vertex AI project configured / import failure

export interface TranscriptLine {
  id: string;
  role: "user" | "roba";
  text: string;
}

export interface VoiceLiveHook {
  state: VoiceState;
  transcript: TranscriptLine[];
  pendingPlan: PlanResult | null;
  clarification: Clarification | null;
  lastError: string | null;
  // Actions
  startListening: () => Promise<void>;
  stopListening: () => void;
  sendText: (text: string) => void;
  confirmPlan: (planId: string) => void;
  cancelPlan: (planId: string) => void;
  clearTranscript: () => void;
  // Tool results
  lastStatus: Record<string, unknown> | null;
  clearStatus: () => void;
  // Mode
  mode: string;
  setMode: (m: string) => void;
}

const CONNECT_TIMEOUT_MS = 8_000;   // "connecting" → "unavailable"
const THINKING_TIMEOUT_MS = 20_000; // "thinking" → "ready" + error msg

let _lineId = 0;
function nextId() {
  return String(++_lineId);
}

export function useVoiceLive(role: string): VoiceLiveHook {
  const [mode, setMode] = useState<string>("confirm");
  const [state, setState] = useState<VoiceState>("idle");
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [pendingPlan, setPendingPlan] = useState<PlanResult | null>(null);
  const [clarification, setClarification] = useState<Clarification | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [lastStatus, setLastStatus] = useState<Record<string, unknown> | null>(null);

  const clientRef = useRef<RobaLiveClient | null>(null);
  const connectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const thinkingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function clearConnectTimer() {
    if (connectTimerRef.current) {
      clearTimeout(connectTimerRef.current);
      connectTimerRef.current = null;
    }
  }
  function clearThinkingTimer() {
    if (thinkingTimerRef.current) {
      clearTimeout(thinkingTimerRef.current);
      thinkingTimerRef.current = null;
    }
  }

  // Whenever we enter "thinking", arm a timeout.
  const setStateWithTimeout = useCallback((next: VoiceState) => {
    clearThinkingTimer();
    setState(next);
    if (next === "thinking") {
      thinkingTimerRef.current = setTimeout(() => {
        setState((cur) => (cur === "thinking" ? "ready" : cur));
        setLastError("No response — Roba may be unavailable. Try again.");
      }, THINKING_TIMEOUT_MS);
    }
  }, []);

  // Connect (or reconnect) when role changes.
  useEffect(() => {
    const client = new RobaLiveClient(role, mode);
    clientRef.current = client;
    setState("connecting");
    setLastError(null);

    // Arm a connection timeout.
    clearConnectTimer();
    connectTimerRef.current = setTimeout(() => {
      setState((cur) => {
        if (cur === "connecting") {
          setLastError("Connection timed out. Check that the server is running.");
          return "unavailable";
        }
        return cur;
      });
    }, CONNECT_TIMEOUT_MS);

    const unsub = client.on((ev) => {
      switch (ev.type) {
        case "connected":
          clearConnectTimer();
          setState("ready");
          break;
        case "unavailable":
          clearConnectTimer();
          setState("unavailable");
          if (ev.reason === "no_gcp_project") {
            setLastError("Voice unavailable — Vertex AI not configured. Use the text input below.");
          } else if (ev.reason) {
            setLastError(`Voice unavailable: ${ev.reason}`);
          }
          break;
        case "transcript": {
          setTranscript((prev) => [
            ...prev.slice(-199),
            { id: nextId(), role: ev.role, text: ev.text },
          ]);
          if (ev.role === "roba") {
            clearThinkingTimer();
            setState("speaking");
          }
          break;
        }
        case "plan_preview":
          clearThinkingTimer();
          setPendingPlan(ev.plan);
          setClarification(ev.plan.clarification ?? null);
          setState("ready");
          break;
        case "applied":
          clearThinkingTimer();
          setPendingPlan(null);
          setClarification(null);
          setState("ready");
          break;
        case "tool_result":
          clearThinkingTimer();
          setState("ready");
          if (ev.tool === "get_kitchen_status" && ev.result) {
            setLastStatus(ev.result as Record<string, unknown>);
          }
          break;
        case "error":
          clearThinkingTimer();
          setLastError(ev.message);
          setState((cur) => (cur === "thinking" || cur === "connecting" ? "ready" : cur));
          break;
        case "disconnected":
          clearConnectTimer();
          clearThinkingTimer();
          setState("idle");
          break;
      }
    });

    client.connect().catch((err) => {
      clearConnectTimer();
      setLastError(String(err));
      setState("unavailable");
    });

    return () => {
      unsub();
      clearConnectTimer();
      clearThinkingTimer();
      client.disconnect();
    };
    // Reconnect on role change only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [role]);

  const startListening = useCallback(async () => {
    if (!clientRef.current) return;
    clearThinkingTimer();
    setLastError(null);
    setState("listening");
    await clientRef.current.startListening();
  }, []);

  const stopListening = useCallback(() => {
    if (!clientRef.current) return;
    clientRef.current.stopListening();
    setStateWithTimeout("thinking");
  }, [setStateWithTimeout]);

  const sendText = useCallback(
    (text: string) => {
      if (!clientRef.current) return;
      clientRef.current.sendText(text);
      setStateWithTimeout("thinking");
    },
    [setStateWithTimeout],
  );

  const confirmPlan = useCallback((planId: string) => {
    clientRef.current?.confirmPlan(planId);
    setPendingPlan(null);
    setClarification(null);
  }, []);

  const cancelPlan = useCallback((planId: string) => {
    clientRef.current?.cancelPlan(planId);
    setPendingPlan(null);
    setClarification(null);
  }, []);

  const clearTranscript = useCallback(() => setTranscript([]), []);
  const clearStatus = useCallback(() => setLastStatus(null), []);

  return {
    state,
    transcript,
    pendingPlan,
    clarification,
    lastError,
    startListening,
    stopListening,
    sendText,
    confirmPlan,
    cancelPlan,
    clearTranscript,
    lastStatus,
    clearStatus,
    mode,
    setMode,
  };
}
