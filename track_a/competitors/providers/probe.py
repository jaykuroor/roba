"""Simulation-only behavioral probing provider."""

from __future__ import annotations

from core.models import Competitor

from ..normalizer import probe_observations
from ..schemas import ProbeResultData


class SimulatedProbeProvider:
    platform = "simulation_probe"

    def probe(self, competitor: Competitor, now: float, window: dict) -> ProbeResultData:
        base_wait = 12.0 + float(competitor.distance_km or 1.0) * 4.0
        if not bool(competitor.is_open):
            availability = "offline"
            wait = 0.0
            labels = ["closed", "capacity_unavailable"]
        else:
            slot_pressure = (int(now // 1800) + int(competitor.id or 0)) % 5
            wait = base_wait + slot_pressure * 6.0
            availability = "accepting_orders"
            labels = []
            if wait >= 25:
                labels.append("capacity_throttled")
            if slot_pressure == 3:
                labels.append("promo_hint")
        transcript = [
            {
                "role": "agent",
                "text": "Hi, roughly how long would an order take right now?",
                "sim_ts": now,
            },
            {
                "role": "counterparty",
                "text": f"We are at about {wait:.0f} minutes right now.",
                "sim_ts": now,
            },
        ]
        observations = probe_observations(competitor, wait, labels, self.platform, window)
        return ProbeResultData(
            competitor_id=int(competitor.id),
            source_channel="probe",
            platform=self.platform,
            estimated_wait_min=wait,
            availability=availability,
            tactic_labels=labels,
            confidence=0.76,
            transcript=transcript,
            raw={"simulated": True, "distance_km": competitor.distance_km},
            sim_time=now,
            observations=observations,
        )
