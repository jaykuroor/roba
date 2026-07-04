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
 *
 * Voice model (persisted in localStorage as "roba.voice.model"):
 *   - Passed as ?model= in the WS URL.
 *   - Reconnects automatically when changed via setVoiceModel().
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { RobaLiveClient } from "./RobaLiveClient";
import type { PlanResult, Clarification } from "./RobaLiveClient";
import type { IntervalForecastResult } from "../track_a/types";

export type VoiceState =
  | "idle"         // not connected
  | "connecting"   // WS open, waiting for "connected" / "unavailable"
  | "reconnecting" // unexpected drop; auto-reconnect in progress
  | "ready"        // connected & ready to talk
  | "listening"    // mic is live, user speaking
  | "thinking"     // awaiting Roba response (PTT only)
  | "speaking"     // Roba audio playing back
  | "unavailable"; // no Vertex AI project configured / import failure

export type MicMode = "ptt" | "conversation";

export interface TranscriptLine {
  /** Stable per-session ID used for React keys. */
  id: string;
  role: "user" | "roba";
  text: string;
  /** Stable per-turn identifier from the server — used for in-place update. */
  turn_id: string;
  /** True once the server has finalised this line. */
  final: boolean;
}

export interface RawFrame {
  ts: string;        // hh:mm:ss timestamp
  role: "user" | "roba";
  text: string;
  turn_id: string;
  final: boolean;
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
  // Interval forecast card
  lastForecast: IntervalForecastResult | null;
  clearForecast: () => void;
  // Last applied action (auto-mode done card)
  lastApplied: { summary: string; tool: string } | null;
  clearLastApplied: () => void;
  setDone: (summary: string) => void;
  // Confirm/auto mode (whether Roba asks for approval before acting)
  mode: string;
  setMode: (m: string) => void;
  // Mic interaction mode
  micMode: MicMode;
  setMicMode: (m: MicMode) => void;
  // Live model (reconnects on change)
  voiceModel: string | undefined;
  setVoiceModel: (m: string | undefined) => void;
  // Card status for done/cancelled badge
  cardStatus: "pending" | "done" | "cancelled";
  // Raw transcript frames for developer portal
  rawFrames: RawFrame[];
  clearRawFrames: () => void;
}

const CONNECT_TIMEOUT_MS = 8_000;   // "connecting" → "unavailable"
// Safety net only — normally cleared by the first audio byte ("speaking"),
// the first roba transcript, a tool_result, or an error.
const THINKING_TIMEOUT_MS = 30_000; // "thinking" → "ready" + error msg
// Minimum mic hold duration to be treated as an intentional utterance.
// Presses shorter than this with no audio captured are silently dropped back
// to "ready" rather than spinning on "thinking" for 30 s.
const MIN_UTTERANCE_MS = 500;
// Conversation mode: auto-exit after this long with no speech from anyone.
const CONVERSATION_IDLE_MS = 60_000;

const MIC_MODE_KEY = "roba.voice.micMode";
const VOICE_MODEL_KEY = "roba.voice.model";

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

function readVoiceModel(): string | undefined {
  try {
    return localStorage.getItem(VOICE_MODEL_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

function writeVoiceModel(m: string | undefined) {
  try {
    if (m) localStorage.setItem(VOICE_MODEL_KEY, m);
    else localStorage.removeItem(VOICE_MODEL_KEY);
  } catch { /* ignore */ }
}

/**
 * Merge an incoming transcript event into the current line array.
 *
 * Logic:
 *   - If a line with the same turn_id already exists: update its text (and
 *     final flag). The id (React key) stays stable.
 *   - Otherwise: append a new line with a fresh id.
 *   - Cap at 200 lines.
 */
function mergeTranscriptLine(
  prev: TranscriptLine[],
  role: "user" | "roba",
  text: string,
  turn_id: string,
  final: boolean,
): TranscriptLine[] {
  const idx = prev.findLastIndex(
    (l) => l.role === role && l.turn_id === turn_id,
  );
  if (idx >= 0) {
    // In-place update (same turn_id).
    const updated = [...prev];
    updated[idx] = { ...updated[idx], text, final };
    return updated.slice(-200);
  }
  // New turn — append.
  return [
    ...prev.slice(-199),
    { id: nextId(), role, text, turn_id, final },
  ];
}

export function useVoiceLive(
  role: string,
  opts?: { extraParams?: Record<string, string> },
): VoiceLiveHook {
  const extraParamsRef = useRef<Record<string, string>>(opts?.extraParams ?? {});
  const [mode, setMode] = useState<string>("confirm");
  const [micMode, _setMicMode] = useState<MicMode>(readMicMode);
  const [voiceModel, _setVoiceModel] = useState<string | undefined>(readVoiceModel);
  const [state, setState] = useState<VoiceState>("idle");
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [pendingPlan, setPendingPlan] = useState<PlanResult | null>(null);
  const [clarification, setClarification] = useState<Clarification | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [lastStatus, setLastStatus] = useState<Record<string, unknown> | null>(null);
  const [lastForecast, setLastForecast] = useState<IntervalForecastResult | null>(null);
  const [lastApplied, setLastApplied] = useState<{ summary: string; tool: string } | null>(null);
  const [cardStatus, setCardStatus] = useState<"pending" | "done" | "cancelled">("pending");
  const [rawFrames, setRawFrames] = useState<RawFrame[]>([]);

  const clientRef = useRef<RobaLiveClient | null>(null);
  const connectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const thinkingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cardDismissTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Conversation-mode idle: fired when no speech in CONVERSATION_IDLE_MS.
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const micModeRef = useRef<MicMode>(micMode);
  const voiceModelRef = useRef<string | undefined>(voiceModel);
  // Timestamp of the most recent startListening() — used to detect sub-threshold
  // accidental presses (empty utterance guardrail).
  const listenStartRef = useRef<number>(0);

  // Keep refs in sync so callbacks close over the latest values.
  micModeRef.current = micMode;
  voiceModelRef.current = voiceModel;

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
  function clearCardDismissTimer() {
    if (cardDismissTimerRef.current) {
      clearTimeout(cardDismissTimerRef.current);
      cardDismissTimerRef.current = null;
    }
  }
  function clearIdleTimer() {
    if (idleTimerRef.current) {
      clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
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

  // Connect (or reconnect) when role, micMode, or voiceModel changes.
  useEffect(() => {
    const client = new RobaLiveClient(role, mode, micModeRef.current, voiceModelRef.current, extraParamsRef.current);
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

    // Arm (or reset) the conversation-mode idle timer.
    // Only active in conversation mode; fires stopListening after CONVERSATION_IDLE_MS of silence.
    function armIdleTimer() {
      if (micModeRef.current !== "conversation") return;
      clearIdleTimer();
      idleTimerRef.current = setTimeout(() => {
        clientRef.current?.stopListening();
        setState("ready");
        setLastError("Conversation paused after 60 s of silence — tap to resume.");
      }, CONVERSATION_IDLE_MS);
    }

    const unsub = client.on((ev) => {
      switch (ev.type) {
        case "connected":
          clearConnectTimer();
          setState("ready");
          break;
        case "reconnecting":
          // Unexpected socket drop; auto-reconnect is in progress.
          // Clear any running connect timer — the timeout was only for initial connect.
          clearConnectTimer();
          clearThinkingTimer();
          clearIdleTimer();
          setState("reconnecting");
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
          // Push raw frame (every partial and final, verbatim) before merging.
          const now = new Date();
          const ts = `${String(now.getHours()).padStart(2,"0")}:${String(now.getMinutes()).padStart(2,"0")}:${String(now.getSeconds()).padStart(2,"0")}`;
          setRawFrames(prev => [...prev.slice(-199), { ts, role: ev.role, text: ev.text, turn_id: ev.turn_id, final: ev.final }]);
          // In-place merge: update the existing bubble for this turn_id,
          // or append a new one. No more fragment spam.
          setTranscript((prev) =>
            mergeTranscriptLine(prev, ev.role, ev.text, ev.turn_id, ev.final),
          );
          if (ev.role === "roba") {
            clearThinkingTimer();
            setState("speaking");
          }
          // Any speech resets the conversation idle timer.
          armIdleTimer();
          break;
        }
        case "plan_preview":
          clearThinkingTimer();
          clearCardDismissTimer();
          setCardStatus("pending");
          setPendingPlan(ev.plan);
          setClarification(ev.plan.clarification ?? null);
          setState("ready");
          break;
        case "applied": {
          clearThinkingTimer();
          const summary = ev.summary || "Done.";
          setLastApplied({ summary, tool: ev.tool ?? "" });
          setTimeout(() => setLastApplied(null), 4000);
          // Show done badge on the confirm card, then auto-dismiss after 2.5s
          setCardStatus("done");
          clearCardDismissTimer();
          cardDismissTimerRef.current = setTimeout(() => {
            setPendingPlan(null);
            setClarification(null);
            setCardStatus("pending");
          }, 2500);
          setState("ready");
          break;
        }
        case "tool_result":
          clearThinkingTimer();
          if (ev.tool === "get_kitchen_status" && ev.result) {
            setLastStatus(ev.result as Record<string, unknown>);
          }
          if (ev.tool === "forecast_demand" && ev.result) {
            setLastForecast(ev.result as IntervalForecastResult);
          }
          break;
        case "speaking":
          // First audio byte arrived — the pipeline is responding.
          clearThinkingTimer();
          setState("speaking");
          // Roba speaking counts as activity — reset idle timer.
          armIdleTimer();
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
          // User is speaking — reset idle timer.
          armIdleTimer();
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
          clearIdleTimer();
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
      clearCardDismissTimer();
      clearIdleTimer();
      client.disconnect();
    };
    // Reconnect when role, micMode, or voiceModel changes. The Live session's
    // VAD config is fixed at connect time (PTT disables auto-VAD, conversation
    // enables it), so switching mic mode requires a new session. MicModeToggle
    // is disabled while state === "listening", so this never reconnects mid-turn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [role, micMode, voiceModel, mode]);

  const startListening = useCallback(async () => {
    if (!clientRef.current) return;
    clearThinkingTimer();
    clearCardDismissTimer();
    setCardStatus("pending");
    setLastError(null);
    listenStartRef.current = Date.now(); // stamp for empty-utterance guardrail
    setState("listening");
    // Arm the idle timer when entering conversation mode.
    if (micModeRef.current === "conversation") {
      clearIdleTimer();
      idleTimerRef.current = setTimeout(() => {
        clientRef.current?.stopListening();
        setState("ready");
        setLastError("Conversation paused after 60 s of silence — tap to resume.");
      }, CONVERSATION_IDLE_MS);
    }
    await clientRef.current.startListening();
  }, []);

  const stopListening = useCallback(() => {
    if (!clientRef.current) return;
    // Always cancel the idle timer on explicit stop (or mode switch).
    clearIdleTimer();
    if (micModeRef.current === "ptt") {
      const elapsed = Date.now() - listenStartRef.current;

      if (elapsed < MIN_UTTERANCE_MS) {
        // Sub-threshold tap: abort — skip activity_end even if silent PCM chunks
        // were already streamed (worklet sends chunks immediately, so <500 ms
        // doesn't represent real speech). Server never starts a generation turn.
        clientRef.current.stopListening({ abort: true });
        clearThinkingTimer();
        setState("ready");
        return;
      }

      // Long enough press — check whether the client actually sent audio chunks
      // (safety net for an immediately-toggled-off press in conversation mode).
      const audioSent = clientRef.current.stopListening();
      if (!audioSent) {
        clearThinkingTimer();
        setState("ready");
        return;
      }

      // Real utterance — wait for Roba's response.
      setStateWithTimeout("thinking");
    } else {
      clientRef.current.stopListening();
      // Conversation ended by the user — just return to ready.
      setState("ready");
    }
  }, [setStateWithTimeout]);

  const sendText = useCallback(
    (text: string) => {
      if (!clientRef.current) return;
      clientRef.current.sendText(text);
      clearCardDismissTimer();
      setCardStatus("pending");
      setStateWithTimeout("thinking");
    },
    [setStateWithTimeout],
  );

  const confirmPlan = useCallback((planId: string) => {
    clientRef.current?.confirmPlan(planId);
    setCardStatus("done");
    clearCardDismissTimer();
    cardDismissTimerRef.current = setTimeout(() => {
      setPendingPlan(null);
      setClarification(null);
      setCardStatus("pending");
    }, 2500);
  }, []);

  const cancelPlan = useCallback((planId: string) => {
    clientRef.current?.cancelPlan(planId);
    setCardStatus("cancelled");
    clearCardDismissTimer();
    cardDismissTimerRef.current = setTimeout(() => {
      setPendingPlan(null);
      setClarification(null);
      setCardStatus("pending");
    }, 2500);
  }, []);

  const setMicMode = useCallback((m: MicMode) => {
    writeMicMode(m);
    _setMicMode(m);
    // No setMicMode call on the client: the reconnect triggered by the micMode
    // dep in the connect effect below builds a fresh session with the new config.
  }, []);

  const setVoiceModel = useCallback((m: string | undefined) => {
    writeVoiceModel(m);
    _setVoiceModel(m);
    // Reconnect is triggered by the voiceModel dep in the connect effect.
  }, []);

  const clearTranscript = useCallback(() => setTranscript([]), []);
  const clearStatus = useCallback(() => setLastStatus(null), []);
  const clearLastApplied = useCallback(() => setLastApplied(null), []);
  const clearForecast = useCallback(() => setLastForecast(null), []);
  const clearRawFrames = useCallback(() => setRawFrames([]), []);

  const setDone = useCallback((summary: string) => {
    setLastApplied({ summary, tool: "" });
    setTimeout(() => setLastApplied(null), 4000);
  }, []);

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
    lastForecast,
    clearForecast,
    lastApplied,
    clearLastApplied,
    setDone,
    mode,
    setMode,
    micMode,
    setMicMode,
    voiceModel,
    setVoiceModel,
    cardStatus,
    rawFrames,
    clearRawFrames,
  };
}
