/**
 * MicButton — voice activation button with two interaction modes:
 *
 *   Push-to-talk (micMode="ptt"):
 *     Click to toggle: tap once to start listening, tap again to stop.
 *     Hold to talk:    press and hold to listen, release to send.
 *     Both modes are active simultaneously.  A press shorter than
 *     HOLD_THRESHOLD_MS is treated as a click (toggle); a longer press is
 *     hold-to-talk (release stops).
 *
 *   Active conversation (micMode="conversation"):
 *     Tap once to start the conversation (mic stays open, Gemini auto-VAD
 *     handles turn-taking).  Tap again to end.  No hold logic.
 */

import { useRef, useState } from "react";
import { Mic, MicOff, Loader2, Volume2, Radio } from "lucide-react";
import type { VoiceState, MicMode } from "./useVoiceLive";

const HOLD_THRESHOLD_MS = 300; // presses shorter than this → click/toggle (PTT only)

export const STATE_LABEL: Record<VoiceState, string> = {
  idle: "—",
  connecting: "Connecting…",
  ready: "Tap or hold to talk",
  listening: "Listening… release or tap to send",
  thinking: "Thinking…",
  speaking: "Roba speaking — tap to interrupt",
  unavailable: "Voice unavailable",
};

export const STATE_LABEL_CONVERSATION: Record<VoiceState, string> = {
  idle: "—",
  connecting: "Connecting…",
  ready: "Tap to start conversation",
  listening: "Live — tap to end",
  thinking: "Thinking…",
  speaking: "Roba speaking",
  unavailable: "Voice unavailable",
};

interface MicButtonProps {
  state: VoiceState;
  micMode?: MicMode;
  size?: "sm" | "md" | "lg";
  onStart: () => void;
  onStop: () => void;
}

const SIZE = {
  sm: { btn: "h-20 w-20", icon: 32 },
  md: { btn: "h-24 w-24", icon: 36 },
  lg: { btn: "h-28 w-28", icon: 40 },
};

export function MicButton({
  state,
  micMode = "ptt",
  size = "md",
  onStart,
  onStop,
}: MicButtonProps) {
  const pressTimeRef = useRef<number | null>(null);
  const isHoldRef = useRef(false);
  // True while the mic is held open by a tap (toggle) rather than a press-hold.
  const [toggleListening, setToggleListening] = useState(false);
  const isListening = state === "listening";
  const disabled =
    state === "idle" ||
    state === "connecting" ||
    state === "unavailable" ||
    state === "thinking";

  const { btn, icon } = SIZE[size];
  const labels = micMode === "conversation" ? STATE_LABEL_CONVERSATION : STATE_LABEL;

  // ---------------------------------------------------------------------------
  // Conversation mode — simple toggle, no hold logic
  // ---------------------------------------------------------------------------

  if (micMode === "conversation") {
    return (
      <div className="flex flex-col items-center gap-3">
        {isListening && (
          <span className="flex items-center gap-1.5 rounded-full bg-danger/15 px-3 py-1 text-xs font-medium text-danger">
            <span className="h-2 w-2 animate-pulse rounded-full bg-danger" />
            Live — tap to end conversation
          </span>
        )}
        <button
          disabled={disabled}
          onClick={() => {
            if (isListening) {
              onStop();
            } else if (!disabled) {
              onStart();
            }
          }}
          aria-label={isListening ? "End conversation" : "Start conversation"}
          aria-pressed={isListening}
          className={[
            "relative flex items-center justify-center rounded-full shadow-lg transition-all select-none touch-none",
            btn,
            isListening
              ? "scale-110 bg-danger ring-4 ring-danger/40"
              : disabled
              ? "cursor-not-allowed bg-muted opacity-40"
              : "cursor-pointer bg-accent hover:bg-accent/90 active:scale-95",
          ].join(" ")}
        >
          {state === "thinking" || state === "connecting" ? (
            <Loader2 size={icon} className="animate-spin text-white" />
          ) : state === "speaking" ? (
            <Volume2 size={icon} className="text-white" />
          ) : isListening ? (
            <Radio size={icon} className="text-white" />
          ) : state === "unavailable" ? (
            <MicOff size={icon} className="text-text/50" />
          ) : (
            <Mic size={icon} className="text-white" />
          )}
          {isListening && (
            <span className="absolute inset-0 animate-ping rounded-full bg-danger/30 pointer-events-none" />
          )}
        </button>
        <span className="text-xs text-text/60 text-center max-w-[160px]">
          {labels[state]}
        </span>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // PTT mode — hold-to-talk + tap-to-toggle
  // ---------------------------------------------------------------------------

  // Stop capturing immediately and clear toggle bookkeeping.
  function stop() {
    setToggleListening(false);
    onStop();
  }

  function handlePointerDown(e: React.PointerEvent) {
    if (disabled) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    pressTimeRef.current = Date.now();
    isHoldRef.current = false;
    // If already listening (toggled on), pressing down is the start of a release/stop.
    // If not listening, we start listening and decide on pointerUp whether it was a tap or hold.
    if (!isListening) {
      onStart();
    }
  }

  function handlePointerUp() {
    if (disabled || pressTimeRef.current === null) return;
    const elapsed = Date.now() - pressTimeRef.current;
    pressTimeRef.current = null;

    if (isListening) {
      if (elapsed < HOLD_THRESHOLD_MS) {
        // Short tap while listening: it was a toggle-on. Keep listening; mark it
        // a toggle so the UI shows a persistent "listening" indicator.
        setToggleListening(true);
        return;
      } else {
        // Hold-to-talk release: stop instantly.
        isHoldRef.current = true;
        stop();
      }
    } else {
      // We called onStart() on pointerDown. If it was a short tap, toggle mode:
      // keep listening until next tap.  If it was a long hold, stop immediately.
      if (elapsed >= HOLD_THRESHOLD_MS) {
        isHoldRef.current = true;
        stop();
      } else {
        // short tap = toggle on → keep listening, show the indicator.
        setToggleListening(true);
      }
    }
  }

  function handlePointerLeave() {
    // If currently in hold mode (long press that left the button), stop instantly.
    if (!disabled && isListening && pressTimeRef.current !== null) {
      const elapsed = Date.now() - pressTimeRef.current;
      pressTimeRef.current = null;
      if (elapsed >= HOLD_THRESHOLD_MS) {
        stop();
      }
    }
  }

  // Pointer interrupted (e.g. OS gesture, mouse left window mid-press): if we're
  // in a hold, stop recording instantly so we never keep a dangling open mic.
  function handlePointerCancel() {
    pressTimeRef.current = null;
    if (isListening && !toggleListening) {
      stop();
    }
  }

  // A second tap while listening (and not holding) = toggle off.
  function handleClick() {
    if (disabled) return;
    // Only treat as toggle-off if we didn't just do a hold-to-talk release.
    if (isListening && !isHoldRef.current) {
      stop();
    }
    isHoldRef.current = false;
  }

  return (
    <div className="flex flex-col items-center gap-3">
      {/* Persistent indicator when the mic is toggled on (tap-to-talk), so the
          user always knows it is still listening until they tap again. */}
      {isListening && toggleListening && (
        <span className="flex items-center gap-1.5 rounded-full bg-danger/15 px-2.5 py-1 text-xs font-medium text-danger">
          <span className="h-2 w-2 animate-pulse rounded-full bg-danger" />
          Listening — tap to send
        </span>
      )}
      <button
        disabled={disabled}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onPointerLeave={handlePointerLeave}
        onPointerCancel={handlePointerCancel}
        onClick={handleClick}
        aria-label={isListening ? "Stop listening" : "Start listening"}
        aria-pressed={isListening}
        className={[
          "relative flex items-center justify-center rounded-full shadow-lg transition-all select-none touch-none",
          btn,
          isListening
            ? "scale-110 bg-danger ring-4 ring-danger/40"
            : disabled
            ? "cursor-not-allowed bg-muted opacity-40"
            : "cursor-pointer bg-accent hover:bg-accent/90 active:scale-95",
        ].join(" ")}
      >
        {state === "thinking" || state === "connecting" ? (
          <Loader2 size={icon} className="animate-spin text-white" />
        ) : state === "speaking" ? (
          <Volume2 size={icon} className="text-white" />
        ) : isListening ? (
          <Mic size={icon} className="text-white" />
        ) : state === "unavailable" ? (
          <MicOff size={icon} className="text-text/50" />
        ) : (
          <Mic size={icon} className="text-white" />
        )}
        {isListening && (
          <span className="absolute inset-0 animate-ping rounded-full bg-danger/30 pointer-events-none" />
        )}
      </button>
      <span className="text-xs text-text/60 text-center max-w-[160px]">
        {labels[state]}
      </span>
    </div>
  );
}
