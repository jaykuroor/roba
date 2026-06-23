"""Helpers for converting competitor observations into bus signals."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from .schemas import CompetitorObservationData


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def observation_state_hash(obs: CompetitorObservationData) -> str:
    return stable_hash(
        {
            "competitor_id": obs.competitor_id,
            "source_channel": obs.source_channel,
            "platform": obs.platform,
            "signal_kind": obs.signal_kind,
            "direction": obs.direction,
            "impact_score": round(float(obs.impact_score), 4),
            "confidence": round(float(obs.confidence), 4),
            "window": obs.window,
            "affected_menu_items": obs.affected_menu_items,
            "affected_categories": obs.affected_categories,
            "raw": obs.raw,
        }
    )


def payload_for_observation(obs: CompetitorObservationData) -> Dict[str, Any]:
    return {
        "signal_kind": obs.signal_kind,
        "source_channel": obs.source_channel,
        "platform": obs.platform,
        "competitor_id": obs.competitor_id,
        "affected_menu_items": list(obs.affected_menu_items),
        "affected_categories": list(obs.affected_categories),
        "direction": obs.direction,
        "impact_score": float(obs.impact_score),
        "confidence": float(obs.confidence),
        "window": dict(obs.window),
        "evidence": list(obs.evidence),
        "raw": dict(obs.raw),
    }


def dedup_key_for_observation(obs: CompetitorObservationData) -> str:
    state_hash = obs.state_hash or observation_state_hash(obs)
    window_start = int(float(obs.window.get("start", 0.0)))
    competitor = obs.competitor_id if obs.competitor_id is not None else "market"
    return (
        f"competitor-market:{competitor}:{obs.signal_kind}:"
        f"{window_start}:{state_hash}"
    )
