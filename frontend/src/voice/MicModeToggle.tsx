/**
 * MicModeToggle — segmented control for choosing the mic interaction mode.
 *
 *   Push to talk     Tap/hold the button per utterance (default).
 *   Active conv.     Tap once to start; mic stays open with auto-VAD.
 */

import { Mic, Radio } from "lucide-react";
import type { MicMode } from "./useVoiceLive";

interface MicModeToggleProps {
  micMode: MicMode;
  onChange: (mode: MicMode) => void;
  disabled?: boolean;
}

export function MicModeToggle({ micMode, onChange, disabled }: MicModeToggleProps) {
  return (
    <div
      className={[
        "inline-flex rounded-lg border border-muted bg-muted/30 p-0.5 gap-0.5",
        disabled ? "opacity-40 pointer-events-none" : "",
      ].join(" ")}
      role="group"
      aria-label="Mic interaction mode"
    >
      <button
        role="radio"
        aria-checked={micMode === "ptt"}
        disabled={disabled}
        onClick={() => onChange("ptt")}
        title="Push to talk"
        className={[
          "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
          micMode === "ptt"
            ? "bg-surface text-text shadow-sm"
            : "text-text/50 hover:text-text/80",
        ].join(" ")}
      >
        <Mic size={12} />
        Push to talk
      </button>
      <button
        role="radio"
        aria-checked={micMode === "conversation"}
        disabled={disabled}
        onClick={() => onChange("conversation")}
        title="Active conversation"
        className={[
          "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
          micMode === "conversation"
            ? "bg-surface text-text shadow-sm"
            : "text-text/50 hover:text-text/80",
        ].join(" ")}
      >
        <Radio size={12} />
        Active conv.
      </button>
    </div>
  );
}
