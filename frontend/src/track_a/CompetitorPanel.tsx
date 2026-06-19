import { PhoneCall, RefreshCw } from "lucide-react";
import { apiPost } from "../api";
import { formatSimTime } from "./helpers";
import { EmptyState, Pill, TrackAShell } from "./ui";
import { useTrackAData } from "./useTrackAData";

export function CompetitorPanel() {
  const { data, loading, error, refresh } = useTrackAData();

  async function research(competitorId: number) {
    await apiPost(`/api/track-a/competitors/${competitorId}/research`);
    await refresh();
  }

  if (loading) return <TrackAShell eyebrow="Track A" title="Competitors"><EmptyState label="Loading competitors" /></TrackAShell>;
  if (error || !data) return <TrackAShell eyebrow="Track A" title="Competitors"><EmptyState label={error ?? "Competitor data unavailable"} /></TrackAShell>;

  return (
    <TrackAShell
      eyebrow="Sensing"
      title="Competitor research"
      action={
        <button type="button" onClick={refresh} className="inline-flex items-center gap-2 rounded-md border border-muted px-3 py-2 text-sm text-text/80 hover:bg-muted/50">
          <RefreshCw size={16} />
          Refresh
        </button>
      }
    >
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="grid gap-3 md:grid-cols-2">
          {data.competitors.map((competitor) => {
            const offers = data.competitor_offers.filter((offer) => offer.competitor_id === competitor.id);
            return (
              <article key={competitor.id} className="rounded-md border border-muted bg-primary/30 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="text-lg font-semibold">{competitor.name}</h3>
                    <div className="mt-1 text-sm text-text/60">
                      {competitor.distance_km} km · {competitor.rating} stars · {competitor.platform}
                    </div>
                  </div>
                  <Pill tone={competitor.is_open ? "good" : "bad"}>{competitor.is_open ? "Open" : "Closed"}</Pill>
                </div>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {(competitor.cuisine ?? []).map((tag) => <Pill key={tag}>{tag}</Pill>)}
                  <Pill tone="accent">{competitor.price_tier}</Pill>
                </div>
                <div className="mt-4 space-y-2">
                  {offers.map((offer) => (
                    <div key={offer.id} className="flex items-center justify-between gap-2 rounded bg-surface/70 px-3 py-2 text-sm">
                      <span>{offer.dish_or_combo}</span>
                      <span className="font-semibold text-warning">${offer.price}</span>
                    </div>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => research(competitor.id)}
                  className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent/90"
                >
                  <PhoneCall size={16} />
                  Research
                </button>
              </article>
            );
          })}
        </div>

        <aside className="rounded-md border border-muted bg-primary/30 p-4">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-text/50">Intel results</h3>
          <div className="mt-3 space-y-3">
            {data.competitor_intel.length === 0 ? (
              <EmptyState label="No call intel yet. Research creates an approval first." />
            ) : (
              data.competitor_intel.map((intel) => {
                const competitor = data.competitors.find((row) => row.id === intel.competitor_id);
                return (
                  <div key={intel.id} className="rounded-md border border-muted bg-surface/70 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-medium">{competitor?.name ?? `Competitor ${intel.competitor_id}`}</div>
                      <Pill>{intel.method}</Pill>
                    </div>
                    <div className="mt-2 text-sm text-text/65">{formatSimTime(intel.sim_time)}</div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {(intel.popular_dishes ?? []).map((dish) => <Pill key={dish} tone="good">{dish}</Pill>)}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </aside>
      </div>
    </TrackAShell>
  );
}
