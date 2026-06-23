"""Normalize provider output and menu/probe deltas into actionable signals."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from core.models import Competitor, MenuItem

from .schemas import CompetitorObservationData
from .signal_engine import observation_state_hash


POSITIVE_DIRECTIONS = {"opportunity"}
NEGATIVE_DIRECTIONS = {"threat", "drag"}


def normalize_raw_observation(raw: Dict[str, Any], window: Dict[str, float]) -> CompetitorObservationData:
    obs = CompetitorObservationData(
        competitor_id=raw.get("competitor_id"),
        source_channel=str(raw.get("source_channel") or "aggregator"),
        platform=str(raw.get("platform") or "unknown"),
        signal_kind=str(raw.get("signal_kind") or "watch"),
        direction=str(raw.get("direction") or "watch"),
        impact_score=float(raw.get("impact_score") or 0.0),
        confidence=float(raw.get("confidence") or 0.7),
        affected_menu_items=[int(v) for v in (raw.get("affected_menu_items") or [])],
        affected_categories=[str(v).lower() for v in (raw.get("affected_categories") or [])],
        window=dict(raw.get("window") or window),
        evidence=[str(v) for v in (raw.get("evidence") or [])],
        raw=dict(raw),
    )
    return _with_hash(obs)


def category_matches_offer(item: MenuItem, offer_name: str, offer_desc: str = "") -> bool:
    haystack = f"{offer_name} {offer_desc}".lower()
    category = str(item.category or "").lower()
    name = str(item.name or "").lower()
    return bool(
        (category and category in haystack)
        or (name and (name in haystack or haystack in name))
    )


def menu_delta_observations(
    competitor: Competitor,
    previous_items: Iterable[Dict[str, Any]],
    current_items: Iterable[Dict[str, Any]],
    menu_items: Iterable[MenuItem],
    platform: str,
    now: float,
    window: Dict[str, float],
) -> List[CompetitorObservationData]:
    previous = {str(row.get("name") or "").lower(): row for row in previous_items}
    current = {str(row.get("name") or "").lower(): row for row in current_items}
    observations: List[CompetitorObservationData] = []
    menu_items = list(menu_items)

    for key, row in current.items():
        old = previous.get(key)
        name = str(row.get("name") or key)
        price = _float_or_none(row.get("price"))
        old_price = _float_or_none(old.get("price")) if old else None
        categories = _matched_categories(menu_items, name, str(row.get("description") or ""))
        affected = _matched_item_ids(menu_items, name, str(row.get("description") or ""))
        if old is None:
            observations.append(
                _with_hash(
                    CompetitorObservationData(
                        competitor_id=int(competitor.id),
                        source_channel="web",
                        platform=platform,
                        signal_kind="menu_item_added",
                        direction="threat",
                        impact_score=0.06,
                        confidence=0.72,
                        affected_menu_items=affected,
                        affected_categories=categories,
                        window=window,
                        evidence=[f"{competitor.name} added {name}"],
                        raw={"name": name, "price": price},
                    )
                )
            )
        elif price is not None and old_price is not None and old_price > 0:
            change_pct = (price - old_price) / old_price
            if abs(change_pct) >= 0.05:
                price_hike = change_pct > 0
                observations.append(
                    _with_hash(
                        CompetitorObservationData(
                            competitor_id=int(competitor.id),
                            source_channel="web",
                            platform=platform,
                            signal_kind="price_hike" if price_hike else "price_drop",
                            direction="opportunity" if price_hike else "threat",
                            impact_score=min(0.18, max(0.04, abs(change_pct))),
                            confidence=0.78,
                            affected_menu_items=affected,
                            affected_categories=categories,
                            window=window,
                            evidence=[
                                f"{competitor.name} changed {name} from {old_price:.2f} to {price:.2f}"
                            ],
                            raw={"name": name, "old_price": old_price, "new_price": price},
                        )
                    )
                )

    for key, row in previous.items():
        if key in current:
            continue
        name = str(row.get("name") or key)
        categories = _matched_categories(menu_items, name, str(row.get("description") or ""))
        observations.append(
            _with_hash(
                CompetitorObservationData(
                    competitor_id=int(competitor.id),
                    source_channel="web",
                    platform=platform,
                    signal_kind="menu_item_removed",
                    direction="opportunity",
                    impact_score=0.10,
                    confidence=0.75,
                    affected_menu_items=_matched_item_ids(menu_items, name, str(row.get("description") or "")),
                    affected_categories=categories,
                    window=window,
                    evidence=[f"{competitor.name} removed or hid {name}"],
                    raw={"name": name},
                )
            )
        )
    return observations


def probe_observations(
    competitor: Competitor,
    wait_min: float,
    labels: List[str],
    platform: str,
    window: Dict[str, float],
) -> List[CompetitorObservationData]:
    direction = "opportunity" if wait_min >= 25 or "capacity_throttled" in labels else "watch"
    impact = 0.12 if wait_min >= 35 else 0.08 if wait_min >= 25 else 0.02
    return [
        _with_hash(
            CompetitorObservationData(
                competitor_id=int(competitor.id),
                source_channel="probe",
                platform=platform,
                signal_kind="probe_wait_time_spike" if wait_min >= 25 else "probe_wait_time_normal",
                direction=direction,
                impact_score=impact,
                confidence=0.74,
                affected_categories=[str(c).lower() for c in (competitor.cuisine or [])],
                window=window,
                evidence=[f"{competitor.name} quoted approximately {wait_min:.0f} minutes"],
                raw={"estimated_wait_min": wait_min, "tactic_labels": labels},
            )
        )
    ]


def _matched_categories(menu_items: Iterable[MenuItem], name: str, desc: str = "") -> List[str]:
    categories = {
        str(item.category or "").lower()
        for item in menu_items
        if item.category and category_matches_offer(item, name, desc)
    }
    return sorted(categories)


def _matched_item_ids(menu_items: Iterable[MenuItem], name: str, desc: str = "") -> List[int]:
    return [
        int(item.id)
        for item in menu_items
        if category_matches_offer(item, name, desc)
    ]


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _with_hash(obs: CompetitorObservationData) -> CompetitorObservationData:
    return CompetitorObservationData(
        competitor_id=obs.competitor_id,
        source_channel=obs.source_channel,
        platform=obs.platform,
        signal_kind=obs.signal_kind,
        direction=obs.direction,
        impact_score=obs.impact_score,
        confidence=obs.confidence,
        affected_menu_items=list(obs.affected_menu_items),
        affected_categories=list(obs.affected_categories),
        window=dict(obs.window),
        evidence=list(obs.evidence),
        raw=dict(obs.raw),
        state_hash=obs.state_hash or observation_state_hash(obs),
    )
