/**
 * MicButton — voice activation button with two interaction modes:
 *
 *   Click to toggle: tap once to start listening, tap again to stop.
 *   Hold to talk:    press and hold to listen, release to send.
 *
 * Both modes are active simultaneously.  A press shorter than HOLD_THRESHOLD_MS
 * is treated as a click (toggle); a longer press is hold-to-talk (release stops).
 * This matches common voice assistant UX conventions.
 */

import { useRef } from "react";
import { Mic, MicOff, Loader2, Volume2 } from "lucide-react";
import type { VoiceState } from "./useVoiceLive";

const HOLD_THRESHOLD_MS = 300; // presses shorter than this → click/toggle

export const STATE_LABEL: Record<VoiceState, string> = {
  idle: "—",
  connecting: "Connecting…",
  ready: "Tap or hold to talk",
  listening: "Listening… release or tap to send",
  thinking: "Thinking…",
  speaking: "Roba speaking",
  unavailable: "Voice unavailable",
};

interface MicButtonProps {
  state: VoiceState;
  size?: "sm" | "md" | "lg";
  onStart: () => void;
  onStop: () => void;
}

const SIZE = {
  sm: { btn: "h-20 w-20", icon: 32 },
  md: { btn: "h-24 w-24", icon: 36 },
  lg: { btn: "h-28 w-28", icon: 40 },
};

export function MicButton({ state, size = "md", onStart, onStop }: MicButtonProps) {
  const pressTimeRef = useRef<number | null>(null);
  const isHoldRef = useRef(false);
  const isListening = state === "listening";
  const disabled =
    state === "idle" ||
    state === "connecting" ||
    state === "unavailable" ||
    state === "thinking";

  const { btn, icon } = SIZE[size];

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
        // Short tap while listening: was a toggle-on. Keep it on (toggle behaviour).
        // Do nothing — user tapped to toggle it on, another tap will toggle off.
        return;
      } else {
        // Hold-to-talk release: stop.
        isHoldRef.current = true;
        onStop();
      }
    } else {
      // We called onStart() on pointerDown. If it was a short tap, toggle mode:
      // keep listening until next tap.  If it was a long hold, stop immediately.
      if (elapsed >= HOLD_THRESHOLD_MS) {
        isHoldRef.current = true;
        onStop();
      }
      // else: short tap = toggle on, do nothing on pointerUp.
    }
  }

  function handlePointerLeave() {
    // If currently in hold mode (long press that left the button), stop.
    if (!disabled && isListening && pressTimeRef.current !== null) {
      const elapsed = Date.now() - pressTimeRef.current;
      pressTimeRef.current = null;
      if (elapsed >= HOLD_THRESHOLD_MS) {
        onStop();
      }
    }
  }

  // A second tap while listening (and not holding) = toggle off.
  function handleClick() {
    if (disabled) return;
    // Only treat as toggle-off if we didn't just do a hold-to-talk release.
    if (isListening && !isHoldRef.current) {
      onStop();
    }
    isHoldRef.current = false;
  }

  return (
    <div className="flex flex-col items-center gap-3">
      <button
        disabled={disabled}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onPointerLeave={handlePointerLeave}
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
        {STATE_LABEL[state]}
      </span>
    </div>
  );
}
