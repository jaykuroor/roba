/**
 * useVoiceLive — React hook wrapping RobaLiveClient.
 *
 * Manages the WS connection lifecycle, exposes a stable state object and
 * action callbacks, and cleans up on unmount.
 *
 * Timeouts:
 *   - Connection timeout: if "connecting" lasts > 8s → "unavailable"
 *   - Thinking timeout: if "thinking" lasts > 30s → reset to "ready" + error
 *
 * Mic modes (persisted in localStorage as "roba.voice.micMode"):
 *   - "ptt"          Push-to-talk (default). Hold/tap the button per utterance;
 *                    sends end_of_turn when released. Enters "thinking" on stop.
 *   - "conversation" Active conversation. Mic stays open; Gemini's auto-VAD
 *                    detects turn ends. Tap once to start, tap again to end.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { RobaLiveClient } from "./RobaLiveClient";
import type { PlanResult, Clarification } from "./RobaLiveClient";

export type VoiceState =
  | "idle"         // not connected
  | "connecting"   // WS open, waiting for "connected" / "unavailable"
  | "ready"        // connected & ready to talk
  | "listening"    // mic is live, user speaking
  | "thinking"     // awaiting Roba response (PTT only)
  | "speaking"     // Roba audio playing back
  | "unavailable"; // no Vertex AI project configured / import failure

export type MicMode = "ptt" | "conversation";

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
  // Confirm/auto mode (whether Roba asks for approval before acting)
  mode: string;
  setMode: (m: string) => void;
  // Mic interaction mode
  micMode: MicMode;
  setMicMode: (m: MicMode) => void;
}

const CONNECT_TIMEOUT_MS = 8_000;   // "connecting" → "unavailable"
// Safety net only — normally cleared by the first audio byte ("speaking"),
// the first roba transcript, a tool_result, or an error.
const THINKING_TIMEOUT_MS = 30_000; // "thinking" → "ready" + error msg

const MIC_MODE_KEY = "roba.voice.micMode";

let _lineId = 0;
function nextId() {
  return String(++_lineId);
}

function readMicMode(): MicMode {
  try {
    const stored = localStorage.getItem(MIC_MODE_KEY);
    if (stored === "ptt" || stored === "conversation") return stored;
  } catch {
    // localStorage unavailable
  }
  return "ptt";
}

function writeMicMode(m: MicMode) {
  try { localStorage.setItem(MIC_MODE_KEY, m); } catch { /* ignore */ }
}

export function useVoiceLive(role: string): VoiceLiveHook {
  const [mode, setMode] = useState<string>("confirm");
  const [micMode, _setMicMode] = useState<MicMode>(readMicMode);
  const [state, setState] = useState<VoiceState>("idle");
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [pendingPlan, setPendingPlan] = useState<PlanResult | null>(null);
  const [clarification, setClarification] = useState<Clarification | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [lastStatus, setLastStatus] = useState<Record<string, unknown> | null>(null);

  const clientRef = useRef<RobaLiveClient | null>(null);
  const connectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const thinkingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const micModeRef = useRef<MicMode>(micMode);

  // Keep the ref in sync so callbacks close over the latest micMode value.
  micModeRef.current = micMode;

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
    const client = new RobaLiveClient(role, mode, micModeRef.current);
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
          if (ev.tool === "get_kitchen_status" && ev.result) {
            setLastStatus(ev.result as Record<string, unknown>);
          }
          break;
        case "speaking":
          // First audio byte arrived — the pipeline is responding.
          clearThinkingTimer();
          setState("speaking");
          break;
        case "turn_complete":
          // Done generating; stay "speaking" until audio drains (playback_done).
          clearThinkingTimer();
          break;
        case "interrupted":
          // Barge-in: user spoke while Roba was talking. In conversation mode
          // the mic stays open; in PTT the user is pressing the button.
          clearThinkingTimer();
          setState("listening");
          break;
        case "playback_done":
          setState((cur) =>
            cur === "speaking" || cur === "thinking" ? "ready" : cur,
          );
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
    // Reconnect on role change only.  mode/micMode are pushed via setters on
    // the live client instead of reconnecting.
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
    if (micModeRef.current === "ptt") {
      // Push-to-talk: enter "thinking" and wait for Roba's response.
      setStateWithTimeout("thinking");
    } else {
      // Conversation ended by the user — just return to ready.
      setState("ready");
    }
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

  const setMicMode = useCallback((m: MicMode) => {
    writeMicMode(m);
    _setMicMode(m);
    clientRef.current?.setMicMode(m);
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
    micMode,
    setMicMode,
  };
}
