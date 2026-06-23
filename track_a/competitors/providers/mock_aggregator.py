"""Deterministic Swiggy/Zomato/UberEats-like aggregator feeds for the PoC."""

from __future__ import annotations

from typing import Iterable, List

from core.clock import SECONDS_PER_DAY
from core.models import Competitor, MenuItem

from ..normalizer import category_matches_offer, normalize_raw_observation
from ..schemas import CompetitorObservationData


class MockAggregatorProvider:
    """Generate repeatable market events from sim time and seeded competitors."""

    def __init__(self, platform: str):
        self.platform = platform

    def poll(
        self,
        competitors: Iterable[Competitor],
        menu_items: Iterable[MenuItem],
        now: float,
        window: dict,
    ) -> List[CompetitorObservationData]:
        competitors = [c for c in competitors if str(c.platform or "").lower() in {self.platform, "google", "yelp"}]
        menu_items = list(menu_items)
        observations: List[CompetitorObservationData] = []
        if not competitors:
            return observations

        slot = int((now % SECONDS_PER_DAY) // 1800)
        for competitor in competitors:
            categories = _competitor_categories(competitor, menu_items)
            affected = _affected_items(categories, menu_items)
            if not bool(competitor.is_open):
                observations.append(
                    normalize_raw_observation(
                        {
                            "competitor_id": competitor.id,
                            "source_channel": "aggregator",
                            "platform": self.platform,
                            "signal_kind": "competitor_offline",
                            "direction": "opportunity",
                            "impact_score": 0.18,
                            "confidence": 0.85,
                            "affected_menu_items": affected,
                            "affected_categories": categories,
                            "evidence": [f"{competitor.name} is unavailable on {self.platform}"],
                            "raw": {"is_open": bool(competitor.is_open)},
                        },
                        window,
                    )
                )

            cadence = (int(competitor.id or 1) + len(self.platform)) % 6
            if slot % 6 == cadence:
                observations.append(
                    normalize_raw_observation(
                        {
                            "competitor_id": competitor.id,
                            "source_channel": "aggregator",
                            "platform": self.platform,
                            "signal_kind": "eta_spike",
                            "direction": "opportunity",
                            "impact_score": 0.11,
                            "confidence": 0.78,
                            "affected_menu_items": affected,
                            "affected_categories": categories,
                            "evidence": [f"{self.platform} shows longer prep/ETA for {competitor.name}"],
                            "raw": {"eta_delta_min": 18, "slot": slot},
                        },
                        window,
                    )
                )
            elif slot % 8 == cadence:
                observations.append(
                    normalize_raw_observation(
                        {
                            "competitor_id": competitor.id,
                            "source_channel": "aggregator",
                            "platform": self.platform,
                            "signal_kind": "promo_started",
                            "direction": "threat",
                            "impact_score": 0.14,
                            "confidence": 0.76,
                            "affected_menu_items": affected,
                            "affected_categories": categories,
                            "evidence": [f"{competitor.name} is running a visible {self.platform} promo"],
                            "raw": {"discount_pct": 20, "slot": slot},
                        },
                        window,
                    )
                )
            elif slot % 10 == cadence:
                observations.append(
                    normalize_raw_observation(
                        {
                            "competitor_id": competitor.id,
                            "source_channel": "aggregator",
                            "platform": self.platform,
                            "signal_kind": "item_sold_out",
                            "direction": "opportunity",
                            "impact_score": 0.13,
                            "confidence": 0.73,
                            "affected_menu_items": affected,
                            "affected_categories": categories,
                            "evidence": [f"A key {competitor.name} item appears unavailable"],
                            "raw": {"slot": slot},
                        },
                        window,
                    )
                )

        if slot % 12 == len(self.platform) % 12:
            observations.append(
                normalize_raw_observation(
                    {
                        "competitor_id": None,
                        "source_channel": "aggregator",
                        "platform": self.platform,
                        "signal_kind": "regional_driver_shortage",
                        "direction": "drag",
                        "impact_score": 0.05,
                        "confidence": 0.70,
                        "affected_menu_items": [],
                        "affected_categories": sorted({str(i.category or "").lower() for i in menu_items if i.category}),
                        "evidence": [f"{self.platform} reports constrained delivery capacity nearby"],
                        "raw": {"slot": slot, "market_wide": True},
                    },
                    window,
                )
            )
        return observations


def _competitor_categories(competitor: Competitor, menu_items: Iterable[MenuItem]) -> List[str]:
    cuisine = {str(c).lower() for c in (competitor.cuisine or [])}
    categories = {str(item.category or "").lower() for item in menu_items if str(item.category or "").lower() in cuisine}
    if not categories:
        categories = cuisine
    return sorted(c for c in categories if c)


def _affected_items(categories: List[str], menu_items: Iterable[MenuItem]) -> List[int]:
    affected = []
    for item in menu_items:
        if str(item.category or "").lower() in set(categories):
            affected.append(int(item.id))
        elif any(category_matches_offer(item, category) for category in categories):
            affected.append(int(item.id))
    return affected
