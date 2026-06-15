import type { ReactNode } from "react";

export function TrackAShell({
  title,
  eyebrow,
  children,
  action,
}: {
  title: string;
  eyebrow: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="min-h-[60vh] rounded-lg border border-muted/70 bg-surface p-4 shadow-2xl shadow-black/20">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3 border-b border-muted pb-3">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-accent">
            {eyebrow}
          </div>
          <h2 className="mt-1 text-2xl font-semibold tracking-normal text-text">
            {title}
          </h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function EmptyState({ label }: { label: string }) {
  return (
    <div className="flex min-h-48 items-center justify-center rounded-md border border-dashed border-muted bg-primary/25 px-4 text-sm text-text/50">
      {label}
    </div>
  );
}

export function Pill({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "good" | "warn" | "bad" | "accent";
  children: ReactNode;
}) {
  const className =
    tone === "good"
      ? "border-success/50 bg-success/15 text-success"
      : tone === "warn"
        ? "border-warning/50 bg-warning/15 text-warning"
        : tone === "bad"
          ? "border-danger/50 bg-danger/15 text-danger"
          : tone === "accent"
            ? "border-accent/50 bg-accent/15 text-accent"
            : "border-muted bg-primary/40 text-text/70";
  return (
    <span className={`inline-flex items-center rounded px-2 py-1 text-xs font-medium ${className}`}>
      {children}
    </span>
  );
}

export function JsonPayload({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-md bg-primary/70 p-3 text-xs leading-5 text-text/75">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
