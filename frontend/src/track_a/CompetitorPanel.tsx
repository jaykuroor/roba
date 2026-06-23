import { Activity, FileSearch, PhoneCall, Radar, RefreshCw } from "lucide-react";
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

  async function pollAggregators() {
    await apiPost("/api/track-a/competitors/poll-aggregators");
    await refresh();
  }

  async function refreshMenu(competitorId: number) {
    await apiPost(`/api/track-a/competitors/${competitorId}/refresh-menu`);
    await refresh();
  }

  async function runProbe(competitorId: number) {
    await apiPost(`/api/track-a/competitors/${competitorId}/probe`);
    await refresh();
  }

  if (loading) return <TrackAShell eyebrow="Track A" title="Competitors"><EmptyState label="Loading competitors" /></TrackAShell>;
  if (error || !data) return <TrackAShell eyebrow="Track A" title="Competitors"><EmptyState label={error ?? "Competitor data unavailable"} /></TrackAShell>;

  return (
    <TrackAShell
      eyebrow="Sensing"
      title="Competitor research"
      action={
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={pollAggregators} className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent/90">
            <Radar size={16} />
            Poll aggregators
          </button>
          <button type="button" onClick={refresh} className="inline-flex items-center gap-2 rounded-md border border-muted px-3 py-2 text-sm text-text/80 hover:bg-muted/50">
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
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
                <div className="mt-4 grid gap-2 sm:grid-cols-3">
                  <button
                    type="button"
                    onClick={() => research(competitor.id)}
                    className="inline-flex items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent/90"
                  >
                    <PhoneCall size={16} />
                    Research
                  </button>
                  <button
                    type="button"
                    onClick={() => refreshMenu(competitor.id)}
                    className="inline-flex items-center justify-center gap-2 rounded-md border border-muted px-3 py-2 text-sm font-semibold text-text/80 hover:bg-muted/50"
                  >
                    <FileSearch size={16} />
                    Menu
                  </button>
                  <button
                    type="button"
                    onClick={() => runProbe(competitor.id)}
                    className="inline-flex items-center justify-center gap-2 rounded-md border border-warning/50 px-3 py-2 text-sm font-semibold text-warning hover:bg-warning/10"
                  >
                    <Activity size={16} />
                    Probe
                  </button>
                </div>
              </article>
            );
          })}
        </div>

        <aside className="space-y-4">
          <section className="rounded-md border border-muted bg-primary/30 p-4">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-text/50">Market signals</h3>
          <div className="mt-3 space-y-3">
            {data.competitor_observations.length === 0 ? (
              <EmptyState label="No market signals yet. Poll aggregators to simulate live market sensing." />
            ) : (
              data.competitor_observations.slice(0, 8).map((observation) => {
                const competitor = data.competitors.find((row) => row.id === observation.competitor_id);
                return (
                  <div key={observation.id} className="rounded-md border border-muted bg-surface/70 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="font-medium">{observation.signal_kind.replaceAll("_", " ")}</div>
                      <Pill tone={observation.direction === "opportunity" ? "good" : observation.direction === "threat" || observation.direction === "drag" ? "warn" : "neutral"}>
                        {observation.direction}
                      </Pill>
                    </div>
                    <div className="mt-1 text-sm text-text/60">
                      {competitor?.name ?? "Regional market"} · {observation.platform} · {formatSimTime(observation.sim_time)}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {(observation.affected_categories ?? []).map((category) => <Pill key={category}>{category}</Pill>)}
                      <Pill tone="accent">{Math.round(observation.impact_score * 100)} impact</Pill>
                      <Pill>{Math.round(observation.confidence * 100)}% confidence</Pill>
                    </div>
                    {observation.evidence?.[0] ? <p className="mt-2 text-sm text-text/70">{observation.evidence[0]}</p> : null}
                  </div>
                );
              })
            )}
          </div>
          </section>

          <section className="rounded-md border border-muted bg-primary/30 p-4">
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
          </section>

          <section className="rounded-md border border-muted bg-primary/30 p-4">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-text/50">Probe and ethics log</h3>
            <div className="mt-3 space-y-3">
              {data.competitor_probe_results.slice(0, 4).map((probe) => {
                const competitor = data.competitors.find((row) => row.id === probe.competitor_id);
                return (
                  <div key={probe.id} className="rounded-md border border-muted bg-surface/70 p-3 text-sm">
                    <div className="font-medium">{competitor?.name ?? `Competitor ${probe.competitor_id}`}</div>
                    <div className="mt-1 text-text/65">{probe.availability} · {Math.round(probe.estimated_wait_min)} min · {formatSimTime(probe.sim_time)}</div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {(probe.tactic_labels ?? []).map((label) => <Pill key={label} tone="warn">{label}</Pill>)}
                    </div>
                  </div>
                );
              })}
              {data.competitor_menu_snapshots.slice(0, 3).map((snapshot) => {
                const competitor = data.competitors.find((row) => row.id === snapshot.competitor_id);
                return (
                  <div key={`snapshot-${snapshot.id}`} className="rounded-md border border-muted bg-surface/70 p-3 text-sm">
                    <div className="font-medium">{competitor?.name ?? `Competitor ${snapshot.competitor_id}`} menu</div>
                    <div className="mt-1 text-text/65">{snapshot.platform} · {formatSimTime(snapshot.fetched_at)}</div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      <Pill tone={snapshot.compliance?.robots_allowed === false ? "bad" : "good"}>
                        {snapshot.compliance?.robots_allowed === false ? "blocked" : "public data"}
                      </Pill>
                      <Pill>{String(snapshot.items?.length ?? 0)} items</Pill>
                    </div>
                  </div>
                );
              })}
              {data.competitor_probe_results.length === 0 && data.competitor_menu_snapshots.length === 0 ? (
                <EmptyState label="No probes or menu refreshes yet." />
              ) : null}
            </div>
          </section>
        </aside>
      </div>
    </TrackAShell>
  );
}
