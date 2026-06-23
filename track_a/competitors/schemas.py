"""Internal structs for competitor intelligence observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class CompetitorObservationData:
    competitor_id: Optional[int]
    source_channel: str
    platform: str
    signal_kind: str
    direction: str
    impact_score: float
    confidence: float
    window: Dict[str, float]
    affected_menu_items: List[int] = field(default_factory=list)
    affected_categories: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    state_hash: str = ""


@dataclass(frozen=True)
class MenuSnapshotData:
    competitor_id: int
    source_channel: str
    platform: str
    menu_hash: str
    items: List[Dict[str, Any]]
    compliance: Dict[str, Any]
    fetched_at: float
    observations: List[CompetitorObservationData] = field(default_factory=list)


@dataclass(frozen=True)
class ProbeResultData:
    competitor_id: int
    source_channel: str
    platform: str
    estimated_wait_min: float
    availability: str
    tactic_labels: List[str]
    confidence: float
    transcript: List[Dict[str, Any]]
    raw: Dict[str, Any]
    sim_time: float
    observations: List[CompetitorObservationData] = field(default_factory=list)
