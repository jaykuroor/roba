/**
 * VoicePage — top-level voice interface for restaurant staff.
 *
 * Step 1: Choose role (Manager or Cook/Kitchen).
 * Step 2: Render the role-specific voice UI (ManagerVoice or CookVoice).
 *
 * Mounted OUTSIDE OperatorLayout so it never opens the operator WS firehose.
 * Link back to the operator console is available in the header.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import { ChefHat, LayoutDashboard, Mic, UserCog } from "lucide-react";
import { ManagerVoice } from "./ManagerVoice";
import { CookVoice } from "./CookVoice";

type Role = "manager" | "cook";

function RoleButton({
  role,
  icon: Icon,
  label,
  description,
  selected,
  onClick,
}: {
  role: Role;
  icon: React.ElementType;
  label: string;
  description: string;
  selected: boolean;
  onClick: (r: Role) => void;
}) {
  return (
    <button
      onClick={() => onClick(role)}
      className={[
        "flex flex-col items-center gap-3 rounded-2xl border p-6 text-center transition-all",
        selected
          ? "border-accent bg-accent/10 shadow-lg shadow-accent/10"
          : "border-muted bg-surface hover:border-muted/80 hover:bg-muted/20",
      ].join(" ")}
    >
      <div
        className={[
          "flex h-14 w-14 items-center justify-center rounded-full",
          selected ? "bg-accent text-white" : "bg-muted text-text/60",
        ].join(" ")}
      >
        <Icon size={28} />
      </div>
      <div>
        <p className="font-semibold text-text">{label}</p>
        <p className="mt-1 text-xs text-text/50">{description}</p>
      </div>
    </button>
  );
}

export default function VoicePage() {
  const [role, setRole] = useState<Role | null>(null);

  return (
    <div className="min-h-screen bg-primary text-text">
      {/* Top bar */}
      <header className="flex items-center justify-between border-b border-muted bg-surface px-5 py-3">
        <div className="flex items-center gap-2">
          <Mic size={18} className="text-accent" />
          <span className="text-sm font-bold tracking-wide text-text">
            {role === "cook" ? "Roba Kitchen Desk" : role === "manager" ? "Roba Ops Desk" : "Roba Desk"}
          </span>
          {role && (
            <span className="ml-1 rounded-full bg-muted px-2 py-0.5 text-xs font-medium capitalize text-text/60">
              {role === "cook" ? "Kitchen" : "Manager"}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {role && (
            <button
              onClick={() => setRole(null)}
              className="rounded-md px-2.5 py-1 text-xs font-medium text-text/50 hover:bg-muted/50 hover:text-text"
            >
              Switch role
            </button>
          )}
          <Link
            to="/"
            className="flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium text-text/50 hover:bg-muted/50 hover:text-text"
          >
            <LayoutDashboard size={13} />
            Dashboard
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-lg px-4 py-8">
        {/* Role chooser */}
        {!role && (
          <div className="flex flex-col gap-6">
            <div className="text-center">
              <h1 className="text-2xl font-bold text-text">Who are you?</h1>
              <p className="mt-1 text-sm text-text/50">
                Ask about batches, approvals, and operations — or speak to record updates.
              </p>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <RoleButton
                role="manager"
                icon={UserCog}
                label="Manager"
                description="Full Roba console, competitor intel, approvals"
                selected={role === "manager"}
                onClick={setRole}
              />
              <RoleButton
                role="cook"
                icon={ChefHat}
                label="Kitchen"
                description="Next batch queue, mark cooked, report waste"
                selected={role === "cook"}
                onClick={setRole}
              />
            </div>
            <p className="text-center text-xs text-text/30">
              Hold the mic button or tap to talk — ask questions or report updates.
            </p>
          </div>
        )}

        {/* Role-specific UI */}
        {role === "manager" && <ManagerVoice />}
        {role === "cook" && <CookVoice />}
      </main>
    </div>
  );
}
