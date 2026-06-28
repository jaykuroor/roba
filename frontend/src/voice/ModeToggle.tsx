/**
 * ModeToggle — confirm-first / auto toggle for the voice interface.
 * "confirm" = Roba reads back a plan and asks for approval before acting.
 * "auto"    = Roba acts immediately.
 */

interface ModeToggleProps {
  mode: string;
  onChange: (mode: string) => void;
  disabled?: boolean;
}

export function ModeToggle({ mode, onChange, disabled }: ModeToggleProps) {
  const isAuto = mode === "auto";

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-text/50">Confirm-first</span>
      <button
        role="switch"
        aria-checked={isAuto}
        disabled={disabled}
        onClick={() => onChange(isAuto ? "confirm" : "auto")}
        className={[
          "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors",
          isAuto ? "bg-accent" : "bg-muted",
          disabled ? "cursor-not-allowed opacity-40" : "cursor-pointer",
        ].join(" ")}
      >
        <span
          className={[
            "absolute h-4 w-4 rounded-full bg-text shadow transition-transform",
            isAuto ? "translate-x-4" : "translate-x-1",
          ].join(" ")}
        />
      </button>
      <span className="text-xs text-text/50">Auto</span>
    </div>
  );
}
