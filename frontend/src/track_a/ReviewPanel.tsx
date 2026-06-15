import { RefreshCw } from "lucide-react";
import { apiPost } from "../api";
import { formatSimTime } from "./helpers";
import { EmptyState, Pill, TrackAShell } from "./ui";
import { useTrackAData } from "./useTrackAData";

export function ReviewPanel() {
  const { data, loading, error, refresh } = useTrackAData();

  async function processReviews() {
    await apiPost("/api/track-a/reviews/process");
    await refresh();
  }

  if (loading) return <TrackAShell eyebrow="Track A" title="Reviews"><EmptyState label="Loading reviews" /></TrackAShell>;
  if (error || !data) return <TrackAShell eyebrow="Track A" title="Reviews"><EmptyState label={error ?? "Review data unavailable"} /></TrackAShell>;

  return (
    <TrackAShell
      eyebrow="Guest sensing"
      title="Review insights"
      action={
        <button type="button" onClick={processReviews} className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent/90">
          <RefreshCw size={16} />
          Process reviews
        </button>
      }
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="space-y-3">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-text/50">Review stream</h3>
          {data.reviews.map((review) => (
            <article key={review.id} className="rounded-md border border-muted bg-primary/30 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="font-medium">{review.source}</div>
                <Pill tone={review.rating <= 2 ? "bad" : review.rating >= 4 ? "good" : "warn"}>{review.rating} stars</Pill>
              </div>
              <p className="mt-2 text-sm leading-6 text-text/75">{review.text}</p>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {(review.dish_mentions ?? []).map((mention) => <Pill key={mention}>{mention}</Pill>)}
                <Pill>{review.processed ? "processed" : "pending"}</Pill>
              </div>
            </article>
          ))}
        </section>
        <section className="space-y-3">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-text/50">Suggested actions</h3>
          {data.review_insights.length === 0 ? (
            <EmptyState label="Insights appear after reviews are processed." />
          ) : (
            data.review_insights.map((insight) => (
              <article key={insight.id} className="rounded-md border border-muted bg-primary/30 p-3">
                <div className="flex items-center justify-between gap-2">
                  <Pill tone={insight.severity === "high" ? "bad" : insight.severity === "medium" ? "warn" : "good"}>
                    {insight.severity}
                  </Pill>
                  <span className="text-xs text-text/45">{formatSimTime(insight.sim_time)}</span>
                </div>
                <p className="mt-3 text-sm leading-6 text-text/80">{insight.summary}</p>
                <div className="mt-3 rounded bg-surface/70 p-3 text-sm text-text/70">
                  {insight.suggested_action}
                </div>
              </article>
            ))
          )}
        </section>
      </div>
    </TrackAShell>
  );
}
