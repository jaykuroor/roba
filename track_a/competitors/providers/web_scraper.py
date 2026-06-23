"""Public-menu snapshot provider for the PoC.

This provider does not perform network access. It applies the same ethical gate
a real scraper would use, then derives a deterministic public menu snapshot from
seeded competitor offers.
"""

from __future__ import annotations

from typing import Iterable, List

from core.models import Competitor, MenuItem

from ..ethics import EthicsGate
from ..normalizer import menu_delta_observations
from ..schemas import MenuSnapshotData
from ..signal_engine import stable_hash


class PublicMenuSnapshotProvider:
    platform = "public_web"

    def __init__(self, ethics_gate: EthicsGate | None = None):
        self.ethics_gate = ethics_gate or EthicsGate()

    def snapshot(
        self,
        competitor: Competitor,
        menu_items: Iterable[MenuItem],
        previous_items: List[dict],
        now: float,
        window: dict,
    ) -> MenuSnapshotData:
        url = f"https://example.local/competitors/{competitor.id}"
        allowed, compliance = self.ethics_gate.check_url(url, now)
        current_items = _synthetic_public_menu(competitor, previous_items, now) if allowed else list(previous_items)
        menu_hash = stable_hash(current_items)
        observations = (
            menu_delta_observations(
                competitor,
                previous_items,
                current_items,
                menu_items,
                self.platform,
                now,
                window,
            )
            if allowed
            else []
        )
        return MenuSnapshotData(
            competitor_id=int(competitor.id),
            source_channel="web",
            platform=self.platform,
            menu_hash=menu_hash,
            items=current_items,
            compliance=compliance,
            fetched_at=now,
            observations=observations,
        )


def _synthetic_public_menu(competitor: Competitor, previous_items: List[dict], now: float) -> List[dict]:
    items = [dict(row) for row in previous_items]
    if not items:
        items = [
            {
                "name": f"{competitor.name} Signature",
                "price": 12.0 + float(competitor.id or 0),
                "description": "Public menu placeholder from seeded watchlist.",
            }
        ]

    slot = int(now // 7200)
    if items and slot % 5 == int(competitor.id or 1) % 5:
        items[0]["price"] = round(float(items[0].get("price") or 10.0) * 1.08, 2)
    if slot % 7 == int(competitor.id or 1) % 7:
        items.append(
            {
                "name": f"Limited Time {competitor.name} Combo",
                "price": 15.0 + float(competitor.id or 0),
                "description": "Seasonal public promo bundle.",
            }
        )
    return items
