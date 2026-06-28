/**
 * PlanConfirmCard — surfaces the pending Roba plan (or a clarification
 * question) before the user commits.
 *
 * Designed for quick, eyes-up decision-making in a noisy kitchen/front-of-house
 * environment — large text, big tap targets, clear hierarchy.
 *
 * Shows:
 *  • A clear header ("Roba's Plan" or "Roba Needs to Know")
 *  • A large hero summary of what will happen
 *  • Target agents that will be triggered (readable chips)
 *  • A "What happens" breakdown of each route
 *  • Large Confirm / Cancel buttons (or clarification option buttons)
 */

import { Check, X, ChevronRight, ClipboardList, HelpCircle } from "lucide-react";
import type { PlanResult, Clarification } from "./RobaLiveClient";

interface PlanConfirmCardProps {
  plan: PlanResult;
  clarification?: Clarification | null;
  onConfirm: (planId: string) => void;
  onCancel: (planId: string) => void;
  onClarify?: (planId: string, answer: string) => void;
}

function AgentChip({ name }: { name: string }) {
  return (
    <span className="inline-flex items-center rounded-full bg-accent/15 px-3 py-1 text-sm font-medium text-accent">
      {name}
    </span>
  );
}

export function PlanConfirmCard({
  plan,
  clarification,
  onConfirm,
  onCancel,
  onClarify,
}: PlanConfirmCardProps) {
  const planId = plan.plan_id ?? "";

  // Collect unique target agents from routes.
  const agentSet = new Set<string>();
  for (const r of plan.routes ?? []) {
    for (const a of r.target_agents ?? []) agentSet.add(a);
  }
  const agents = Array.from(agentSet);

  // The human-readable summary is the primary hero text; fall back to summary.
  const heroText = plan.human_readable || plan.summary;

  // Clarification options can be either strings or {value, label} objects.
  function labelOf(opt: { value: string; label: string } | string): string {
    return typeof opt === "string" ? opt : opt.label;
  }
  function valueOf(opt: { value: string; label: string } | string): string {
    return typeof opt === "string" ? opt : opt.value;
  }

  return (
    <div className="rounded-2xl border-2 border-accent/30 bg-surface shadow-md overflow-hidden">
      {/* Coloured header band */}
      <div className="flex items-center gap-3 bg-accent/10 px-5 py-3">
        {clarification ? (
          <HelpCircle size={22} className="shrink-0 text-accent" />
        ) : (
          <ClipboardList size={22} className="shrink-0 text-accent" />
        )}
        <span className="flex-1 text-sm font-bold uppercase tracking-wide text-accent">
          {clarification ? "Roba Needs to Know" : "Roba's Plan"}
        </span>
        <button
          onClick={() => onCancel(planId)}
          className="rounded-full p-1 text-accent/50 hover:bg-accent/20 hover:text-accent transition-colors"
          aria-label="Dismiss"
        >
          <X size={16} />
        </button>
      </div>

      <div className="px-5 py-4 space-y-4">
        {/* Hero summary — large readable text */}
        {heroText && (
          <p className="text-lg font-semibold text-text leading-snug">{heroText}</p>
        )}

        {/* Target agents */}
        {agents.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {agents.map((a) => (
              <AgentChip key={a} name={a} />
            ))}
          </div>
        )}

        {/* Route breakdown — "What happens" list */}
        {(plan.routes ?? []).length > 0 && !clarification && (
          <div className="space-y-1.5">
            <p className="text-xs font-semibold uppercase tracking-wide text-text/40">
              What happens
            </p>
            <ul className="space-y-1.5">
              {(plan.routes ?? []).map((r, i) => (
                <li key={i} className="flex items-start gap-2 text-sm text-text/70">
                  <ChevronRight size={14} className="mt-0.5 shrink-0 text-accent/60" />
                  <span>{r.summary}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Clarification options */}
        {clarification && (
          <div className="space-y-3">
            <p className="text-base font-semibold text-text leading-snug">
              {clarification.question}
            </p>
            <div className="flex flex-col gap-2">
              {(clarification.options ?? []).map((opt, i) => (
                <button
                  key={i}
                  onClick={() => onClarify?.(planId, valueOf(opt))}
                  className="w-full rounded-xl border-2 border-muted bg-surface px-4 py-3 text-left text-base font-medium text-text hover:border-accent/50 hover:bg-accent/5 active:scale-[0.98] transition-all"
                >
                  {labelOf(opt)}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Confirm / Cancel actions */}
        {!clarification && (
          <div className="flex gap-3 pt-1">
            <button
              onClick={() => onConfirm(planId)}
              disabled={!planId}
              className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-accent px-4 py-3.5 text-base font-semibold text-white shadow hover:bg-accent/90 active:scale-[0.98] transition-all disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Check size={18} />
              Confirm
            </button>
            <button
              onClick={() => onCancel(planId)}
              className="flex items-center gap-2 rounded-xl border-2 border-muted bg-surface px-4 py-3.5 text-base font-medium text-text/70 hover:bg-muted/50 active:scale-[0.98] transition-all"
            >
              <X size={18} />
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
