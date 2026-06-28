/**
 * PlanConfirmCard — surfaces the pending Roba plan (or a clarification
 * question) before the user commits.
 *
 * Shows:
 *  • A human-readable summary of what will happen ("Roba will boost demand
 *    forecast for dinner service by 30%").
 *  • Target agents that will be triggered.
 *  • Confirm / Cancel buttons.
 *  • If `clarification` is present, renders options instead of confirm/cancel
 *    and calls onClarify(answer) so the voice planner can re-plan.
 */

import { Check, X, ChevronRight } from "lucide-react";
import type { PlanResult, Clarification } from "./RobaLiveClient";

interface PlanConfirmCardProps {
  plan: PlanResult;
  clarification?: Clarification | null;
  onConfirm: (planId: string) => void;
  onCancel: (planId: string) => void;
  onClarify?: (planId: string, answer: string) => void;
}

function AgentPill({ name }: { name: string }) {
  return (
    <span className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-text/70">
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

  // Clarification options can be either strings or {value, label} objects.
  function labelOf(opt: { value: string; label: string } | string): string {
    return typeof opt === "string" ? opt : opt.label;
  }
  function valueOf(opt: { value: string; label: string } | string): string {
    return typeof opt === "string" ? opt : opt.value;
  }

  return (
    <div className="rounded-xl border border-muted/60 bg-surface p-4 shadow-sm">
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-text/40">
          {clarification ? "Roba needs to know" : "Roba's plan"}
        </span>
        <button
          onClick={() => onCancel(planId)}
          className="rounded p-0.5 text-text/30 hover:bg-muted/50 hover:text-text/60"
          aria-label="Cancel plan"
        >
          <X size={14} />
        </button>
      </div>

      {/* Summary */}
      {plan.human_readable && (
        <p className="mt-2 text-sm text-text leading-snug">{plan.human_readable}</p>
      )}

      {/* Target agents */}
      {agents.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {agents.map((a) => (
            <AgentPill key={a} name={a} />
          ))}
        </div>
      )}

      {/* Route breakdown */}
      {(plan.routes ?? []).length > 0 && !clarification && (
        <ul className="mt-2 space-y-1">
          {(plan.routes ?? []).map((r, i) => (
            <li key={i} className="flex items-start gap-1.5 text-xs text-text/60">
              <ChevronRight size={12} className="mt-0.5 shrink-0 text-text/30" />
              <span>{r.summary}</span>
            </li>
          ))}
        </ul>
      )}

      {/* Clarification options */}
      {clarification && (
        <div className="mt-3 space-y-2">
          <p className="text-sm font-medium text-text">{clarification.question}</p>
          <div className="flex flex-col gap-1.5">
            {(clarification.options ?? []).map((opt, i) => (
              <button
                key={i}
                onClick={() => onClarify?.(planId, valueOf(opt))}
                className="rounded-lg border border-muted bg-primary/60 px-3 py-1.5 text-left text-sm text-text hover:border-accent/50 hover:bg-muted/50 transition-colors"
              >
                {labelOf(opt)}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Actions */}
      {!clarification && (
        <div className="mt-4 flex gap-2">
          <button
            onClick={() => onConfirm(planId)}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/90 transition-colors"
          >
            <Check size={14} />
            Confirm
          </button>
          <button
            onClick={() => onCancel(planId)}
            className="flex items-center gap-1.5 rounded-lg border border-muted bg-surface px-3 py-2 text-sm text-text/70 hover:bg-muted/50 transition-colors"
          >
            <X size={14} />
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
