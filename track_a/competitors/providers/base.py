"""Provider protocols for competitor intelligence sources."""

from __future__ import annotations

from typing import Iterable, Protocol

from core.models import Competitor, MenuItem

from ..schemas import CompetitorObservationData, MenuSnapshotData, ProbeResultData


class AggregatorProvider(Protocol):
    platform: str

    def poll(
        self,
        competitors: Iterable[Competitor],
        menu_items: Iterable[MenuItem],
        now: float,
        window: dict,
    ) -> list[CompetitorObservationData]:
        ...


class MenuSnapshotProvider(Protocol):
    platform: str

    def snapshot(
        self,
        competitor: Competitor,
        menu_items: Iterable[MenuItem],
        previous_items: list[dict],
        now: float,
        window: dict,
    ) -> MenuSnapshotData:
        ...


class ProbeProvider(Protocol):
    platform: str

    def probe(
        self,
        competitor: Competitor,
        now: float,
        window: dict,
    ) -> ProbeResultData:
        ...
