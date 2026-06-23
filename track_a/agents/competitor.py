"""Track A competitor intelligence agent."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from core import config
from core.agent_base import BaseAgent
from core.models import (
    Call,
    Competitor,
    CompetitorIntel,
    CompetitorMenuSnapshot,
    CompetitorObservation,
    CompetitorOffer,
    CompetitorProbeResult,
    MenuItem,
    Signal,
)
from core.signals import SignalType
from track_a.competitors.providers.mock_aggregator import MockAggregatorProvider
from track_a.competitors.providers.probe import SimulatedProbeProvider
from track_a.competitors.providers.web_scraper import PublicMenuSnapshotProvider
from track_a.competitors.schemas import (
    CompetitorObservationData,
    MenuSnapshotData,
    ProbeResultData,
)
from track_a.competitors.signal_engine import (
    dedup_key_for_observation,
    observation_state_hash,
    payload_for_observation,
)


class CompetitorAgent(BaseAgent):
    """Passive competitor sensing and approval-gated research calls."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        calls: Optional[Any] = None,
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        super().__init__(bus, db_session_factory, "track_a.competitor")
        self.calls = calls
        self.ws_broadcast = ws_broadcast
        self._last_offers: Dict[int, str] = {}
        self.aggregator_providers = [
            MockAggregatorProvider("swiggy"),
            MockAggregatorProvider("zomato"),
            MockAggregatorProvider("ubereats"),
        ]
        self.menu_provider = PublicMenuSnapshotProvider()
        self.probe_provider = SimulatedProbeProvider()
        self.subscribe(["sensing", "forecasting"])

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            self.passive_monitor,
            interval_sim_s=10800.0,
            name="track_a_competitor_monitor",
        )
        orchestrator.register(
            "interval",
            self.poll_aggregators,
            interval_sim_s=config.COMPETITOR_AGGREGATOR_POLL_SIM_S,
            name="track_a_competitor_aggregators",
        )
        orchestrator.register(
            "interval",
            self.refresh_all_menus,
            interval_sim_s=config.COMPETITOR_MENU_REFRESH_SIM_S,
            name="track_a_competitor_menu_refresh",
        )

    def on_signal(self, signal: Signal) -> None:
        if signal.type == SignalType.CALL_OUTCOME.value:
            self.handle_call_outcome(signal)

    def passive_monitor(self) -> List[Dict[str, Any]]:
        updates: List[Dict[str, Any]] = []
        after_commit: List[tuple[str, Any]] = []
        session = self.db_session_factory()
        try:
            competitors = session.query(Competitor).order_by(Competitor.id.asc()).all()
            for competitor in competitors:
                offers = (
                    session.query(CompetitorOffer)
                    .filter(CompetitorOffer.competitor_id == competitor.id)
                    .order_by(CompetitorOffer.id.asc())
                    .all()
                )
                summary = ", ".join(o.dish_or_combo for o in offers[:3]) or "No tracked offers"
                serialized = self._serialize_offers(offers)
                previous = self._last_offers.get(int(competitor.id))
                offers_changed = previous is not None and serialized != previous
                self._last_offers[int(competitor.id)] = serialized
                payload = {
                    "competitor_id": competitor.id,
                    "is_open": bool(competitor.is_open),
                    "offers_changed": offers_changed,
                    "summary": summary,
                }
                after_commit.append(
                    (
                        "emit",
                        (
                            SignalType.COMPETITOR_UPDATE,
                            payload,
                            {"dedup_key": f"competitor:{competitor.id}"},
                        ),
                    )
                )
                updates.append(payload)
        finally:
            session.close()
        self._run_after_commit(after_commit)
        self._broadcast("competitor_update", {"updates": updates})
        return updates

    def poll_aggregators(self) -> List[Dict[str, Any]]:
        now = float(self.bus.sim_time)
        window = self._market_window(now, config.COMPETITOR_AGGREGATOR_POLL_SIM_S)
        session = self.db_session_factory()
        try:
            competitors = session.query(Competitor).order_by(Competitor.id.asc()).all()
            menu_items = session.query(MenuItem).filter(MenuItem.active == 1).order_by(MenuItem.id.asc()).all()
            observations: List[CompetitorObservationData] = []
            for provider in self.aggregator_providers:
                observations.extend(provider.poll(competitors, menu_items, now, window))
            persisted = self._persist_observations(session, observations, now)
            session.commit()
        finally:
            session.close()

        self._emit_market_observations(observations)
        if persisted:
            self.log_event(
                "competitor",
                f"Aggregator scan produced {len(persisted)} market signals",
                {"observations": persisted},
            )
        self._broadcast("competitor_observations", {"observations": persisted})
        return persisted

    def refresh_all_menus(self) -> List[Dict[str, Any]]:
        session = self.db_session_factory()
        try:
            ids = [row.id for row in session.query(Competitor).order_by(Competitor.id.asc()).all()]
        finally:
            session.close()
        results: List[Dict[str, Any]] = []
        for competitor_id in ids:
            results.append(self.refresh_menu(int(competitor_id)))
        return results

    def refresh_menu(self, competitor_id: int) -> Dict[str, Any]:
        now = float(self.bus.sim_time)
        window = self._market_window(now, config.COMPETITOR_MENU_REFRESH_SIM_S)
        observations: List[CompetitorObservationData] = []
        session = self.db_session_factory()
        try:
            competitor = session.get(Competitor, competitor_id)
            if competitor is None:
                raise ValueError(f"Competitor {competitor_id} not found")
            menu_items = session.query(MenuItem).filter(MenuItem.active == 1).order_by(MenuItem.id.asc()).all()
            previous_items = self._latest_menu_items(session, competitor_id)
            snapshot = self.menu_provider.snapshot(
                competitor,
                menu_items,
                previous_items,
                now,
                window,
            )
            observations = list(snapshot.observations)
            row = self._store_menu_snapshot(session, snapshot)
            self._sync_latest_offers(session, competitor_id, snapshot.items, now)
            persisted = self._persist_observations(session, observations, now)
            session.commit()
            result = self._snapshot_to_dict(row)
            result["observations"] = persisted
        finally:
            session.close()

        self._emit_market_observations(observations)
        self._broadcast("competitor_menu_snapshot", {"snapshot": result})
        self.log_event(
            "competitor",
            f"Refreshed public menu for competitor #{competitor_id}",
            result,
        )
        return result

    def run_probe(self, competitor_id: int) -> Dict[str, Any]:
        now = float(self.bus.sim_time)
        window = self._market_window(now, 3600.0)
        observations: List[CompetitorObservationData] = []
        session = self.db_session_factory()
        try:
            competitor = session.get(Competitor, competitor_id)
            if competitor is None:
                raise ValueError(f"Competitor {competitor_id} not found")
            result = self.probe_provider.probe(competitor, now, window)
            observations = list(result.observations)
            row = self._store_probe_result(session, result)
            persisted = self._persist_observations(session, observations, now)
            session.commit()
            output = self._probe_to_dict(row)
            output["observations"] = persisted
        finally:
            session.close()

        self._emit_market_observations(observations)
        self._broadcast("competitor_probe_result", {"probe": output})
        self.log_event(
            "competitor",
            f"Simulated probe for competitor #{competitor_id}: {output['estimated_wait_min']:.0f} min",
            output,
        )
        return output

    def discover_targets(self) -> List[Dict[str, Any]]:
        session = self.db_session_factory()
        try:
            candidates = []
            for competitor in session.query(Competitor).all():
                cuisines = [str(c).lower() for c in (competitor.cuisine or [])]
                if competitor.distance_km is None or float(competitor.distance_km) > config.COMPETITOR_RADIUS_KM:
                    continue
                if "italian" not in cuisines and cuisines:
                    continue
                proximity = 1.0 / max(float(competitor.distance_km or 0.1), 0.1)
                candidates.append((float(competitor.rating or 0.0) * proximity, competitor))
            candidates.sort(key=lambda pair: pair[0], reverse=True)
            return [self._competitor_to_dict(c) for _score, c in candidates[: config.COMPETITOR_CALL_TARGETS]]
        finally:
            session.close()

    def request_research(self, competitor_id: int) -> Dict[str, Any]:
        if self.calls is None:
            raise RuntimeError("Call subsystem is not wired")
        call = self.calls.request(
            agent="competitor_intel",
            counterparty_type="competitor",
            counterparty_id=competitor_id,
            purpose="ask favourite dish",
        )
        self.log_event(
            "competitor",
            f"Requested undercover customer call for competitor #{competitor_id}",
            {"call_id": call.id, "competitor_id": competitor_id},
        )
        return {"call_id": call.id, "status": call.status, "approval_id": call.approval_id}

    def handle_call_outcome(self, signal: Signal) -> Optional[CompetitorIntel]:
        payload = signal.payload or {}
        if payload.get("counterparty_type") != "competitor":
            return None
        call_id = payload.get("call_id")
        outcome = payload.get("outcome") or {}

        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is None or call.agent != "competitor_intel":
                return None
            popular = list(outcome.get("popular_dishes") or [])
            price_points = dict(outcome.get("price_points") or {})
            if not popular:
                fallback_offer = (
                    session.query(CompetitorOffer)
                    .filter(CompetitorOffer.competitor_id == call.counterparty_id)
                    .order_by(CompetitorOffer.id.asc())
                    .first()
                )
                if fallback_offer is not None:
                    popular = [fallback_offer.dish_or_combo]
                    price_points = {fallback_offer.dish_or_combo: fallback_offer.price}
            if not popular:
                return None
            row = CompetitorIntel(
                competitor_id=call.counterparty_id,
                method="call",
                popular_dishes=popular,
                price_points=price_points,
                notes="approval-gated customer-style research call",
                call_id=call.id,
                sim_time=float(self.bus.sim_time),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
        finally:
            session.close()

        self.emit(
            SignalType.COMPETITOR_INTEL,
            {
                "competitor_id": row.competitor_id,
                "popular_dishes": row.popular_dishes or [],
                "price_points": row.price_points or {},
                "method": "call",
                "call_id": row.call_id,
            },
            dedup_key=f"competitor-intel:{row.competitor_id}:{row.call_id}",
        )
        self.log_event(
            "competitor",
            f"Competitor #{row.competitor_id} favourite dishes: {', '.join(row.popular_dishes or [])}",
            self._intel_to_dict(row),
        )
        self._broadcast("competitor_intel", {"intel": self._intel_to_dict(row)})
        return row

    def map_popular_to_menu_item(self, popular_dish: str) -> Optional[int]:
        needle = popular_dish.lower()
        session = self.db_session_factory()
        try:
            for item in session.query(MenuItem).all():
                name = (item.name or "").lower()
                if needle in name or name in needle:
                    return item.id
        finally:
            session.close()
        return None

    @staticmethod
    def _serialize_offers(offers: List[CompetitorOffer]) -> str:
        parts = []
        for offer in offers:
            parts.append(
                "|".join(
                    [
                        str(offer.id),
                        str(offer.dish_or_combo or ""),
                        str(float(offer.price or 0.0)),
                        str(offer.description or ""),
                        str(float(offer.updated_at or 0.0)),
                    ]
                )
            )
        return "\n".join(parts)

    def _persist_observations(
        self,
        session: Any,
        observations: Iterable[CompetitorObservationData],
        now: float,
    ) -> List[Dict[str, Any]]:
        persisted: List[Dict[str, Any]] = []
        for obs in observations:
            state_hash = obs.state_hash or observation_state_hash(obs)
            row = CompetitorObservation(
                competitor_id=obs.competitor_id,
                source_channel=obs.source_channel,
                platform=obs.platform,
                signal_kind=obs.signal_kind,
                direction=obs.direction,
                impact_score=float(obs.impact_score),
                confidence=float(obs.confidence),
                affected_menu_items=list(obs.affected_menu_items),
                affected_categories=list(obs.affected_categories),
                window=dict(obs.window),
                evidence=list(obs.evidence),
                raw=dict(obs.raw),
                state_hash=state_hash,
                sim_time=now,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            persisted.append(self._observation_to_dict(row))
        return persisted

    def _emit_market_observations(self, observations: Iterable[CompetitorObservationData]) -> None:
        for obs in observations:
            ttl = max(float(obs.window.get("end", self.bus.sim_time)) - float(self.bus.sim_time), 1.0)
            priority = 3 if abs(float(obs.impact_score or 0.0)) >= 0.12 else 2
            self.emit(
                SignalType.COMPETITOR_MARKET_SIGNAL,
                payload_for_observation(obs),
                ttl=ttl,
                priority=priority,
                dedup_key=dedup_key_for_observation(obs),
            )

    @staticmethod
    def _market_window(now: float, duration: float) -> Dict[str, float]:
        return {"start": float(now), "end": float(now + duration)}

    def _latest_menu_items(self, session: Any, competitor_id: int) -> List[Dict[str, Any]]:
        snapshot = (
            session.query(CompetitorMenuSnapshot)
            .filter(CompetitorMenuSnapshot.competitor_id == competitor_id)
            .order_by(CompetitorMenuSnapshot.fetched_at.desc(), CompetitorMenuSnapshot.id.desc())
            .first()
        )
        if snapshot is not None:
            return [dict(row) for row in (snapshot.items or [])]
        offers = (
            session.query(CompetitorOffer)
            .filter(CompetitorOffer.competitor_id == competitor_id)
            .order_by(CompetitorOffer.id.asc())
            .all()
        )
        return [
            {
                "name": offer.dish_or_combo,
                "price": offer.price,
                "description": offer.description or "",
            }
            for offer in offers
        ]

    @staticmethod
    def _store_menu_snapshot(session: Any, snapshot: MenuSnapshotData) -> CompetitorMenuSnapshot:
        row = CompetitorMenuSnapshot(
            competitor_id=snapshot.competitor_id,
            source_channel=snapshot.source_channel,
            platform=snapshot.platform,
            menu_hash=snapshot.menu_hash,
            items=snapshot.items,
            compliance=snapshot.compliance,
            fetched_at=snapshot.fetched_at,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return row

    @staticmethod
    def _sync_latest_offers(
        session: Any,
        competitor_id: int,
        items: Iterable[Dict[str, Any]],
        now: float,
    ) -> None:
        existing = {
            str(offer.dish_or_combo or "").lower(): offer
            for offer in (
                session.query(CompetitorOffer)
                .filter(CompetitorOffer.competitor_id == competitor_id)
                .all()
            )
        }
        for item in items:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            price = float(item.get("price") or 0.0)
            description = str(item.get("description") or "")
            current = existing.get(name.lower())
            if current is None:
                session.add(
                    CompetitorOffer(
                        competitor_id=competitor_id,
                        dish_or_combo=name,
                        price=price,
                        description=description,
                        updated_at=now,
                    )
                )
            else:
                current.price = price
                current.description = description
                current.updated_at = now

    @staticmethod
    def _store_probe_result(session: Any, result: ProbeResultData) -> CompetitorProbeResult:
        row = CompetitorProbeResult(
            competitor_id=result.competitor_id,
            source_channel=result.source_channel,
            platform=result.platform,
            estimated_wait_min=result.estimated_wait_min,
            availability=result.availability,
            tactic_labels=result.tactic_labels,
            confidence=result.confidence,
            transcript=result.transcript,
            raw=result.raw,
            sim_time=result.sim_time,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return row

    @staticmethod
    def _observation_to_dict(row: CompetitorObservation) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _snapshot_to_dict(row: CompetitorMenuSnapshot) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _probe_to_dict(row: CompetitorProbeResult) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _competitor_to_dict(row: Competitor) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _intel_to_dict(row: CompetitorIntel) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}
